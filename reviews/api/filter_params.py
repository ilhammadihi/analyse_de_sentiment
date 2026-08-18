"""
Traduction des paramètres d'URL en :class:`StatsFilter`.

Une seule dépendance FastAPI, partagée par tous les endpoints d'agrégats. C'est
ce qui donne au dashboard un contrat de filtre uniforme : la même URL de filtre
s'applique à la vue d'ensemble, aux courbes, aux classements, à la matrice et
aux motifs, sans qu'aucun endpoint ne redéclare ses propres paramètres.

Conséquence voulue côté interface : la barre de filtres sérialise son état une
fois, et chaque requête réutilise la même chaîne de query.
"""

from datetime import date
from typing import Annotated, Optional

from fastapi import Depends, Query

from reviews.storage.filters import (
    APP,
    BOTH_SIDES,
    CUSTOMER,
    OPERATOR,
    PRESS,
    StatsFilter,
)


def get_stats_filter(
    days: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=36500,
            description="Fenêtre glissante en jours. Absent = tout l'historique. "
            "Le plafond est volontairement large : la presse collectée remonte "
            "à 2002 et le dashboard propose « tout l'historique ».",
        ),
    ] = None,
    date_from: Annotated[
        Optional[date],
        Query(alias="from", description="Début de période (inclus). Prioritaire sur `days`."),
    ] = None,
    date_to: Annotated[
        Optional[date],
        Query(alias="to", description="Fin de période (incluse). Défaut : aujourd'hui."),
    ] = None,
    country: Annotated[
        Optional[list[str]],
        Query(description="Codes pays ISO alpha-2, répétables (ex. country=SN&country=CI)."),
    ] = None,
    region: Annotated[
        Optional[list[str]],
        Query(description="Régions, répétables (ex. « Afrique de l'Ouest »)."),
    ] = None,
    operator: Annotated[
        Optional[list[int]],
        Query(description="Identifiants d'opérateur, répétables."),
    ] = None,
    subsidiary: Annotated[
        Optional[list[int]],
        Query(description="Identifiants de filiale, répétables."),
    ] = None,
    source_kind: Annotated[
        Optional[str],
        Query(
            pattern=f"^({CUSTOMER}|{PRESS})$",
            description="Restreint aux avis clients ou à la presse. Absent = les deux, "
            "la satisfaction restant de toute façon calculée sur les seuls avis clients.",
        ),
    ] = None,
    source: Annotated[
        Optional[list[str]],
        Query(description="Codes de source, répétables (google_play, rss_feed…)."),
    ] = None,
    about: Annotated[
        str,
        Query(
            pattern=f"^({OPERATOR}|{APP}|{BOTH_SIDES})$",
            description="Objet des avis retenus. « operator » (défaut) ne garde "
            "que ce qui juge le SERVICE de la filiale : réseau, facturation, "
            "recharge, service client, boutique. « app » ne garde que ce qui "
            "juge l'APPLICATION : bugs, connexion, ergonomie. « all » mélange "
            "les deux, comme avant la séparation. Les avis qui nomment les deux "
            "griefs sont comptés des deux côtés.",
        ),
    ] = OPERATOR,
    min_subsidiary_reviews: Annotated[
        int,
        Query(
            ge=0,
            le=1000,
            description="Écarte les filiales comptant moins de N avis clients au "
            "total, toutes périodes confondues. Un taux calculé sur trois avis "
            "n'est pas comparable à un taux calculé sur quatre cents. 0 = aucun "
            "seuil.",
        ),
    ] = 0,
) -> StatsFilter:
    """Assemble le périmètre demandé.

    Les listes sont converties en tuples : `StatsFilter` est gelé (frozen) pour
    qu'un périmètre ne puisse pas être modifié après construction — une méthode
    de repository qui ajouterait discrètement un pays au filtre reçu produirait
    des chiffres impossibles à rapprocher de ce que l'URL annonce.
    """
    return StatsFilter(
        days=days,
        date_from=date_from,
        date_to=date_to,
        countries=tuple(country or ()),
        regions=tuple(region or ()),
        operators=tuple(operator or ()),
        subsidiaries=tuple(subsidiary or ()),
        source_kind=source_kind,
        sources=tuple(source or ()),
        about=about,
        min_subsidiary_reviews=min_subsidiary_reviews,
    )


#: Alias de type, pour que la signature des endpoints reste lisible.
FilterDep = Annotated[StatsFilter, Depends(get_stats_filter)]
