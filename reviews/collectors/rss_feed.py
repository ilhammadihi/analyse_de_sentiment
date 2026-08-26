"""
Scraper RSS Feed.
Collecte les actualités depuis Google News.
"""

import logging
import urllib.parse
import calendar
import concurrent.futures
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import press_matchers, rss_search_terms
from reviews.domain.models import Review, SourceEnum
from reviews.domain.press_attribution import NOISE, classify, compile_matchers
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class RSSFeedScraper(BaseCollector):
    """Scraper pour Google News RSS."""
    
    # Opérateurs : chargés depuis config/operators.json (targets.py). Les
    # mots-clés sont réglables (RSS_KEYWORDS) car le nombre de flux est le
    # produit filiales x mots-clés.

    LANGUAGE = "fr"

    # Les flux sont récupérés en parallèle : ces requêtes sont I/O-bound
    # (on attend le réseau), les enchaîner séquentiellement coûtait ~136 s
    # rien que pour 40 flux — d'où les expirations observées en production.
    MAX_WORKERS = settings.rss_feed.max_workers

    # Timeout d'UNE requête HTTP. Distinct du budget global du collecteur
    # (RetryConfig.timeout), qui couvre lui tous les flux cumulés.
    FETCH_TIMEOUT = 15

    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.rss_feed.collector_timeout,
        )
        super().__init__("rss_feed", retry_config)
        # Google News est un moteur FLOU : interroger « Orange internet » ramène
        # « Orange Actualités », un portail d'information, et « Les oranges, un
        # luxe pour les Marocains ». Sans vérification, ces articles entraient
        # sous le nom de l'opérateur cherché — mesuré sur le corpus collecté :
        # 3 026 articles sur 7 718 ne nommaient personne du périmètre, et
        # Telesom comme TN Mobile totalisaient 526 articles dont AUCUN ne les
        # mentionnait.
        self._matchers = compile_matchers(press_matchers())

    def collect(self) -> list[Review]:
        """Collecte les actualités depuis Google News RSS."""
        operators = rss_search_terms()
        keywords = settings.rss_feed.keywords_list()
        requetes = [(op, kw) for op in operators for kw in keywords]
        self.logger.info(
            "%d filiale(s) x %d mot(s)-clé = %d flux à récupérer",
            len(operators), len(keywords), len(requetes),
        )
        all_reviews: list[Review] = []
        seen: set[str] = set()
        doublons = 0

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.MAX_WORKERS
        ) as pool:
            futures = {
                pool.submit(self._fetch_articles, op, kw): (op, kw)
                for op, kw in requetes
            }
            for future in concurrent.futures.as_completed(futures):
                operator, keyword = futures[future]
                try:
                    for article in future.result():
                        # Un même article remonte sur plusieurs mots-clés
                        # ("panne" + "réseau" + "service") : près de la moitié
                        # des résultats étaient des doublons, re-parsés puis
                        # rejetés en base. On les écarte dès la collecte.
                        if article.id in seen:
                            doublons += 1
                            continue
                        seen.add(article.id)
                        all_reviews.append(article)
                except Exception as e:
                    self.logger.debug(f"Erreur {operator} + {keyword} : {e}")
                    continue

        self.logger.info(
            "%d articles uniques collectés sur %d flux (%d doublons écartés)",
            len(all_reviews), len(requetes), doublons,
        )
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

            # feedparser.parse(url) télécharge lui-même, sans timeout possible :
            # un flux qui ne répondait pas bloquait le thread indéfiniment. On
            # récupère donc le contenu via requests (timeout explicite) avant
            # de le parser.
            response = requests.get(url, timeout=self.FETCH_TIMEOUT)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            articles = []
            ecartes = 0

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
                    
                    # L'article parle-t-il vraiment de quelqu'un du périmètre ?
                    #
                    # On n'écarte ICI que le bruit franc — aucun opérateur suivi
                    # n'est nommé. Le reste (bonne filiale, mauvaise filiale,
                    # actualité de groupe) est laissé passer et tranché par
                    # `tools/reattribute_press.py`, qui dispose des dimensions
                    # en base : ce collecteur ne connaît que des noms, et
                    # l'actualité de groupe n'a pas de représentation dans
                    # `Review`, qui exige une entreprise.
                    etat, filiales, _ = classify(
                        self._matchers, title, summary, operator
                    )
                    if etat == NOISE:
                        ecartes += 1
                        continue

                    article = Review(
                        id=article_id,
                        # Une seule filiale nommée : on corrige tout de suite,
                        # plutôt que d'écrire un nom qu'on sait faux.
                        company=filiales[0] if len(filiales) == 1 else operator,
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
            
            if articles or ecartes:
                self.logger.debug(
                    "✓ %d article(s) retenu(s), %d écarté(s) faute de citer le "
                    "périmètre : %s", len(articles), ecartes, query,
                )

            return articles
            
        except Exception as e:
            self.logger.error(f"Erreur récupération RSS ({operator} + {keyword}) : {e}")
            return []