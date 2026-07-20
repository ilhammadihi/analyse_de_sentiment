"""Tests pour les modèles."""

import pytest
from datetime import datetime
from models import Review, SourceEnum, SentimentEnum


def test_review_creation():
    """Test création d'une Review."""
    review = Review(
        id="test-1",
        company="Moov Africa Benin",
        source=SourceEnum.GOOGLE_PLAY,
        text="Bon service",
        rating=4,
    )
    
    assert review.id == "test-1"
    assert review.company == "Moov Africa Benin"
    assert review.rating == 4
    assert review.sentiment == SentimentEnum.POSITIVE


def test_review_sentiment_calculation():
    """Test calcul du sentiment."""
    # Rating 5 = positive
    r1 = Review(
        id="1", company="Test", source=SourceEnum.TRUSTPILOT,
        text="Excellent", rating=5
    )
    assert r1.sentiment == SentimentEnum.POSITIVE
    
    # Rating 3 = neutral
    r2 = Review(
        id="2", company="Test", source=SourceEnum.TRUSTPILOT,
        text="OK", rating=3
    )
    assert r2.sentiment == SentimentEnum.NEUTRAL
    
    # Rating 1 = negative
    r3 = Review(
        id="3", company="Test", source=SourceEnum.TRUSTPILOT,
        text="Nul", rating=1
    )
    assert r3.sentiment == SentimentEnum.NEGATIVE


def test_review_checksum():
    """Test génération du checksum."""
    r1 = Review(
        id="1", company="Test", source=SourceEnum.TRUSTPILOT,
        text="Même contenu"
    )
    r2 = Review(
        id="2", company="Test", source=SourceEnum.TRUSTPILOT,
        text="Même contenu"
    )
    
    assert r1.get_checksum() == r2.get_checksum()


def test_review_text_normalization():
    """Test normalisation du texte."""
    review = Review(
        id="1", company="Test", source=SourceEnum.TRUSTPILOT,
        text="  Texte   avec   espaces  "
    )
    
    assert review.text == "Texte avec espaces"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])