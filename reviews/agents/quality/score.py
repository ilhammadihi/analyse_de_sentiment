"""
Module 8 — le score de qualité, et surtout son EXPLICATION.

POURQUOI CHAQUE COMPOSANTE RETOURNE AUSSI SA PHRASE
    Un score composite sans ses composantes est indéfendable. « Orange Mali :
    42 % » n'appelle aucune action ; la première question posée en réunion sera
    « pourquoi 42 ? », et il faut pouvoir y répondre six semaines plus tard,
    y compris après un changement de pondération.

    Chaque fonction rend donc un couple (valeur, phrase). La phrase est
    calculée avec la valeur, jamais rédigée après coup par un modèle : un texte
    produit séparément finirait par contredire le nombre qu'il commente — c'est
    exactement la panne que `_periode_lisible` documente côté Agent 1.

LES COMPOSANTES NON MESURABLES VALENT `None`, JAMAIS ZÉRO
    C'est la décision la plus importante du module. Une filiale sans aucun avis
    n'a pas une « fraîcheur de 0 % » : elle n'a pas de fraîcheur du tout. Lui
    donner 0 la punirait deux fois pour le même fait — une fois en couverture,
    une fois en fraîcheur — et rendrait son score incomparable à celui d'une
    filiale simplement en retard de collecte.

    Les composantes absentes sont donc RETIRÉES du calcul, et les poids
    restants renormalisés. Le score reste sur la même échelle, et
    `components` dit lesquelles ont servi.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from reviews.agents.quality.couverture import CouvertureFiliale
from reviews.agents.quality.diagnostic import Cas, Diagnostic

logger = logging.getLogger(__name__)

#: Statuts de confiance, du plus sûr au moins sûr. Ce sont les mots — et non
#: les nombres — que lisent les Agents 1 et 2.
TRUSTED = "TRUSTED"
ACCEPTABLE = "ACCEPTABLE"
DEGRADED = "DEGRADED"
UNTRUSTED = "UNTRUSTED"


@dataclass
class Composante:
    """Une note entre 0 et 1, ou None si la question ne se pose pas."""

    valeur: Optional[float]
    explication: str

    def as_dict(self) -> dict[str, Any]:
        return {"valeur": self.valeur, "explication": self.explication}


@dataclass
class ScoreQualite:
    """Score global d'une filiale, avec le détail qui permet de le défendre."""

    subsidiary_id: int
    subsidiary: str
    global_score: float
    statut: str
    diagnostic: Optional[str] = None
    composantes: dict[str, Composante] = field(default_factory=dict)
    poids_appliques: dict[str, float] = field(default_factory=dict)

    def valeur(self, nom: str) -> Optional[float]:
        c = self.composantes.get(nom)
        return c.valeur if c else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subsidiary_id": self.subsidiary_id,
            "subsidiary": self.subsidiary,
            "global_score": round(self.global_score, 4),
            "status": self.statut,
            "diagnostic": self.diagnostic,
            "composantes": {k: v.as_dict() for k, v in self.composantes.items()},
            # LES POIDS SONT FIGÉS DANS LA LIGNE, pas seulement dans la
            # configuration : un score relu dans six semaines doit rester
            # vérifiable même si les pondérations ont changé entre-temps.
            "poids_appliques": self.poids_appliques,
        }


# ---------------------------------------------------------------------------
# Composantes
# ---------------------------------------------------------------------------


def couverture_score(
    c: CouvertureFiliale, *, min_reviews: int, min_sources: int
) -> Composante:
    """Y a-t-il assez d'avis, sur assez des sources attendues ?

    DEUX MOITIÉS, ET C'EST VOLONTAIRE. Le volume seul récompenserait une
    filiale qui tire 400 avis d'une plateforme unique autant qu'une filiale
    équilibrée sur trois sources. La part de sources attendues réellement
    actives corrige cela sans rien enlever au volume.
    """
    if not c.sources_attendues:
        return Composante(
            None,
            "Aucune source d'avis clients déclarée : la couverture n'est pas "
            "mesurable pour cette filiale.",
        )

    # Le volume sature au seuil : au-delà, « plus d'avis » n'améliore plus la
    # confiance qu'on accorde au taux, il ne fait que grossir le corpus.
    volume = min(1.0, c.avis_clients / max(1, min_reviews))
    part_sources = c.taux_couverture_sources or 0.0
    valeur = 0.5 * volume + 0.5 * part_sources

    actives, attendues = len(c.sources_actives), len(c.sources_attendues)
    return Composante(
        round(valeur, 4),
        f"{c.avis_clients} avis clients (seuil {min_reviews}) et "
        f"{actives}/{attendues} source(s) attendue(s) réellement active(s).",
    )


def fraicheur_score(
    c: CouvertureFiliale,
    *,
    cadences_minutes: dict[str, int],
    stale_factor: float,
    maintenant: Optional[datetime] = None,
) -> Composante:
    """Les données arrivent-elles encore, au rythme attendu de chaque source ?

    COMPARÉE À LA CADENCE DÉCLARÉE, jamais à un délai fixe. Les cadences vont
    de 6 h (flux) à 24 h (Google Maps) : « rien depuis 48 h » est normal pour
    l'un et anormal pour l'autre. Un seuil absolu produirait du bruit sur les
    sources lentes et de la cécité sur les rapides.
    """
    maintenant = maintenant or datetime.now(timezone.utc)
    notes: list[float] = []
    retards: list[str] = []

    for code in c.sources_actives:
        etat = c.sources[code]
        if etat.derniere_collecte is None:
            continue
        quand = etat.derniere_collecte
        if quand.tzinfo is None:
            quand = quand.replace(tzinfo=timezone.utc)
        heures = (maintenant - quand).total_seconds() / 3600.0
        cadence_h = max(1.0, cadences_minutes.get(code, 360) / 60.0)
        limite = cadence_h * stale_factor
        # 1.0 tant qu'on est dans la cadence, puis décroissance linéaire
        # jusqu'à 0 à deux fois la limite. Une chute brutale à 0 dès la limite
        # franchie ferait passer une source d'« à l'heure » à « morte » en une
        # minute, et le score global sauterait sans que rien n'ait changé.
        if heures <= cadence_h:
            notes.append(1.0)
        else:
            notes.append(max(0.0, 1.0 - (heures - cadence_h) / max(1.0, limite)))
            if heures > limite:
                retards.append(f"{code} ({heures:.0f} h)")

    if not notes:
        return Composante(
            None,
            "Aucune source active datée : la fraîcheur ne se mesure pas sur une "
            "filiale qui n'a encore rien reçu.",
        )

    valeur = sum(notes) / len(notes)
    if retards:
        detail = "en retard : " + ", ".join(retards)
    else:
        detail = "toutes les sources actives sont dans leur cadence"
    return Composante(round(valeur, 4), f"{len(notes)} source(s) datée(s), {detail}.")


def completude_score(stats: Optional[dict[str, Any]]) -> Composante:
    """Les avis sont-ils exploitables : un texte, et une date de publication ?

    LA NOTE N'EST PAS DANS LE CALCUL, et c'est une mesure du corpus qui
    l'impose : quatre avis Google Maps sur cinq n'ont pas de commentaire, et
    Reddit n'a pas de note du tout (`has_rating = false`). Compter l'absence de
    note comme un défaut de complétude punirait les sources pour leur nature.
    """
    if not stats or not stats.get("total"):
        return Composante(None, "Aucun avis à évaluer.")

    total = stats["total"]
    avec_texte = stats.get("avec_texte", 0)
    avec_date = stats.get("avec_date", 0)
    valeur = (avec_texte / total) * 0.7 + (avec_date / total) * 0.3
    return Composante(
        round(valeur, 4),
        f"{avec_texte}/{total} avis portent un texte exploitable, "
        f"{avec_date}/{total} une date de publication.",
    )


def coherence_score(constats: int, avis: int) -> Composante:
    """Combien de constats de qualité non résolus pèsent sur cette filiale ?

    RAPPORTÉ AU VOLUME, jamais en valeur absolue : dix doublons sur quarante
    avis est un problème, dix sur quatre mille est du bruit statistique. Un
    seuil absolu ferait apparaître les grosses filiales comme les plus sales
    alors qu'elles sont seulement les plus fournies.
    """
    if not avis:
        return Composante(None, "Aucun avis : la cohérence ne se mesure pas.")
    part = constats / avis
    # 10 % de constats non résolus ramène la note à zéro. Au-delà, ce n'est
    # plus une question de degré : le corpus de la filiale n'est pas utilisable.
    valeur = max(0.0, 1.0 - part * 10.0)
    return Composante(
        round(valeur, 4),
        f"{constats} constat(s) de qualité non résolu(s) sur {avis} avis."
        if constats
        else f"Aucun constat de qualité en attente sur {avis} avis.",
    )


def diversite_score(c: CouvertureFiliale, *, min_sources: int) -> Composante:
    """Le signal repose-t-il sur une seule plateforme ?

    MESURÉ ET NON SUPPOSÉ : Google Maps est la seule source de 130 filiales sur
    135. Cette composante est donc structurellement basse sur presque tout le
    périmètre, et c'est une information juste — un taux de mécontentement fondé
    sur une seule plateforme hérite des biais de cette plateforme, ce que la
    migration 007 a déjà établi pour HelloPeter.
    """
    actives = len(c.sources_actives)
    if actives == 0:
        return Composante(0.0, "Aucune source active : signal inexistant.")
    valeur = min(1.0, actives / max(1, min_sources))
    return Composante(
        round(valeur, 4),
        f"{actives} source(s) active(s) pour {min_sources} attendue(s) : "
        + (
            "le signal ne dépend pas d'une plateforme unique."
            if actives >= min_sources
            else "le signal dépend d'une seule plateforme."
        ),
    )


def fiabilite_score(c: CouvertureFiliale) -> Composante:
    """Les collecteurs attendus aboutissent-ils ?

    Compte les unités qui n'ont JAMAIS abouti, et non les échecs récents : une
    unité qui échoue aujourd'hui après avoir réussi hier ne signale pas une
    panne. C'est la lecture retenue par `collection_jobs` depuis l'origine.
    """
    attendues = c.sources_attendues
    if not attendues:
        return Composante(None, "Aucune source attendue : rien à fiabiliser.")

    saines = sum(
        1
        for code in attendues
        if c.sources[code].unites_jamais_reussies == 0
        or c.sources[code].unites_deja_reussies > 0
    )
    valeur = saines / len(attendues)
    return Composante(
        round(valeur, 4),
        f"{saines}/{len(attendues)} source(s) attendue(s) aboutissent."
        + ("" if saines == len(attendues) else f" En échec : {', '.join(c.sources_en_erreur)}."),
    )


# ---------------------------------------------------------------------------
# Agrégation
# ---------------------------------------------------------------------------


def poids_normalises(poids: dict[str, float], presentes: set[str]) -> dict[str, float]:
    """Renormalise les poids sur les seules composantes mesurables.

    C'est ce qui permet à une composante absente de ne PAS pénaliser : le score
    reste sur [0, 1] quel que soit le nombre de composantes disponibles.
    Sans renormalisation, une filiale à trois composantes sur six plafonnerait
    mécaniquement à 55 % même parfaite — et son statut de confiance serait faux.
    """
    retenus = {k: max(0.0, v) for k, v in poids.items() if k in presentes}
    total = sum(retenus.values())
    if total <= 0:
        # Aucun poids exploitable : on répartit également plutôt que de diviser
        # par zéro. Cas de figure d'une configuration mise entièrement à 0.
        if not retenus:
            return {}
        part = 1.0 / len(retenus)
        return {k: part for k in retenus}
    return {k: v / total for k, v in retenus.items()}


def statut_confiance(
    score: float, *, trusted_at: float, acceptable_at: float, degraded_at: float
) -> str:
    """Traduit un score en mot. C'est ce mot que consomment les Agents 1 et 2."""
    if score >= trusted_at:
        return TRUSTED
    if score >= acceptable_at:
        return ACCEPTABLE
    if score >= degraded_at:
        return DEGRADED
    return UNTRUSTED


def calculer_score(
    couverture: CouvertureFiliale,
    diagnostic: Optional[Diagnostic] = None,
    *,
    poids: dict[str, float],
    min_reviews: int = 10,
    min_sources: int = 2,
    stale_factor: float = 3.0,
    cadences_minutes: Optional[dict[str, int]] = None,
    stats_completude: Optional[dict[str, Any]] = None,
    constats_ouverts: int = 0,
    trusted_at: float = 0.75,
    acceptable_at: float = 0.55,
    degraded_at: float = 0.30,
    maintenant: Optional[datetime] = None,
) -> ScoreQualite:
    """Assemble les six composantes en un score explicable.

    Fonction PURE : aucune base, aucun réseau. Tout ce dont elle a besoin lui
    est passé. C'est ce qui la rend testable sans infrastructure, et c'est une
    exigence de l'énoncé au même titre que le reste.
    """
    composantes = {
        "coverage": couverture_score(
            couverture, min_reviews=min_reviews, min_sources=min_sources
        ),
        "freshness": fraicheur_score(
            couverture,
            cadences_minutes=cadences_minutes or {},
            stale_factor=stale_factor,
            maintenant=maintenant,
        ),
        "completeness": completude_score(stats_completude),
        "consistency": coherence_score(constats_ouverts, couverture.avis_clients),
        "diversity": diversite_score(couverture, min_sources=min_sources),
        "reliability": fiabilite_score(couverture),
    }

    presentes = {k for k, c in composantes.items() if c.valeur is not None}
    appliques = poids_normalises(poids, presentes)
    global_score = sum(
        composantes[k].valeur * p for k, p in appliques.items()  # type: ignore[operator]
    )

    # UN DIAGNOSTIC BLOQUANT PLAFONNE LE SCORE, il ne s'y ajoute pas.
    #
    # Sans ce plafond, une filiale dont le collecteur est en panne depuis une
    # semaine peut afficher un score honorable : ses avis anciens restent
    # nombreux, complets et cohérents. Le score décrirait alors fidèlement un
    # corpus figé — et les Agents 1 et 2 continueraient de raisonner dessus
    # comme s'il vivait encore. Le plafond force le statut à dire ce que le
    # diagnostic sait.
    if diagnostic is not None and diagnostic.bloquant:
        global_score = min(global_score, degraded_at)

    return ScoreQualite(
        subsidiary_id=couverture.subsidiary_id,
        subsidiary=couverture.subsidiary,
        global_score=round(max(0.0, min(1.0, global_score)), 4),
        statut=statut_confiance(
            global_score,
            trusted_at=trusted_at,
            acceptable_at=acceptable_at,
            degraded_at=degraded_at,
        ),
        diagnostic=diagnostic.cas.value if diagnostic else None,
        composantes=composantes,
        poids_appliques={k: round(v, 4) for k, v in appliques.items()},
    )
