"""
Collecteur GDELT DOC 2.0 — presse mondiale, multilingue, gratuite.

    GET https://api.gdeltproject.org/api/v2/doc/doc
        ?query="MTN" sourcecountry:NI&mode=artlist&format=json&timespan=7d

Ce que GDELT apporte que Google News RSS n'apporte pas :

* **le pays-source et la langue** de chaque article, rendus dans la réponse —
  les deux dimensions que le dashboard filtre et que le flux Google News ne
  fournit pas ;
* **100+ langues** indexées, là où le collecteur RSS interroge en français
  (`hl=fr`) et rate donc l'essentiel de la presse anglophone, lusophone et
  arabophone du périmètre ;
* **un ciblage par pays de publication**, qui permet d'interroger l'opérateur
  sous son nom courant (« MTN ») sans confondre ses dix-sept filiales.

DEUX PIÈGES, tous deux vérifiés en conditions réelles et traités ici :

1. **Le débit.** Au-delà d'une requête toutes les ~5 s, l'API répond HTTP 200
   avec un corps en TEXTE BRUT (« Please limit requests to one every 5
   seconds »). Un client qui ferait confiance au code HTTP enregistrerait
   « 0 article » au lieu d'une erreur : la panne serait totalement silencieuse.
   D'où le limiteur séquentiel et la vérification que la réponse est bien du
   JSON.

2. **Les codes pays.** `sourcecountry:` attend du FIPS 10-4, pas de l'ISO
   3166 : l'Afrique du Sud est `SF`, pas `ZA` — et `ZA` y désigne la Zambie.
   La correspondance vit dans `countries.py`, avec la liste des faux amis.
"""

import logging
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import requests

from reviews.collectors.base import BaseCollector, CollectorBackoff
from reviews.collectors.countries import fips_code
from reviews.collectors.targets import gdelt_targets
from reviews.config import get_settings
from reviews.domain.models import Review, SourceEnum
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class GDELTScraper(BaseCollector):
    """Scraper pour l'API GDELT DOC 2.0."""

    def __init__(self):
        settings = get_settings()
        self.cfg = settings.gdelt
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=self.cfg.collector_timeout,
        )
        super().__init__("gdelt", retry_config)
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; telecom-sentiment/1.0)"}
        )
        self._request_timeout = max(settings.scraping.request_timeout, 45)
        self._last_call: float = 0.0
        #: Rallonge du délai, apprise en cours de route (voir `_penaliser`).
        self._penalite: float = 0.0

    #: Plafond de la rallonge. Au-delà, insister ne sert plus à rien : mieux
    #: vaut rendre la main et reprendre les filiales au run suivant.
    BACKOFF_MAX = 60.0

    #: Marqueurs d'un refus DE DÉBIT dans un corps en texte brut. Tout autre
    #: message non-JSON décrit un défaut de la requête elle-même, que ni
    #: l'attente ni la pénalité ne corrigeront.
    _MOTIFS_DEBIT = ("limit requests", "rate limit", "too many requests")

    def collect(self) -> list[Review]:
        targets = gdelt_targets()
        self.logger.info(
            "%d filiale(s) à interroger, %.1f s entre deux appels : ~%.0f min",
            len(targets), self.cfg.min_interval_seconds,
            len(targets) * self.cfg.min_interval_seconds / 60,
        )

        all_reviews: list[Review] = []
        seen: set[str] = set()
        rejets = 0

        for target in targets:
            try:
                articles = self._fetch_avec_repli(target)
            except _RateLimited:
                # Ce n'est pas une panne de l'API mais notre propre débit :
                # la filiale sera reprise au run suivant, avec une pénalité
                # désormais plus élevée.
                rejets += 1
                continue
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "Erreur GDELT sur %s : %s", target["name"], e
                )
                continue

            for article in articles:
                # Un même article ressort sur plusieurs filiales d'un même
                # groupe ; on le garde une fois par filiale (l'attribution
                # diffère) mais jamais deux fois pour la même.
                key = f"{article.company}:{article.id}"
                if key in seen:
                    continue
                seen.add(key)
                all_reviews.append(article)

        if rejets:
            self.logger.warning(
                "%d filiale(s) rejetée(s) pour dépassement de débit GDELT — "
                "augmenter GDELT_MIN_INTERVAL_SECONDS si cela persiste", rejets
            )
        # Toutes les cibles rejetées = le limiteur est mal réglé ou l'API a
        # changé de politique. C'est une panne, pas une absence d'actualité.
        if targets and rejets == len(targets):
            raise RuntimeError(
                "Toutes les requêtes GDELT ont été rejetées pour dépassement "
                "de débit : augmenter GDELT_MIN_INTERVAL_SECONDS"
            )

        self.logger.info("%d article(s) GDELT collecté(s)", len(all_reviews))
        return all_reviews

    # ------------------------------------------------------------------
    # MODE UNITÉS — une filiale = un job (collection_jobs)
    #
    # POURQUOI CE COLLECTEUR EN AVAIT BESOIN PLUS QUE TOUT AUTRE
    #   Il n'avait JAMAIS enregistré une seule ligne dans `run_metrics`, depuis
    #   toujours : sixième du cycle, il était derrière Google Maps et n'était
    #   jamais atteint. Sa première vraie occasion a montré pourquoi il n'aurait
    #   pas abouti non plus : GDELT bride, le délai monte jusqu'au plafond de
    #   60 s, et 132 filiales à ce rythme demandent ~145 min pour un budget de
    #   30. Élargir le budget n'aurait rien réglé — ce n'est pas nous qui sommes
    #   lents, c'est la source qui refuse d'aller plus vite.
    #
    #   Découpé, chaque passage interroge les filiales qu'il peut dans son
    #   budget, rend la main dès que la source bride, et le reste attend en
    #   file. Aucune filiale n'est perdue, la couverture s'étale sur quelques
    #   passages.
    # ------------------------------------------------------------------

    SUPPORTS_UNITS = True

    def plan_units(self) -> list[dict]:
        """Une unité par filiale interrogée."""
        return [
            {
                "job_key": f"{t['name']}|{t['term']}",
                "company": t["name"],
                "country": (t.get("iso2") or "").upper() or None,
                "location": (t.get("iso2") or "").upper() or None,
                "query": t["term"],
            }
            for t in gdelt_targets()
        ]

    def collect_unit(self, job, save_cursor) -> list[Review]:
        """Interroge UNE filiale.

        Pas de curseur : une requête GDELT est atomique — elle rend sa page
        d'articles ou rien. Le grain de reprise est donc l'unité elle-même, et
        le curseur n'aurait rien de plus fin à décrire.
        """
        # `iso2` en MAJUSCULES, comme le rend `gdelt_targets` : `_build_query`
        # le passe tel quel à `fips_code`, qui ne trouverait rien en minuscules
        # et interrogerait alors sans filtre pays — donc attribuerait à cette
        # filiale des articles d'un autre pays, en silence.
        target = {
            "name": job.company,
            "term": job.query,
            "iso2": (job.country or "").upper(),
        }
        try:
            return self._fetch_avec_repli(target)
        except _RateLimited as e:
            # La seconde chance a échoué : la fenêtre de bridage est ouverte et
            # les filiales suivantes seraient refusées elles aussi. On rend la
            # main plutôt que d'user les tentatives de 132 unités saines.
            raise CollectorBackoff(
                f"débit GDELT dépassé (délai courant {self._delai_courant():.0f} s)"
            ) from e

    def _fetch_avec_repli(self, target: dict) -> list[Review]:
        """Interroge une filiale, avec une seconde chance après ralentissement.

        Sans cela, une fenêtre de bridage fait échouer TOUTES les filiales
        suivantes : le délai fixe ne suffit plus, et le collecteur enchaîne
        132 refus en récoltant zéro article. La pénalité s'apprend donc en
        cours de route — elle double à chaque refus, retombe progressivement
        quand ça passe — et le collecteur se règle tout seul sur ce que l'API
        tolère à cet instant.
        """
        try:
            articles = self._fetch_target(target)
        except _RateLimited:
            self._penaliser()
            articles = self._fetch_target(target)   # une seule seconde chance
        self._recompenser()
        return articles

    def _penaliser(self) -> None:
        self._penalite = min(
            max(self._penalite * 2, self.cfg.min_interval_seconds),
            self.BACKOFF_MAX,
        )
        self.logger.info(
            "Débit GDELT dépassé : délai porté à %.1f s", self._delai_courant()
        )
        time.sleep(self._penalite)

    def _recompenser(self) -> None:
        """Relâche la pénalité après un appel réussi, sans repartir de zéro."""
        if self._penalite:
            self._penalite = max(0.0, self._penalite / 2)

    def _delai_courant(self) -> float:
        return self.cfg.min_interval_seconds + self._penalite

    def _fetch_target(self, target: dict) -> list[Review]:
        """Articles d'une filiale, via une requête GDELT."""
        query = self._build_query(target)
        payload = self._call_api(query)
        articles = payload.get("articles") or []

        cutoff = self.cutoff_for_key(target["name"], SourceEnum.GDELT.value)
        reviews = []
        for art in articles:
            created = self._parse_seendate(art.get("seendate"))
            if self.is_already_known(created, cutoff):
                continue
            review = self._to_review(art, target["name"], created)
            if review is not None:
                reviews.append(review)

        if reviews:
            self.logger.debug("%s : %d article(s)", target["name"], len(reviews))
        return reviews

    def _build_query(self, target: dict) -> str:
        """Requête GDELT pour une filiale.

        Sans code FIPS connu, on interroge SANS filtre pays : une requête large
        ramène du bruit, un code pays inventé ramènerait les articles d'un
        autre pays et les attribuerait à cette filiale. Le bruit se voit, la
        mauvaise attribution non.
        """
        term = f'"{target["term"]}"'
        fips = fips_code(target["iso2"])
        if not fips:
            self.logger.warning(
                "Pas de code GDELT pour %s (ISO2 %s) : requête sans filtre pays",
                target["name"], target["iso2"],
            )
            return term
        return f"{term} sourcecountry:{fips}"

    def _call_api(self, query: str) -> dict:
        """Appelle l'API en respectant le débit imposé.

        La limitation est appliquée AVANT l'appel et sur l'instance : les
        requêtes sont donc strictement séquentielles. Paralléliser ce
        collecteur — comme le fait celui de Google News — ferait rejeter la
        quasi-totalité des appels.
        """
        delai = self._delai_courant()
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < delai:
            time.sleep(delai - elapsed)

        url = (
            f"{self.cfg.base_url}?query={urllib.parse.quote(query)}"
            f"&mode=artlist&maxrecords={self.cfg.max_records}"
            f"&format=json&timespan={self.cfg.timespan}"
        )
        response = self.session.get(url, timeout=self._request_timeout)
        self._last_call = time.monotonic()

        # GDELT signale le dépassement de débit de DEUX façons distinctes, et
        # il faut traiter les deux :
        #   * HTTP 429, la forme classique ;
        #   * HTTP 200 avec un corps en TEXTE BRUT (« Please limit requests to
        #     one every 5 seconds »), la forme piégeuse — un client qui se fie
        #     au code HTTP y lit « 0 article » et croit à une absence
        #     d'actualité alors qu'il n'a rien pu interroger.
        if response.status_code == 429:
            raise _RateLimited("HTTP 429")
        response.raise_for_status()

        body = response.text.lstrip()
        if not body.startswith("{"):
            extrait = body[:150]
            if any(m in extrait.lower() for m in self._MOTIFS_DEBIT):
                raise _RateLimited(extrait)
            # REFUS PROPRE À CETTE REQUÊTE, et non au débit.
            #
            # Mesuré : GDELT répond « The specified phrase is too short. » pour
            # `"MTN"` — un terme de trois lettres. Tout corps non-JSON était
            # jusqu'ici lu comme un bridage : le collecteur pénalisait le débit,
            # attendait, réessayait, et depuis le passage en unités déclenchait
            # un `CollectorBackoff` qui arrêtait le passage entier. Une poignée
            # d'opérateurs à nom court (MTN, Glo, e&, WE, BTC) pouvait donc
            # bloquer TOUS les passages, indéfiniment, sans que rien ne le dise
            # — le message de l'API était écrasé par un diagnostic de débit.
            #
            # Remonté comme une erreur ordinaire, le refus fait échouer LA seule
            # unité concernée. Son message atterrit dans
            # `collection_jobs.error_message`, où il se lit tel quel.
            raise _RequeteRefusee(extrait)
        return response.json()

    @staticmethod
    def _parse_seendate(raw: Optional[str]) -> Optional[datetime]:
        """« 20260803T000000Z » → datetime UTC."""
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            return None

    def _to_review(self, art: dict, company: str,
                   created: Optional[datetime]) -> Optional[Review]:
        """Un article GDELT → un Review (sans note : c'est de la presse)."""
        title = (art.get("title") or "").strip()
        if not title:
            return None
        url = art.get("url") or ""

        # GDELT ne rend que le TITRE, jamais le corps de l'article. Le pays et
        # la langue sont donc joints au texte analysé : ce sont les seules
        # métadonnées disponibles, et les perdre reviendrait à jeter ce qui
        # fait l'intérêt de cette source par rapport à Google News.
        contexte = " · ".join(
            p for p in (art.get("sourcecountry"), art.get("domain")) if p
        )
        text = f"{title} ({contexte})" if contexte else title

        try:
            return Review(
                id=f"gdelt_{url or title}"[:255],
                company=company,
                source=SourceEnum.GDELT,
                title=title,
                text=text[:5000],
                rating=None,          # un article n'a pas de note
                created_at=created or datetime.now(timezone.utc),
            )
        except Exception as e:  # noqa: BLE001
            self.logger.debug("Article GDELT ignoré : %s", e)
            return None


class _RateLimited(RuntimeError):
    """L'API a refusé l'appel pour cause de débit trop élevé.

    Transitoire, et étranger à la requête : attendre suffit. C'est le seul cas
    qui justifie de pénaliser le débit, puis de rendre la main (voir
    `CollectorBackoff`).
    """


class _RequeteRefusee(RuntimeError):
    """L'API a refusé CETTE requête, et attendre n'y changera rien.

    Mesuré : « The specified phrase is too short. » pour un terme de trois
    lettres. Confondre ce refus avec un bridage faisait pénaliser le débit et
    interrompre le passage pour un défaut qui ne concernait qu'une filiale —
    et le message de l'API, seul à dire la vraie cause, était perdu.
    """
