"""
Persistance de l'Agent 3 : constats, candidates, affirmations, instantanés.

CE DÉPÔT N'ÉCRIT QUE DANS SES PROPRES TABLES (migration 022). Il ne touche
jamais `reviews`, `dim_*` ni `collection_jobs` — c'est ce qui garantit qu'une
panne de l'Agent 3 ne peut pas abîmer les données, au même titre que l'Agent 1.

TOUTES LES ÉCRITURES SONT IDEMPOTENTES. L'agent repasse sur le même corpus à
chaque cycle ; sans `ON CONFLICT`, la table de constats gagnerait une ligne par
passage et par problème, et deviendrait illisible en une semaine.

LES ÉCHECS D'ÉCRITURE NE SONT PAS PROPAGÉS, à une exception près (les
instantanés de score). Même arbitrage que `AgentRepository.record` : le travail
utile est déjà fait, et lever ferait échouer un passage abouti. Le coût réel
est un constat non tracé, rattrapé au passage suivant.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)


def _json(valeur: Any) -> str:
    """Sérialise pour JSONB, en tolérant dates et décimaux."""
    return json.dumps(valeur or [], ensure_ascii=False, default=str)


class QualityRepository:
    """Lecture et écriture des tables de l'Agent 3."""

    def __init__(self, db: Database):
        self.db = db

    # ===================================================== Constats de qualité

    def enregistrer_constats(self, constats: list[dict[str, Any]]) -> int:
        """Écrit ou rafraîchit des constats. Renvoie le nombre traité.

        SUR CONFLIT, ON MET À JOUR LA RAISON ET LA PREUVE, JAMAIS LE STATUT.
        C'est la règle qui protège le travail humain : un constat déjà instruit
        et passé à ACCEPTED ne doit pas repasser à FLAGGED au prochain passage,
        sinon la file d'instruction ne se vide jamais et l'écran redemande
        éternellement le même arbitrage.
        """
        if not constats:
            return 0
        lignes = [
            (
                c["kind"], c["scope"], str(c["subject_key"]),
                c.get("subsidiary_id"), c.get("source_code"),
                c.get("severity") or "warning", c.get("confidence"),
                c.get("detected_by") or "regle",
                c["reason"], _json(c.get("evidence")),
            )
            for c in constats
        ]
        try:
            with self.db.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO data_quality_flags
                        (kind, scope, subject_key, subsidiary_id, source_code,
                         severity, confidence, detected_by, reason, evidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (kind, scope, subject_key) DO UPDATE SET
                        reason     = EXCLUDED.reason,
                        evidence   = EXCLUDED.evidence,
                        severity   = EXCLUDED.severity,
                        confidence = EXCLUDED.confidence,
                        updated_at = now()
                    """,
                    lignes,
                )
            return len(lignes)
        except Exception:  # noqa: BLE001
            logger.warning("Constats de qualité non enregistrés.", exc_info=True)
            return 0

    def statuer(self, flag_id: int, status: str, *, detected_by: str = "llm") -> bool:
        """Fait passer un constat à un statut instruit (ACCEPTED, REJECTED…)."""
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE data_quality_flags
                       SET status = %s, detected_by = %s, updated_at = now()
                     WHERE flag_id = %s
                    """,
                    (status, detected_by, flag_id),
                )
                return cur.rowcount > 0
        except Exception:  # noqa: BLE001
            logger.warning("Statut de constat non écrit.", exc_info=True)
            return False

    def constats_ouverts_par_filiale(self) -> dict[int, int]:
        """Constats non instruits, comptés par filiale — alimente le score."""
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT subsidiary_id, COUNT(*) AS n
                    FROM data_quality_flags
                    WHERE status IN ('FLAGGED', 'REVIEW_REQUIRED')
                      AND subsidiary_id IS NOT NULL
                    GROUP BY subsidiary_id
                    """
                )
                return {r["subsidiary_id"]: r["n"] for r in cur.fetchall()}
        except Exception:  # noqa: BLE001
            logger.warning("Constats ouverts illisibles.", exc_info=True)
            return {}

    def constats(
        self,
        *,
        status: Optional[str] = None,
        subsidiary_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT f.flag_id, f.kind, f.scope, f.subject_key, f.status,
                       f.severity, f.confidence, f.detected_by, f.reason,
                       f.evidence, f.source_code, f.subsidiary_id,
                       sub.name AS subsidiary, f.created_at, f.updated_at
                FROM data_quality_flags f
                LEFT JOIN dim_subsidiary sub
                       ON sub.subsidiary_id = f.subsidiary_id
                WHERE (%s::text IS NULL OR f.status = %s)
                  AND (%s::int  IS NULL OR f.subsidiary_id = %s)
                ORDER BY f.updated_at DESC
                LIMIT %s
                """,
                (status, status, subsidiary_id, subsidiary_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def avis_a_valider(self, limit: int = 40) -> list[dict]:
        """Avis portant un constat non instruit, à soumettre au modèle.

        JOINT SUR `reviews` POUR RAPPORTER LE TEXTE : le modèle a besoin de
        l'avis lui-même, pas de son identifiant. La jointure est ici plutôt que
        dans l'appelant pour que le contexte transmis soit constitué au plus
        près de la donnée — c'est ce que demande la section 18 de l'énoncé.
        """
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT f.flag_id, f.kind, f.reason, r.review_id, r.title,
                           LEFT(r.text, 600) AS text, r.rating,
                           sub.name AS subsidiary, op.name AS operator,
                           co.name AS country, s.code AS source
                    FROM data_quality_flags f
                    JOIN reviews r ON r.review_id = f.subject_key
                    LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
                    LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
                    LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
                    LEFT JOIN dim_source     s   ON s.source_id       = r.source_id
                    WHERE f.scope = 'review'
                      AND f.status = 'FLAGGED'
                      AND r.text IS NOT NULL
                    ORDER BY f.created_at
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            logger.warning("Avis à valider illisibles.", exc_info=True)
            return []

    # ======================================================= Sources candidates

    def enregistrer_candidates(self, candidates: list[dict[str, Any]]) -> int:
        """Écrit ou rafraîchit des candidates.

        Le statut EST mis à jour ici, contrairement aux constats : il découle
        entièrement de la sonde, qui est refaite à chaque passage. Une source
        qui répondait hier et répond 404 aujourd'hui doit basculer en REJECTED,
        sinon l'écran continue de proposer une piste morte.
        """
        if not candidates:
            return 0
        lignes = [
            (
                c["source_name"], c["url"], (c.get("country") or None),
                c.get("operator"), c.get("subsidiary_id"), c.get("source_type"),
                c.get("accessibility") or "inconnu", c.get("estimated_relevance"),
                c.get("reason"), _json(c.get("evidence")),
                c.get("probe_status"),
                c.get("probe_at"),
                bool(c.get("connector_required", True)),
                c.get("status") or "CANDIDATE", c.get("confidence"),
                c.get("avis_estimes"),
            )
            for c in candidates
        ]
        try:
            with self.db.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO source_candidates
                        (source_name, url, country, operator, subsidiary_id,
                         source_type, accessibility, estimated_relevance, reason,
                         evidence, probe_status, probe_at, connector_required,
                         status, confidence, avis_estimes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (subsidiary_id, url) DO UPDATE SET
                        accessibility      = EXCLUDED.accessibility,
                        estimated_relevance= EXCLUDED.estimated_relevance,
                        evidence           = EXCLUDED.evidence,
                        probe_status       = EXCLUDED.probe_status,
                        probe_at           = EXCLUDED.probe_at,
                        connector_required = EXCLUDED.connector_required,
                        confidence         = EXCLUDED.confidence,
                        avis_estimes       = EXCLUDED.avis_estimes,
                        -- `notified_at` N'EST JAMAIS DANS CETTE LISTE. Une
                        -- source déjà annoncée le reste : la sonde qui la
                        -- reconfirme à chaque passage ne doit pas produire un
                        -- second message Telegram pour la même découverte.
                        -- INTEGRATED est un état posé par un humain : la sonde
                        -- ne doit jamais le défaire. Une source intégrée qui
                        -- répond mal est un incident de collecte, pas une
                        -- candidate à re-proposer.
                        status = CASE WHEN source_candidates.status = 'INTEGRATED'
                                      THEN 'INTEGRATED' ELSE EXCLUDED.status END,
                        updated_at = now()
                    """,
                    lignes,
                )
            return len(lignes)
        except Exception:  # noqa: BLE001
            logger.warning("Sources candidates non enregistrées.", exc_info=True)
            return 0

    def candidates_a_notifier(self, limit: int = 10) -> list[dict]:
        """Sources VÉRIFIÉES jamais encore annoncées à l'équipe.

        SEUL LE STATUT VERIFIED PASSE ICI. Une simple `CANDIDATE` — sondée
        mais pas confirmée, ou pas sondée du tout — n'est pas une découverte,
        c'est une piste : l'annoncer sur Telegram enverrait chercher une
        source dont on ne sait même pas si elle contient quoi que ce soit.
        """
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT c.candidate_id, c.source_name, c.url, c.source_type,
                           c.avis_estimes, c.accessibility,
                           sub.name AS subsidiary
                    FROM source_candidates c
                    LEFT JOIN dim_subsidiary sub
                           ON sub.subsidiary_id = c.subsidiary_id
                    WHERE c.status = 'VERIFIED' AND c.notified_at IS NULL
                    ORDER BY c.avis_estimes DESC NULLS LAST, c.updated_at
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            logger.warning("Candidates à notifier illisibles.", exc_info=True)
            return []

    def marquer_notifiees(self, candidate_ids: list[int]) -> None:
        """Empêche une seconde annonce Telegram pour ces candidates."""
        if not candidate_ids:
            return
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE source_candidates SET notified_at = now() "
                    "WHERE candidate_id = ANY(%s)",
                    (candidate_ids,),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Marquage de notification non écrit.", exc_info=True)

    def candidates(
        self, *, subsidiary_id: Optional[int] = None, limit: int = 100
    ) -> list[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT c.*, sub.name AS subsidiary
                FROM source_candidates c
                LEFT JOIN dim_subsidiary sub
                       ON sub.subsidiary_id = c.subsidiary_id
                WHERE (%s::int IS NULL OR c.subsidiary_id = %s)
                  AND c.status <> 'REJECTED'
                ORDER BY c.confidence DESC NULLS LAST, c.updated_at DESC
                LIMIT %s
                """,
                (subsidiary_id, subsidiary_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    # ============================================================ Affirmations

    def enregistrer_affirmations(self, affirmations: list[dict[str, Any]]) -> int:
        if not affirmations:
            return 0
        lignes = [
            (
                a["claim"], a.get("topic"), a.get("subsidiary_id"),
                (a.get("country") or None),
                a["window"][0], a["window"][1],
                a["status"], a.get("confidence") or 0.0, _json(a.get("evidence")),
            )
            for a in affirmations
        ]
        try:
            with self.db.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO data_claims
                        (claim, topic, subsidiary_id, country,
                         window_from, window_to, status, confidence, evidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (subsidiary_id, topic, window_from) DO UPDATE SET
                        claim      = EXCLUDED.claim,
                        window_to  = EXCLUDED.window_to,
                        status     = EXCLUDED.status,
                        confidence = EXCLUDED.confidence,
                        evidence   = EXCLUDED.evidence
                    """,
                    lignes,
                )
            return len(lignes)
        except Exception:  # noqa: BLE001
            logger.warning("Affirmations non enregistrées.", exc_info=True)
            return 0

    def affirmations(
        self,
        *,
        subsidiary_id: Optional[int] = None,
        status: Optional[str] = None,
        jours: int = 30,
        limit: int = 100,
    ) -> list[dict]:
        depuis = datetime.now(timezone.utc) - timedelta(days=jours)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT c.*, sub.name AS subsidiary
                FROM data_claims c
                LEFT JOIN dim_subsidiary sub
                       ON sub.subsidiary_id = c.subsidiary_id
                WHERE c.created_at >= %s
                  AND (%s::int  IS NULL OR c.subsidiary_id = %s)
                  AND (%s::text IS NULL OR c.status = %s)
                ORDER BY c.confidence DESC, c.created_at DESC
                LIMIT %s
                """,
                (depuis, subsidiary_id, subsidiary_id, status, status, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    # ======================================================= Avis orphelins

    def enregistrer_propositions(self, propositions: list[dict[str, Any]]) -> int:
        """Écrit ou rafraîchit les propositions de réattribution.

        SUR CONFLIT, `applied_at` N'EST JAMAIS TOUCHÉ. Une proposition déjà
        appliquée doit garder sa date d'application : c'est elle qui distingue
        une analyse d'une modification de données, et la remettre à nul ferait
        croire que l'écriture n'a pas eu lieu — donc autoriser un second passage
        à la refaire.
        """
        if not propositions:
            return 0
        lignes = [
            (
                p["review_id"], p.get("company"), p.get("source_code"),
                p.get("previous_subsidiary_id"), p.get("proposed_subsidiary_id"),
                p["method"], p.get("confidence") or 0.0, p["status"],
                _json(p.get("evidence")), p.get("reason"),
            )
            for p in propositions
        ]
        try:
            with self.db.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO orphan_resolutions
                        (review_id, company, source_code, previous_subsidiary_id,
                         proposed_subsidiary_id, method, confidence, status,
                         evidence, reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (review_id) DO UPDATE SET
                        proposed_subsidiary_id = EXCLUDED.proposed_subsidiary_id,
                        method     = EXCLUDED.method,
                        confidence = EXCLUDED.confidence,
                        status     = EXCLUDED.status,
                        evidence   = EXCLUDED.evidence,
                        reason     = EXCLUDED.reason,
                        updated_at = now()
                    """,
                    lignes,
                )
            return len(lignes)
        except Exception:  # noqa: BLE001
            logger.warning("Propositions de réattribution non enregistrées.", exc_info=True)
            return 0

    def orphelins_resume(self) -> dict[str, Any]:
        """Compteurs de l'arriéré, pour le dashboard et la CLI."""
        vide = {"restants": 0, "par_statut": {}, "appliques": 0, "par_methode": {}}
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM reviews WHERE subsidiary_id IS NULL"
                )
                restants = (cur.fetchone() or {}).get("n", 0)

                cur.execute(
                    "SELECT status, COUNT(*) AS n FROM orphan_resolutions "
                    "WHERE applied_at IS NULL GROUP BY status"
                )
                par_statut = {r["status"]: r["n"] for r in cur.fetchall()}

                cur.execute(
                    "SELECT method, COUNT(*) AS n FROM orphan_resolutions "
                    "WHERE applied_at IS NOT NULL GROUP BY method"
                )
                par_methode = {r["method"]: r["n"] for r in cur.fetchall()}
        except Exception:  # noqa: BLE001
            logger.warning("Résumé des orphelins illisible.", exc_info=True)
            return vide

        return {
            "restants": restants,
            "par_statut": par_statut,
            "appliques": sum(par_methode.values()),
            "par_methode": par_methode,
        }

    def propositions(
        self, *, status: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT o.resolution_id, o.review_id, o.company, o.source_code,
                       o.proposed_subsidiary_id, sub.name AS proposed_subsidiary,
                       o.method, o.confidence, o.status, o.reason, o.evidence,
                       o.applied_at, o.created_at
                FROM orphan_resolutions o
                LEFT JOIN dim_subsidiary sub
                       ON sub.subsidiary_id = o.proposed_subsidiary_id
                WHERE (%s::text IS NULL OR o.status = %s)
                ORDER BY o.confidence DESC, o.created_at DESC
                LIMIT %s
                """,
                (status, status, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    # ============================================================= Instantanés

    def enregistrer_scores(self, scores: list[dict[str, Any]]) -> int:
        """Écrit les instantanés de score.

        SEULE ÉCRITURE DONT L'ÉCHEC EST PROPAGÉ. Les autres tables sont des
        journaux : les perdre coûte de la traçabilité. Celle-ci porte le DATA
        TRUST STATUS que consomment les Agents 1 et 2 ; s'il n'est pas écrit,
        ils travailleront sur un instantané périmé en le croyant à jour. Mieux
        vaut un passage en échec visible qu'une confiance silencieusement fausse.
        """
        if not scores:
            return 0
        lignes = [
            (
                s["subsidiary_id"],
                s["composantes"].get("coverage", {}).get("valeur"),
                s["composantes"].get("freshness", {}).get("valeur"),
                s["composantes"].get("completeness", {}).get("valeur"),
                s["composantes"].get("consistency", {}).get("valeur"),
                s["composantes"].get("diversity", {}).get("valeur"),
                s["composantes"].get("reliability", {}).get("valeur"),
                s["global_score"], s["status"], s.get("diagnostic"),
                json.dumps(
                    {
                        "composantes": s["composantes"],
                        "poids_appliques": s.get("poids_appliques", {}),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            for s in scores
        ]
        with self.db.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO quality_snapshots
                    (subsidiary_id, coverage, freshness, completeness,
                     consistency, diversity, reliability, global_score,
                     status, diagnostic, components)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                lignes,
            )
        return len(lignes)

    def trust(self, subsidiary_id: Optional[int] = None) -> list[dict]:
        """DATA TRUST STATUS — ce que lisent les Agents 1 et 2.

        LE CONTRAT LE PLUS IMPORTANT DU MODULE. Les trois scores rendus ici
        commandent la retenue des autres agents : un briefing sur une filiale
        `UNTRUSTED` ne devrait pas être écrit, et une campagne encore moins.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT subsidiary_id, subsidiary, operator, country, iso2,
                       coverage, freshness, reliability, global_score,
                       status, diagnostic, computed_at
                FROM v_quality_latest
                WHERE (%s::int IS NULL OR subsidiary_id = %s)
                ORDER BY global_score
                """,
                (subsidiary_id, subsidiary_id),
            )
            lignes = [dict(r) for r in cur.fetchall()]

        return [
            {
                "subsidiary_id": r["subsidiary_id"],
                "operator": r["operator"],
                "subsidiary": r["subsidiary"],
                "country": r["country"],
                "iso2": r["iso2"],
                "coverage_score": _arrondi(r["coverage"]),
                "quality_score": _arrondi(r["global_score"]),
                "reliability_score": _arrondi(r["reliability"]),
                "overall_confidence": _arrondi(r["global_score"]),
                "status": r["status"],
                "diagnostic": r["diagnostic"],
                "computed_at": (
                    r["computed_at"].isoformat() if r["computed_at"] else None
                ),
            }
            for r in lignes
        ]

    def dernier_score(self, subsidiary_id: int) -> Optional[float]:
        """Score du dernier instantané, pour mesurer une aggravation."""
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "SELECT global_score FROM v_quality_latest "
                    "WHERE subsidiary_id = %s",
                    (subsidiary_id,),
                )
                row = cur.fetchone()
                return float(row[0]) if row else None
        except Exception:  # noqa: BLE001
            return None

    def resume(self) -> dict[str, Any]:
        """Vue d'ensemble, pour l'écran d'accueil « Data Quality »."""
        vide = {
            "filiales": 0, "score_global": None, "par_statut": {},
            "constats_ouverts": 0, "candidates": 0, "non_corrobores": 0,
        }
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS filiales, AVG(global_score) AS moyenne
                    FROM v_quality_latest
                    """
                )
                base = dict(cur.fetchone() or {})

                cur.execute("SELECT status, COUNT(*) AS n FROM v_quality_latest GROUP BY status")
                par_statut = {r["status"]: r["n"] for r in cur.fetchall()}

                cur.execute(
                    "SELECT COUNT(*) AS n FROM data_quality_flags "
                    "WHERE status IN ('FLAGGED','REVIEW_REQUIRED')"
                )
                constats = (cur.fetchone() or {}).get("n", 0)

                cur.execute(
                    "SELECT COUNT(*) AS n FROM source_candidates "
                    "WHERE status IN ('CANDIDATE','VERIFIED')"
                )
                candidates = (cur.fetchone() or {}).get("n", 0)

                # NON EXPLOITABLE, et non « UNCONFIRMED » seulement.
                #
                # Le compteur doit dire la même chose que `Affirmation
                # .exploitable`, qui commande la retenue des Agents 1 et 2 :
                # seuls CONFIRMED et CORROBORATED peuvent être relayés.
                # PLAUSIBLE ne le peut pas — c'est une espèce de preuve unique,
                # donc une coïncidence possible.
                #
                # Compter les seuls UNCONFIRMED affichait 0 sur un corpus
                # portant 4 affirmations plausibles et non relayables : l'écran
                # annonçait « rien à surveiller » précisément là où il fallait
                # regarder. Deux définitions sous un même nom, et c'est
                # l'écran qui perd.
                cur.execute(
                    "SELECT COUNT(*) AS n FROM data_claims "
                    "WHERE status NOT IN ('CONFIRMED', 'CORROBORATED') "
                    "AND created_at >= now() - INTERVAL '30 days'"
                )
                non_corrobores = (cur.fetchone() or {}).get("n", 0)
        except Exception:  # noqa: BLE001
            logger.warning("Résumé qualité illisible.", exc_info=True)
            return vide

        return {
            "filiales": base.get("filiales") or 0,
            "score_global": _arrondi(base.get("moyenne")),
            "par_statut": par_statut,
            "constats_ouverts": constats,
            "candidates": candidates,
            "non_corrobores": non_corrobores,
        }


def _arrondi(valeur: Any) -> Optional[float]:
    """Arrondi défensif : PostgreSQL rend des `Decimal`, `round` veut un float."""
    if valeur is None:
        return None
    try:
        return round(float(valeur), 3)
    except (TypeError, ValueError):
        return None
