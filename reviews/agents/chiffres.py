"""
Garde-fou numérique : un texte rédigé n'a le droit d'employer que des nombres
qui ont été mesurés.

POURQUOI CE MODULE EST PARTAGÉ ET NON RECOPIÉ
    La règle « le modèle ne calcule jamais » est écrite dans tous les prompts du
    projet. Une consigne de prompt est une probabilité ; ceci en fait une
    vérification. Deux agents rédigent aujourd'hui — l'assistant conversationnel
    et l'assistant de campagne — et un troisième rédigera demain. Recopier la
    vérification chez chacun, c'est accepter qu'une des copies dérive : celle
    qui aura oublié la tolérance d'arrondi rejettera tout, celle qui aura oublié
    les rangs laissera passer un pourcentage inventé.

CE QU'IL ATTRAPE, ET CE QU'IL N'ATTRAPE PAS
    Il attrape un NOMBRE absent des mesures : un pourcentage calculé de tête, un
    volume arrondi au millier, une comparaison à l'an dernier qui n'a jamais été
    demandée. C'est le mode de panne le plus dangereux d'un texte généré, parce
    qu'il est parfaitement lisible : rien, dans « soit 40 % de plus que l'an
    dernier », ne signale que ce chiffre n'existe pas.

    Il n'attrape pas une affirmation fausse sans chiffre (« la situation
    s'améliore »). C'est le rôle des prompts, qui interdisent de conclure, et de
    la structure des appels, qui ne donne au modèle que des faits déjà établis.
"""

import re
from typing import Any, Iterable

#: Repère un nombre écrit à la française comme à l'anglaise.
NOMBRE_RE = re.compile(r"\d+(?:[.,]\d+)?")

#: Écart toléré entre un nombre rédigé et la mesure dont il est censé venir.
#:
#: STRICTEMENT INFÉRIEUR À 1 : cela autorise « 92 % » pour une mesure de 92,2 —
#: l'arrondi est explicitement permis par les prompts et c'est ce qu'un humain
#: écrirait — mais refuse « 90 % », qui n'est plus un arrondi mais une
#: approximation, et « 85 % », qui est une invention.
TOLERANCE_ARRONDI = 1.0


def chiffres_autorises(
    lignes: Iterable[dict], cles: Iterable[str], avec_rangs: bool = True
) -> set[float]:
    """Tous les nombres qu'une rédaction a le droit d'employer.

    Args:
        lignes: les mesures fournies au modèle.
        cles: colonnes à retenir. EXPLICITES et non « toutes les valeurs
            numériques » : une ligne de classement porte une quinzaine de
            champs, dont des identifiants et des horodatages. Les autoriser
            reviendrait à ouvrir la vérification à des nombres que le lecteur ne
            verra jamais, donc à ne plus rien vérifier.
        avec_rangs: autorise 1, 2, 3… jusqu'au nombre de lignes. Une liste
            numérotée est une mise en forme, pas une mesure ; « en deuxième
            position » doit rester dicible.
    """
    lignes = list(lignes)
    autorises: set[float] = (
        {float(rang) for rang in range(1, len(lignes) + 1)} if avec_rangs else set()
    )
    for ligne in lignes:
        for cle in cles:
            valeur = ligne.get(cle)
            if valeur is None:
                continue
            try:
                autorises.add(float(valeur))
            except (TypeError, ValueError):
                continue
    return autorises


def chiffres_inventes(texte: str, autorises: set[float]) -> list[str]:
    """Nombres de `texte` qui ne viennent d'aucune mesure fournie.

    Vide = la rédaction est fidèle. Non vide = elle a produit un chiffre, ce que
    les prompts interdisent et que rien d'autre ne détecterait.
    """
    inventes: list[str] = []
    for brut in NOMBRE_RE.findall(texte):
        try:
            valeur = float(brut.replace(",", "."))
        except ValueError:
            continue
        if not any(abs(valeur - a) < TOLERANCE_ARRONDI for a in autorises):
            inventes.append(brut)
    return inventes


def nombre(valeur: Any, decimales: int = 1) -> str:
    """Un nombre à la française (virgule décimale, espace de millier).

    Les mesures arrivent souvent en `Decimal` — PostgreSQL rend ainsi les
    pourcentages —, que le formatage flottant refuse. La conversion est faite
    ici, une fois, plutôt que sur chaque site d'appel.
    """
    try:
        valeur = float(valeur)
    except (TypeError, ValueError):
        return str(valeur)
    return f"{valeur:,.{decimales}f}".replace(",", " ").replace(".", ",")
