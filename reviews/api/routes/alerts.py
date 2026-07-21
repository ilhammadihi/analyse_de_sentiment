"""Endpoints des alertes."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from reviews.api.deps import get_alert_repo
from reviews.storage.repository import AlertRepository

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
def list_alerts(
    limit: int = Query(50, ge=1, le=200),
    severity: Optional[str] = Query(None, pattern="^(info|warning|error)$"),
    repo: AlertRepository = Depends(get_alert_repo),
):
    """Alertes récentes (fil d'alerting du dashboard)."""
    return repo.list_recent(limit=limit, severity=severity)
