"""
Arbitre de pertinence : lequel de ces mouvements mérite qu'on dérange quelqu'un ?

LA QUESTION QUE CE MODULE TRANCHE, ET POURQUOI PAS LE MODÈLE
    Au 10 août 2026, le dashboard affiche 14 pics critiques. Un agent qui les
    remonterait tous les 14 n'apporterait rien de plus que l'écran — il
    coûterait juste une notification. Il doit choisir.

    Ce choix N'EST PAS confié au LLM, et c'est la même règle que celle qui régit
    déjà `llm/insights.py` et `llm/briefing.py` : un modèle à qui l'on demande
    « lequel est le plus grave ? » répond toujours quelque chose de plausible,
    dans un ordre qui varie d'un appel à l'autre pour les mêmes données. Un
    briefing quotidien dont le classement bouge sans que les chiffres bougent
    est un briefing auquel on cesse de se fier.

    Ici, la même entrée donne toujours la même note. Le modèle n'intervient
    qu'ensuite, pour rédiger ce qui a déjà été trié.

LES QUATRE CRITÈRES, ET CE QU'ILS ÉCARTENT
    ampleur      Combien de points la part de négatifs a-t-elle pris ? C'est le
                 signal de base — sans lui, rien n'est arrivé.
    volume       Sur combien d'avis ? GARDE-FOU PRINCIPAL : mesuré, Vodacom
                 South Africa affichait « −75,2 points » parce que sa fenêtre
                 antérieure contenait UN avis. Une variation énorme portée par
                 trois avis n'est pas un incident, c'est du bruit
                 d'échantillonnage, et le remonter décrédibilise l'agent en une
                 fois.
    persistance  Combien d'alertes critiques d'affilée sur cette filiale ?
                 Trois pics en cinq jours ne sont pas trois accidents : c'est
                 une dégradation installée, et cela vaut plus qu'un pic isolé
                 deux fois plus ample.
    étendue      Combien de filiales du MÊME PAYS bougent en même temps ?
                 Trois filiales sud-africaines qui décrochent ensemble, c'est
                 un fait national — coupure, décision du régulateur — et non
                 trois incidents séparés à traiter séparément.

CE QUE L'ARBITRE NE FAIT PAS
    Il ne dit pas la CAUSE. Il dit ce qui mérite d'être regardé, et pourquoi il
    l'a retenu. La cause reste le travail de `briefing.diagnose`, qui dispose
    des concentrations et, depuis le 10 août, des faits externes de presse.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

#: Volume d'avis exigé sur CHACUNE des deux fenêtres comparées.
#:
#: QUINZE, ET NON TRENTE — le seuil suit la fenêtre.
#:
#: Trente était aligné sur `_MIN_VOLUME_FOR_DELTA` des synthèses, qui portent
#: sur quatre-vingt-dix jours. Depuis que l'agent compare deux semaines à deux
#: semaines, exiger trente avis de chaque côté revient à demander soixante avis
#: par mois : mesuré, une seule filiale du parc y parvient, et l'agent se
#: tairait presque toujours.
#:
#: Quinze reste plus exigeant que l'alerting, qui déclenche dès dix avis sur
#: sept jours. Le principe demeure : le seuil se juge par rapport à la fenêtre,
#: jamais dans l'absolu.
VOLUME_PLANCHER = 15

#: Ampleur en dessous de laquelle on ne dérange personne, quel que soit le
#: volume. Dix points de part de négatifs, c'est le seuil que l'alerting
#: applique déjà pour lever un pic : le reprendre ici évite qu'un agent
#: quotidien remonte des mouvements qu'aucune alerte n'a jugés dignes.
AMPLEUR_PLANCHER = 10.0

#: Poids des critères dans la note finale.
#:
#: L'ampleur domine — c'est le fait —, mais elle ne peut pas l'emporter seule :
#: la persistance et l'étendue existent précisément pour faire remonter un
#: mouvement modéré mais installé, ou modéré mais généralisé, devant un pic
#: spectaculaire et isolé. Sans elles, l'agent ne signalerait que des accidents.
POIDS_AMPLEUR = 1.0
POIDS_PERSISTANCE = 6.0
POIDS_ETENDUE = 5.0

#: Plafond du nombre d'occurrences prises en compte par critère.
MAX_PERSISTANCE = 3
MAX_ETENDUE = 3

#: Les bonus ne peuvent pas dépasser l'ampleur elle-même.
#:
#: MESURÉ EN TEST, et c'est la raison de ce plafond relatif : avec des plafonds
#: seulement absolus, une filiale à +12 points cumulant 8 alertes et 8 voisines
#: atteignait 45 — exactement la note d'un décrochage de 45 points. Les bonus
#: fabriquaient un sujet majeur à partir d'un mouvement mineur.
#:
#: Un bonus doit AMPLIFIER un signal réel, jamais s'y substituer. Plafonné à
#: l'ampleur, il peut au mieux doubler la note : de quoi faire passer une
#: dégradation installée devant un pic isolé comparable, jamais devant un
#: décrochage quatre fois plus ample.
PLAFOND_BONUS_RELATIF = 1.0


@dataclass
class Candidat:
    """Un mouvement soumis à l'arbitrage."""

    level: str
    key: str
    label: str
    pays: Optional[str] = None
    #: Code ISO alpha-2, au sens du contrat de filtre. Distinct de `pays`, qui
    #: est le nom affichable : le contexte marché se lit par code, jamais par
    #: nom — « Côte d'Ivoire » et « Cote d'Ivoire » ne joindraient pas.
    iso2: Optional[str] = None
    delta_negatifs: float = 0.0
    part_negatifs: Optional[float] = None
    avis_clients: int = 0
    avis_clients_avant: int = 0
    #: Nombre d'alertes critiques récentes portant sur cette entité.
    alertes_recentes: int = 0
    #: Nombre d'entités du même pays également en dégradation notable.
    voisins_degrades: int = 0

    #: Renseignés par `arbitrer`.
    score: float = 0.0
    retenu: bool = False
    raisons: list[str] = field(default_factory=list)
    ecarte_parce_que: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "niveau": self.level,
            "entite": self.label,
            "pays": self.pays,
            "variation_negatifs_points": round(self.delta_negatifs, 1),
            "part_negatifs": self.part_negatifs,
            "avis_clients": self.avis_clients,
            "avis_clients_avant": self.avis_clients_avant,
            "alertes_critiques_recentes": self.alertes_recentes,
            "filiales_du_pays_aussi_touchees": self.voisins_degrades,
            "score": round(self.score, 1),
            "raisons_de_la_selection": list(self.raisons),
        }


def arbitrer(candidats: list[Candidat]) -> list[Candidat]:
    """Note et trie les candidats. Ne garde rien, ne coupe rien : l'appelant décide.

    Chaque candidat repart annoté — `score`, `retenu`, et surtout `raisons` ou
    `ecarte_parce_que`. Ces textes ne sont pas décoratifs : ils sont transmis au
    modèle comme faits établis, et affichés en mode verbeux pour qu'on puisse
    déboguer un silence. Un agent qui se tait sans dire pourquoi est le plus
    difficile à corriger.
    """
    for c in candidats:
        c.score = 0.0
        c.raisons = []
        c.ecarte_parce_que = None

        # --- Éliminations. Elles priment sur toute note : un mouvement non
        # défendable ne doit pas pouvoir être rattrapé par des bonus.
        volume_utile = min(c.avis_clients, c.avis_clients_avant)
        if volume_utile < VOLUME_PLANCHER:
            c.ecarte_parce_que = (
                f"volume insuffisant ({c.avis_clients_avant} avis avant, "
                f"{c.avis_clients} après ; minimum {VOLUME_PLANCHER} sur chaque "
                "fenêtre) — la variation n'est pas distinguable du bruit"
            )
            continue
        if c.delta_negatifs < AMPLEUR_PLANCHER:
            c.ecarte_parce_que = (
                f"variation trop faible ({c.delta_negatifs:+.1f} pts ; "
                f"minimum {AMPLEUR_PLANCHER:.0f})"
            )
            continue

        # --- Notation. L'ampleur est la base ; le reste ne fait que la moduler.
        ampleur = c.delta_negatifs * POIDS_AMPLEUR
        c.raisons.append(
            f"part de négatifs en hausse de {c.delta_negatifs:.1f} points "
            f"sur {c.avis_clients} avis clients"
        )

        bonus = 0.0
        if c.alertes_recentes >= 2:
            bonus += min(c.alertes_recentes, MAX_PERSISTANCE) * POIDS_PERSISTANCE
            c.raisons.append(
                f"{c.alertes_recentes} alertes critiques sur les derniers jours — "
                "dégradation installée, pas un pic isolé"
            )

        if c.voisins_degrades >= 1 and c.pays:
            bonus += min(c.voisins_degrades, MAX_ETENDUE) * POIDS_ETENDUE
            c.raisons.append(
                f"{c.voisins_degrades} autre(s) filiale(s) de {c.pays} se dégradent "
                "sur la même période — possible fait national"
            )

        c.score = ampleur + min(bonus, ampleur * PLAFOND_BONUS_RELATIF)
        c.retenu = True

    return sorted(candidats, key=lambda c: (-c.score, c.label))


def retenus(candidats: list[Candidat], limite: int) -> list[Candidat]:
    """Les `limite` premiers candidats retenus, déjà triés par `arbitrer`.

    La limite n'est pas une économie de jetons mais une règle de lisibilité :
    un briefing quotidien de dix sujets n'est pas lu. Trois sujets tiennent
    dans une notification et laissent au lecteur la capacité d'agir sur chacun.
    """
    return [c for c in candidats if c.retenu][:limite]
