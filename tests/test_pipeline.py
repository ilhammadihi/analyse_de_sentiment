"""
Test du pipeline en injection de dépendances : aucune BD, aucun réseau.
Illustre le gain de l'architecture — l'orchestration est testable avec des
faux repositories et un faux collecteur.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from reviews.domain.models import Review, SourceEnum, SentimentEnum
from reviews.collectors.base import BaseCollector
from reviews.pipeline import runner
from reviews.pipeline.runner import Pipeline


class FakeCollector(BaseCollector):
    """Collecteur factice qui retourne des avis en dur (aucun réseau)."""

    def __init__(self):
        super().__init__("playstore")

    def collect(self):
        return [
            Review(id="1", company="Moov", source=SourceEnum.GOOGLE_PLAY,
                   text="Service excellent, très rapide"),
            Review(id="2", company="Moov", source=SourceEnum.GOOGLE_PLAY,
                   text="Arnaque, panne totale, horrible"),
        ]


def _fake_settings():
    return SimpleNamespace(
        get_enabled_scrapers=lambda: ["playstore"],
        alerting=None,
    )


def test_dry_run_enriches_sentiment_without_db(monkeypatch):
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)

    pipeline = Pipeline(
        settings=_fake_settings(),
        review_repo=Mock(),
        run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
    )

    run = pipeline.run(dry_run=True)

    assert run.status == "success"
    assert run.total_reviews == 2                       # inséré (simulé) en dry-run
    result = run.scraper_results["playstore"]
    sentiments = {r.sentiment for r in result.reviews}
    # Le sentiment a été recalculé depuis le texte (NLP)
    assert SentimentEnum.POSITIVE in sentiments
    assert SentimentEnum.NEGATIVE in sentiments


def test_dry_run_does_not_touch_repositories(monkeypatch):
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)
    review_repo, run_repo = Mock(), Mock()

    Pipeline(
        settings=_fake_settings(),
        review_repo=review_repo,
        run_repo=run_repo,
        alert_manager=Mock(process=Mock(return_value=[])),
    ).run(dry_run=True)

    review_repo.batch_insert.assert_not_called()
    run_repo.start_run.assert_not_called()
    run_repo.end_run.assert_not_called()
