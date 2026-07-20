"""
Modèles de données avec validation Pydantic.
Valide les avis collectés avant insertion en BD.
"""

from typing import Optional, Any
from datetime import datetime
from pydantic import BaseModel, Field, validator, root_validator, computed_field
from enum import Enum
import hashlib
import re


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
    """
    Modèle d'un avis collecté.
    Validé avant insertion en base de données.
    """
    
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
    
    class Config:
        use_enum_values = True
    
    @validator("text", pre=True)
    def normalize_text(cls, v):
        """Normalise le texte (whitespace, encodage)."""
        if not v:
            return v
        s = str(v).strip()
        s = " ".join(s.split())  # Collapse whitespace
        return s
    
    @validator("title", pre=True)
    def normalize_title(cls, v):
        """Normalise le titre."""
        if not v:
            return None
        return str(v).strip()
    
    @root_validator(skip_on_failure=True)
    def compute_sentiment(cls, values):
        """Calcule le sentiment à partir du rating si absent."""
        if values.get("sentiment") is None and values.get("rating"):
            rating = values["rating"]
            if rating >= 4:
                values["sentiment"] = SentimentEnum.POSITIVE
            elif rating == 3:
                values["sentiment"] = SentimentEnum.NEUTRAL
            else:
                values["sentiment"] = SentimentEnum.NEGATIVE
        return values
    
    def get_checksum(self) -> str:
        """Retourne le hash SHA256 du texte (déduplication)."""
        content = f"{self.company}:{self.source}:{self.text}"
        return hashlib.sha256(content.encode()).hexdigest()
    
    def is_duplicate(self, existing_checksums: set[str]) -> bool:
        """Vérifie si cet avis est un doublon."""
        return self.get_checksum() in existing_checksums


class ScraperResult(BaseModel):
    """Résultat d'un scraper."""
    
    scraper_name: str
    reviews: list[Review]
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
        """Durée d'exécution, calculée à la volée (started_at/ended_at peuvent être modifiés après construction)."""
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class PipelineRun(BaseModel):
    """Représente une exécution du pipeline."""
    
    run_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    status: str = "running"  # running, success, failed
    total_reviews: int = 0
    total_duplicates: int = 0
    total_errors: int = 0
    error_message: Optional[str] = None
    scraper_results: dict[str, ScraperResult] = {}

    @computed_field
    @property
    def duration_seconds(self) -> Optional[float]:
        """Durée totale, calculée à la volée (started_at/ended_at peuvent être modifiés après construction)."""
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class TextStats(BaseModel):
    """Statistiques d'un texte."""
    
    text: str
    word_count: int = 0
    char_count: int = 0
    sentence_count: int = 0
    caps_ratio: float = 0.0  # Proportion de majuscules
    punctuation_count: int = 0
    exclamation_count: int = 0
    question_count: int = 0
    
    @root_validator(pre=True)
    def compute_stats(cls, values):
        """Calcule les statistiques du texte."""
        text = values.get("text", "")
        
        values["char_count"] = len(text)
        values["word_count"] = len(text.split())
        values["sentence_count"] = len(re.split(r'[.!?]+', text)) - 1
        
        if text:
            uppercase = sum(1 for c in text if c.isupper())
            values["caps_ratio"] = uppercase / len(text)
        
        values["exclamation_count"] = text.count("!")
        values["question_count"] = text.count("?")
        values["punctuation_count"] = len(re.findall(r'[!?.,:;-]', text))
        
        return values


def compute_text_stats(text: str) -> TextStats:
    """Calcule les statistiques d'un texte."""
    return TextStats(text=text)