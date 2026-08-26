"""
Accès à la table `campaigns` : proposer, retrouver, décider, rapporter.

CE QUE CE MODULE GARANTIT À L'AGENT
    1. Une lecture en échec ne le fait jamais taire : `derniere_pour` rend None
       et l'agent proposera peut-être une campagne déjà proposée — gênant, là où
       une exception l'aurait rendu entièrement muet.
    2. Une écriture en échec est signalée par un `None` de retour, jamais par une
       exception : au moment où elle survient, la proposition est déjà rédigée et
       souvent déjà partie.

    C'est la même asymétrie que celle d'`agent_repository`, et elle vient du même
    constat : la panne qu'on ne veut pas est celle où l'agent cesse de parler
    sans que personne ne le remarque.

LE STATUT N'EST JAMAIS DÉDUIT, IL EST ÉCRIT
    `proposed` -> `approved` | `rejected`, avec l'auteur et la date. Aucune
    transition automatique, aucune expiration silencieuse : une campagne oubliée
    reste « en attente », visible dans la liste, plutôt que de disparaître d'un
    fil que plus personne ne relit.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)

#: Statuts possibles. Liste blanche : `decider` compose un UPDATE, et un statut
#: venu d'une commande Telegram ne doit jamais y arriver sans contrôle.
STATUTS = ("proposed", "approved", "rejected")


class CampaignRepository:
    """Lecture et écriture des campagnes proposées."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- Écriture

    def creer(
        self,
        *,
        entity_level: str,
        entity_key: str,
        entity_label: str,
        segment: str,
        objective: str,
        channel: str,
        segment_size: float,
        window_days: int,
        hook: str,
        message: str,
        name: str = "",
        problem: str = "",
        brief: Optional[str] = None,
        written_by_llm: bool = False,
        payload: Optional[dict] = None,
        delivered: bool = False,
        tone: str = "factuel",
        strategy: Optional[str] = None,
        parent_id: Optional[int] = None,
        strategies: Optional[list] = None,
    ) -> Optional[int]:
        """Consigne une campagne proposée. Rend son identifiant, ou None."""
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO campaigns
                        (entity_level, entity_key, entity_label, segment,
                         objective, channel, segment_size, window_days,
                         hook, message, name, problem, brief, written_by_llm,
                         payload, delivered, tone, strategy, parent_id,
                         strategies)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s)
                    RETURNING campaign_id
                    """,
                    (
                        entity_level, str(entity_key), entity_label, segment,
                        objective, channel, float(segment_size), int(window_days),
                        hook, message, name, problem, brief, written_by_llm,
                        json.dumps(payload or {}, ensure_ascii=False, default=str),
                        delivered, tone, strategy, parent_id,
                        json.dumps(strategies or [], ensure_ascii=False, default=str),
                    ),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:  # noqa: BLE001
            logger.warning("Campagne non enregistrée.", exc_info=True)
            return None

    def enregistrer_contenus(self, campaign_id: int, contenus: dict) -> None:
        """Consigne les déclinaisons multi-formats.

        ÉCRIT UNE SEULE FOIS EN PRATIQUE : `contenus()` rend les formats déjà
        produits plutôt que d'en régénérer. Régénérer donnerait un autre texte
        pour la même campagne, et l'équipe ne saurait plus lequel a été validé.
        """
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE campaigns SET contents = %s WHERE campaign_id = %s",
                    (
                        json.dumps(contenus or {}, ensure_ascii=False, default=str),
                        campaign_id,
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Contenus de campagne non enregistrés.", exc_info=True)

    def marquer_transmise(
        self, campaign_id: int, message_id: Optional[int] = None
    ) -> None:
        """Trace l'acheminement, et l'identifiant du message qui le porte.

        L'IDENTIFIANT EST FACULTATIF ET NE CONDITIONNE RIEN : un envoi abouti
        dont on n'a pas su lire l'identifiant reste un envoi abouti. On perd
        seulement la possibilité de retirer ce message — d'où le `COALESCE`, qui
        n'écrase jamais un identifiant déjà connu par un NULL.
        """
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE campaigns SET delivered = TRUE, "
                    "telegram_message_id = COALESCE(%s, telegram_message_id) "
                    "WHERE campaign_id = %s",
                    (message_id, campaign_id),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Acheminement de campagne non tracé.", exc_info=True)

    def decider(self, campaign_id: int, statut: str, par: str) -> bool:
        """Valide ou rejette une campagne. Rend False si rien n'a été décidé.

        LA DÉCISION NE SE REJOUE PAS : la clause `status = 'proposed'` fait
        échouer une seconde validation. Sans elle, deux personnes cliquant à
        quelques secondes d'intervalle écraseraient mutuellement leur décision,
        et le journal attribuerait la campagne à la dernière — celle qui n'a
        peut-être fait que confirmer ce qu'elle croyait déjà validé.
        """
        if statut not in STATUTS:
            raise ValueError(f"Statut « {statut} » inconnu.")
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE campaigns
                       SET status = %s, decided_at = now(), decided_by = %s
                     WHERE campaign_id = %s AND status = 'proposed'
                    """,
                    (statut, par, campaign_id),
                )
                return cur.rowcount > 0
        except Exception:  # noqa: BLE001
            logger.warning("Décision de campagne non enregistrée.", exc_info=True)
            return False

    def enregistrer_rapport(self, campaign_id: int, rapport: dict) -> None:
        """Écrase le rapport précédent : c'est une photo, pas une série."""
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE campaigns
                       SET report = %s, reported_at = now()
                     WHERE campaign_id = %s
                    """,
                    (json.dumps(rapport, ensure_ascii=False, default=str), campaign_id),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Rapport de campagne non enregistré.", exc_info=True)

    # -------------------------------------------------------------- Lecture

    def derniere_pour(self, entity_level: str, entity_key: str) -> Optional[dict]:
        """Dernière campagne proposée pour cette entité, ou None.

        Rendue au format attendu par `should_report` — `created_at` et `score` —
        pour que la RÈGLE de non-répétition de l'agent de veille s'applique ici
        sans être réécrite. La table diffère, la règle est la même : ne pas
        reproposer ce qui vient de l'être, sauf si le segment a nettement grossi.
        """
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT campaign_id, segment, objective, segment_size AS score,
                           status, created_at
                      FROM campaigns
                     WHERE entity_level = %s AND entity_key = %s
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    (entity_level, str(entity_key)),
                )
                row = cur.fetchone()
        except Exception:  # noqa: BLE001
            logger.warning("Historique des campagnes illisible.", exc_info=True)
            return None
        return dict(row) if row else None

    def par_id(self, campaign_id: int) -> Optional[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT * FROM campaigns WHERE campaign_id = %s", (campaign_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def lister(
        self, statut: Optional[str] = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Campagnes, de la plus récente à la plus ancienne.

        Le texte du message n'est PAS rendu ici : la liste sert à choisir un
        identifiant, et faire tenir cinq messages complets dans une réponse
        Telegram la rendrait illisible. `par_id` donne le détail.
        """
        clause, params = "", []
        if statut:
            if statut not in STATUTS:
                raise ValueError(f"Statut « {statut} » inconnu.")
            clause = "WHERE status = %s"
            params = [statut]
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT campaign_id, entity_label, segment, objective, channel,
                       segment_size, status, hook, created_at, reported_at
                  FROM campaigns
                  {clause}
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    def date_de_reference(self, campagne: dict) -> datetime:
        """Point de départ du rapport : la validation, à défaut la proposition.

        POURQUOI PAS SIMPLEMENT `created_at`. Une campagne proposée le 3 et
        validée le 10 n'a rien pu produire entre les deux — elle n'existait que
        dans une conversation Telegram. Compter la période depuis la proposition
        ferait porter au bilan une semaine pendant laquelle rien n'a été fait, et
        diluerait d'autant l'effet qu'on cherche à voir.
        """
        depuis = campagne.get("decided_at") or campagne.get("created_at")
        if depuis is None:
            return datetime.now(timezone.utc)
        if depuis.tzinfo is None:
            depuis = depuis.replace(tzinfo=timezone.utc)
        return depuis
