"""
Gestion centralisée de PostgreSQL.
Connexions, pool, migrations, opérations CRUD.
"""

import logging
import psycopg2
from psycopg2 import pool, extras, sql
from psycopg2.extensions import connection
from typing import Optional, Any
from datetime import datetime
from pathlib import Path
from config import settings
from models import Review, ScraperResult, PipelineRun
import json

logger = logging.getLogger(__name__)


class Database:
    """Gestionnaire de base de données PostgreSQL."""
    
    _instance = None
    _pool = None
    
    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialise le pool de connexions."""
        if self._initialized:
            return
        
        self._pool = psycopg2.pool.SimpleConnectionPool(
            settings.database.pool_size,
            settings.database.pool_size + settings.database.max_overflow,
            settings.database.connection_string(),
        )
        self._initialized = True
        logger.info("Pool de connexions PostgreSQL initialisé", extra={
            "pool_size": settings.database.pool_size,
            "host": settings.database.host,
            "database": settings.database.name,
        })
    
    def get_connection(self) -> connection:
        """Récupère une connexion du pool."""
        return self._pool.getconn()
    
    def return_connection(self, conn: connection):
        """Retourne une connexion au pool."""
        if conn:
            self._pool.putconn(conn)
    
    def close_all(self):
        """Ferme toutes les connexions du pool."""
        if self._pool:
            self._pool.closeall()
    
    def create_tables(self):
        """Crée le schéma de la base de données."""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                # Table des runs du pipeline
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pipeline_runs (
                        run_id TEXT PRIMARY KEY,
                        started_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        ended_at TIMESTAMP WITH TIME ZONE,
                        status VARCHAR(20) NOT NULL DEFAULT 'running',
                        total_reviews INTEGER DEFAULT 0,
                        total_duplicates INTEGER DEFAULT 0,
                        total_errors INTEGER DEFAULT 0,
                        error_message TEXT,
                        duration_seconds FLOAT,
                        metadata JSONB DEFAULT '{}',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Table des avis
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reviews (
                        review_id TEXT PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id),
                        company VARCHAR(255) NOT NULL,
                        source VARCHAR(50) NOT NULL,
                        title TEXT,
                        text TEXT NOT NULL,
                        rating INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
                        sentiment VARCHAR(20),
                        verified BOOLEAN,
                        checksum VARCHAR(64),
                        created_at TIMESTAMP WITH TIME ZONE,
                        collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Table des métriques par scraper
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS run_metrics (
                        metric_id SERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
                        scraper_name VARCHAR(50) NOT NULL,
                        inserted_count INTEGER DEFAULT 0,
                        duplicate_count INTEGER DEFAULT 0,
                        error_count INTEGER DEFAULT 0,
                        duration_seconds FLOAT,
                        status VARCHAR(20),
                        error_message TEXT,
                        recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Index pour les requêtes courantes
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_company ON reviews(company)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_source ON reviews(source)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_sentiment ON reviews(sentiment)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews(created_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_checksum ON reviews(checksum)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_run_id ON reviews(run_id)")
                
                conn.commit()
                logger.info("Schéma de base de données créé/vérifié")
                
        except Exception as e:
            conn.rollback()
            logger.error(f"Erreur lors de la création du schéma : {e}")
            raise
        finally:
            self.return_connection(conn)
    
    def start_run(self, run_id: str) -> dict[str, Any]:
        """Enregistre le début d'une exécution."""
        conn = self.get_connection()
        try:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                started_at = datetime.utcnow()
                cur.execute("""
                    INSERT INTO pipeline_runs 
                    (run_id, started_at, status)
                    VALUES (%s, %s, %s)
                    RETURNING *
                """, (run_id, started_at, "running"))
                
                result = cur.fetchone()
                conn.commit()
                
                logger.info(f"Run {run_id} démarrée", extra={"run_id": run_id})
                return dict(result)
                
        except Exception as e:
            conn.rollback()
            logger.error(f"Erreur au démarrage du run : {e}")
            raise
        finally:
            self.return_connection(conn)
    
    def end_run(self, run_id: str, status: str, stats: dict[str, Any] = None):
        """Enregistre la fin d'une exécution."""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                ended_at = datetime.utcnow()
                metadata = json.dumps(stats or {})
                
                cur.execute("""
                    UPDATE pipeline_runs
                    SET 
                        status = %s,
                        ended_at = %s,
                        metadata = %s,
                        total_reviews = COALESCE((
                            SELECT COUNT(*) FROM reviews WHERE run_id = %s
                        ), 0),
                        total_duplicates = COALESCE((
                            SELECT SUM(duplicate_count) FROM run_metrics
                            WHERE run_id = %s
                        ), 0),
                        total_errors = COALESCE((
                            SELECT SUM(error_count) FROM run_metrics 
                            WHERE run_id = %s
                        ), 0)
                    WHERE run_id = %s
                """, (status, ended_at, metadata, run_id, run_id, run_id, run_id))
                
                conn.commit()
                logger.info(f"Run {run_id} terminée", extra={
                    "run_id": run_id,
                    "status": status,
                })
                
        except Exception as e:
            conn.rollback()
            logger.error(f"Erreur à la fermeture du run : {e}")
            raise
        finally:
            self.return_connection(conn)
    
    def batch_insert_reviews(self, run_id: str, reviews: list[Review]) -> dict[str, int]:
        """
        Insère en masse les avis en évitant les doublons.
        Utilise COPY pour performance.
        """
        if not reviews:
            return {"inserted": 0, "duplicates": 0, "errors": 0}
        
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                # Récupérer les checksums existants
                checksums = set()
                if reviews:
                    cur.execute("SELECT checksum FROM reviews WHERE checksum IS NOT NULL")
                    checksums = {row[0] for row in cur.fetchall()}
                
                # Préparer les données
                inserted = 0
                duplicates = 0
                errors = 0
                
                for review in reviews:
                    checksum = review.get_checksum() if hasattr(review, 'get_checksum') else None
                    
                    if checksum and checksum in checksums:
                        duplicates += 1
                        continue
                    
                    try:
                        cur.execute("""
                            INSERT INTO reviews
                            (review_id, run_id, company, source, title, text,
                             rating, sentiment, verified, checksum,
                             created_at, collected_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (review_id) DO NOTHING
                        """, (
                            review.id,
                            run_id,
                            review.company,
                            review.source,
                            review.title,
                            review.text,
                            review.rating,
                            review.sentiment,
                            review.verified,
                            checksum,
                            review.created_at,
                            datetime.utcnow(),
                        ))
                        
                        if cur.rowcount > 0:
                            inserted += 1
                        else:
                            duplicates += 1
                        
                        if checksum:
                            checksums.add(checksum)
                            
                    except Exception as e:
                        errors += 1
                        logger.warning(f"Erreur insertion avis {review.id}: {e}")
                        continue
                
                conn.commit()
                
                logger.info(f"Batch insertion terminée", extra={
                    "inserted": inserted,
                    "duplicates": duplicates,
                    "errors": errors,
                    "total": len(reviews),
                })
                
                return {
                    "inserted": inserted,
                    "duplicates": duplicates,
                    "errors": errors,
                }
                
        except Exception as e:
            conn.rollback()
            logger.error(f"Erreur batch insertion : {e}")
            raise
        finally:
            self.return_connection(conn)
    
    def record_scraper_metric(self, run_id: str, result: ScraperResult):
        """Enregistre les métriques d'un scraper."""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO run_metrics 
                    (run_id, scraper_name, inserted_count, duplicate_count, 
                     error_count, duration_seconds, status, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    run_id,
                    result.scraper_name,
                    result.inserted_count,
                    result.duplicate_count,
                    result.error_count,
                    result.duration_seconds,
                    result.status,
                    result.error_message,
                ))
                conn.commit()
                
        except Exception as e:
            conn.rollback()
            logger.error(f"Erreur enregistrement métrique : {e}")
            raise
        finally:
            self.return_connection(conn)
    
    def get_run_status(self, run_id: str) -> Optional[dict]:
        """Récupère le statut d'un run."""
        conn = self.get_connection()
        try:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self.return_connection(conn)
    
    def get_latest_reviews(self, limit: int = 100, company: Optional[str] = None) -> list[dict]:
        """Récupère les avis les plus récents."""
        conn = self.get_connection()
        try:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                if company:
                    cur.execute("""
                        SELECT * FROM reviews 
                        WHERE company = %s
                        ORDER BY collected_at DESC
                        LIMIT %s
                    """, (company, limit))
                else:
                    cur.execute("""
                        SELECT * FROM reviews 
                        ORDER BY collected_at DESC
                        LIMIT %s
                    """, (limit,))
                
                return [dict(row) for row in cur.fetchall()]
        finally:
            self.return_connection(conn)


# Instance globale
db = Database()