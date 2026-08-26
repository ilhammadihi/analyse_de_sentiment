"""Endpoints des avis."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from reviews.api.deps import get_review_repo, get_stats_repo
from reviews.api.filter_params import FilterDep
from reviews.storage.repository import ReviewRepository
from reviews.storage.stats_repository import StatsRepository

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("")
def list_reviews(
    limit: int = Query(100, ge=1, le=500),
    company: Optional[str] = None,
    sentiment: Optional[str] = Query(None, pattern="^(positive|neutral|negative)$"),
    repo: ReviewRepository = Depends(get_review_repo),
):
    """Derniers avis, filtrables par entreprise et par sentiment.

    HÉRITÉ, ET PLUS CONSOMMÉ. Il prédate le contrat de filtre commun : ni la
    période, ni le pays, ni l'opérateur, ni la source ne l'atteignent, et il
    ignore la presse. Le dashboard utilise `/reviews/feed`, qui accepte le même
    périmètre que tous les autres écrans.

    Conservé pour l'instant faute d'inconvénient, mais supprimable : plus aucun
    client connu ne l'appelle.
    """
    return repo.latest(limit=limit, company=company, sentiment=sentiment)


@router.get("/feed")
def review_feed(
    f: FilterDep,
    limit: int = Query(25, ge=1, le=100, description="Par flux, non au total."),
    sentiment: Optional[str] = Query(
        None,
        pattern="^(positive|neutral|negative)$",
        description="Restreint les deux flux à un sentiment.",
    ),
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Fil d'actualité du périmètre : avis clients et presse, du plus récent.

    Accepte le contrat de filtre commun à tous les écrans du dashboard, ce que
    `/reviews` ne fait pas. Les deux flux sont rendus dans des listes séparées
    (`avis`, `presse`) : la presse est deux fois plus volumineuse, entrelacée
    elle recouvrirait la voix du client.
    """
    return repo.feed(f, limit=limit, sentiment=sentiment)
