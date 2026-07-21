"""Tests du moteur de sentiment (lexique FR/EN)."""

import pytest

from reviews.domain.sentiment import analyze_sentiment
from reviews.domain.models import SentimentEnum


@pytest.mark.parametrize("text,expected", [
    ("Service excellent, très rapide et efficace !", SentimentEnum.POSITIVE),
    ("Je recommande vivement, parfait", SentimentEnum.POSITIVE),
    ("Arnaque totale, réseau en panne, injoignable", SentimentEnum.NEGATIVE),
    ("Horrible, pire opérateur, service client nul", SentimentEnum.NEGATIVE),
])
def test_polarity(text, expected):
    assert analyze_sentiment(text).sentiment == expected


def test_negation_inverts():
    # "pas bien" ne doit pas être classé positif
    assert analyze_sentiment("ce n'est pas bien du tout").sentiment != SentimentEnum.POSITIVE


def test_empty_text_is_neutral():
    assert analyze_sentiment("").sentiment == SentimentEnum.NEUTRAL
    assert analyze_sentiment(None).sentiment == SentimentEnum.NEUTRAL


def test_score_bounds():
    s = analyze_sentiment("excellent parfait génial super")
    assert -1.0 <= s.score <= 1.0
