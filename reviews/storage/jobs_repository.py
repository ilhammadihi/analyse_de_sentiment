"""
File de travail de la collecte : `collection_jobs`.

UNE LIGNE = UNE UNITÉ INDÉPENDANTE ET REPRENABLE. Pour Google Maps, une
filiale × un lieu. L'état vit en base, donc il survit au processus : après un
redémarrage, la file dit ce qui reste à faire, et une unité déjà réussie n'est
pas refaite.

Ce module ne connaît AUCUN collecteur. Il ne sait pas ce qu'est une recherche
Google Maps ; il manipule des unités opaques identifiées par `job_key` et un
curseur JSON dont il ignore la forme. C'est ce qui permet d'y brancher une autre
source sans le toucher.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class CollectionJob:
    """Une unité de collecte réservée, telle que la voit un collecteur."""

    job_id: int
    source: str
    job_key: str
    job_type: str = "unit"
    company: Optional[str] = None
    operator: Optional[str] = None
    country: Optional[str] = None
    location: Optional[str] = None
    query: Optional[str] = None
    attempts: int = 0
    cursor: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Libellé lisible pour les journaux : « Orange Mali · Bamako »."""
        return " · ".join(x for x in (self.company, self.location) if x) or self.job_key


class JobRepository:
    """Accès à `collection_jobs` : planification, réservation, clôture."""

    #: Tentatives avant qu'une unité soit déclarée `failed` et sorte de la file.
    #:
    #: Trois plutôt qu'une : un échec de scraping est le plus souvent
    #: transitoire (page lente, consentement affiché différemment). Mais
    #: réessayer indéfiniment est pire que renoncer — une unité dont la fiche
    #: Google a disparu bloquerait un créneau à chaque cycle, indéfiniment.
    MAX_ATTEMPTS = 3

    def __init__(self, db: Database, max_attempts: Optional[int] = None):
        self.db = db
        self.max_attempts = max_attempts or self.MAX_ATTEMPTS

    # -- Planification ---------------------------------------------------

    def plan(self, source: str, units: list[dict], job_type: str = "unit") -> dict:
        """Inscrit (ou rafraîchit) le catalogue d'unités d'une source.

        IDEMPOTENT, et c'est essentiel : cette méthode est appelée à chaque
        cycle avec le catalogue complet, qui varie d'un jour à l'autre (les
        villes tournent). Sur conflit de `job_key`, on met à jour le contexte
        métier et la requête — une ville renommée doit se refléter — mais on ne
        touche NI au statut, NI au curseur, NI à l'historique. Écraser le statut
        remettrait chaque cycle toutes les unités à « pending » et supprimerait
        l'intérêt de la file.

        `units` : dicts portant au minimum `job_key`. Les autres clés
        (`company`, `operator`, `country`, `location`, `query`, `priority`)
        sont facultatives.
        """
        if not units:
            return {"planifies": 0}

        lignes = [
            (
                source,
                job_type,
                u["job_key"],
                u.get("company"),
                u.get("operator"),
                (u.get("country") or None),
                u.get("location"),
                u.get("query"),
                u.get("priority", 100),
            )
            for u in units
        ]

        with self.db.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO collection_jobs
                    (source, job_type, job_key, company, operator, country,
                     location, query, priority)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, job_key) DO UPDATE SET
                    company    = EXCLUDED.company,
                    operator   = EXCLUDED.operator,
                    country    = EXCLUDED.country,
                    location   = EXCLUDED.location,
                    query      = EXCLUDED.query,
                    job_type   = EXCLUDED.job_type,
                    updated_at = CURRENT_TIMESTAMP
                """,
                lignes,
            )
        return {"planifies": len(lignes)}

    def reschedule_due(self, source: str, interval_minutes: int) -> int:
        """Remet en file les unités dont la cadence est écoulée.

        Une unité réussie ne doit pas rester `success` pour toujours, sinon la
        file se vide et plus rien n'est collecté. Elle redevient `pending` quand
        son dernier succès date de plus que la cadence de la source.

        Les unités `failed` reviennent aussi, avec leur compteur de tentatives
        remis à zéro : un blocage anti-bot d'hier ne doit pas condamner une
        agence définitivement.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_jobs SET
                    status       = 'pending',
                    attempts     = 0,
                    scheduled_at = CURRENT_TIMESTAMP,
                    updated_at   = CURRENT_TIMESTAMP
                WHERE source = %s
                  AND status IN ('success', 'failed')
                  AND COALESCE(last_success_at, finished_at)
                      < CURRENT_TIMESTAMP - make_interval(mins => %s)
                """,
                (source, interval_minutes),
            )
            return cur.rowcount

    def reclaim_stale(self, source: Optional[str] = None,
                      lease_minutes: int = 120,
                      before: Optional[datetime] = None) -> int:
        """Libère les unités laissées « running » par un processus disparu.

        Même raisonnement que `reclaim_interrupted_runs` pour les runs : seul
        `complete()`/`fail()` referme une unité, et un conteneur tué ne passe
        par aucun des deux. Sans cette reprise, chaque arrêt brutal retirerait
        définitivement quelques unités de la file.

        DEUX APPELS, DEUX CRITÈRES.

          Au DÉMARRAGE du worker : `before` = l'heure de démarrage du processus,
          `source=None`. Toutes les unités réservées avant sont mortes, quelle
          que soit leur source. C'est le nettoyage exact et immédiat.

          Au début d'un PASSAGE : le bail de `lease_minutes`, qui rattrape une
          unité abandonnée par un exécutant concurrent encore vivant.

        Le bail seul ne suffisait pas : il ne se déclenche qu'au passage suivant
        de LA MÊME source. Une unité Google Maps abandonnée à 17 h 29 attendait
        donc le passage de 23 h 27 pour être libérée, six heures plus tard,
        alors que son propriétaire était mort depuis longtemps.

        Le curseur est CONSERVÉ : c'est précisément le cas où il sert — l'unité
        reprend là où elle s'était arrêtée.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_jobs SET
                    status     = 'pending',
                    started_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                  AND (%s::text IS NULL OR source = %s)
                  AND started_at < COALESCE(
                        %s::timestamptz,
                        CURRENT_TIMESTAMP - make_interval(mins => %s))
                """,
                (source, source, before, lease_minutes),
            )
            return cur.rowcount

    # -- Exécution -------------------------------------------------------

    def claim(self, source: str, limit: int = 1,
              run_id: Optional[str] = None) -> list[CollectionJob]:
        """Réserve jusqu'à `limit` unités et les passe en « running ».

        `FOR UPDATE SKIP LOCKED` est le cœur de la correction : deux exécutants
        qui réservent en même temps ne se marchent pas dessus et n'attendent pas
        l'un l'autre — le second saute simplement les lignes déjà verrouillées.
        Sans lui, il faudrait un verrou applicatif, donc un état hors base,
        c'est-à-dire le problème qu'on vient de supprimer.

        L'ordre (`priority`, `scheduled_at`) fait remonter d'abord ce qui n'a
        jamais réussi : voir le commentaire de `priority` dans la migration 012.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                WITH prochaines AS (
                    SELECT job_id FROM collection_jobs
                    WHERE source = %s
                      AND status = 'pending'
                      AND scheduled_at <= CURRENT_TIMESTAMP
                      AND attempts < %s
                    ORDER BY priority, scheduled_at, job_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE collection_jobs j SET
                    status     = 'running',
                    started_at = CURRENT_TIMESTAMP,
                    attempts   = j.attempts + 1,
                    run_id     = COALESCE(%s, j.run_id),
                    updated_at = CURRENT_TIMESTAMP
                FROM prochaines p
                WHERE j.job_id = p.job_id
                RETURNING j.job_id, j.source, j.job_key, j.job_type, j.company,
                          j.operator, j.country, j.location, j.query,
                          j.attempts, j.cursor
                """,
                (source, self.max_attempts, limit, run_id),
            )
            return [
                CollectionJob(
                    job_id=r["job_id"], source=r["source"], job_key=r["job_key"],
                    job_type=r["job_type"], company=r["company"],
                    operator=r["operator"], country=r["country"],
                    location=r["location"], query=r["query"],
                    attempts=r["attempts"], cursor=r["cursor"] or {},
                )
                for r in cur.fetchall()
            ]

    def save_cursor(self, job_id: int, cursor: dict[str, Any]) -> None:
        """Enregistre l'avancement DANS une unité, sans la refermer.

        Appelée au fil de l'eau pendant l'exécution : c'est ce qui permet de
        reprendre à la fiche 3 sur 5 plutôt qu'à la première. Un curseur écrit
        après coup, à la clôture, ne servirait à rien — l'interruption arrive
        justement avant.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_jobs
                SET cursor = %s, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (json.dumps(cursor), job_id),
            )

    def release(self, job_id: int, retry_in_minutes: int = 0) -> None:
        """Rend une unité à la file SANS lui compter de tentative.

        Pour ce qui n'est pas la faute de l'unité : la source bride le débit et
        refuse tout le monde (voir `CollectorBackoff`), ou le budget du passage
        est épuisé. Passer par `fail()` dans ces cas-là ferait atteindre
        `MAX_ATTEMPTS` à des unités parfaitement saines, qui sortiraient alors
        de la file pour de bon.

        Le curseur est conservé : le travail déjà fait à l'intérieur reste
        acquis.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_jobs SET
                    status       = 'pending',
                    attempts     = GREATEST(attempts - 1, 0),
                    started_at   = NULL,
                    scheduled_at = CURRENT_TIMESTAMP + make_interval(mins => %s),
                    updated_at   = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (retry_in_minutes, job_id),
            )

    def complete(self, job_id: int, items_found: int = 0,
                 items_inserted: int = 0) -> None:
        """Clôt une unité réussie et EFFACE son curseur.

        L'effacement est délibéré : le curseur décrit une exécution en cours.
        Le garder ferait reprendre le prochain passage au milieu d'un travail
        déjà terminé, donc sauter des fiches.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_jobs SET
                    status          = 'success',
                    finished_at     = CURRENT_TIMESTAMP,
                    last_success_at = CURRENT_TIMESTAMP,
                    items_found     = %s,
                    items_inserted  = %s,
                    error_message   = NULL,
                    cursor          = NULL,
                    updated_at      = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (items_found, items_inserted, job_id),
            )

    def fail(self, job_id: int, error: str, retry_in_minutes: int = 10) -> None:
        """Marque un échec, et REMET l'unité en file tant qu'il reste des essais.

        Le curseur est conservé : une unité interrompue à la troisième fiche
        reprendra à la troisième. `priority = 0` la fait repasser en tête au
        prochain cycle — une unité qui n'a jamais réussi mérite d'être tentée
        avant celles qui ont déjà des avis en base.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_jobs SET
                    status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                    priority = CASE WHEN last_success_at IS NULL THEN 0 ELSE priority END,
                    scheduled_at = CURRENT_TIMESTAMP + make_interval(mins => %s),
                    finished_at = CURRENT_TIMESTAMP,
                    error_message = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (self.max_attempts, retry_in_minutes, (error or "")[:2000], job_id),
            )

    # -- Lecture ---------------------------------------------------------

    def summary(self, source: Optional[str] = None) -> list[dict]:
        """État de la file, par source et statut. Alimente le diagnostic."""
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT source, status, count(*) AS nb,
                       sum(items_inserted) AS avis,
                       max(last_success_at) AS dernier_succes
                FROM collection_jobs
                WHERE (%s::text IS NULL OR source = %s)
                GROUP BY source, status
                ORDER BY source, status
                """,
                (source, source),
            )
            return [dict(r) for r in cur.fetchall()]

    def pending_count(self, source: str) -> int:
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM collection_jobs
                WHERE source = %s AND status = 'pending' AND attempts < %s
                """,
                (source, self.max_attempts),
            )
            return cur.fetchone()[0]
