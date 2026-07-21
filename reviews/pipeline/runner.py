"""
Orchestrateur du pipeline (injection de dépendances).

Enchaîne, pour chaque source activée :
    collecte → sentiment (NLP) → déduplication/persistance → métriques
puis, en fin de run : évaluation des alertes et notifications.

Les dépendances (repositories, alert manager) sont injectées : le pipeline est
testable sans BD réelle (mocks), et rien ne se connecte au moment de l'import.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from reviews.config import Settings, get_settings
from reviews.domain.models import PipelineRun, ScraperResult
from reviews.domain.sentiment import analyze_sentiment
from reviews.collectors import COLLECTORS
from reviews.alerting.manager import AlertManager
from reviews.storage.db import get_database
from reviews.storage.repository import ReviewRepository, RunRepository, AlertRepository

logger = logging.getLogger("pipeline")


class Pipeline:
    """Orchestrateur principal."""

    def __init__(
        self,
        settings: Settings,
        review_repo: ReviewRepository,
        run_repo: RunRepository,
        alert_manager: AlertManager,
    ):
        self.settings = settings
        self.review_repo = review_repo
        self.run_repo = run_repo
        self.alert_manager = alert_manager

    def run(self, dry_run: bool = False) -> PipelineRun:
        run_id = str(uuid.uuid4())
        run = PipelineRun(run_id=run_id, started_at=datetime.utcnow(), status="running")
        logger.info("Démarrage du pipeline", extra={"extra_data": {"run_id": run_id}})

        if not dry_run:
            self.run_repo.start_run(run_id)

        try:
            enabled = self.settings.get_enabled_scrapers()
            if not enabled:
                raise ValueError("Aucun collecteur activé")
            logger.info("Collecteurs activés : %s", enabled)

            for name in enabled:
                run.scraper_results[name] = self._run_collector(name, run_id, dry_run)

            run.total_reviews = sum(r.inserted_count for r in run.scraper_results.values())
            run.total_duplicates = sum(r.duplicate_count for r in run.scraper_results.values())
            run.total_errors = sum(r.error_count for r in run.scraper_results.values())
            run.ended_at = datetime.utcnow()
            run.status = "success"

            if not dry_run:
                self.run_repo.end_run(run_id, run.status, run.model_dump(mode="json"))

            self.alert_manager.process(run)
            logger.info("Pipeline terminé", extra={"extra_data": {
                "run_id": run_id, "inserted": run.total_reviews,
                "duplicates": run.total_duplicates, "errors": run.total_errors}})
            return run

        except Exception as e:
            logger.error("Erreur pipeline : %s", e, exc_info=True)
            run.ended_at = datetime.utcnow()
            run.status = "failed"
            run.error_message = str(e)
            if not dry_run:
                self.run_repo.end_run(run_id, "failed", {"error": str(e)})
            self.alert_manager.process(run)
            raise

    def _run_collector(self, name: str, run_id: str, dry_run: bool) -> ScraperResult:
        """Collecte, enrichit (sentiment) et persiste une source."""
        collector_cls = COLLECTORS.get(name)
        if collector_cls is None:
            logger.error("Collecteur inconnu : %s", name)
            return ScraperResult(scraper_name=name, started_at=datetime.utcnow(),
                                 ended_at=datetime.utcnow(), status="failed",
                                 error_message="collecteur inconnu")

        result = collector_cls().run()          # collecte SEULE (avec retry)
        if result.status == "failed" or not result.reviews:
            if not dry_run and result.status != "failed":
                self.run_repo.record_metric(run_id, result)
            return result

        # Enrichissement : sentiment NLP à partir du texte
        for review in result.reviews:
            review.sentiment = analyze_sentiment(review.text).sentiment.value

        if dry_run:
            result.inserted_count = len(result.reviews)
            logger.info("Dry-run : %d avis (pas d'insertion) pour %s",
                        len(result.reviews), name)
            return result

        stats = self.review_repo.batch_insert(run_id, result.reviews)
        result.inserted_count = stats["inserted"]
        result.duplicate_count = stats["duplicates"]
        result.error_count += stats["errors"]
        self.run_repo.record_metric(run_id, result)
        return result


def build_pipeline(settings: Optional[Settings] = None) -> Pipeline:
    """Assemble un Pipeline câblé sur la BD réelle (composition root partagée)."""
    settings = settings or get_settings()
    db = get_database()
    return Pipeline(
        settings=settings,
        review_repo=ReviewRepository(db),
        run_repo=RunRepository(db),
        alert_manager=AlertManager(settings.alerting, AlertRepository(db)),
    )
