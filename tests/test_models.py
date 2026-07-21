"""Tests des modèles du domaine (purs, sans BD)."""

from reviews.domain.models import Review, SourceEnum, SentimentEnum


def test_review_creation():
    review = Review(id="test-1", company="Moov Africa Benin",
                    source=SourceEnum.GOOGLE_PLAY, text="Bon service", rating=4)
    assert review.id == "test-1"
    assert review.rating == 4
    assert review.sentiment == SentimentEnum.POSITIVE


def test_sentiment_fallback_from_rating():
    assert Review(id="1", company="T", source=SourceEnum.TRUSTPILOT,
                  text="Excellent", rating=5).sentiment == SentimentEnum.POSITIVE
    assert Review(id="2", company="T", source=SourceEnum.TRUSTPILOT,
                  text="OK", rating=3).sentiment == SentimentEnum.NEUTRAL
    assert Review(id="3", company="T", source=SourceEnum.TRUSTPILOT,
                  text="Nul", rating=1).sentiment == SentimentEnum.NEGATIVE


def test_checksum_is_content_based():
    r1 = Review(id="1", company="T", source=SourceEnum.TRUSTPILOT, text="Même contenu")
    r2 = Review(id="2", company="T", source=SourceEnum.TRUSTPILOT, text="Même contenu")
    assert r1.get_checksum() == r2.get_checksum()
    assert len(r1.get_checksum()) == 64


def test_text_normalization():
    r = Review(id="1", company="T", source=SourceEnum.TRUSTPILOT,
               text="  Texte   avec   espaces  ")
    assert r.text == "Texte avec espaces"
