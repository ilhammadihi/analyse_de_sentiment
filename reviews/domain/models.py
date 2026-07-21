"""
Modèles de données du domaine (Pydantic v2).
Purs : aucune dépendance I/O (ni BD, ni réseau). Testables isolément.
"""

from typing import Optional
from datetime import datetime
from enum import Enum
import hashlib

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator, computed_field


class SentimentEnum(str, Enum):
    """Sentiments possibles."""
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class SourceEnum(str, Enum):
    """Sources possibles."""
    TRUSTPILOT = "trustpilot"
    GOOGLE_PLAY = "google_play"
    APP_STORE = "app_store"
    GOOGLE_MAPS = "google_maps"
    RSS_FEED = "rss_feed"


class Review(BaseModel):
    """Un avis collecté, validé avant insertion en base."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(..., min_length=1, description="ID unique de l'avis")
    company: str = Field(..., min_length=1, description="Nom de l'entreprise")
    source: SourceEnum = Field(..., description="Source de collecte")
    title: Optional[str] = Field(None, max_length=500, description="Titre de l'avis")
    text: str = Field(..., min_length=1, max_length=5000, description="Texte de l'avis")
    rating: Optional[int] = Field(None, ge=1, le=5, description="Note de 1 à 5")
    sentiment: Optional[SentimentEnum] = Field(None, description="Sentiment détecté")
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow, description="Date de création")
    author: Optional[str] = Field(None, max_length=255, description="Auteur de l'avis")
    likes: Optional[int] = Field(None, ge=0, description="Nombre de likes")
    verified: Optional[bool] = Field(None, description="Achat vérifié")

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, v):
        """Normalise le texte (whitespace)."""
        if not v:
            return v
        return " ".join(str(v).split()).strip()

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, v):
        """Normalise le titre."""
        if not v:
            return None
        return str(v).strip()

    @model_validator(mode="after")
    def compute_sentiment_from_rating(self):
        """Sentiment de repli déduit de la note si non fourni.

        Le pipeline recalcule ensuite le sentiment à partir du texte (NLP) ;
        cette règle ne sert que de valeur par défaut cohérente.
        """
        if self.sentiment is None and self.rating:
            if self.rating >= 4:
                self.sentiment = SentimentEnum.POSITIVE.value
            elif self.rating == 3:
                self.sentiment = SentimentEnum.NEUTRAL.value
            else:
                self.sentiment = SentimentEnum.NEGATIVE.value
        return self

    def get_checksum(self) -> str:
        """Hash SHA256 du contenu (déduplication)."""
        content = f"{self.company}:{self.source}:{self.text}"
        return hashlib.sha256(content.encode()).hexdigest()


class ScraperResult(BaseModel):
    """Résultat de collecte + traitement d'une source."""

    scraper_name: str
    reviews: list[Review] = Field(default_factory=list)
    inserted_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    started_at: datetime
    ended_at: Optional[datetime] = None
    error_message: Optional[str] = None
    status: str = "pending"  # pending, running, success, failed

    @computed_field
    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class PipelineRun(BaseModel):
    """Une exécution complète du pipeline."""

    run_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    status: str = "running"  # running, success, failed
    total_reviews: int = 0
    total_duplicates: int = 0
    total_errors: int = 0
    error_message: Optional[str] = None
    scraper_results: dict[str, ScraperResult] = Field(default_factory=dict)

    @computed_field
    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Alert(BaseModel):
    """Une alerte métier détectée pendant un run."""

    type: str                       # ex: negative_spike, zero_reviews, run_failed
    severity: AlertSeverity
    title: str
    message: str
    run_id: Optional[str] = None
    company: Optional[str] = None
    source: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
