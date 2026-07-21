"""
Repositories : toutes les requêtes SQL métier.
Séparés de la gestion de connexion (db.py) pour rester testables et lisibles.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

from psycopg2.extras import execute_values

from reviews.domain.models import Review, ScraperResult, PipelineRun, Alert
from reviews.storage.db import Database

logger = logging.getLogger(__name__)


class RunRepository:
    """Cycle de vie des runs du pipeline."""

    def __init__(self, db: Database):
        self.db = db

    def start_run(self, run_id: str) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO pipeline_runs (run_id, started_at, status) VALUES (%s, %s, %s)",
                (run_id, datetime.utcnow(), "running"),
            )

    def end_run(self, run_id: str, status: str, metadata: Optional[dict] = None) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE pipeline_runs SET
                    status = %s,
                    ended_at = %s,
                    metadata = %s,
                    total_reviews = COALESCE(
                        (SELECT COUNT(*) FROM reviews WHERE run_id = %s), 0),
                    total_duplicates = COALESCE(
                        (SELECT SUM(duplicate_count) FROM run_metrics WHERE run_id = %s), 0),
                    total_errors = COALESCE(
                        (SELECT SUM(error_count) FROM run_metrics WHERE run_id = %s), 0),
                    duration_seconds = EXTRACT(EPOCH FROM (%s - started_at))
                WHERE run_id = %s
                """,
                (status, datetime.utcnow(), json.dumps(metadata or {}),
                 run_id, run_id, run_id, datetime.utcnow(), run_id),
            )

    def record_metric(self, run_id: str, result: ScraperResult) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_metrics
                    (run_id, scraper_name, inserted_count, duplicate_count,
                     error_count, duration_seconds, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, result.scraper_name, result.inserted_count,
                 result.duplicate_count, result.error_count,
                 result.duration_seconds, result.status, result.error_message),
            )

    def get(self, run_id: str) -> Optional[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT %s", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]


class ReviewRepository:
    """Insertion et lecture des avis."""

    def __init__(self, db: Database):
        self.db = db

    def batch_insert(self, run_id: str, reviews: list[Review]) -> dict[str, int]:
        """Insère les avis en une fois, déduplication déléguée à PostgreSQL.

        La dédup se fait via ON CONFLICT DO NOTHING (clé primaire review_id et
        contrainte UNIQUE sur checksum) — aucun chargement des checksums en
        mémoire, ça passe à l'échelle.
        """
        if not reviews:
            return {"inserted": 0, "duplicates": 0, "errors": 0}

        # Dédup intra-lot (même checksum présent 2× dans le même batch)
        seen: set[str] = set()
        unique: list[Review] = []
        intra_dupes = 0
        for r in reviews:
            cs = r.get_checksum()
            if cs in seen:
                intra_dupes += 1
                continue
            seen.add(cs)
            unique.append(r)

        rows = [
            (
                r.id, run_id, r.company, r.source, r.title, r.text, r.rating,
                r.sentiment, r.verified, r.get_checksum(), r.created_at, datetime.utcnow(),
            )
            for r in unique
        ]

        with self.db.cursor() as cur:
            returned = execute_values(
                cur,
                """
                INSERT INTO reviews
                    (review_id, run_id, company, source, title, text, rating,
                     sentiment, verified, checksum, created_at, collected_at)
                VALUES %s
                ON CONFLICT DO NOTHING
                RETURNING review_id
                """,
                rows,
                fetch=True,
            )

        inserted = len(returned)
        duplicates = (len(unique) - inserted) + intra_dupes
        logger.info(
            "Insertion terminée",
            extra={"extra_data": {"inserted": inserted, "duplicates": duplicates,
                                  "total": len(reviews)}},
        )
        return {"inserted": inserted, "duplicates": duplicates, "errors": 0}

    def latest(self, limit: int = 100, company: Optional[str] = None,
               sentiment: Optional[str] = None) -> list[dict]:
        clauses, params = [], []
        if company:
            clauses.append("company = %s")
            params.append(company)
        if sentiment:
            clauses.append("sentiment = %s")
            params.append(sentiment)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"SELECT * FROM reviews {where} ORDER BY collected_at DESC LIMIT %s",
                params,
            )
            return [dict(r) for r in cur.fetchall()]


class AlertRepository:
    """Persistance des alertes."""

    def __init__(self, db: Database):
        self.db = db

    def insert(self, alert: Alert, notified: Optional[list[str]] = None) -> int:
        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts
                    (run_id, type, severity, title, message, company, source, notified, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING alert_id
                """,
                (alert.run_id, alert.type,
                 alert.severity.value if hasattr(alert.severity, "value") else alert.severity,
                 alert.title, alert.message, alert.company, alert.source,
                 json.dumps(notified or []), alert.created_at),
            )
            return cur.fetchone()[0]

    def list_recent(self, limit: int = 50, severity: Optional[str] = None) -> list[dict]:
        clause = "WHERE severity = %s" if severity else ""
        params: list[Any] = [severity] if severity else []
        params.append(limit)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"SELECT * FROM alerts {clause} ORDER BY created_at DESC LIMIT %s",
                params,
            )
            return [dict(r) for r in cur.fetchall()]


class StatsRepository:
    """Requêtes d'agrégats pour l'API / le dashboard temps réel."""

    def __init__(self, db: Database):
        self.db = db

    def overview(self) -> dict:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)                                        AS total_reviews,
                    COUNT(*) FILTER (WHERE sentiment='positive')    AS positive,
                    COUNT(*) FILTER (WHERE sentiment='neutral')     AS neutral,
                    COUNT(*) FILTER (WHERE sentiment='negative')    AS negative,
                    ROUND(AVG(rating)::numeric, 2)                  AS avg_rating,
                    COUNT(DISTINCT company)                         AS companies,
                    COUNT(*) FILTER (WHERE collected_at > now() - interval '24 hours') AS last_24h
                FROM reviews
                """
            )
            return dict(cur.fetchone())

    def sentiment_trend(self, days: int = 30, company: Optional[str] = None) -> list[dict]:
        clause = "WHERE day >= current_date - %s"
        params: list[Any] = [days]
        if company:
            clause += " AND company = %s"
            params.append(company)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT day, SUM(total) AS total, SUM(positive) AS positive,
                       SUM(neutral) AS neutral, SUM(negative) AS negative,
                       ROUND(AVG(avg_rating)::numeric, 2) AS avg_rating
                FROM sentiment_daily
                {clause}
                GROUP BY day ORDER BY day
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def by_company(self) -> list[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT company,
                       COUNT(*)                                     AS total,
                       COUNT(*) FILTER (WHERE sentiment='negative') AS negative,
                       ROUND(AVG(rating)::numeric, 2)               AS avg_rating
                FROM reviews GROUP BY company ORDER BY total DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]
