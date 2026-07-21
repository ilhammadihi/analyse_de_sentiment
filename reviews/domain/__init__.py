"""Couche domaine : modèles purs et moteur de sentiment (aucune I/O)."""

from reviews.domain.models import (
    Review,
    ScraperResult,
    PipelineRun,
    Alert,
    AlertSeverity,
    SentimentEnum,
    SourceEnum,
)
from reviews.domain.sentiment import analyze_sentiment, SentimentScore

__all__ = [
    "Review",
    "ScraperResult",
    "PipelineRun",
    "Alert",
    "AlertSeverity",
    "SentimentEnum",
    "SourceEnum",
    "analyze_sentiment",
    "SentimentScore",
]
