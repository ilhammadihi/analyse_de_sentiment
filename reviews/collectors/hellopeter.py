"""
Collecteur HelloPeter — plateforme d'avis n°1 en Afrique du Sud.

API JSON publique et sans authentification :

    GET https://api.hellopeter.com/consumer/business/{slug}/reviews?page=N

Le gisement le plus dense du périmètre : les quatre opérateurs sud-africains y
cumulent plus de 400 000 avis notés, avec titre, texte, date et auteur — à
comparer aux quelques milliers d'avis d'applications que ramènent les stores
pour l'ensemble du continent.

Deux conséquences, toutes deux traitées ici :

1. La source est TRIÉE du plus récent au plus ancien, donc l'arrêt anticipé
   incrémental de BaseCollector s'y applique pleinement.
2. Elle est aussi capable de noyer le corpus. Le plafond de pages
   (`HELLOPETER_MAX_PAGES`) n'est pas une optimisation réseau, c'est une
   décision méthodologique : voir le commentaire de `HelloPeterConfig`.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import hellopeter_companies
from reviews.config import get_settings
from reviews.domain.models import Review, SourceEnum
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class HelloPeterScraper(BaseCollector):
    """Scraper pour HelloPeter (Afrique du Sud)."""

    #: L'API renvoie 11 avis par page et l'impose ; `per_page` n'est pas
    #: paramétrable côté client. Constante seulement pour la journalisation.
    PAGE_SIZE = 11

    def __init__(self):
        settings = get_settings()
        self.cfg = settings.hellopeter
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=self.cfg.collector_timeout,
        )
        super().__init__("hellopeter", retry_config)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; telecom-sentiment/1.0)",
            "Accept": "application/json",
        })
        self._request_timeout = settings.scraping.request_timeout

    def collect(self) -> list[Review]:
        companies = hellopeter_companies()
        self.logger.info("%d entreprise(s) HelloPeter déclarée(s)", len(companies))
        if not companies:
            return []

        all_reviews: list[Review] = []
        failures = 0
        for company in companies:
            try:
                reviews = self._fetch_company(company)
                all_reviews.extend(reviews)
                self.logger.info(
                    "%s : %d avis retenus", company["name"], len(reviews)
                )
            except Exception as e:  # noqa: BLE001
                failures += 1
                self.logger.error(
                    "Erreur sur %s (%s) : %s", company["name"], company["slug"], e
                )
                continue

        # Toutes les cibles en échec = l'API est tombée ou a changé, pas une
        # absence d'avis. On lève pour que le run soit « failed » et déclenche
        # une alerte, au lieu d'un « succès avec 0 avis » qui passerait inaperçu.
        if companies and failures == len(companies):
            raise RuntimeError(
                f"Les {failures} entreprises HelloPeter ont échoué : "
                f"API injoignable ou format modifié"
            )
        return all_reviews

    def _fetch_company(self, company: dict) -> list[Review]:
        """Pagine les avis d'une entreprise, du plus récent au plus ancien."""
        slug, name = company["slug"], company["name"]
        cutoff = self.cutoff_for_key(name, SourceEnum.HELLOPETER.value)
        collected: list[Review] = []

        for page in range(1, self.cfg.max_pages + 1):
            payload = self._get_page(slug, page)
            if payload is None:
                break
            rows = payload.get("data") or []
            if not rows:
                break

            dates = [self._parse_date(r.get("created_at")) for r in rows]
            # Arrêt anticipé : la page entière est déjà en base, et la source
            # étant triée par date décroissante, tout ce qui suit l'est aussi.
            if self.batch_fully_known(dates, cutoff):
                self.logger.debug(
                    "%s : page %d entièrement connue, pagination interrompue",
                    name, page,
                )
                break

            for row, created in zip(rows, dates):
                review = self._to_review(row, name, created)
                if review is not None:
                    collected.append(review)

            last_page = payload.get("last_page")
            if last_page and page >= last_page:
                break
            time.sleep(self.cfg.request_delay)

        return collected

    def _get_page(self, slug: str, page: int) -> Optional[dict]:
        """Une page de l'API. None si la fiche n'existe pas (404).

        Le 404 est un cas NORMAL, pas une panne : un slug peut disparaître ou
        avoir été mal saisi en configuration. On le distingue donc d'une erreur
        réseau, qui doit remonter pour déclencher le retry.
        """
        url = f"{self.cfg.base_url}/{slug}/reviews"
        response = self.session.get(
            url, params={"page": page}, timeout=self._request_timeout
        )
        if response.status_code == 404:
            self.logger.warning("Fiche HelloPeter introuvable : %s", slug)
            return None
        # NE PAS tester `status_code == 200` : derrière son Cloudflare, l'API
        # répond 202 Accepted avec le JSON complet et valide. Un contrôle
        # d'égalité stricte rejetterait donc 100 % des réponses correctes.
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_date(raw: Optional[str]) -> Optional[datetime]:
        """« 2026-08-04 15:51:48 » → datetime UTC. None si illisible.

        L'API ne précise aucun fuseau ; les dates sont en heure sud-africaine
        (UTC+2). On les marque UTC plutôt que de les laisser naïves : le reste
        du pipeline compare des dates entre sources, et une date sans fuseau
        s'y compare mal. Le décalage de deux heures est sans effet sur des
        agrégats journaliers.
        """
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            return None

    def _to_review(self, row: dict, company: str,
                   created: Optional[datetime]) -> Optional[Review]:
        """Une ligne d'API → un Review. None si inexploitable."""
        text = (row.get("review_content") or "").strip()
        if not text:
            return None  # Un avis sans texte n'apporte rien à l'analyse.

        review_id = row.get("id")
        if review_id is None:
            return None

        rating = row.get("review_rating")
        try:
            rating = int(rating) if rating is not None else None
        except (TypeError, ValueError):
            rating = None
        if rating is not None and not 1 <= rating <= 5:
            rating = None

        try:
            return Review(
                id=f"hellopeter_{review_id}",
                company=company,
                source=SourceEnum.HELLOPETER,
                title=row.get("review_title"),
                # Le modèle plafonne le texte à 5 000 caractères et les récits
                # de litige HelloPeter les dépassent régulièrement : on tronque
                # ici plutôt que de laisser la validation rejeter l'avis entier.
                text=text[:5000],
                rating=rating,
                created_at=created,
                author=(row.get("authorDisplayName") or row.get("author") or None),
            )
        except Exception as e:  # noqa: BLE001
            self.logger.debug("Avis HelloPeter ignoré (%s) : %s", review_id, e)
            return None
