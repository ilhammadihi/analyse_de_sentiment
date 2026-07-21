"""Fournisseurs de dépendances pour l'API (repositories câblés sur la BD)."""

from reviews.storage.db import get_database
from reviews.storage.repository import (
    ReviewRepository, RunRepository, AlertRepository, StatsRepository,
)


def get_review_repo() -> ReviewRepository:
    return ReviewRepository(get_database())


def get_run_repo() -> RunRepository:
    return RunRepository(get_database())


def get_alert_repo() -> AlertRepository:
    return AlertRepository(get_database())


def get_stats_repo() -> StatsRepository:
    return StatsRepository(get_database())
