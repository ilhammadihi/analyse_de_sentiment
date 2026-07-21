"""Endpoints des avis."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from reviews.api.deps import get_review_repo
from reviews.storage.repository import ReviewRepository

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("")
def list_reviews(
    limit: int = Query(100, ge=1, le=500),
    company: Optional[str] = None,
    sentiment: Optional[str] = Query(None, pattern="^(positive|neutral|negative)$"),
    repo: ReviewRepository = Depends(get_review_repo),
):
    """Derniers avis, filtrables par entreprise et par sentiment."""
    return repo.latest(limit=limit, company=company, sentiment=sentiment)
