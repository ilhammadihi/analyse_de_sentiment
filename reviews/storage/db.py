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

#: Dossier des migrations, à la racine du dépôt.
#:
#: Elles sont appliquées DANS L'ORDRE DES NOMS DE FICHIER (001, 002, …) — d'où
#: le préfixe numérique, qui n'est pas décoratif : la 002 crée les dimensions que
#: la 003 peuple et que la 004 étend.
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


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

    _MIGRATIONS_TABLE = """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """

    def apply_schema(self) -> None:
        """Applique les migrations NON ENCORE APPLIQUÉES, dans l'ordre des noms.

        POURQUOI ELLE S'EXÉCUTE AU DÉMARRAGE
            docker-compose monte `migrations/` dans /docker-entrypoint-initdb.d,
            qui n'est exécuté qu'à la CRÉATION du volume PostgreSQL. Sur une base
            déjà en place, une migration nouvellement ajoutée ne serait jamais
            appliquée : il faudrait s'en souvenir et la passer à la main, et le
            code partirait entre-temps du principe que ses colonnes existent.

        POURQUOI UN REGISTRE, ALORS QUE LES MIGRATIONS SONT IDEMPOTENTES
            L'idempotence ne suffit pas, et la panne a été observée en vrai.

            Les migrations sont EMBARQUÉES DANS L'IMAGE (`build: .`), pas
            montées. Un conteneur construit avant l'ajout d'une migration ne
            connaît donc que les anciennes — et les rejoue. Or plusieurs
            d'entre elles font `DROP VIEW ... CASCADE` puis recréent la vue :
            rejouer la 005 après la 007 REVIENT EN ARRIÈRE, la vue perd la
            colonne `source_comparable`, et plus rien ne calcule la
            satisfaction. Aucune erreur, aucun avertissement : le seul symptôme
            observé fut une alerte « détection de pic indisponible ».

            Chaque migration n'est donc jouée qu'UNE fois. Un conteneur périmé
            devient inoffensif : il ne rejoue rien.

        Chaque fichier est exécuté dans SA PROPRE transaction : une migration en
        échec n'annule pas celles qui ont réussi avant elle, et le message
        d'erreur nomme le fichier fautif au lieu de désigner « le schéma ».
        """
        files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            raise FileNotFoundError(f"Aucune migration dans {_MIGRATIONS_DIR}")

        with self.cursor() as cur:
            cur.execute(self._MIGRATIONS_TABLE)
            cur.execute("SELECT filename FROM schema_migrations")
            deja = {row[0] for row in cur.fetchall()}

        # Le registre connaît des migrations absentes de l'image : celle-ci est
        # PLUS ANCIENNE que la base. C'est exactement la situation qui produisait
        # le retour en arrière silencieux. On ne peut rien y faire ici — les
        # fichiers manquants n'existent pas dans ce conteneur — mais on refuse de
        # le taire : sans ce message, le diagnostic prend des heures.
        inconnues = deja - {p.name for p in files}
        if inconnues:
            logger.warning(
                "Image plus ancienne que la base : %d migration(s) appliquée(s) "
                "en base sont absentes de cette image (%s). Reconstruire les "
                "conteneurs (docker compose build) — le code tourne avec un "
                "schéma qu'il ne connaît pas.",
                len(inconnues), ", ".join(sorted(inconnues)),
            )

        applied = []
        for path in files:
            if path.name in deja:
                continue
            sql = path.read_text(encoding="utf-8")
            try:
                with self.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s) "
                        "ON CONFLICT DO NOTHING",
                        (path.name,),
                    )
            except Exception:
                logger.error("Migration en échec : %s", path.name)
                raise
            applied.append(path.name)

        if applied:
            logger.info(
                "Schéma mis à jour (%d migration(s) appliquée(s))",
                len(applied), extra={"extra_data": {"migrations": applied}},
            )
        else:
            logger.info("Schéma à jour (%d migration(s) déjà appliquées)", len(deja))

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
