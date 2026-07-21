"""Endpoints d'agrégats (dashboard)."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from reviews.api.deps import get_stats_repo
from reviews.storage.repository import StatsRepository

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview")
def overview(repo: StatsRepository = Depends(get_stats_repo)):
    """KPI globaux : volume, distribution de sentiment, note moyenne, 24h."""
    return repo.overview()


@router.get("/sentiment-trend")
def sentiment_trend(
    days: int = Query(30, ge=1, le=365),
    company: Optional[str] = None,
    repo: StatsRepository = Depends(get_stats_repo),
):
    """Tendance quotidienne du sentiment (courbe du dashboard)."""
    return repo.sentiment_trend(days=days, company=company)


@router.get("/by-company")
def by_company(repo: StatsRepository = Depends(get_stats_repo)):
    """Répartition par entreprise (volume, négatifs, note moyenne)."""
    return repo.by_company()
