"""
Synthèses en langage naturel — traduire un écart de KPI en explication lisible.

CE QUE CETTE ROUTE N'EST PAS
    Elle ne calcule aucun indicateur. Les chiffres viennent des mêmes agrégats
    que le reste du dashboard, par les mêmes filtres. Le modèle ne fait que les
    rédiger : c'est ce qui garantit qu'une phrase affichée ne contredit jamais
    le graphique au-dessus d'elle.

POURQUOI POST, ALORS QUE LA RÉPONSE EST UNE LECTURE
    Un appel non caché consomme le quota d'un fournisseur gratuit. Un GET serait
    préchargé, rejoué au retour dans l'onglet et revalidé par le cache du
    navigateur — autant de dépenses qu'aucun utilisateur n'a demandées. POST
    exprime que l'appel est un ACTE, déclenché par un clic et une seule fois.

    Les filtres restent dans la QUERY, et non dans le corps : ils réutilisent
    ainsi le contrat de filtre commun (`FilterDep`), sans le redéclarer une
    seconde fois sous forme de modèle Pydantic — une duplication qui finirait
    par diverger, et donc par produire des synthèses sur un autre périmètre que
    celui affiché.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query

from reviews.api.deps import get_briefing_service, get_insight_service
from reviews.api.filter_params import FilterDep
from reviews.llm.insights import COMPARISON, SPIKE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("/status")
def status(service=Depends(get_insight_service)):
    """État de la couche IA : disponible ou non, et pourquoi, plus la consommation.

    Interrogé par le dashboard AVANT d'afficher un bouton « Expliquer » :
    proposer une action qui échouera systématiquement — faute de clé d'API —
    est pire que ne pas la proposer.
    """
    reason = service.client.unavailable_reason()
    return {
        "available": reason is None,
        "reason": reason,
        "model": service.client.cfg.effective_synthesis_model(),
        "usage_today": service.client.usage_today(),
        "daily_budget": service.client.cfg.daily_call_budget,
        "remaining_today": service.client.remaining_budget(),
    }


@router.post("/explain")
def explain(
    f: FilterDep,
    kind: str = Query(
        SPIKE,
        pattern=f"^({SPIKE}|{COMPARISON})$",
        description=f"`{SPIKE}` explique l'évolution d'une entité entre la période "
        f"courante et la précédente ; `{COMPARISON}` explique l'écart entre "
        "plusieurs entités sur la même période.",
    ),
    level: str = Query(
        "subsidiary",
        pattern="^(subsidiary|operator|country|region)$",
        description="Niveau des entités désignées ci-dessous.",
    ),
    entity: Optional[list[str]] = Query(
        None,
        description="Entités concernées, répétables. Les identifiants sont ceux "
        "du contrat de filtre — un pays se désigne par son code ISO alpha-2 "
        "(`SN`), pas par son country_id. Absent sur un `spike` : la synthèse "
        "porte alors sur tout le périmètre filtré.",
    ),
    refresh: bool = Query(
        False,
        description="Ignore le cache et régénère la synthèse. À n'utiliser que "
        "sur demande explicite : chaque régénération consomme un appel.",
    ),
    _body: Optional[dict] = Body(default=None),
    service=Depends(get_insight_service),
):
    """Explique un pic de négatifs ou un écart entre entités, en deux ou trois phrases.

    RENVOIE TOUJOURS 200, y compris quand la synthèse est impossible. Une clé
    d'API absente ou un quota gratuit épuisé sont des états prévus, pas des
    pannes : la réponse porte alors `available: false` et une raison rédigée en
    français, que le dashboard affiche telle quelle. Un 500 enverrait chercher
    un incident inexistant.

    En cas d'indisponibilité, `payload` contient malgré tout les chiffres
    rassemblés — variation de la part de négatifs, motifs, volumes. L'écran
    reste donc utilisable sans modèle : seule la phrase manque.
    """
    return service.explain(
        kind=kind,
        f=f,
        level=level,
        entities=list(entity or []),
        use_cache=not refresh,
    )


@router.post("/digest")
def digest(
    f: FilterDep,
    refresh: bool = Query(
        False,
        description="Ignore le cache et régénère le résumé. Chaque régénération "
        "consomme un appel du quota.",
    ),
    _body: Optional[dict] = Body(default=None),
    service=Depends(get_briefing_service),
):
    """Résumé de la période : ce qui remonte, et dans quel pays.

    C'est la tuile d'accueil du dashboard — trois phrases qui remplacent la
    lecture de plusieurs milliers d'avis. Les motifs sont croisés PAYS x MOTIF,
    parce que « des problèmes de recharge » n'envoie personne travailler, là où
    « des problèmes de recharge au Ghana » le fait.

    Le résumé est mis en cache par tranche de 6 h, soit la cadence de collecte :
    entre deux passages du planificateur, la matière n'a pas changé d'un avis, et
    recalculer reviendrait à payer pour obtenir le même texte.

    RENVOIE TOUJOURS 200. Une clé absente, un quota épuisé ou une période trop
    peu fournie sont des états prévus : la réponse porte `available: false` et sa
    raison en français, avec les chiffres déjà rassemblés dans `payload`.
    """
    return service.digest(f=f, use_cache=not refresh)


@router.post("/diagnose")
def diagnose(
    f: FilterDep,
    refresh: bool = Query(
        False,
        description="Ignore le cache et régénère le diagnostic. Chaque "
        "régénération consomme un appel du quota.",
    ),
    _body: Optional[dict] = Body(default=None),
    service=Depends(get_briefing_service),
):
    """Cause probable, éléments à vérifier, et recommandations d'action.

    CE QUE CETTE ROUTE NE FAIT PAS : demander au modèle de deviner. Les signaux
    qui séparent deux explications concurrentes — concentration du motif, du
    pays, de la source, de la journée, et antériorité du motif — sont calculés
    en SQL, et les conclusions qu'ils imposent sont calculées en Python avant
    l'appel. Le modèle rédige des faits déjà établis ; il ne conclut pas.

    Conséquence visible : sur un périmètre où les plaintes sont diffuses, la
    réponse dit qu'aucune cause ne domine au lieu d'en désigner une au hasard. Et
    quand plus de 60 % des avis négatifs viennent d'une seule plateforme, elle
    signale que le phénomène peut n'être qu'un biais de collecte.

    La réponse structurée (cause, vérifications, recommandations) est dans
    `payload._reponse`, de sorte que le dashboard puisse en faire deux widgets
    sans redécouper une phrase.
    """
    return service.diagnose(f=f, use_cache=not refresh)
