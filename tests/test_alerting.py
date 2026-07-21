"""Tests des règles d'alerting (pures : aucune BD, aucun envoi réseau)."""

from datetime import datetime

from reviews.config import AlertingConfig
from reviews.domain.models import Review, ScraperResult, PipelineRun, SourceEnum


def _run_with(reviews, **run_kwargs):
    sr = ScraperResult(scraper_name="trustpilot", reviews=reviews,
                       inserted_count=len(reviews), started_at=datetime.utcnow(),
                       ended_at=datetime.utcnow(), status="success")
    run = PipelineRun(run_id="r", started_at=datetime.utcnow(),
                      ended_at=datetime.utcnow(), status="success", **run_kwargs)
    run.scraper_results["trustpilot"] = sr
    return run


def _neg(i):
    return Review(id=str(i), company="Moov Benin", source=SourceEnum.TRUSTPILOT,
                  text="arnaque panne", sentiment="negative")


def test_negative_spike_detected():
    from reviews.alerting import rules
    run = _run_with([_neg(i) for i in range(12)], total_reviews=12)
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "negative_spike" in types


def test_no_spike_below_min_reviews():
    from reviews.alerting import rules
    run = _run_with([_neg(i) for i in range(3)], total_reviews=3)
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "negative_spike" not in types


def test_zero_reviews_alert():
    from reviews.alerting import rules
    run = PipelineRun(run_id="r", started_at=datetime.utcnow(),
                      ended_at=datetime.utcnow(), status="success", total_reviews=0)
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "zero_reviews" in types


def test_disabled_alerting_returns_nothing():
    from reviews.alerting import rules
    run = _run_with([_neg(i) for i in range(12)], total_reviews=12)
    assert rules.evaluate(run, AlertingConfig(enabled=False)) == []
