"""
Endpoints d'agrégats du dashboard.

Tous les endpoints de cette route — hormis /filters et /pipeline-health, qui
décrivent le périmètre suivi et l'état de la collecte — acceptent LE MÊME jeu de
paramètres de filtre, décrit dans `reviews/api/filter_params.py` :

    ?days=30&country=SN&country=CI&operator=7&source_kind=customer_review

Le dashboard sérialise donc son état de filtre une seule fois et le rejoue
tel quel sur chaque appel. C'est ce qui garantit que tous les écrans parlent du
même périmètre au même instant — la propriété qui manquait à la version
précédente, où chaque onglet interrogeait tout le corpus depuis 2013.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from reviews.api.deps import get_market_repo, get_stats_repo
from reviews.api.filter_params import FilterDep
from reviews.storage.filters import ALLOWED_GRANULARITIES, LEVELS
from reviews.storage.stats_repository import StatsRepository

router = APIRouter(prefix="/stats", tags=["stats"])

#: Décrit les niveaux d'agrégation dans la documentation OpenAPI, sans les
#: dupliquer : la liste de référence est celle du module de filtres.
_LEVELS_DOC = "Niveau d'agrégation : " + ", ".join(sorted(LEVELS))


# ---------------------------------------------------------------------------
# Périmètre suivi
# ---------------------------------------------------------------------------


@router.get("/filters")
def filter_options(repo: StatsRepository = Depends(get_stats_repo)):
    """Valeurs de la barre de filtres (pays, opérateurs, filiales, régions, sources).

    Non filtré, volontairement : la barre doit toujours proposer l'ensemble du
    périmètre suivi, sinon on ne peut plus élargir une sélection sans la
    remettre à zéro. Chaque entrée porte son volume, pour trier par importance
    et signaler les entités encore sans aucun avis.
    """
    return repo.filter_options()


# ---------------------------------------------------------------------------
# Vue d'ensemble
# ---------------------------------------------------------------------------


@router.get("/overview")
def overview(f: FilterDep, repo: StatsRepository = Depends(get_stats_repo)):
    """Indicateurs de tête du périmètre, avec la période antérieure pour comparaison.

    La réponse distingue `avis_clients` et `articles_presse`, et ne calcule la
    satisfaction que sur les premiers. C'est ce qui corrige l'indicateur central
    du dashboard : la part de négatifs était divisée par le total presse
    comprise, et affichait donc 8,3 % là où la valeur réelle est 16,7 %.

    `previous` est nul quand aucune période n'est bornée — sur tout
    l'historique, il n'y a pas de période antérieure à laquelle se comparer.
    """
    return repo.overview(f)


@router.get("/movers")
def movers(
    f: FilterDep,
    level: Optional[str] = Query("subsidiary", description=_LEVELS_DOC),
    limit: int = Query(5, ge=1, le=25),
    min_reviews: int = Query(
        20,
        ge=0,
        description="Avis clients exigés sur les DEUX périodes. Sans seuil, le "
        "classement est monopolisé par des entités passant de 1 à 2 avis.",
    ),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Entités qui se sont le plus dégradées / améliorées sur la période.

    Répond à « qu'est-ce qui a changé ? », question hors de portée d'un cumul
    historique. La variation est en POINTS de part de négatifs : « +5 pts » ne
    dépend pas du point de départ, là où « +50 % » serait exact mais trompeur.
    """
    try:
        return repo.movers(f, level=level, limit=limit, min_reviews=min_reviews)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Courbes
# ---------------------------------------------------------------------------


@router.get("/point-context")
def point_context(
    f: FilterDep,
    level: Optional[str] = Query("subsidiary", description=_LEVELS_DOC),
    entity: Optional[list[str]] = Query(
        None,
        description="Identifiants d'entités, répétables. Les mêmes que ceux des "
        "courbes de `/stats/trend`.",
    ),
    granularity: Optional[str] = Query(
        None,
        description="Doit valoir celle employée par la courbe affichée, sinon la "
        "phrase décrit une autre période que le point survolé.",
    ),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """De quoi expliquer un point de courbe au survol, sans appeler de modèle.

    Rend, pour chaque point : le nombre d'avis, le motif négatif dominant avec
    son niveau à la période précédente, et les articles de presse de la même
    fenêtre.

    Le VOLUME vient en premier délibérément. Mesuré sur 90 jours, deux points
    sur trois reposent sur moins de cinq avis : l'inflexion y est un artefact
    d'échantillonnage, et proposer une cause donnerait un sens à du bruit.
    """
    if granularity is not None and granularity not in ALLOWED_GRANULARITIES:
        raise HTTPException(
            status_code=422,
            detail="Granularité invalide. Valeurs acceptées : "
            + ", ".join(sorted(ALLOWED_GRANULARITIES)),
        )
    try:
        return repo.point_context(
            f,
            level=level,
            entities=tuple(entity or ()),
            granularity=granularity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/trend")
def trend(
    f: FilterDep,
    level: Optional[str] = Query(
        None,
        description="Absent = une seule courbe pour le périmètre. Sinon une courbe "
        f"par entité. {_LEVELS_DOC}",
    ),
    granularity: Optional[str] = Query(
        None,
        description="day, week ou month. Choisi automatiquement selon la durée si "
        "absent : un pas journalier sur douze mois produit 365 points illisibles.",
    ),
    limit: int = Query(
        6, ge=1, le=12, description="Nombre maximal de courbes, les plus volumineuses."
    ),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Tendance du sentiment, en une courbe ou une courbe par entité.

    Le mode groupé (`level`) est ce qui rend possible la comparaison directe de
    plusieurs filiales, pays ou opérateurs sur un même graphique.
    """
    if granularity is not None and granularity not in ALLOWED_GRANULARITIES:
        raise HTTPException(
            status_code=422,
            detail="Granularité invalide. Valeurs acceptées : "
            + ", ".join(sorted(ALLOWED_GRANULARITIES)),
        )
    try:
        return repo.trend(f, level=level, granularity=granularity, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Classements et matrice
# ---------------------------------------------------------------------------


@router.get("/ranking")
def ranking(
    f: FilterDep,
    level: str = Query("subsidiary", description=_LEVELS_DOC),
    sort: str = Query(
        "negatifs",
        description="volume, presse, negatifs, note_asc, note_desc, score_asc, score_desc.",
    ),
    min_reviews: int = Query(
        0,
        ge=0,
        description="Exclut les entités sous ce nombre d'avis clients : un taux "
        "calculé sur 3 avis n'est pas comparable à un taux calculé sur 400.",
    ),
    limit: int = Query(200, ge=1, le=500),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Classement filtré, à n'importe quel niveau d'agrégation.

    Remplace by-country / by-operator / by-subsidiary, qui ne connaissaient ni
    période ni filtre.
    """
    try:
        return repo.ranking(
            f, level=level, sort=sort, min_reviews=min_reviews, limit=limit
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/matrix")
def matrix(f: FilterDep, repo: StatsRepository = Depends(get_stats_repo)):
    """Croisement opérateur × pays, pour lecture en carte de chaleur.

    Renvoie les cellules ET les deux axes, ordonnés par volume, afin que le
    dashboard dessine une grille complète — cellules vides comprises, car une
    case vide est une information : l'opérateur n'est pas suivi dans ce pays.
    """
    return repo.matrix(f)


# ---------------------------------------------------------------------------
# Motifs d'insatisfaction
# ---------------------------------------------------------------------------


@router.get("/themes")
def themes(
    f: FilterDep,
    polarity: str = Query("negative", pattern="^(negative|positive)$"),
    dimension: str = Query(
        "aspects",
        pattern="^(terms|aspects)$",
        description="aspects = motifs métier nommés par l'analyse sémantique "
        "(migration 005) ; terms = mots du lexique déclenchés (migration 004).",
    ),
    limit: int = Query(25, ge=1, le=100),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Motifs les plus fréquents sur le périmètre — le « pourquoi » du sentiment.

    DEUX DIMENSIONS, ET ELLES NE DISENT PAS LA MÊME CHOSE.

    `terms` agrège les mots du lexique qui ont fait pencher le score. C'est
    gratuit et complet, mais borné par nature : un sac de mots ne remonte que
    des mots. Mesuré sur 90 jours, il classe en tête des motifs
    d'insatisfaction « can't », « bad », « doesn't », « its », « without » —
    exact statistiquement, inexploitable pour décider quoi que ce soit.

    `aspects` agrège les aspects métier reconnus par l'analyse sémantique dans
    une taxonomie fermée (facturation, coupures réseau, service client…). C'est
    la dimension par DÉFAUT, parce que c'est celle qui répond réellement à la
    question posée. Elle dépend d'une couche optionnelle : `base.non_analyses`
    indique combien d'avis du périmètre n'y sont pas encore passés.

    `nb_filiales` sépare un motif systémique (présent partout, donc structurel)
    d'un motif local (concentré, donc actionnable sur place).

    Restreint aux avis clients sauf `source_kind` explicite — le vocabulaire de
    la presse est journalistique, ce n'est pas une plainte de client.
    """
    try:
        return repo.themes(f, polarity=polarity, limit=limit, dimension=dimension)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/semantic-coverage")
def semantic_coverage(f: FilterDep, repo: StatsRepository = Depends(get_stats_repo)):
    """Part du périmètre déjà traitée par l'analyse sémantique.

    À afficher partout où un taux de sentiment est présenté tant que la
    couverture n'est pas complète : en deçà de 100 %, l'indicateur agrège le
    jugement du modèle là où il est passé et celui du lexique ailleurs. Le
    signaler est la condition pour que le chiffre reste défendable.
    """
    return repo.semantic_coverage(f)


@router.get("/verbatims")
def verbatims(
    f: FilterDep,
    term: Optional[str] = Query(None, description="Restreint aux avis contenant ce terme."),
    polarity: str = Query("negative", pattern="^(negative|positive)$"),
    limit: int = Query(20, ge=1, le=100),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Avis d'exemple du périmètre — ce que les chiffres ne montrent pas.

    Un motif sans verbatim n'est pas défendable devant un métier : « 120 avis
    mentionnent coupure » ne devient exploitable qu'accompagné de trois avis
    qu'on peut lire.
    """
    return repo.verbatims(f, term=term, polarity=polarity, limit=limit)


# ---------------------------------------------------------------------------
# Santé de la collecte
# ---------------------------------------------------------------------------


@router.get("/pipeline-health")
def pipeline_health(
    runs_limit: int = Query(10, ge=1, le=50),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Fraîcheur par source, derniers runs, bilan par collecteur.

    Sans filtre de période : la question est « la chaîne tourne-t-elle
    maintenant ? ». `heures_depuis_collecte` est l'indicateur qui révèle une
    source morte — un total élevé n'empêche pas d'avoir cessé de collecter hier.
    """
    return repo.pipeline_health(runs_limit=runs_limit)


# ---------------------------------------------------------------------------
# Endpoints historiques
# ---------------------------------------------------------------------------
# Conservés pour ne pas casser une version du dashboard encore déployée. Ils
# ignorent tout filtre et renvoient des cumuls depuis 2013 : ne rien construire
# de nouveau dessus. Équivalents filtrés : /stats/trend et /stats/ranking.


@router.get("/sentiment-trend", deprecated=True)
def sentiment_trend(
    days: int = Query(30, ge=1, le=36500),
    company: Optional[str] = None,
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Tendance quotidienne, sans filtre dimensionnel. Préférer /stats/trend."""
    return repo.sentiment_trend(days=days, company=company)


@router.get("/by-company", deprecated=True)
def by_company(repo: StatsRepository = Depends(get_stats_repo)):
    """Répartition par nom libre d'entreprise. Préférer /stats/ranking."""
    return repo.by_company()


@router.get("/by-country", deprecated=True)
def by_country(repo: StatsRepository = Depends(get_stats_repo)):
    """Cumul par pays, sans période. Préférer /stats/ranking?level=country."""
    return repo.by_country()


@router.get("/by-operator", deprecated=True)
def by_operator(repo: StatsRepository = Depends(get_stats_repo)):
    """Cumul par opérateur, sans période. Préférer /stats/ranking?level=operator."""
    return repo.by_operator()


@router.get("/by-subsidiary", deprecated=True)
def by_subsidiary(repo: StatsRepository = Depends(get_stats_repo)):
    """Cumul par filiale, sans période. Préférer /stats/ranking?level=subsidiary."""
    return repo.by_subsidiary()


@router.get("/market")
def market(
    country: str = Query(
        ..., min_length=2, max_length=2,
        description="Code ISO alpha-2 du pays, comme partout dans le contrat de filtre.",
    ),
    latest_only: bool = Query(
        True,
        description="Vrai : dernière valeur connue par indicateur, avec sa "
        "variation par rapport au point précédent. Faux : la série complète.",
    ),
    repo=Depends(get_market_repo),
):
    """Indicateurs de marché d'un pays : abonnés, trafic data, couverture réseau.

    CE QUE CET ENDPOINT N'EST PAS, ET IL FAUT LE LIRE AVANT DE S'EN SERVIR
        Ces chiffres sont PAR PAYS et ANNUELS. La source (Banque Mondiale /
        UIT) ne descend pas au niveau de l'opérateur : « le Maroc compte 58,3 M
        d'abonnements mobiles en 2024 » est soutenable, « Orange Maroc a gagné
        5 % d'abonnés » ne l'est pas et ne le sera pas par cette route.

        Ils servent de CONTEXTE à la satisfaction, pas de mesure de performance
        d'un opérateur : un recul de satisfaction dans un pays couvert à 99,8 %
        en 4G ne raconte pas la même histoire que le même recul à 60 %.

    La variation entre deux années est calculée en base, jamais par l'appelant
    ni par un modèle — même règle que pour tous les écarts du dashboard.
    """
    iso2 = country.upper()
    if latest_only:
        derniers = repo.latest(iso2)
        return {
            "country": iso2,
            "granularity": "country",
            "indicators": [
                {
                    "indicator": v["indicator"],
                    "label": _market_label(v["indicator"]),
                    "unit": v["unit"],
                    "unit_label": _market_unit_label(v["unit"]),
                    "year": v["year"],
                    "value": v["value"],
                    "previous_year": v.get("annee_precedente"),
                    "previous_value": v.get("valeur_precedente"),
                    "variation_pct": v.get("variation_pct"),
                }
                for v in derniers.values()
            ],
        }
    # Les libellés accompagnent la série, et pas seulement la vue « dernière
    # valeur » : un écran qui affiche seize courbes doit pouvoir les nommer
    # sans refaire un second appel juste pour un dictionnaire de traduction.
    from reviews.collectors.market_data import INDICATEURS

    series = repo.for_country(iso2)
    return {
        "country": iso2,
        "granularity": "country",
        "series": series,
        "labels": {r["indicator"]: _market_label(r["indicator"]) for r in series}
        or {c: _market_label(c) for c in INDICATEURS},
    }


@router.get("/market/countries")
def market_countries(repo=Depends(get_market_repo)):
    """Contexte marché de TOUS les pays, dernière année connue par indicateur.

    Sert le tableau comparatif et le croisement satisfaction × réseau. Les
    indicateurs trop peu renseignés pour être montrés sont écartés ici —
    `IT_INV_COMP` n'existe que pour quatre pays sur cinquante-quatre, et une
    colonne vide neuf fois sur dix fait douter de tout le tableau.

    Chaque mesure porte SON année : la couverture 4G est connue pour 2024 sur
    les 54 pays, le trafic data pour 34 seulement. Comparer sans le voir serait
    comparer deux choses différentes.
    """
    from reviews.collectors.market_data import INDICATEURS_AFFICHABLES

    rows = repo.latest_by_country(list(INDICATEURS_AFFICHABLES))
    return {
        "rows": rows,
        "labels": {c: _market_label(c) for c in INDICATEURS_AFFICHABLES},
        "granularity": "country",
    }


@router.get("/market/coverage")
def market_coverage(repo=Depends(get_market_repo)):
    """Quels pays ont des indicateurs, et jusqu'à quelle année.

    Destiné au futur agent de qualité de données : un pays sans indicateur est
    un trou de couverture, au même titre qu'une filiale sans avis.
    """
    return {"rows": repo.coverage()}


def _market_label(code: str) -> str:
    """Libellé lisible, importé du collecteur pour n'exister qu'à un endroit."""
    from reviews.collectors.market_data import libelle

    return libelle(code)


def _market_unit_label(code: str) -> str:
    """Unité lisible. Sans elle, « 153,1 SB_10P2_HB » se lit comme un nombre
    d'abonnés alors que c'est un taux pour 100 habitants."""
    from reviews.collectors.market_data import unite_libelle

    return unite_libelle(code)
