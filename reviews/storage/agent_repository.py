"""
Journal des signalements d'agent : ce qui a déjà été dit, et à quel niveau.

POURQUOI CE MODULE EXISTE
    Un agent parle sans qu'on le lui demande. C'est sa raison d'être et c'est
    aussi son danger : répété trois jours de suite sous les mêmes chiffres, un
    signalement juste devient un bruit qu'on filtre mentalement, puis qu'on ne
    lit plus. Le jour où il compte, il ne sera pas lu non plus.

    Ce journal permet la seule règle qui empêche cela : se taire sur ce qui a
    déjà été dit, SAUF si la situation a empiré.

LA DISSYMÉTRIE EST VOULUE
    « Déjà signalé » fait taire. « Déjà signalé mais pire » fait parler. Sans
    le second terme, l'agent deviendrait silencieux exactement au moment où une
    dégradation s'installe — c'est-à-dire quand il sert à quelque chose. C'est
    pourquoi la note d'arbitrage est stockée avec le signalement, et pas
    seulement l'entité et la date.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)


class AgentRepository:
    """Lecture et écriture du journal des agents."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- Lecture

    def last_report(
        self, agent: str, entity_level: str, entity_key: str
    ) -> Optional[dict]:
        """Dernier signalement connu pour cette entité, ou None.

        Ne filtre pas sur une fenêtre : c'est l'appelant qui décide ce qu'il
        considère comme récent. Une requête qui trancherait ici obligerait à
        connaître la règle de refroidissement en deux endroits.
        """
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT report_id, score, text, payload, created_at
                    FROM agent_reports
                    WHERE agent = %s AND entity_level = %s AND entity_key = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (agent, entity_level, str(entity_key)),
                )
                row = cur.fetchone()
        except Exception:  # noqa: BLE001
            # Un journal illisible ne doit pas empêcher l'agent de tourner : il
            # reparlera d'un sujet déjà traité, ce qui est gênant, là où une
            # exception le rendrait entièrement muet.
            logger.warning("Journal des agents illisible.", exc_info=True)
            return None
        return dict(row) if row else None

    def recent(self, agent: str, days: int = 7, limit: int = 50) -> list[dict]:
        """Signalements récents, du plus frais au plus ancien."""
        depuis = datetime.now(timezone.utc) - timedelta(days=days)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT report_id, entity_level, entity_key, entity_label,
                       score, text, delivered, created_at
                FROM agent_reports
                WHERE agent = %s AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (agent, depuis, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def count_since(
        self, agent: str, depuis: datetime, *, utilisateur: Optional[str] = None
    ) -> int:
        """Signalements écrits par un agent depuis une date, éventuellement par
        personne.

        SERT LE PLAFOND PAR UTILISATEUR DE L'AGENT CONVERSATIONNEL. Ce décompte
        vit en base et non en mémoire pour la même raison que le budget d'appels
        du client LLM : un compteur en mémoire repart de zéro à chaque
        redémarrage du processus, c'est-à-dire précisément après l'incident qui
        aurait épuisé le quota.

        En cas d'erreur, rend 0 — donc LAISSE PASSER. Un plafond qui se ferme
        parce que la base est indisponible serait une panne plus grave que celle
        qu'il prévient : le robot cesserait de répondre sans dire pourquoi.
        """
        try:
            with self.db.cursor() as cur:
                if utilisateur is None:
                    cur.execute(
                        "SELECT COUNT(*) FROM agent_reports "
                        "WHERE agent = %s AND created_at >= %s",
                        (agent, depuis),
                    )
                else:
                    # L'utilisateur est dans le `payload` et non dans une
                    # colonne : `agent_reports` décrit ce dont un agent a parlé,
                    # pas à qui. Ajouter une colonne pour un seul agent
                    # imposerait un NULL à tous les autres. Le volume — quelques
                    # dizaines de lignes par jour — rend l'index sur
                    # `created_at` largement suffisant.
                    cur.execute(
                        "SELECT COUNT(*) FROM agent_reports "
                        "WHERE agent = %s AND created_at >= %s "
                        "AND payload->>'utilisateur' = %s",
                        (agent, depuis, utilisateur),
                    )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            logger.warning("Décompte des signalements illisible.", exc_info=True)
            return 0

    # ------------------------------------------------------------ Écriture

    def record(
        self,
        *,
        agent: str,
        entity_level: str,
        entity_key: str,
        entity_label: str,
        score: float,
        text: str,
        payload: Optional[dict] = None,
        delivered: bool = False,
    ) -> Optional[int]:
        """Consigne un signalement. Renvoie son identifiant, ou None si échec.

        Un échec d'écriture n'est pas propagé : le briefing est déjà parti, et
        lever ici ferait échouer un passage dont le travail utile est fait. Le
        coût réel est une répétition possible au passage suivant.
        """
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_reports
                        (agent, entity_level, entity_key, entity_label,
                         score, text, payload, delivered)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING report_id
                    """,
                    (
                        agent,
                        entity_level,
                        str(entity_key),
                        entity_label,
                        float(score),
                        text,
                        json.dumps(payload or {}, ensure_ascii=False, default=str),
                        delivered,
                    ),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:  # noqa: BLE001
            logger.warning("Signalement non journalisé.", exc_info=True)
            return None

    def mark_delivered(self, report_id: int) -> None:
        """Marque un signalement comme effectivement acheminé."""
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE agent_reports SET delivered = TRUE WHERE report_id = %s",
                    (report_id,),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Acheminement non tracé.", exc_info=True)


def should_report(
    dernier: Optional[dict],
    score: float,
    *,
    cooldown_days: int,
    aggravation_points: float,
    maintenant: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Faut-il reparler de cette entité ? Décision pure, sans base ni horloge.

    Args:
        dernier: dernier signalement connu (`last_report`), ou None.
        score: note d'arbitrage du candidat au passage courant.
        cooldown_days: durée pendant laquelle on ne se répète pas.
        aggravation_points: écart de note à partir duquel on reparle malgré
            le refroidissement. Ce n'est pas un pourcentage : les notes sont
            déjà exprimées dans la même échelle, et une hausse relative
            écraserait les petits scores tout en banalisant les gros.

    Returns:
        (décision, raison lisible). La raison est journalisée puis affichée
        en mode verbeux : un agent qui se tait sans dire pourquoi est
        indébogable, et c'est le premier reproche qu'on lui fera.
    """
    if dernier is None:
        return True, "jamais signalé"

    maintenant = maintenant or datetime.now(timezone.utc)
    quand = dernier.get("created_at")
    if quand is None:
        return True, "signalement antérieur sans date"
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=timezone.utc)

    age = maintenant - quand
    if age >= timedelta(days=cooldown_days):
        return True, f"dernier signalement il y a {age.days} j"

    ancien = float(dernier.get("score") or 0.0)
    delta = score - ancien
    if delta >= aggravation_points:
        return True, f"aggravation (+{delta:.1f} depuis le dernier signalement)"

    return (
        False,
        f"déjà signalé il y a {age.days} j sans aggravation "
        f"({ancien:.1f} → {score:.1f})",
    )
