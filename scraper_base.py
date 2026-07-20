"""
Classe abstraite de base pour tous les scrapers.
Implémente la logique commune : retry, logging, validation, insertion BD.
"""

from abc import ABC, abstractmethod
import logging
import time
from datetime import datetime
from typing import Optional
from models import Review, ScraperResult
from database import db
from scheduler import execute_with_retry, RetryConfig
from sentiment_analyzer import analyze_sentiment

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Classe abstraite pour tous les scrapers."""

    # À passer à False dans les sous-classes basées sur Playwright (API sync) :
    # Playwright est lié au thread OS qui le démarre et ne supporte pas le
    # timeout par thread worker (voir scheduler.execute_with_retry).
    USES_THREAD_TIMEOUT = True

    def __init__(self, name: str, retry_config: Optional[RetryConfig] = None):
        """
        Initialise le scraper.
        
        Args:
            name: Nom du scraper (trustpilot, playstore, etc.)
            retry_config: Configuration des retries
        """
        self.name = name
        self.retry_config = retry_config or RetryConfig()
        self.logger = logging.getLogger(f"scraper.{name}")
    
    @abstractmethod
    def collect(self) -> list[Review]:
        """
        Collecte les avis.
        Implémenté par les sous-classes.
        
        Returns:
            Liste des avis collectés
        """
        pass
    
    def validate_reviews(self, reviews: list[Review]) -> tuple[list[Review], int]:
        """
        Valide les avis avec Pydantic.
        
        Args:
            reviews: Avis bruts à valider
            
        Returns:
            (avis valides, nombre d'erreurs)
        """
        valid_reviews = []
        error_count = 0
        
        for review in reviews:
            try:
                if isinstance(review, dict):
                    review = Review(**review)
                # Classification du sentiment à partir du texte (remplace le
                # sentiment déduit de la note, calculé par défaut par Review).
                review.sentiment = analyze_sentiment(review.text).sentiment
                valid_reviews.append(review)
            except Exception as e:
                error_count += 1
                self.logger.warning(f"Avis invalide : {e}", extra={
                    "review_id": review.get("id") if isinstance(review, dict) else review.id,
                    "error": str(e),
                })
        
        return valid_reviews, error_count
    
    def run(self, run_id: str, dry_run: bool = False) -> ScraperResult:
        """
        Lance la collecte avec retry et gestion d'erreurs.

        Args:
            run_id: ID du run actuel
            dry_run: Si True, valide les avis mais n'écrit rien en BD

        Returns:
            Résultat de la collecte
        """
        result = ScraperResult(
            scraper_name=self.name,
            reviews=[],
            started_at=datetime.utcnow(),
            status="running",
        )
        
        try:
            self.logger.info(f"Démarrage de {self.name}")
            
            # Collector avec retry
            reviews = execute_with_retry(
                func=self.collect,
                retry_config=self.retry_config,
                logger=self.logger,
                use_thread_timeout=self.USES_THREAD_TIMEOUT,
            )
            
            if not reviews:
                self.logger.warning(f"Aucun avis collecté par {self.name}")
                result.ended_at = datetime.utcnow()
                result.status = "success"
                return result
            
            self.logger.info(f"{len(reviews)} avis bruts collectés")
            
            # Valider les avis
            valid_reviews, validation_errors = self.validate_reviews(reviews)
            result.error_count = validation_errors
            
            if not valid_reviews:
                self.logger.error(f"Tous les avis sont invalides ({validation_errors} erreurs)")
                result.ended_at = datetime.utcnow()
                result.status = "failed"
                result.error_message = f"Validation failed: {validation_errors} errors"
                return result
            
            self.logger.info(f"{len(valid_reviews)} avis valides après validation")

            if dry_run:
                self.logger.info(f"Dry-run : pas d'insertion BD pour {self.name}")
                result.reviews = valid_reviews
                result.inserted_count = len(valid_reviews)
                result.ended_at = datetime.utcnow()
                result.status = "success"
                return result

            # Insérer en base de données
            insert_result = db.batch_insert_reviews(run_id, valid_reviews)
            result.reviews = valid_reviews
            result.inserted_count = insert_result["inserted"]
            result.duplicate_count = insert_result["duplicates"]
            result.error_count += insert_result["errors"]

            # Enregistrer les métriques
            result.ended_at = datetime.utcnow()
            result.status = "success"
            db.record_scraper_metric(run_id, result)
            
            self.logger.info(f"{self.name} terminé avec succès", extra={
                "inserted": result.inserted_count,
                "duplicates": result.duplicate_count,
                "errors": result.error_count,
                "duration": result.duration_seconds,
            })
            
            return result
            
        except Exception as e:
            self.logger.error(f"Erreur dans {self.name} : {e}", exc_info=True)
            result.ended_at = datetime.utcnow()
            result.status = "failed"
            result.error_message = str(e)
            return result