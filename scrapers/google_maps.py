"""
Scraper Google Maps.
Collecte les avis des agences Moov Africa.
"""

import logging
import time
import re
from typing import Optional
from datetime import datetime, timedelta, timezone
import sys

from playwright.sync_api import sync_playwright, Page, Browser
from scraper_base import BaseScraper
from models import Review, SourceEnum
from config import settings
from scheduler import RetryConfig

logger = logging.getLogger(__name__)


class GoogleMapsScraper(BaseScraper):
    """Scraper pour Google Maps."""

    # Playwright (API sync) est lié au thread OS qui le démarre : pas de
    # timeout par thread worker (voir scheduler.execute_with_retry).
    USES_THREAD_TIMEOUT = False

    # Configuration des agences à scraper
    LOCATIONS = [
        {
            "query": "Agence Moov Africa Cotonou Bénin",
            "name": "Moov Africa Benin"
        },
        {
            "query": "Moov Africa Siège Ouagadougou Burkina Faso",
            "name": "Moov Africa Burkina"
        },
        {
            "query": "Agence Moov Africa Bamako Mali",
            "name": "Moov Africa Mali"
        },
        {
            "query": "Agence Moov Africa Bangui Centrafrique",
            "name": "Moov Africa Centrafrique"
        },
    ]
    
    # Mapping durées relatives → jours
    TIME_UNITS = {
        "year": 365, "month": 30, "week": 7, "day": 1,
        "hour": 1/24, "minute": 1/1440, "second": 0
    }
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.scraping.request_timeout,
        )
        super().__init__("googlemaps", retry_config)
        self.browser: Optional[Browser] = None
    
    def collect(self) -> list[Review]:
        """Collecte les avis depuis Google Maps pour toutes les agences."""
        all_reviews = []
        playwright = None

        try:
            playwright = sync_playwright().start()
            self.browser = playwright.chromium.launch(
                channel="chrome",
                headless=settings.googlemaps.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ]
            )
            
            context = self.browser.new_context(
                locale="en-US",
                timezone_id="Africa/Casablanca",
                viewport={"width": 1400, "height": 1000}
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            
            for location in self.LOCATIONS:
                try:
                    self.logger.info(f"Scraping {location['name']}")
                    reviews = self._scrape_location(context, location)
                    all_reviews.extend(reviews)
                    time.sleep(5)  # Pause entre les agences
                except Exception as e:
                    self.logger.error(f"Erreur scraping {location['name']} : {e}")
                    continue
            
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
    
    def _scrape_location(self, context, location: dict) -> list[Review]:
        """Scrape les avis pour une agence spécifique."""
        page = context.new_page()
        reviews_list = []
        
        try:
            # Ouvrir le lieu
            if not self._open_place(page, location["query"]):
                return []
            
            # Ouvrir l'onglet Reviews
            if not self._open_reviews_tab(page):
                self.logger.warning(f"Impossible d'ouvrir l'onglet Reviews")
                return []
            
            time.sleep(3)
            
            # Scroller pour charger les avis
            self.logger.info("Scrapping des avis...")
            reviews_list = self._scroll_and_extract(page)
            
            # Parser les avis bruts
            parsed = self._parse_reviews(reviews_list, location["name"])
            self.logger.info(f"{len(parsed)} avis parsés pour {location['name']}")
            
            return parsed
            
        finally:
            page.close()
    
    def _open_place(self, page: Page, query: str) -> bool:
        """Ouvre un lieu sur Google Maps."""
        try:
            self.logger.debug(f"Recherche : {query}")
            
            url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            # Accepter le bandeau de consentement cookies s'il est présent
            # (sans ça, l'UI de la fiche lieu ne se charge jamais et l'onglet
            # Avis reste introuvable)
            self._handle_consent(page)
            time.sleep(1)

            # Chercher un résultat de recherche
            try:
                first_result = page.locator('a.hfpxzc').first
                if first_result.count() > 0:
                    first_result.click()
                    page.wait_for_url("**/place/**", timeout=10000)
            except:
                pass
            
            # Vérifier qu'on est sur une page de lieu
            if "/place/" not in page.url:
                self.logger.warning(f"Aucun lieu trouvé pour : {query}")
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Erreur ouverture lieu : {e}")
            return False

    def _handle_consent(self, page: Page):
        """Accepte le bandeau de consentement cookies Google si présent."""
        try:
            buttons = page.query_selector_all("button")
            for btn in buttons:
                try:
                    text = (btn.inner_text() or "").strip().lower()
                    aria_label = (btn.get_attribute("aria-label") or "").lower()
                    if any(
                        keyword in text or keyword in aria_label
                        for keyword in ("accept all", "tout accepter", "j'accepte", "i agree")
                    ):
                        btn.click()
                        return
                except Exception:
                    continue
        except Exception as e:
            self.logger.debug(f"Erreur gestion consentement : {e}")

    def _open_reviews_tab(self, page: Page) -> bool:
        """Ouvre l'onglet Reviews."""
        try:
            self.logger.debug("Ouverture onglet Reviews")
            
            # Chercher le bouton Reviews
            review_tab = page.locator('button[role="tab"]').filter(
                has_text=re.compile(r"Reviews|Avis", re.IGNORECASE)
            )
            
            review_tab.wait_for(state="visible", timeout=60000)
            review_tab.first.click()
            
            # Attendre que les avis chargent
            page.wait_for_selector('div.jftiEf', timeout=60000)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Erreur ouverture Reviews : {e}")
            return False
    
    def _scroll_and_extract(self, page: Page) -> list[dict]:
        """Scrolle pour charger les avis et les extrait."""
        reviews_list = []
        prev_count = 0
        stable_count = 0
        
        for i in range(400):
            try:
                # Scroller
                count = page.evaluate("""
                    () => {
                        const scrollable = document.querySelector(
                            'div.m6QErb.DxyBCb.kA9KIf.dS8AEf'
                        ) || document.querySelector('div[role="main"]');
                        if (scrollable) {
                            scrollable.scrollTop = scrollable.scrollHeight;
                        }
                        return document.querySelectorAll('div[data-review-id]').length;
                    }
                """)
                
                time.sleep(1.3)
                self.logger.debug(f"Scroll {i+1} : {count} avis")
                
                if count >= settings.googlemaps.max_reviews:
                    break
                
                if count == prev_count:
                    stable_count += 1
                    if stable_count >= 4:
                        self.logger.debug("Fin du scraping (stable)")
                        break
                else:
                    stable_count = 0
                
                prev_count = count
                
            except Exception as e:
                self.logger.debug(f"Erreur scroll {i+1} : {e}")
                break
        
        # Développer les textes tronqués
        try:
            page.evaluate("""
                () => {
                    let n = 0;
                    const buttons = document.querySelectorAll(
                        'button[aria-label="See more"], button.w8nwRe'
                    );
                    buttons.forEach(b => {
                        if (b.innerText.toLowerCase().includes('more')) {
                            b.click();
                            n++;
                        }
                    });
                    return n;
                }
            """)
            time.sleep(1)
        except:
            pass
        
        # Extraire les avis
        try:
            raw = page.evaluate("""
                () => {
                    const out = [];
                    document.querySelectorAll('div[data-review-id]').forEach(r => {
                        const id = r.getAttribute('data-review-id');
                        const starEl = r.querySelector('span[role="img"]');
                        const textEl = r.querySelector('span.wiI7pd');
                        const dateEl = r.querySelector('span.rsqaWe');
                        const authorEl = r.querySelector('div.d4r55');
                        
                        out.push({
                            id: id,
                            author: authorEl ? authorEl.innerText : null,
                            rating_aria: starEl ? starEl.getAttribute('aria-label') : null,
                            date_rel: dateEl ? dateEl.innerText : null,
                            text: textEl ? textEl.innerText : null
                        });
                    });
                    return out;
                }
            """)
            
            self.logger.info(f"{len(raw)} avis bruts extraits")
            return raw
            
        except Exception as e:
            self.logger.error(f"Erreur extraction : {e}")
            return []
    
    def _parse_reviews(self, raw_reviews: list, location_name: str) -> list[Review]:
        """Parse les avis bruts en objets Review."""
        parsed = []
        now = datetime.now(timezone.utc)
        
        for rv in raw_reviews[:settings.googlemaps.max_reviews]:
            try:
                rating = self._parse_rating(rv.get("rating_aria"))
                published = self._relative_to_iso(rv.get("date_rel"), now)
                
                if not rv.get("text"):
                    continue
                
                review = Review(
                    id=rv.get("id"),
                    company=location_name,
                    source=SourceEnum.GOOGLE_MAPS,
                    title=None,  # Google Maps n'a pas de titre
                    text=rv.get("text"),
                    rating=rating,
                    author=rv.get("author"),
                    created_at=published,
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed
    
    def _parse_rating(self, aria: Optional[str]) -> Optional[int]:
        """Parse la note depuis aria-label."""
        if not aria:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)", aria)
        if match:
            return int(round(float(match.group(1))))
        return None
    
    def _relative_to_iso(
        self,
        rel: Optional[str],
        now: datetime
    ) -> Optional[str]:
        """Convertit une date relative en ISO."""
        if not rel:
            return None
        
        r = rel.lower().strip()
        
        # Cas spéciaux
        if "yesterday" in r:
            return (now - timedelta(days=1)).isoformat()
        if "today" in r or "just now" in r:
            return now.isoformat()
        
        # Parsrer "X <unit> ago"
        match = re.search(
            r"(\d+|a|an)\s+(year|month|week|day|hour|minute|second)",
            r
        )
        if not match:
            return None
        
        n = 1 if match.group(1) in ("a", "an") else int(match.group(1))
        unit = match.group(2)
        
        delta_days = n * self.TIME_UNITS.get(unit, 0)
        return (now - timedelta(days=delta_days)).isoformat()