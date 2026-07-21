"""
Scraper Google Play Store.
Collecte les avis des apps Moov Africa.
"""

import logging
from datetime import datetime, timedelta
import time
from google_play_scraper import Sort, reviews
from reviews.collectors.base import BaseCollector
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig, execute_with_retry

logger = logging.getLogger(__name__)


class PlayStoreScraper(BaseCollector):
    """Scraper pour Google Play Store."""
    
    # Configuration des apps à scraper
    APPS = [
        {"package_id": "com.tlc.flous.subs.bn", "name": "Moov Africa Benin", "country": "bj"},
        {"package_id": "bf.moovmoney.hwmm", "name": "Moov Africa Burkina", "country": "bf"},
        {"package_id": "ml.moovmoney.mmpayorg", "name": "Moov Africa Mali", "country": "ml"},
    ]
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.scraping.request_timeout,
        )
        super().__init__("playstore", retry_config)
    
    def collect(self) -> list[Review]:
        """Collecte tous les avis des apps."""
        all_reviews = []
        
        for app in self.APPS:
            self.logger.info(f"Scraping {app['name']}")
            try:
                app_reviews = self._fetch_app_reviews(app)
                all_reviews.extend(app_reviews)
            except Exception as e:
                self.logger.error(f"Erreur scraping {app['name']} : {e}")
                continue
            
            time.sleep(1)  # Pause entre les apps
        
        return all_reviews
    
    def _fetch_app_reviews(self, app: dict) -> list[Review]:
        """Fetch les avis pour une app spécifique avec stratégies de fallback."""
        
        # Stratégie 1 : Par pays
        result = self._try_fetch(
            app["package_id"],
            country=app["country"],
            lang=None,
            label=f"store {app['country'].upper()}"
        )
        if result:
            return self._parse_reviews(result, app)
        
        # Stratégie 2 : Global
        result = self._try_fetch(
            app["package_id"],
            country=None,
            lang=None,
            label="global"
        )
        if result:
            return self._parse_reviews(result, app)
        
        # Stratégie 3 : Langue FR + pays
        result = self._try_fetch(
            app["package_id"],
            country=app["country"],
            lang="fr",
            label=f"fr {app['country'].upper()}"
        )
        if result:
            return self._parse_reviews(result, app)
        
        self.logger.warning(f"Aucun avis trouvé pour {app['name']}")
        return []
    
    def _try_fetch(self, package_id: str, country: str = None, lang: str = None, label: str = "") -> list:
        """Tente de récupérer les avis."""
        try:
            self.logger.debug(f"Tentative {label}")
            
            kwargs = {
                "app_id": package_id,
                "sort": Sort.NEWEST,
                "count": 100,
            }
            if country:
                kwargs["country"] = country
            if lang:
                kwargs["lang"] = lang
            
            result, _ = reviews(**kwargs)
            
            if result:
                self.logger.info(f"✓ {len(result)} avis trouvés ({label})")
                return result
            
            return None
            
        except Exception as e:
            self.logger.debug(f"Échec {label} : {e}")
            return None
    
    def _parse_reviews(self, raw_reviews: list, app: dict) -> list[Review]:
        """Parse les avis bruts en objets Review."""
        parsed = []
        
        for rv in raw_reviews:
            try:
                review = Review(
                    id=rv.get("reviewId"),
                    company=app["name"],
                    source=SourceEnum.GOOGLE_PLAY,
                    title=None,  # Play Store ne fournit pas de titre
                    text=rv.get("content", ""),
                    rating=rv.get("score"),
                    author=rv.get("userName"),
                    verified=True,  # Supposé vérifié sur Play Store
                    likes=rv.get("thumbsUpCount", 0),
                    created_at=rv.get("at"),
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed