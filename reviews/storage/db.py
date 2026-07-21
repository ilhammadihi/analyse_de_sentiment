"""
Gestion des connexions PostgreSQL (pool uniquement).
Aucune requête métier ici (voir repository.py), aucun DDL (voir migrations/).
Le pool n'est ouvert qu'au premier appel à get_database() — jamais à l'import.
"""

import logging
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2 import extras
from psycopg2.extensions import connection as PgConnection

from reviews.config import DatabaseConfig, get_settings

logger = logging.getLogger(__name__)

# Chemin du schéma canonique (migrations/001_init_schema.sql à la racine du repo)
_SCHEMA_FILE = Path(__file__).resolve().parent.parent.parent / "migrations" / "001_init_schema.sql"


class Database:
    """Gestionnaire de pool de connexions PostgreSQL."""

    def __init__(self, config: DatabaseConfig):
        self._config = config
        self._pool = pg_pool.SimpleConnectionPool(
            config.pool_size,
            config.pool_size + config.max_overflow,
            config.connection_string(),
        )
        logger.info(
            "Pool PostgreSQL initialisé",
            extra={"extra_data": {"host": config.host, "database": config.name}},
        )

    def get_connection(self) -> PgConnection:
        return self._pool.getconn()

    def return_connection(self, conn: PgConnection) -> None:
        if conn:
            self._pool.putconn(conn)

    def close_all(self) -> None:
        if self._pool:
            self._pool.closeall()

    @contextmanager
    def connection(self) -> Iterator[PgConnection]:
        """Emprunte une connexion et la rend au pool automatiquement."""
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.return_connection(conn)

    @contextmanager
    def cursor(self, dict_rows: bool = False) -> Iterator[extras.RealDictCursor]:
        """Ouvre un curseur transactionnel : commit si OK, rollback si erreur."""
        with self.connection() as conn:
            factory = extras.RealDictCursor if dict_rows else None
            cur = conn.cursor(cursor_factory=factory)
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    def apply_schema(self) -> None:
        """Applique le schéma SQL canonique (idempotent)."""
        sql = _SCHEMA_FILE.read_text(encoding="utf-8")
        with self.cursor() as cur:
            cur.execute(sql)
        logger.info("Schéma appliqué depuis %s", _SCHEMA_FILE.name)

    def ping(self) -> bool:
        """Vérifie que la base répond (utilisé par le healthcheck de l'API)."""
        try:
            with self.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Ping BD échoué : %s", e)
            return False


@lru_cache
def get_database() -> Database:
    """Instance unique de Database (pool créé à la demande)."""
    return Database(get_settings().database)
