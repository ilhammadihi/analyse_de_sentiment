"""
Scraper RSS Feed.
Collecte les actualités depuis Google News.
"""

import logging
import urllib.parse
import calendar
from datetime import datetime, timezone

import feedparser
from bs4 import BeautifulSoup

from reviews.collectors.base import BaseCollector
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class RSSFeedScraper(BaseCollector):
    """Scraper pour Google News RSS."""
    
    # Opérateurs télécoms (Moov Africa uniquement)
    OPERATORS = [
        "Moov Africa Benin", "Moov Africa Burkina", "Moov Africa Mali",
        "Moov Africa Centrafrique",
    ]
    
    # Mots-clés de recherche
    KEYWORDS = [
        "panne", "reseau", "service", "prix", "recharge",
        "connexion", "4g", "5g", "internet", "données"
    ]
    
    LANGUAGE = "fr"
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.scraping.request_timeout,
        )
        super().__init__("rss_feed", retry_config)
    
    def collect(self) -> list[Review]:
        """Collecte les actualités depuis Google News RSS."""
        all_reviews = []
        
        for operator in self.OPERATORS:
            for keyword in self.KEYWORDS:
                try:
                    articles = self._fetch_articles(operator, keyword)
                    all_reviews.extend(articles)
                except Exception as e:
                    self.logger.debug(f"Erreur {operator} + {keyword} : {e}")
                    continue
        
        self.logger.info(f"{len(all_reviews)} articles collectés")
        return all_reviews
    
    def _fetch_articles(self, operator: str, keyword: str) -> list[Review]:
        """
        Récupère les articles depuis Google News pour un opérateur et mot-clé.
        """
        try:
            query = f"{operator} {keyword}"
            query_encoded = urllib.parse.quote_plus(query)
            
            url = (
                f"https://news.google.com/rss/search?"
                f"q={query_encoded}&hl={self.LANGUAGE}"
            )
            
            self.logger.debug(f"Récupération RSS : {query}")
            
            # Parser le flux RSS
            feed = feedparser.parse(url)
            
            articles = []
            
            for entry in feed.entries:
                try:
                    # Extraire les informations
                    title = getattr(entry, "title", "") or ""
                    summary_html = getattr(entry, "summary", "") or ""
                    
                    # Nettoyer le HTML
                    summary = BeautifulSoup(
                        summary_html, "html.parser"
                    ).get_text()
                    
                    source = getattr(entry, "source", {}).get("title", "Google News")
                    
                    # ID unique
                    article_id = (
                        getattr(entry, "id", None)
                        or getattr(entry, "link", "")
                        or f"{operator}:{title}"
                    )
                    
                    # feedparser fournit la date au format RFC 822 ("Sat, 16 May 2020...")
                    # dans entry.published, que Pydantic v2 refuse de parser directement.
                    # entry.published_parsed est un struct_time UTC déjà normalisé.
                    published_parsed = getattr(entry, "published_parsed", None)
                    if published_parsed:
                        published = datetime.fromtimestamp(
                            calendar.timegm(published_parsed), tz=timezone.utc
                        )
                    else:
                        published = datetime.now(timezone.utc)
                    
                    # Créer l'avis (article)
                    article = Review(
                        id=article_id,
                        company=operator,
                        source=SourceEnum.RSS_FEED,
                        title=title,
                        text=summary,
                        rating=None,  # Les articles n'ont pas de rating
                        created_at=published,
                    )
                    
                    articles.append(article)
                    
                except Exception as e:
                    self.logger.debug(f"Erreur parsing entry : {e}")
                    continue
            
            if articles:
                self.logger.debug(f"✓ {len(articles)} articles trouvés : {query}")
            
            return articles
            
        except Exception as e:
            self.logger.error(f"Erreur récupération RSS ({operator} + {keyword}) : {e}")
            return []