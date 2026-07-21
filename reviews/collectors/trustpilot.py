"""
Scraper Trustpilot.
Collecte les avis depuis Trustpilot en utilisant Playwright et l'API interne.
"""

import logging
import time
import json
from typing import Any, Optional
from datetime import datetime
from pathlib import Path
import re

from playwright.sync_api import sync_playwright, Page, Browser
from reviews.collectors.base import BaseCollector
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class TrustpilotScraper(BaseCollector):
    """Scraper pour Trustpilot."""

    # Playwright (API sync) est lié au thread OS qui le démarre : pas de
    # timeout par thread worker (voir scheduler.execute_with_retry).
    USES_THREAD_TIMEOUT = False

    # Configuration des domaines à scraper
    COMPANIES = [
        {"domain": "moov-africa.bj", "name": "Moov Africa Benin"},
        {"domain": "moov-africa.bf", "name": "Moov Africa Burkina"},
        {"domain": "moov-africa.ml", "name": "Moov Africa Mali"},
        {"domain": "moov-africa.cf", "name": "Moov Africa Centrafrique"},
    ]
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.scraping.request_timeout,
        )
        super().__init__("trustpilot", retry_config)
        self.state_path = Path(settings.trustpilot.cache_path)
        self.browser: Optional[Browser] = None
    
    def collect(self) -> list[Review]:
        """Collecte les avis depuis Trustpilot pour tous les domaines."""
        all_reviews = []
        playwright = None

        try:
            playwright = sync_playwright().start()
            self.browser = playwright.chromium.launch(
                # Pas de channel="chrome" : cela exige Google Chrome de marque,
                # absent des images Docker. On utilise le Chromium fourni par
                # Playwright, présent partout (local et conteneur).
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ]
            )
            
            # Charger l'état de session s'il existe
            context_kwargs = {
                "locale": "en-US",
                "timezone_id": "Africa/Casablanca",
                "viewport": {"width": 1400, "height": 900},
            }
            
            if self.state_path.exists():
                self.logger.info(f"Chargement de l'état de session depuis {self.state_path}")
                try:
                    with open(self.state_path, "r") as f:
                        state = json.load(f)
                        context_kwargs["storage_state"] = state
                except Exception as e:
                    self.logger.warning(f"Impossible de charger l'état : {e}")
            
            context = self.browser.new_context(**context_kwargs)
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            
            for company in self.COMPANIES:
                try:
                    self.logger.info(f"Scraping {company['name']} ({company['domain']})")
                    reviews = self._scrape_company(context, company)
                    all_reviews.extend(reviews)
                    time.sleep(2)  # Pause entre les domaines
                except Exception as e:
                    self.logger.error(f"Erreur scraping {company['name']} : {e}")
                    continue
            
            # Sauvegarder l'état de session
            try:
                state = context.storage_state()
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.state_path, "w") as f:
                    json.dump(state, f)
                self.logger.info(f"État de session sauvegardé dans {self.state_path}")
            except Exception as e:
                self.logger.warning(f"Impossible de sauvegarder l'état : {e}")
            
            context.close()

            return all_reviews

        except Exception as e:
            self.logger.error(f"Erreur Playwright : {e}")
            raise
        finally:
            # Toujours tout fermer, sinon le driver Playwright (et sa boucle
            # asyncio interne) reste actif dans ce thread et empêche tout
            # nouveau sync_playwright().start() ultérieur dans le même thread
            # (erreur "Playwright Sync API inside the asyncio loop").
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass
    
    def _scrape_company(self, context, company: dict) -> list[Review]:
        """Scrape les avis pour un domaine spécifique."""
        page = context.new_page()
        reviews_list = []
        
        try:
            url = f"https://www.trustpilot.com/review/{company['domain']}"
            self.logger.info(f"Navigation vers {url}")
            
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            
            # Accepter les cookies/consentement
            self._handle_consent(page)
            time.sleep(1)
            
            # Récupérer le buildId (ID du build Next.js)
            build_id = self._get_build_id(page)
            if not build_id:
                self.logger.warning("Impossible de récupérer le buildId")
                return []
            
            # Récupérer le slug depuis l'URL
            slug = company["domain"]
            
            # Scraper les pages
            for page_num in range(1, settings.trustpilot.max_pages + 1):
                self.logger.debug(f"Scraping page {page_num}")
                
                try:
                    page_reviews = self._fetch_reviews_page(
                        page, build_id, slug, page_num
                    )
                    
                    if not page_reviews:
                        self.logger.info(f"Fin du scraping (page {page_num} vide)")
                        break
                    
                    reviews_list.extend(page_reviews)
                    time.sleep(1)
                    
                except Exception as e:
                    self.logger.warning(f"Erreur page {page_num} : {e}")
                    continue
            
            # Parser les avis bruts
            parsed = self._parse_reviews(reviews_list, company["name"])
            self.logger.info(f"{len(parsed)} avis parsés pour {company['name']}")
            
            return parsed
            
        finally:
            page.close()
    
    def _handle_consent(self, page: Page):
        """Accepte les cookies/consentement Google."""
        try:
            # Chercher le bouton d'acceptation
            buttons = page.query_selector_all("button")
            for btn in buttons:
                try:
                    aria_label = btn.get_attribute("aria-label")
                    if aria_label and "accept" in aria_label.lower():
                        btn.click()
                        return
                except:
                    pass
        except Exception as e:
            self.logger.debug(f"Erreur gestion consentement : {e}")
    
    def _get_build_id(self, page: Page) -> Optional[str]:
        """Récupère le buildId Next.js."""
        try:
            build_id = page.evaluate("""
                () => {
                    // Chercher dans window.__NEXT_DATA__
                    if (window.__NEXT_DATA__) {
                        return window.__NEXT_DATA__.buildId;
                    }
                    return null;
                }
            """)
            return build_id
        except Exception as e:
            self.logger.debug(f"Erreur récupération buildId : {e}")
            return None
    
    def _fetch_reviews_page(
        self,
        page: Page,
        build_id: str,
        slug: str,
        page_num: int
    ) -> list[dict[str, Any]]:
        """
        Récupère les avis d'une page via l'API interne.
        """
        try:
            # URL de l'API interne Trustpilot
            api_url = (
                f"https://www.trustpilot.com/_next/data/{build_id}/"
                f"en/review/{slug}.json?page={page_num}"
            )
            
            self.logger.debug(f"Requête API : {api_url}")
            
            # Faire la requête depuis le navigateur (pour utiliser les cookies)
            response = page.evaluate(f"""
                async () => {{
                    const response = await fetch('{api_url}');
                    if (!response.ok) return null;
                    return await response.json();
                }}
            """)
            
            if not response:
                return []
            
            # Parser la réponse JSON
            reviews = self._extract_reviews_from_api_response(response)
            return reviews
            
        except Exception as e:
            self.logger.warning(f"Erreur API page {page_num} : {e}")
            return []
    
    def _extract_reviews_from_api_response(self, data: dict) -> list[dict]:
        """Parse les avis depuis la réponse API JSON."""
        reviews = []
        
        try:
            # Naviguer dans la structure JSON (selon Trustpilot API)
            # La structure peut varier, il faut adapter selon l'API réelle
            if "pageProps" not in data:
                return []
            
            page_props = data["pageProps"]
            if "reviews" not in page_props:
                return []
            
            for review in page_props["reviews"]:
                reviews.append({
                    "id": review.get("id"),
                    "title": review.get("title"),
                    "text": review.get("text"),
                    "rating": review.get("rating"),
                    "author": review.get("author", {}).get("displayName"),
                    "created_at": review.get("createdAt"),
                    "likes": review.get("likes"),
                })
        
        except Exception as e:
            self.logger.debug(f"Erreur parsing API response : {e}")
        
        return reviews
    
    def _parse_reviews(self, raw_reviews: list, company_name: str) -> list[Review]:
        """Parse les avis bruts en objets Review."""
        parsed = []
        
        for rv in raw_reviews:
            try:
                if not rv.get("id") or not rv.get("text"):
                    continue
                
                review = Review(
                    id=rv["id"],
                    company=company_name,
                    source=SourceEnum.TRUSTPILOT,
                    title=rv.get("title"),
                    text=rv["text"],
                    rating=rv.get("rating"),
                    author=rv.get("author"),
                    created_at=rv.get("created_at"),
                    likes=rv.get("likes"),
                    verified=True,  # Trustpilot vérifie généralement
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed