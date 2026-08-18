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
from reviews.collectors.targets import trustpilot_companies
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class BusinessUnitNotFound(Exception):
    """Aucune fiche Trustpilot pour ce domaine (HTTP 404).

    Distinct d'une panne : la fiche n'existe pas, il n'y a rien à collecter.
    """


class TrustpilotScraper(BaseCollector):
    """Scraper pour Trustpilot."""

    # Playwright (API sync) est lié au thread OS qui le démarre : pas de
    # timeout par thread worker (voir scheduler.execute_with_retry).
    USES_THREAD_TIMEOUT = False

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Domaines à scraper. Le slug Trustpilot est le domaine EXACT enregistré par
    # l'entreprise, préfixe `www.` compris : "moov-africa.bj" renvoie 404 alors
    # que "www.moov-africa.bj" existe.
    # État vérifié le 2026-07-21 : seule la fiche Bénin existe (et elle est
    # vide — "Be the first to review"), les trois autres n'ont aucune fiche
    # Trustpilot. Ce collecteur ne peut donc rien remonter tant que Moov Africa
    # n'a pas d'avis sur Trustpilot ; le code ci-dessous le signale
    # explicitement au lieu de renvoyer 0 avis en silence.
    # Fiches à scraper : chargées depuis config/operators.json (targets.py).
    # Un domaine sans fiche Trustpilot est signalé explicitement (voir
    # BusinessUnitNotFound) au lieu de renvoyer 0 avis en silence.
    
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
                # Sans UA explicite, Chromium annonce "HeadlessChrome" et
                # Trustpilot durcit sa protection anti-bot.
                "user_agent": self.USER_AGENT,
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
            
            companies = trustpilot_companies()
            self.logger.info("%d fiche(s) déclarée(s) en configuration", len(companies))
            not_found = 0
            failures = 0
            for company in companies:
                try:
                    self.logger.info(f"Scraping {company['name']} ({company['domain']})")
                    reviews = self._scrape_company(context, company)
                    all_reviews.extend(reviews)
                    time.sleep(2)  # Pause entre les domaines
                except BusinessUnitNotFound as e:
                    not_found += 1
                    self.logger.warning(str(e))
                    continue
                except Exception as e:
                    failures += 1
                    self.logger.error(f"Erreur scraping {company['name']} : {e}")
                    continue

            if not_found:
                self.logger.warning(
                    "%d/%d domaines sans fiche Trustpilot : rien à collecter "
                    "pour ceux-ci (ce n'est pas une panne du scraper)",
                    not_found, len(companies),
                )
            
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

            # Toutes les fiches existantes en erreur => panne réelle (blocage
            # anti-bot, refonte du site). On lève pour déclencher le retry et
            # marquer le run "failed", au lieu d'un "success" à 0 avis.
            if failures and failures == len(companies) - not_found:
                raise RuntimeError(
                    f"Les {failures} fiches Trustpilot accessibles ont toutes "
                    f"échoué : site inaccessible ou structure modifiée"
                )

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
            slug = company["domain"]
            url = f"https://www.trustpilot.com/review/{slug}"
            self.logger.info(f"Navigation vers {url}")

            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)

            # 404 = la fiche n'existe pas. Sans ce test, le scraper enchaînait
            # sur une page vide et concluait "0 avis" comme s'il avait réussi.
            if response is not None and response.status == 404:
                raise BusinessUnitNotFound(
                    f"Aucune fiche Trustpilot pour {slug} (HTTP 404)"
                )

            # Accepter les cookies/consentement
            self._handle_consent(page)
            time.sleep(1)

            # Page 1 : lue directement dans __NEXT_DATA__, déjà présent dans le
            # HTML servi. Aucun appel réseau, et cela fonctionne même quand
            # Trustpilot répond 403 derrière son écran anti-bot "Verifying
            # Connection" (les données restent embarquées dans la page).
            reviews_list.extend(self._reviews_from_next_data(page))
            self.logger.debug(f"Page 1 : {len(reviews_list)} avis via __NEXT_DATA__")

            # Pages suivantes : API interne Next.js
            build_id = self._get_build_id(page)
            if not build_id:
                self.logger.warning(
                    "buildId introuvable : pagination désactivée (page 1 seule)"
                )
            else:
                for page_num in range(2, settings.trustpilot.max_pages + 1):
                    self.logger.debug(f"Scraping page {page_num}")
                    try:
                        page_reviews = self._fetch_reviews_page(
                            page, build_id, slug, page_num
                        )
                        if not page_reviews:
                            self.logger.info(f"Fin de la pagination (page {page_num})")
                            break

                        reviews_list.extend(page_reviews)
                        time.sleep(1)
                    except Exception as e:
                        self.logger.warning(f"Erreur page {page_num} : {e}")
                        break

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
            # URL de l'API interne Trustpilot.
            # PAS de segment de locale : ".../_next/data/<build>/en/review/..."
            # renvoyait 404 systématiquement — c'est ce qui vidait chaque page.
            api_url = (
                f"https://www.trustpilot.com/_next/data/{build_id}/"
                f"review/{slug}.json?page={page_num}"
            )

            self.logger.debug(f"Requête API : {api_url}")

            # Requête émise depuis la page pour réutiliser cookies et session.
            # On passe l'URL en argument plutôt qu'en f-string : un slug
            # contenant une apostrophe casserait le littéral JS.
            response = page.evaluate(
                """async (u) => {
                    const res = await fetch(u);
                    if (!res.ok) return {status: res.status};
                    return {status: res.status, body: await res.json()};
                }""",
                api_url,
            )

            if not response:
                return []

            # 404 = fin de pagination (dernière page dépassée), pas une erreur.
            if response.get("status") != 200:
                self.logger.debug(
                    f"Page {page_num} : HTTP {response.get('status')} — fin"
                )
                return []

            return self._extract_reviews_from_api_response(response.get("body") or {})

        except Exception as e:
            self.logger.warning(f"Erreur API page {page_num} : {e}")
            return []
    
    def _reviews_from_next_data(self, page: Page) -> list[dict]:
        """Lit les avis de la page courante dans window.__NEXT_DATA__."""
        try:
            raw = page.evaluate(
                """() => {
                    const props = window.__NEXT_DATA__ && window.__NEXT_DATA__.props;
                    const pp = props && props.pageProps;
                    return (pp && pp.reviews) ? pp.reviews : [];
                }"""
            )
            return [self._normalize_review(r) for r in (raw or [])]
        except Exception as e:
            self.logger.debug(f"Erreur lecture __NEXT_DATA__ : {e}")
            return []

    def _extract_reviews_from_api_response(self, data: dict) -> list[dict]:
        """Parse les avis depuis la réponse API JSON."""
        try:
            page_props = data.get("pageProps") or {}

            # Réponse de redirection (308) : aucun avis dedans. L'ancien code la
            # traitait comme une page normale et concluait "0 avis".
            if "__N_REDIRECT" in page_props:
                self.logger.debug(
                    f"Redirection API vers {page_props['__N_REDIRECT']}"
                )
                return []

            return [
                self._normalize_review(r)
                for r in (page_props.get("reviews") or [])
            ]

        except Exception as e:
            self.logger.debug(f"Erreur parsing API response : {e}")
            return []

    @staticmethod
    def _normalize_review(review: dict) -> dict:
        """Aplatit un avis Trustpilot brut vers les champs du modèle Review.

        Le schéma réel imbrique l'auteur, la date et la vérification : les lire
        à plat (`author`, `createdAt`) renvoyait None pour les trois, d'où des
        avis sans auteur ni date réelle.
        """
        consumer = review.get("consumer") or {}
        dates = review.get("dates") or {}
        verification = (review.get("labels") or {}).get("verification") or {}

        return {
            "id": review.get("id"),
            "title": review.get("title"),
            "text": review.get("text"),
            "rating": review.get("rating"),
            "author": consumer.get("displayName"),
            "created_at": dates.get("publishedDate") or dates.get("experiencedDate"),
            "likes": review.get("likes"),
            "verified": verification.get("isVerified"),
        }
    
    def _parse_reviews(self, raw_reviews: list, company_name: str) -> list[Review]:
        """Parse les avis bruts en objets Review."""
        parsed = []
        seen: set[str] = set()

        for rv in raw_reviews:
            try:
                if not rv.get("id") or not rv.get("text"):
                    continue

                # Deux pages consécutives peuvent renvoyer le même avis.
                if rv["id"] in seen:
                    continue
                seen.add(rv["id"])

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
                    # Valeur réelle : tous les avis Trustpilot ne sont pas
                    # vérifiés (labels.verification.isVerified vaut souvent false).
                    verified=rv.get("verified"),
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed