"""
Le garde-fou : ce que l'Agent 3 dit aux Agents 1 et 2 avant qu'ils ne parlent.

CE QUI SÉPARE UN OBSERVATEUR D'UN GARDIEN
    Jusqu'ici l'Agent 3 mesurait la qualité et l'affichait. Les Agents 1 et 2
    continuaient de raisonner sans jamais la consulter : un briefing pouvait
    donc annoncer une dégradation de satisfaction sur une filiale dont le taux
    repose sur quatre avis, et une campagne pouvait s'appuyer sur une
    affirmation qu'aucune source indépendante ne corrobore.

    Ce module est le point de contact. Il ne fait qu'une chose : rendre un
    verdict lisible sur un périmètre, et la phrase à faire figurer.

UNE DÉPENDANCE PYTHON, PAS UN APPEL HTTP
    Les trois agents vivent dans le même processus. Passer par `/quality/trust`
    ferait sortir la requête sur le réseau pour revenir au même code, ajouterait
    une panne possible (l'API arrêtée pendant que le worker tourne — c'est le
    cas nominal en exploitation, ce sont deux conteneurs distincts) et une
    latence, pour rigoureusement rien. L'API reste le point d'entrée des
    consommateurs EXTERNES ; en interne, on appelle la fonction.

TROIS PROPRIÉTÉS QUI RENDENT L'INTÉGRATION SANS DANGER
    1. CONFIGURABLE — `ENABLE_QUALITY_GATE`. À faux, `verdict()` rend toujours
       `INDETERMINE` et rien ne change pour personne.
    2. JAMAIS BLOQUANTE — une base indisponible, une table absente, un
       instantané jamais calculé rendent `INDETERMINE`, qui laisse passer. Un
       garde-fou qui fait taire les deux autres agents parce qu'il n'a pas pu
       lire son propre score serait une panne plus grave que celle qu'il
       prévient.
    3. ADDITIVE — elle ajoute une mention, elle ne retire jamais un chiffre.
       Le lecteur garde la mesure ET sa réserve.

POURQUOI `INDETERMINE` LAISSE PASSER PLUTÔT QUE DE BLOQUER
    C'est le choix inverse de celui qu'on ferait pour un contrôle d'accès, et
    il est délibéré. Le risque ici n'est pas qu'une donnée douteuse soit lue :
    c'est que les agents se taisent. Un agent muet ne se remarque pas — c'est
    exactement le mode de panne qui a fait taire l'alerting trois jours durant.
    Une réserve manquante, elle, se voit.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)

TRUSTED = "TRUSTED"
ACCEPTABLE = "ACCEPTABLE"
DEGRADED = "DEGRADED"
UNTRUSTED = "UNTRUSTED"

#: Rendu quand la question ne peut pas être tranchée : garde-fou désactivé,
#: filiale jamais évaluée, ou lecture impossible. LAISSE PASSER — voir l'en-tête.
INDETERMINE = "INDETERMINE"

#: Statuts sur lesquels une recommandation ne doit pas être présentée comme
#: fiable. Volontairement réduit à un seul : `DEGRADED` appelle une réserve,
#: pas un renoncement — et renoncer trop tôt viderait le briefing de son
#: intérêt sur les filiales les moins couvertes, qui sont justement celles
#: dont personne ne parle jamais.
STATUTS_NON_FIABLES = frozenset({UNTRUSTED})

#: Statuts appelant une mention de réserve.
STATUTS_A_MENTIONNER = frozenset({UNTRUSTED, DEGRADED})

#: Mentions rendues aux agents. RÉDIGÉES ICI, jamais par un modèle : ce sont
#: des avertissements, ils doivent être identiques d'un passage à l'autre et
#: reconnaissables au premier coup d'œil.
_MENTIONS = {
    UNTRUSTED: (
        "⚠️ Les données disponibles sont insuffisantes pour établir une "
        "recommandation fiable sur cette filiale."
    ),
    DEGRADED: (
        "⚠️ Recommandation possible, mais les données présentent une couverture "
        "limitée."
    ),
}


@dataclass(frozen=True)
class Verdict:
    """Ce que le garde-fou répond sur un périmètre."""

    statut: str
    score: Optional[float] = None
    diagnostic: Optional[str] = None
    mention: Optional[str] = None

    @property
    def fiable(self) -> bool:
        """Une recommandation peut-elle être présentée comme fiable ?

        `INDETERMINE` est fiable : voir l'en-tête du module. On ne fait pas
        taire un agent parce qu'on n'a pas su lire un score.
        """
        return self.statut not in STATUTS_NON_FIABLES

    @property
    def evalue(self) -> bool:
        return self.statut != INDETERMINE

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.statut,
            "score": self.score,
            "diagnostic": self.diagnostic,
            "mention": self.mention,
            "fiable": self.fiable,
        }


#: Verdict neutre, réutilisé plutôt que reconstruit : il n'a pas d'état.
NEUTRE = Verdict(statut=INDETERMINE)


class GardeQualite:
    """Consulte le dernier instantané de qualité d'une filiale."""

    def __init__(self, db: Optional[Database], enabled: bool = True):
        self.db = db
        self.enabled = enabled
        #: Les agents interrogent souvent la même filiale plusieurs fois dans un
        #: même passage (candidat, rédaction, envoi). Le cache évite d'en faire
        #: autant de requêtes, et sa durée de vie est celle de l'objet — donc
        #: d'un passage. Aucun risque de servir un score périmé au passage
        #: suivant, qui construit un nouveau garde.
        self._cache: dict[int, Verdict] = {}

    def verdict(self, subsidiary_id: Optional[Any]) -> Verdict:
        """Verdict sur une filiale. Ne lève JAMAIS.

        Rend `NEUTRE` — qui laisse passer — dans tous les cas où la question
        n'a pas de réponse : garde-fou désactivé, identifiant illisible, base
        indisponible, filiale jamais évaluée.
        """
        if not self.enabled or self.db is None or subsidiary_id is None:
            return NEUTRE
        try:
            sid = int(subsidiary_id)
        except (TypeError, ValueError):
            return NEUTRE

        if sid in self._cache:
            return self._cache[sid]

        verdict = NEUTRE
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    "SELECT global_score, status, diagnostic "
                    "FROM v_quality_latest WHERE subsidiary_id = %s",
                    (sid,),
                )
                row = cur.fetchone()
            if row:
                statut = row["status"]
                verdict = Verdict(
                    statut=statut,
                    score=(
                        round(float(row["global_score"]), 3)
                        if row["global_score"] is not None else None
                    ),
                    diagnostic=row["diagnostic"],
                    mention=_MENTIONS.get(statut),
                )
        except Exception:  # noqa: BLE001
            # Table absente (migration non appliquée), base indisponible : on
            # laisse passer. Journalisé en `warning` et non en `exception` :
            # c'est un état prévu sur un déploiement neuf, pas un incident.
            logger.warning(
                "Garde-fou qualité illisible pour la filiale %s : on laisse passer.",
                sid, exc_info=True,
            )

        self._cache[sid] = verdict
        return verdict

    def mention(self, subsidiary_id: Optional[Any]) -> Optional[str]:
        """Raccourci : la phrase de réserve, ou None s'il n'y en a pas."""
        return self.verdict(subsidiary_id).mention


def construire_garde(
    db: Optional[Database], settings: Any
) -> GardeQualite:
    """Assemble le garde-fou depuis la configuration.

    Tolère un `settings` incomplet — un double de test, une configuration plus
    ancienne — en retombant sur « actif ». C'est le comportement voulu : un
    attribut manquant ne doit pas désactiver silencieusement un garde-fou.
    """
    quality = getattr(settings, "quality", None)
    enabled = bool(getattr(quality, "gate_enabled", True)) if quality else True
    return GardeQualite(db, enabled=enabled)
