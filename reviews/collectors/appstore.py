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

from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import appstore_apps
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class AppStoreScraper(BaseCollector):
    """Scraper pour Apple App Store."""
    
    # Apps à scraper : chargées depuis config/operators.json (voir
    # collectors/targets.py). Les app_id y sont vérifiés contre l'API iTunes,
    # jamais devinés — un identifiant inventé pointe vers une autre entité
    # (bug déjà rencontré : 1612447230 -> "Claustrophobia") ou vers la filiale
    # d'un autre pays. Une filiale sans app déclarée est simplement ignorée.

    # Namespaces XML
    NAMESPACES = {
        'feed': 'http://www.w3.org/2005/Atom',
        'im': 'http://itunes.apple.com/rss'
    }
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            # Budget du collecteur ENTIER : 186 paquets parcourus à la suite.
            # Voir le commentaire équivalent côté Play Store.
            timeout=settings.appstore.collector_timeout,
        )
        super().__init__("appstore", retry_config)
    
    _pages_lues = 0
    _pages_evitees = 0

    def collect(self) -> list[Review]:
        """Collecte les avis depuis App Store pour toutes les apps."""
        all_reviews = []
        apps = appstore_apps()
        # Pages réellement téléchargées vs pages qu'on aurait téléchargées sans
        # arrêt anticipé. Journalisé à la fin : c'est la seule façon de vérifier
        # en production que l'incrémental fonctionne, un arrêt silencieux étant
        # indiscernable d'une source vide.
        self._pages_lues = 0
        self._pages_evitees = 0
        self.logger.info("%d app(s) App Store déclarée(s) en configuration", len(apps))

        for app in apps:
            try:
                self.logger.info(f"Scraping {app['name']}")
                reviews = self._fetch_app_reviews(app)
                all_reviews.extend(reviews)
                time.sleep(1)
            except Exception as e:
                self.logger.error(f"Erreur scraping {app['name']} : {e}")
                continue

        if self._pages_evitees:
            total = self._pages_lues + self._pages_evitees
            self.logger.info(
                "Collecte incrémentale : %d page(s) téléchargée(s) sur %d "
                "(%d évitée(s), %.0f %% de trafic en moins)",
                self._pages_lues, total, self._pages_evitees,
                100.0 * self._pages_evitees / total,
            )

        return all_reviews
    
    def _fetch_app_reviews(self, app: dict) -> list[Review]:
        """
        Fetch les avis pour une app avec stratégies de fallback.
        """
        
        # UNE SEULE boutique : celle du pays de la filiale.
        #
        # Il existait ici un repli sur la boutique FRANÇAISE quand la boutique
        # du pays ne renvoyait rien. Il a été retiré : les avis ainsi récupérés
        # étaient rattachés à la filiale interrogée, si bien qu'un avis déposé
        # en France était compté comme nigérian. Cela fausse exactement ce que
        # le dashboard sert à comparer — la satisfaction par pays et par
        # filiale — et de façon invisible, puisque la collecte réussissait.
        #
        # Une boutique vide est une information, pas un échec à contourner :
        # l'app existe mais n'a pas encore d'avis dans ce pays. L'onglet
        # « Collecte » l'expose, et c'est la bonne façon de la traiter.
        raw_reviews = self._try_fetch_reviews(
            app["app_id"],
            app["country"],
            label=f"store {app['country'].upper()}",
            company=app["name"],
        )

        if raw_reviews:
            return self._parse_reviews(raw_reviews, app["name"], app)

        self.logger.info(
            "Aucun avis sur la boutique %s pour %s — aucun repli, "
            "un avis d'un autre marché ne doit pas être attribué à cette filiale.",
            app["country"].upper(), app["name"],
        )
        return []
    
    def _try_fetch_reviews(
        self,
        app_id: str,
        country: str,
        label: str = "",
        company: Optional[str] = None,
    ) -> Optional[list[dict]]:
        """
        Essaie de récupérer les avis pour une app.
        """
        reviews_list = []
        
        # Repère de collecte incrémentale pour cette app. Le flux Apple est
        # trié du plus récent au plus ancien (`sortby=mostrecent` dans l'URL) :
        # dès qu'une page entière est antérieure au repère, tout ce qui suit
        # l'est aussi et il est inutile de continuer à télécharger.
        # Le repère est propre à CETTE app : une filiale en suit désormais
        # plusieurs (self-care, mobile money, TV…), et un repère commun ferait
        # écarter en bloc les avis de l'app la moins active.
        cutoff = (self.cutoff_for_key(company, "app_store", str(app_id))
                  if company else None)

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
                    self._pages_lues += 1
                    root = ET.fromstring(xml_data)
                    entries = root.findall('feed:entry', self.NAMESPACES)
                    
                    # Premier entry = fiche app, skip
                    if not entries or len(entries) <= 1:
                        self.logger.debug(f"Fin du scraping page {page} (aucun avis)")
                        break
                    
                    # Extraire les avis (skip le premier)
                    page_reviews = []
                    for entry in entries[1:]:
                        review = self._parse_xml_entry(entry)
                        if review:
                            page_reviews.append(review)
                    reviews_list.extend(page_reviews)

                    # ARRÊT ANTICIPÉ : cette page ne contient-elle que des avis
                    # déjà en base ? Si oui, les pages suivantes — plus
                    # anciennes encore — le sont forcément aussi.
                    #
                    # Sans cela le collecteur retéléchargeait ses cinq pages à
                    # chaque passage pour n'en retenir presque rien : 8 534
                    # doublons pour 3 294 insertions sur sept jours, soit 72 %
                    # de trafic inutile et autant de quota Apple consommé pour
                    # rien.
                    if self.batch_fully_known(
                        [r.get("created_at") for r in page_reviews], cutoff
                    ):
                        self._pages_evitees += settings.appstore.max_pages - page
                        self.logger.debug(
                            "Arrêt page %d : lot entièrement déjà connu", page
                        )
                        break

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
            
            # `is None` obligatoire : un Element ElementTree sans enfant est
            # *falsy*, donc `not review_id` valait True même pour un <id>
            # parfaitement renseigné. Ce test rejetait ainsi TOUS les avis
            # (vérifié : 49/49 sur un flux de test).
            if review_id is None or content is None:
                return None
            
            # Parser la date
            published_date = datetime.utcnow()
            if updated is not None and updated.text:
                try:
                    # Apple date les avis avec un décalage horaire négatif
                    # ("2026-04-30T08:57:22-07:00"). L'ancien découpage ne
                    # traitait que "+" et "Z" : strptime échouait donc à chaque
                    # fois et TOUS les avis retombaient sur utcnow(), c'est-à-dire
                    # la date de collecte — toute analyse temporelle en dépendait.
                    # fromisoformat gère nativement les décalages signés.
                    published_date = datetime.fromisoformat(updated.text.strip())
                except ValueError:
                    self.logger.debug(f"Date illisible : {updated.text!r}")
            
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
    
    def _parse_reviews(self, raw_reviews: list, app_name: str,
                       app: Optional[dict] = None) -> list[Review]:
        """Parse les avis bruts en objets Review.

        `app` porte l'identité de la SOUS-CIBLE. Elle est indispensable au
        repère incrémental : une filiale suit plusieurs applications, et sans
        cette identité le repère resterait commun à toutes — l'app la plus
        active fixerait la date et les autres seraient écartées en bloc.
        """
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
                    target_id=str((app or {}).get("app_id") or "") or None,
                    target_name=(app or {}).get("app_label") or None,
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed