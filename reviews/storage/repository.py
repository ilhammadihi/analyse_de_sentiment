"""
Repositories du chemin d'écriture : runs, avis, alertes.
Séparés de la gestion de connexion (db.py) pour rester testables et lisibles.

Les agrégats de lecture du dashboard vivent dans `stats_repository.py` — ils
n'ont ni les mêmes contraintes ni le même volume de code, et leur cohérence
repose sur un mécanisme qui leur est propre (voir le bloc `_MEASURES`).
`StatsRepository` est ré-exporté ici pour que les imports historiques
(`from reviews.storage.repository import StatsRepository`) continuent de
fonctionner.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg2.extras import execute_values

from reviews.domain.models import Review, ScraperResult, PipelineRun, Alert
from reviews.storage.db import Database
from reviews.storage.filters import ALERTS, StatsFilter
from reviews.storage.stats_repository import (  # noqa: F401  (ré-export)
    BUSINESS_ALERT_TYPES,
    StatsRepository,
)

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

    def reclaim_interrupted_runs(self, grace_hours: float = 6.0,
                                 before: Optional[datetime] = None) -> int:
        """Referme les runs restés « running » après un arrêt brutal.

        POURQUOI C'EST NÉCESSAIRE
          `start_run` marque le run « running » et seul `end_run` le referme.
          Un `docker restart`, un OOM-kill ou un Ctrl-C tuent le processus sans
          passer par là : la ligne reste « running » indéfiniment. La base en
          comptait 33, et l'onglet Collecte — précisément l'écran censé prouver
          que la chaîne tourne — affichait 7 de ses 9 derniers passages comme
          « en cours ». Un dashboard qui ne sait pas distinguer un run vivant
          d'un run mort ne prouve plus rien.

        `failed` plutôt qu'un statut « interrompu » dédié : le dashboard ne
        connaît que « running », « failed » et le reste (affiché « ok »). Un
        troisième statut retomberait dans la branche « ok » et présenterait un
        run avorté comme réussi — l'inverse du but recherché.

        DEUX CRITÈRES, ET `before` EST LE BON.

          `before` = l'instant où le processus courant a démarré. Un run encore
          « running » qui a commencé AVANT est nécessairement mort : le
          processus qui le tenait n'existe plus. Le ménage est donc immédiat et
          exact, sans rien supposer d'une durée.

          `grace_hours` est le critère de repli, conservé pour les appels qui ne
          connaissent pas l'heure de démarrage. Il a montré sa limite : calé sur
          la plus longue cadence — douze heures depuis que les sources ont
          chacune la leur — il laissait huit runs morts affichés « en cours »
          toute une soirée.

        Le risque résiduel de `before` est une exécution manuelle lancée AVANT
        le worker et encore vivante : elle serait refermée à tort. Le défaut se
        corrige tout seul, `end_run` réécrivant le statut à la fin de cette
        exécution — un état faux et transitoire contre des runs morts affichés
        pendant des heures, l'échange est clairement favorable.

        Renvoie le nombre de runs refermés.
        """
        if before is not None:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pipeline_runs SET
                        status = 'failed',
                        ended_at = COALESCE(ended_at, %s),
                        error_message = COALESCE(
                            error_message,
                            'Run interrompu : processus arrêté avant la fin '
                            '(redémarrage du conteneur ou arrêt manuel). '
                            'Refermé automatiquement au démarrage suivant.'),
                        total_reviews = COALESCE(NULLIF(total_reviews, 0),
                            (SELECT COUNT(*) FROM reviews WHERE run_id = pipeline_runs.run_id)),
                        total_duplicates = COALESCE(NULLIF(total_duplicates, 0),
                            (SELECT COALESCE(SUM(duplicate_count), 0) FROM run_metrics
                              WHERE run_id = pipeline_runs.run_id))
                    WHERE status = 'running' AND started_at < %s
                    """,
                    (datetime.utcnow(), before),
                )
                return cur.rowcount
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE pipeline_runs SET
                    status = 'failed',
                    ended_at = COALESCE(ended_at, %s),
                    error_message = COALESCE(
                        error_message,
                        'Run interrompu : processus arrêté avant la fin '
                        '(redémarrage du conteneur ou arrêt manuel). '
                        'Refermé automatiquement au démarrage suivant.'),
                    -- Durée laissée NULLE, délibérément. Un run interrompu est
                    -- mort à un instant inconnu ; calculer « maintenant moins
                    -- le départ » lui prêterait la durée de l'arrêt, pas celle
                    -- du travail. Le dashboard affiche « — », qui est la
                    -- vérité, plutôt qu'un « 28 124 s » qui ferait passer un
                    -- run avorté pour une exécution de huit heures.
                    total_reviews = COALESCE(NULLIF(total_reviews, 0),
                        (SELECT COUNT(*) FROM reviews WHERE run_id = pipeline_runs.run_id)),
                    total_duplicates = COALESCE(NULLIF(total_duplicates, 0),
                        (SELECT COALESCE(SUM(duplicate_count), 0) FROM run_metrics
                          WHERE run_id = pipeline_runs.run_id))
                WHERE status = 'running'
                  AND started_at < %s - make_interval(secs => %s)
                """,
                (datetime.utcnow(), datetime.utcnow(), grace_hours * 3600),
            )
            return cur.rowcount

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

    def last_attempt_by_scraper(self) -> dict[str, datetime]:
        """Date du dernier passage de chaque collecteur, réussi ou non.

        Sert à la planification par source : un collecteur n'est relancé que si
        sa propre cadence est écoulée (voir `Settings.scraper_interval_minutes`).

        La date retenue est celle de la dernière TENTATIVE, pas du dernier
        succès. Un collecteur qui échoue en boucle serait sinon relancé à chaque
        cycle sans jamais respecter sa cadence — Google Maps, qui met jusqu'à
        55 minutes avant d'échouer, monopoliserait le worker. L'échec est déjà
        traité par le retry interne au collecteur et par l'alerting ; ce n'est
        pas au planificateur de s'acharner.

        `run_metrics` porte déjà l'information : aucune table d'état à créer,
        donc rien à maintenir cohérent avec le reste.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT scraper_name, MAX(recorded_at)
                FROM run_metrics
                GROUP BY scraper_name
                """
            )
            return {name: last for name, last in cur.fetchall() if last}

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

    def latest_review_dates(self) -> dict[tuple[str, str, Optional[str]], datetime]:
        """Date du dernier avis connu, par (entreprise, source, SOUS-CIBLE).

        Sert de repère à la collecte incrémentale : inutile de re-parser et de
        re-proposer à l'insertion des avis déjà en base. Sans ce garde-fou, un
        run RSS récupère ~23 000 articles pour n'en retenir que ~10 000, le
        reste étant rejeté en doublon — un gaspillage qui empêche d'augmenter
        la fréquence de collecte.

        LA SOUS-CIBLE FAIT PARTIE DE LA CLÉ, et ce n'est pas un détail.

        Tant qu'une filiale n'avait qu'une cible par source, (company, source)
        suffisait. Depuis que Google Maps visite plusieurs AGENCES et que les
        boutiques suivent plusieurs APPLICATIONS par filiale, un repère unique
        par filiale est destructeur : l'agence la plus active fixe la date, et
        toute agence dont les avis lui sont antérieurs est intégralement
        écartée comme « déjà en base » alors qu'aucun de ses avis n'y est.

        Autrement dit, sans cette clé élargie, plus on découvre de sous-cibles,
        plus on en jette le contenu — sans erreur ni trace. Une sous-cible
        inconnue n'a pas d'entrée ici, donc pas de repère, donc elle est
        collectée en entier : c'est exactement le comportement voulu.

        La clé s'appuie sur (company, source) et non sur subsidiary_id : c'est
        ce dont dispose un collecteur, qui ne connaît pas le modèle dimensionnel.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT company, source, target_id, MAX(created_at)
                FROM reviews
                WHERE created_at IS NOT NULL
                GROUP BY company, source, target_id
                """
            )
            return {(row[0], row[1], row[2]): row[3] for row in cur.fetchall()}

    #: Âge maximal d'un avis conservé, en années.
    #:
    #: POURQUOI UNE BORNE, ET POURQUOI ICI
    #:   Aucune source ne borne ce qu'elle renvoie : le flux RSS d'Apple, les
    #:   fiches Google Maps et HelloPeter remontent des avis de 2013. Mesuré
    #:   avant la coupe : 10 615 avis sur 26 762 — 40 % du corpus — avaient plus
    #:   de trois ans, dont la MOITIÉ des avis Google Maps.
    #:
    #:   Un avis d'agence de 2015 ne dit rien de la satisfaction d'aujourd'hui,
    #:   mais il pèse dans toutes les moyennes au même titre qu'un avis d'hier.
    #:
    #:   Le filtre est posé à l'INSERTION et non par un nettoyage périodique :
    #:   c'est le seul point de passage obligé. Une purge programmée laisserait
    #:   les vieux avis entrer puis attendrait, et surtout elle serait
    #:   silencieusement contournée à chaque PREMIER passage sur une cible
    #:   nouvelle — le collecteur Play Store descend alors jusqu'à 200 avis, soit
    #:   plusieurs années d'historique d'un coup.
    MAX_AGE_YEARS = 3

    def _filtrer_par_age(self, reviews: list[Review]) -> tuple[list[Review], int]:
        """Écarte les avis plus vieux que `MAX_AGE_YEARS`. Retourne (gardés, écartés).

        Un avis sans date est CONSERVÉ : on ne sait pas s'il est vieux, et le
        jeter serait perdre une donnée sur une simple ignorance.
        """
        limite = datetime.now(timezone.utc) - timedelta(days=365 * self.MAX_AGE_YEARS)
        gardes, ecartes = [], 0
        for r in reviews:
            quand = r.created_at
            if quand is None:
                gardes.append(r)
                continue
            # Les collecteurs produisent tantôt des dates naïves (Play Store),
            # tantôt des dates avec fuseau (flux Apple). Comparer les deux lève
            # un TypeError qui ferait échouer tout le lot.
            if quand.tzinfo is None:
                quand = quand.replace(tzinfo=timezone.utc)
            if quand < limite:
                ecartes += 1
                continue
            gardes.append(r)

        if ecartes:
            logger.info(
                "%d avis écarté(s) : plus de %d ans (limite %s)",
                ecartes, self.MAX_AGE_YEARS, limite.date(),
            )
        return gardes, ecartes

    def batch_insert(self, run_id: str, reviews: list[Review]) -> dict[str, int]:
        """Insère les avis en une fois, déduplication déléguée à PostgreSQL.

        La dédup se fait via ON CONFLICT DO NOTHING (clé primaire review_id et
        contrainte UNIQUE sur checksum) — aucun chargement des checksums en
        mémoire, ça passe à l'échelle.
        """
        if not reviews:
            return {"inserted": 0, "duplicates": 0, "errors": 0}

        reviews, trop_vieux = self._filtrer_par_age(reviews)
        if not reviews:
            return {"inserted": 0, "duplicates": 0, "errors": 0,
                    "trop_vieux": trop_vieux}

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

        now = datetime.utcnow()
        rows = [
            (
                r.id, run_id, r.company, r.source, r.title, r.text, r.rating,
                r.sentiment, r.verified, r.get_checksum(), r.created_at, now,
                r.sentiment_score, r.pos_terms, r.neg_terms, r.lexicon_version,
                r.target_id, r.target_name,
            )
            for r in unique
        ]

        with self.db.cursor() as cur:
            returned = execute_values(
                cur,
                """
                INSERT INTO reviews
                    (review_id, run_id, company, source, title, text, rating,
                     sentiment, verified, checksum, created_at, collected_at,
                     subsidiary_id, source_id,
                     sentiment_score, pos_terms, neg_terms, lexicon_version,
                     target_id, target_name)
                SELECT
                    v.review_id::text, v.run_id::text, v.company::text,
                    v.source::text, v.title::text, v.text::text, v.rating::int,
                    v.sentiment::text, v.verified::boolean, v.checksum::text,
                    v.created_at::timestamptz, v.collected_at::timestamptz,
                    sub.subsidiary_id, src.source_id,
                    v.sentiment_score::real,
                    -- COALESCE : les colonnes sont NOT NULL DEFAULT '{}', mais
                    -- un SELECT explicite court-circuite le DEFAULT — sans lui,
                    -- un avis sans terme déclenché violerait la contrainte.
                    COALESCE(v.pos_terms::text[], '{}'),
                    COALESCE(v.neg_terms::text[], '{}'),
                    v.lexicon_version::smallint,
                    v.target_id::text, v.target_name::text
                FROM (VALUES %s) AS v(review_id, run_id, company, source, title,
                                      text, rating, sentiment, verified,
                                      checksum, created_at, collected_at,
                                      sentiment_score, pos_terms, neg_terms,
                                      lexicon_version, target_id, target_name)
                -- Rattachement aux dimensions (migration 002). Les collecteurs
                -- ne connaissent que le nom de l'entité : la correspondance se
                -- fait ici via dim_subsidiary.aliases, ce qui absorbe les
                -- changements de marque (Etisalat -> Moov) sans toucher au code.
                -- LATERAL + LIMIT 1 : garantit au plus une filiale par avis,
                -- même si deux alias venaient à se recouvrir.
                LEFT JOIN LATERAL (
                    SELECT s.subsidiary_id
                    FROM dim_subsidiary s
                    WHERE v.company::text = ANY (s.aliases)
                    LIMIT 1
                ) sub ON TRUE
                LEFT JOIN dim_source src ON src.code = v.source::text
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

    def has_recent(self, alert_type: str, company: str, hours: int) -> bool:
        """Une alerte de ce type existe-t-elle déjà pour cette entité ?

        Silence anti-répétition de l'alerting métier. Un pic d'insatisfaction
        dure plusieurs jours, alors que le pipeline repasse plusieurs fois par
        jour : sans ce contrôle, la même filiale serait re-signalée à chaque
        passage et le fil métier se noierait dans ses propres redites.

        Le silence porte sur (type, entité) et non sur le message : c'est bien
        le MÊME problème qui est signalé, même si les pourcentages ont bougé
        d'un point entre deux passages.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM alerts
                WHERE type = %s AND company = %s
                  AND created_at > now() - make_interval(hours => %s)
                LIMIT 1
                """,
                (alert_type, company, hours),
            )
            return cur.fetchone() is not None

    def list_recent(
        self,
        limit: int = 50,
        severity: Optional[str] = None,
        kind: Optional[str] = None,
        f: Optional[StatsFilter] = None,
    ) -> list[dict]:
        """Alertes récentes, filtrables par gravité, par nature et par périmètre.

        Args:
            f: périmètre du dashboard. Le fil d'alertes obéit au MÊME filtre que
                le reste des écrans : consulter le Mali et lire une alerte sur la
                Zambie contredit la promesse d'un périmètre unique, et fait
                douter de tous les autres chiffres affichés.

                Le rattachement passe par les alias de filiale : une alerte dont
                `company` ne correspond à aucun alias n'a pas d'axe
                dimensionnel, elle est donc écartée dès qu'un filtre de pays,
                d'opérateur ou de filiale est actif — et conservée sinon.
            kind: 'business' pour les alertes qui parlent de satisfaction
                client, 'technical' pour celles qui parlent de la collecte.
                None renvoie tout.

                Cette séparation n'est pas cosmétique : la base contient 216
                alertes dont 215 sont techniques (« scraper_zero »,
                « high_duplicates »). Présentées dans un même fil, elles
                enterrent le seul signal métier qui s'y trouve. Un métier veut
                lire « le mécontentement grimpe au Mali », pas « le collecteur
                Trustpilot n'a rien remonté ».
        """
        clauses: list[str] = []
        params: list[Any] = []

        if f is not None:
            # `for_alerts()` retire les prédicats de source : une alerte n'est ni
            # un avis client ni un article, et les conserver viderait le fil.
            scope_sql, scope_params = f.for_alerts().where(cols=ALERTS)
            clauses.append(scope_sql.removeprefix("WHERE "))
            params.extend(scope_params)

        if severity:
            clauses.append("a.severity = %s")
            params.append(severity)
        if kind == "business":
            clauses.append("a.type = ANY(%s)")
            params.append(list(BUSINESS_ALERT_TYPES))
        elif kind == "technical":
            # NOT ... ANY plutôt qu'une liste explicite de types techniques :
            # une nouvelle règle technique apparaît ici automatiquement, alors
            # qu'une liste en dur l'aurait fait disparaître des deux filtres.
            clauses.append("NOT (a.type = ANY(%s))")
            params.append(list(BUSINESS_ALERT_TYPES))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT a.*,
                       -- Rattachement à la filiale via les alias dimensionnels :
                       -- `alerts.company` est un nom libre écrit par la règle
                       -- d'alerting, il ne permet pas à lui seul de savoir de
                       -- quel pays ni de quel opérateur on parle.
                       sub.subsidiary_id, sub.name AS subsidiary,
                       op.name AS operator, co.name AS country, co.iso2
                FROM alerts a
                LEFT JOIN dim_subsidiary sub ON a.company = ANY (sub.aliases)
                LEFT JOIN dim_operator   op  ON op.operator_id = sub.operator_id
                LEFT JOIN dim_country    co  ON co.country_id  = sub.country_id
                {where}
                ORDER BY a.created_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]
