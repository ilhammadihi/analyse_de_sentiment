"""Endpoints des runs du pipeline."""

from fastapi import APIRouter, Depends, HTTPException, Query

from reviews.api.deps import get_run_repo
from reviews.storage.repository import RunRepository

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
def list_runs(
    limit: int = Query(20, ge=1, le=100),
    repo: RunRepository = Depends(get_run_repo),
):
    """Derniers runs du pipeline."""
    return repo.list_recent(limit=limit)


@router.get("/{run_id}")
def get_run(run_id: str, repo: RunRepository = Depends(get_run_repo)):
    """Détail d'un run."""
    run = repo.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run introuvable")
    return run
