"""
Scraper Apple App Store.
Collecte les avis depuis le flux RSS officiel iTunes.
"""

import logging
import time
import xml.etree.ElementTree as ET
from typing import Optional
from datetime import datetime
import urllib.request

from scraper_base import BaseScraper
from models import Review, SourceEnum
from config import settings
from scheduler import RetryConfig

logger = logging.getLogger(__name__)


class AppStoreScraper(BaseScraper):
    """Scraper pour Apple App Store."""
    
    # Configuration des apps à scraper
    APPS = [
        {"app_id": "1565251147", "name": "Moov Africa Benin", "country": "bj"},
        {"app_id": "1591871295", "name": "Moov Africa Burkina", "country": "bf"},
        {"app_id": "1612447230", "name": "Moov Africa Mali", "country": "ml"},
        {"app_id": "1545892144", "name": "Moov Africa Centrafrique", "country": "cf"},
    ]
    
    # Namespaces XML
    NAMESPACES = {
        'feed': 'http://www.w3.org/2005/Atom',
        'im': 'http://itunes.apple.com/rss'
    }
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.scraping.request_timeout,
        )
        super().__init__("appstore", retry_config)
    
    def collect(self) -> list[Review]:
        """Collecte les avis depuis App Store pour toutes les apps."""
        all_reviews = []
        
        for app in self.APPS:
            try:
                self.logger.info(f"Scraping {app['name']}")
                reviews = self._fetch_app_reviews(app)
                all_reviews.extend(reviews)
                time.sleep(1)
            except Exception as e:
                self.logger.error(f"Erreur scraping {app['name']} : {e}")
                continue
        
        return all_reviews
    
    def _fetch_app_reviews(self, app: dict) -> list[Review]:
        """
        Fetch les avis pour une app avec stratégies de fallback.
        """
        
        # Stratégie 1 : Essayer le store du pays
        raw_reviews = self._try_fetch_reviews(
            app["app_id"],
            app["country"],
            label=f"store {app['country'].upper()}"
        )
        
        if raw_reviews:
            return self._parse_reviews(raw_reviews, app["name"])
        
        # Stratégie 2 : Essayer le store FR en fallback
        if app["country"] != "fr":
            self.logger.info(f"Aucun avis sur {app['country']}, essai fallback FR")
            raw_reviews = self._try_fetch_reviews(
                app["app_id"],
                "fr",
                label="store FR (fallback)"
            )
            
            if raw_reviews:
                return self._parse_reviews(raw_reviews, app["name"])
        
        self.logger.warning(f"Aucun avis trouvé pour {app['name']}")
        return []
    
    def _try_fetch_reviews(
        self,
        app_id: str,
        country: str,
        label: str = ""
    ) -> Optional[list[dict]]:
        """
        Essaie de récupérer les avis pour une app.
        """
        reviews_list = []
        
        try:
            self.logger.debug(f"Tentative {label}")
            
            # Itérer sur les pages
            for page in range(1, settings.appstore.max_pages + 1):
                url = (
                    f"https://itunes.apple.com/{country}/rss/"
                    f"customerreviews/page={page}/id={app_id}/"
                    f"sortby=mostrecent/xml"
                )
                
                try:
                    # Requête HTTP
                    headers = {
                        'User-Agent': (
                            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36'
                        )
                    }
                    req = urllib.request.Request(url, headers=headers)
                    
                    with urllib.request.urlopen(req, timeout=settings.scraping.request_timeout) as response:
                        xml_data = response.read()
                    
                    # Parser XML
                    root = ET.fromstring(xml_data)
                    entries = root.findall('feed:entry', self.NAMESPACES)
                    
                    # Premier entry = fiche app, skip
                    if not entries or len(entries) <= 1:
                        self.logger.debug(f"Fin du scraping page {page} (aucun avis)")
                        break
                    
                    # Extraire les avis (skip le premier)
                    for entry in entries[1:]:
                        review = self._parse_xml_entry(entry)
                        if review:
                            reviews_list.append(review)
                    
                    time.sleep(0.5)
                    
                except Exception as e:
                    self.logger.debug(f"Erreur page {page} : {e}")
                    break
            
            if reviews_list:
                self.logger.info(
                    f"✓ {len(reviews_list)} avis trouvés ({label})"
                )
                return reviews_list
            
            return None
            
        except Exception as e:
            self.logger.debug(f"Échec {label} : {e}")
            return None
    
    def _parse_xml_entry(self, entry) -> Optional[dict]:
        """Parse un entry XML en dictionnaire."""
        try:
            review_id = entry.find('feed:id', self.NAMESPACES)
            title = entry.find('feed:title', self.NAMESPACES)
            content = entry.find('feed:content', self.NAMESPACES)
            rating = entry.find('im:rating', self.NAMESPACES)
            updated = entry.find('feed:updated', self.NAMESPACES)
            author = entry.find('feed:author/feed:name', self.NAMESPACES)
            
            if not review_id or not content:
                return None
            
            # Parser la date
            published_date = datetime.utcnow()
            if updated is not None and updated.text:
                try:
                    # Format: 2024-01-15T10:30:45Z
                    date_str = updated.text.split('+')[0].split('Z')[0].strip()
                    published_date = datetime.strptime(
                        date_str, "%Y-%m-%dT%H:%M:%S"
                    )
                except Exception:
                    pass
            
            return {
                "id": review_id.text or "",
                "title": title.text if title is not None else "",
                "text": content.text if content is not None else "",
                "rating": int(rating.text) if rating is not None and rating.text else None,
                "author": author.text if author is not None else None,
                "created_at": published_date,
            }
        
        except Exception as e:
            self.logger.debug(f"Erreur parsing entry : {e}")
            return None
    
    def _parse_reviews(self, raw_reviews: list, app_name: str) -> list[Review]:
        """Parse les avis bruts en objets Review."""
        parsed = []
        
        for rv in raw_reviews:
            try:
                if not rv.get("id") or not rv.get("text"):
                    continue
                
                review = Review(
                    id=rv["id"],
                    company=app_name,
                    source=SourceEnum.APP_STORE,
                    title=rv.get("title"),
                    text=rv["text"],
                    rating=rv.get("rating"),
                    author=rv.get("author"),
                    created_at=rv.get("created_at"),
                    verified=True,  # App Store vérifie les achats
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed