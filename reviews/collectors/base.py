"""
Classe de base des collecteurs.

Responsabilité UNIQUE : aller chercher des avis et les retourner.
La validation métier est faite par le modèle Review à la construction ;
le calcul du sentiment, la déduplication et la persistance sont du ressort
du pipeline (reviews/pipeline/runner.py). Un collecteur ne touche jamais la BD.
"""

from abc import ABC, abstractmethod
import logging
from datetime import datetime
from typing import Optional

from reviews.domain.models import Review, ScraperResult
from reviews.processing.resilience import RetryConfig, execute_with_retry


class BaseCollector(ABC):
    """Collecteur abstrait. Sous-classer et implémenter collect()."""

    # False pour les collecteurs Playwright (API sync liée au thread OS) :
    # pas de timeout par thread worker (voir execute_with_retry).
    USES_THREAD_TIMEOUT = True

    def __init__(self, name: str, retry_config: Optional[RetryConfig] = None):
        self.name = name
        self.retry_config = retry_config or RetryConfig()
        self.logger = logging.getLogger(f"collector.{name}")

    @abstractmethod
    def collect(self) -> list[Review]:
        """Collecte et retourne les avis bruts (déjà en objets Review)."""
        raise NotImplementedError

    def run(self) -> ScraperResult:
        """Lance la collecte avec retry/timeout. NE persiste rien.

        Retourne un ScraperResult dont `reviews` contient les avis collectés,
        que le pipeline enrichira (sentiment) puis persistera.
        """
        result = ScraperResult(
            scraper_name=self.name,
            reviews=[],
            started_at=datetime.utcnow(),
            status="running",
        )
        try:
            self.logger.info(f"Démarrage de {self.name}")
            reviews = execute_with_retry(
                func=self.collect,
                retry_config=self.retry_config,
                logger=self.logger,
                use_thread_timeout=self.USES_THREAD_TIMEOUT,
            ) or []

            result.reviews = reviews
            result.ended_at = datetime.utcnow()
            result.status = "success"
            self.logger.info(f"{self.name} : {len(reviews)} avis collectés")
            return result
        except Exception as e:
            self.logger.error(f"Erreur dans {self.name} : {e}", exc_info=True)
            result.ended_at = datetime.utcnow()
            result.status = "failed"
            result.error_message = str(e)
            return result
