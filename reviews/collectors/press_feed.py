"""
Collecteur de presse tech africaine spécialisée (flux RSS natifs).

Le principe est INVERSE de celui du collecteur Google News. Là-bas, on part de
la filiale et on demande au moteur « que dit-on de MTN Nigeria ? » — soit
132 filiales × 10 mots-clés = plus d'un millier de requêtes, dont la moitié
ramène les mêmes articles. Ici, on lit dix flux généralistes une seule fois,
puis on reconnaît dans chaque article les filiales réellement citées. Dix
requêtes au lieu de mille trois cents, et une presse choisie pour sa pertinence
plutôt que subie via un agrégateur.

Les titres retenus (voir `PressFeedConfig.feeds`) ont été vérifiés actifs le
4 août 2026 : chacun publiait un article de moins de sept jours. Deux absents
notables, Techpoint Africa et MyBroadband, répondent 403 derrière Cloudflare et
demanderaient un navigateur — hors de propos pour un collecteur HTTP.

Le rattachement article → filiale est la partie délicate : voir
`targets.press_matchers()`, qui exige un marqueur de pays dès qu'un opérateur
est présent dans plusieurs pays.
"""

import calendar
import concurrent.futures
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import press_matchers
from reviews.config import get_settings
from reviews.domain.models import Review, SourceEnum
from reviews.domain.press_attribution import normalize
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


#: La normalisation vit désormais dans le domaine : trois appelants la
#: partagent (ce collecteur, `rss_feed`, et la ré-attribution du corpus), et
#: une règle de reconnaissance dupliquée finit par diverger — produisant des
#: articles rangés différemment selon le chemin qui les a fait entrer.
_normalize = normalize


class PressFeedScraper(BaseCollector):
    """Scraper des flux RSS de la presse tech africaine."""

    def __init__(self):
        settings = get_settings()
        self.cfg = settings.press_feed
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=self.cfg.collector_timeout,
        )
        super().__init__("press_feed", retry_config)
        self.session = requests.Session()
        # User-agent de navigateur : plusieurs de ces titres répondent 403 à un
        # client qui s'annonce comme un robot, alors que leur flux est public.
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })
        self._matchers = self._compile_matchers()

    def _compile_matchers(self) -> dict[str, dict]:
        """Pré-compile les expressions de reconnaissance, groupées par opérateur.

        `\\b` autour du nom d'opérateur : sans lui, « Orange » se déclencherait
        sur « oranges » et « MTN » sur n'importe quelle suite de lettres le
        contenant.

        Le groupement PAR OPÉRATEUR n'est pas qu'une optimisation (une seule
        recherche de « MTN » au lieu de dix-sept) : c'est ce qui permet de
        savoir si l'article nomme UN pays de cet opérateur, information dont
        `_match_entries` a besoin pour arbitrer entre le texte et le pays
        d'édition du flux.
        """
        groupes: dict[str, dict] = {}
        for m in press_matchers():
            groupe = groupes.setdefault(m["operator"], {
                "operator": re.compile(
                    r"\b" + re.escape(_normalize(m["operator"])) + r"\b"
                ),
                "filiales": [],
            })
            groupe["filiales"].append({
                "name": m["name"],
                "iso2": m["iso2"],
                "countries": [
                    re.compile(r"\b" + re.escape(_normalize(c)) + r"\b")
                    for c in m["country_markers"]
                ],
            })
        return groupes

    def collect(self) -> list[Review]:
        feeds = self.cfg.feeds_list()
        self.logger.info(
            "%d flux de presse, %d opérateur(s) / %d filiale(s) reconnaissable(s)",
            len(feeds), len(self._matchers),
            sum(len(g["filiales"]) for g in self._matchers.values()),
        )
        if not feeds or not self._matchers:
            return []

        entries: list[tuple[str, Optional[str], dict]] = []
        echecs = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.cfg.max_workers
        ) as pool:
            futures = {
                pool.submit(self._fetch_feed, url, iso2): url for url, iso2 in feeds
            }
            for future in concurrent.futures.as_completed(futures):
                url = futures[future]
                try:
                    fetched = future.result()
                    if not fetched:
                        echecs += 1
                    entries.extend(fetched)
                except Exception as e:  # noqa: BLE001
                    echecs += 1
                    self.logger.warning("Flux injoignable (%s) : %s", url, e)

        if feeds and echecs == len(feeds):
            raise RuntimeError(
                f"Les {echecs} flux de presse ont échoué : réseau coupé ou "
                f"tous les titres hors service"
            )

        reviews = self._match_entries(entries)
        self.logger.info(
            "%d article(s) lu(s) sur %d flux → %d rattaché(s) à une filiale",
            len(entries), len(feeds), len(reviews),
        )
        return reviews

    def _fetch_feed(self, url: str,
                    iso2: Optional[str]) -> list[tuple[str, Optional[str], dict]]:
        """Articles d'un flux, sous forme (titre_du_flux, iso2_du_titre, entrée)."""
        response = self.session.get(url, timeout=self.cfg.fetch_timeout)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        titre = (feed.feed.get("title") if hasattr(feed, "feed") else None) or url
        self.logger.debug("%s : %d entrée(s)", titre, len(feed.entries))
        return [(titre, iso2, entry) for entry in feed.entries]

    def _match_entries(
        self, entries: list[tuple[str, Optional[str], dict]]
    ) -> list[Review]:
        """Rattache chaque article aux filiales qu'il cite réellement.

        Un article peut légitimement concerner PLUSIEURS filiales (« Orange et
        MTN sanctionnés en Côte d'Ivoire ») : on émet alors un avis par
        filiale. La déduplication en base porte sur (entreprise, source, texte),
        ces lignes ne sont donc pas des doublons.
        """
        reviews: list[Review] = []
        vus: set[str] = set()

        for feed_title, feed_iso2, entry in entries:
            titre = (getattr(entry, "title", "") or "").strip()
            resume = self._entry_summary(entry)
            haystack = _normalize(f"{titre} {resume}")
            if not titre:
                continue

            for groupe in self._matchers.values():
                if not groupe["operator"].search(haystack):
                    continue

                for company in self._filiales_citees(groupe, haystack, feed_iso2):
                    article_id = (
                        getattr(entry, "id", None)
                        or getattr(entry, "link", "")
                        or titre
                    )
                    key = f"{company}:{article_id}"
                    if key in vus:
                        continue
                    vus.add(key)

                    review = self._to_review(
                        entry, company, titre, resume, feed_title, article_id
                    )
                    if review is not None:
                        reviews.append(review)

        return reviews

    @staticmethod
    def _filiales_citees(groupe: dict, haystack: str,
                         feed_iso2: Optional[str]) -> list[str]:
        """Filiales d'un opérateur réellement concernées par cet article.

        LE TEXTE PRIME TOUJOURS SUR LE PAYS D'ÉDITION DU FLUX.

        Vérifié en production : « MTN Nigeria's growth engine stalled », publié
        par TechCentral (titre sud-africain), était rattaché À LA FOIS à
        MTN Nigeria — correctement, le pays est dans le titre — et à
        MTN South Africa, par le seul pays d'édition du flux. Une filiale se
        voyait donc imputer l'actualité d'une autre, ce qui est exactement la
        faute que le marqueur de pays existe pour empêcher.

        D'où la règle : dès que l'article nomme UN pays où cet opérateur est
        présent, seul le texte décide. Le pays d'édition ne sert plus que de
        repli, quand l'article ne nomme aucun pays du périmètre de l'opérateur.
        """
        filiales = groupe["filiales"]
        # Mono-pays : aucun marqueur, donc aucune ambiguïté à lever.
        mono = [f["name"] for f in filiales if not f["countries"]]
        if mono:
            return mono

        cites = [
            f["name"] for f in filiales
            if any(rx.search(haystack) for rx in f["countries"])
        ]
        if cites:
            return cites
        if feed_iso2:
            return [f["name"] for f in filiales if f["iso2"] == feed_iso2]
        return []

    @staticmethod
    def _entry_summary(entry) -> str:
        """Résumé d'un article, débarrassé de son HTML."""
        raw = getattr(entry, "summary", "") or ""
        if not raw:
            return ""
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _published(entry) -> Optional[datetime]:
        """Date de publication en UTC, ou None.

        `published_parsed` est un struct_time déjà normalisé UTC par
        feedparser ; le champ texte `published` est en RFC 822, que Pydantic
        refuse de parser directement.
        """
        parsed = (getattr(entry, "published_parsed", None)
                  or getattr(entry, "updated_parsed", None))
        if not parsed:
            return None
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
        except (ValueError, OverflowError, TypeError):
            return None

    def _to_review(self, entry, company: str, titre: str, resume: str,
                   feed_title: str, article_id: str) -> Optional[Review]:
        published = self._published(entry)
        cutoff = self.cutoff_for_key(company, SourceEnum.PRESS_FEED.value)
        if self.is_already_known(published, cutoff):
            return None

        # Le titre est repris dans le texte : sur beaucoup de flux le résumé
        # est vide ou tronqué, et l'analyse de sentiment n'aurait alors rien à
        # se mettre sous la dent.
        text = f"{titre}. {resume}".strip() if resume else titre
        try:
            return Review(
                id=f"press_{article_id}"[:255],
                company=company,
                source=SourceEnum.PRESS_FEED,
                title=titre[:500],
                text=f"{text} [{feed_title}]"[:5000],
                rating=None,          # un article n'a pas de note
                created_at=published or datetime.now(timezone.utc),
            )
        except Exception as e:  # noqa: BLE001
            self.logger.debug("Article de presse ignoré : %s", e)
            return None
