"""
Collecteur Reddit — la parole spontanée des abonnés.

    GET https://www.reddit.com/r/{subreddit}/search.rss
        ?q=Vodacom OR MTN OR "Cell C"&restrict_sr=1&sort=new&t=month

CE QUE CETTE SOURCE APPORTE
    Les huit autres recueillent une parole SOLLICITÉE (noter une application,
    déposer une plainte) ou RAPPORTÉE (la presse). Reddit est la seule où
    l'abonné parle de lui-même, à ses pairs, sans formulaire ni modération
    commerciale : pannes en cours, hausses de tarif, contournements — souvent
    avant qu'un article ne les relaie.

TROIS CONTRAINTES, TOUTES MESURÉES LE 7 AOÛT 2026, TOUTES TRAITÉES ICI

1.  **L'API JSON est fermée.** `search.json`, `new.json` et `old.reddit.com`
    répondent HTTP 403 « Blocked » sans authentification, même avec un en-tête
    de navigateur. Seuls les flux `.rss` répondent 200. Le format n'est donc
    pas un choix.

2.  **UNE requête par minute.** Reddit le dit lui-même dans ses en-têtes :
    `x-ratelimit-remaining: 0.0` juste APRÈS un appel réussi. Mesuré : à 15 s
    d'espacement, 4 requêtes sur 5 sont refusées ; à 65 s, 3 sur 3 passent.
    C'est deux ordres de grandeur sous GDELT, et cela commande toute la
    conception : les appels sont strictement séquentiels et le collecteur
    interroge des PAYS, pas des filiales — 20 requêtes au lieu de 135.

3.  **Un forum généraliste parle d'autre chose.** Dans r/Senegal, « orange »
    est aussi une couleur ; « free » un adjectif. Le nom de l'opérateur ne
    suffit donc pas : on exige en plus du vocabulaire télécom, via le contrôle
    déjà éprouvé de `press_relevance.est_pertinent`.

LE RATTACHEMENT FIL → FILIALE
    Le subreddit pays joue exactement le rôle de l'annotation `|ISO2` des flux
    de presse : dans r/Nigeria, « MTN » désigne forcément MTN Nigeria. On
    réutilise donc `press_attribution.subsidiaries_named()`, qui porte déjà la
    règle du marqueur de pays et sa hiérarchie « le texte prime sur le pays du
    support ». Une quatrième copie de cette règle finirait par diverger.
"""

import calendar
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import press_matchers, reddit_targets
from reviews.config import get_settings
from reviews.domain.models import Review, SourceEnum
from reviews.domain.press_attribution import compile_matchers, normalize, subsidiaries_named
from reviews.domain.press_relevance import est_pertinent
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class RedditScraper(BaseCollector):
    """Scraper des flux Atom de recherche Reddit, un par subreddit pays."""

    #: Taille de page OBSERVÉE du flux Atom : les subreddits actifs sondés le
    #: 7 août 2026 ont tous rendu exactement 25 entrées, les petits moins.
    #:
    #: Sert uniquement de seuil de journalisation : atteindre ce compte signifie
    #: que le flux est plafonné et que des fils plus anciens n'ont pas été vus.
    #: Le dire dans le journal plutôt que de le taire permet de poser la
    #: question « faut-il resserrer la cadence ? » sur des faits.
    PAGE_SIZE = 25

    #: Plafond de la rallonge de débit, en secondes. Au-delà, insister ne sert
    #: plus à rien : mieux vaut rendre la main et reprendre au run suivant.
    BACKOFF_MAX = 300.0

    def __init__(self):
        settings = get_settings()
        self.cfg = settings.reddit
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=self.cfg.collector_timeout,
        )
        super().__init__("reddit", retry_config)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; telecom-sentiment/1.0)",
            "Accept": "application/atom+xml, application/xml, text/xml, */*",
        })
        self._request_timeout = max(settings.scraping.request_timeout, 30)
        self._last_call: float = 0.0
        #: Rallonge apprise en cours de route (voir `_penaliser`).
        self._penalite: float = 0.0
        self._matchers = compile_matchers(press_matchers())

    # -- Orchestration -------------------------------------------------------

    def collect(self) -> list[Review]:
        targets = reddit_targets(self.cfg.subreddits_list())
        if not targets:
            self.logger.warning(
                "Aucun subreddit configuré : renseigner REDDIT_SUBREDDITS"
            )
            return []
        if not self._matchers:
            self.logger.warning("Aucune filiale reconnaissable : rien à rattacher")
            return []

        self.logger.info(
            "%d subreddit(s), %.0f s entre deux appels : ~%.0f min",
            len(targets), self.cfg.min_interval_seconds,
            len(targets) * self.cfg.min_interval_seconds / 60,
        )

        all_reviews: list[Review] = []
        seen: set[str] = set()
        rejets = 0

        for target in targets:
            try:
                reviews = self._fetch_avec_repli(target)
            except _RateLimited:
                # Notre propre débit, pas une panne de Reddit : le subreddit
                # sera repris au run suivant, avec une pénalité plus élevée.
                rejets += 1
                continue
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "Erreur Reddit sur r/%s : %s", target["subreddit"], e
                )
                continue

            for review in reviews:
                # Un même fil peut concerner plusieurs filiales (« Airtel et
                # Safaricom en panne ») : on le garde une fois PAR filiale, mais
                # jamais deux fois pour la même.
                key = f"{review.company}:{review.id}"
                if key in seen:
                    continue
                seen.add(key)
                all_reviews.append(review)

        if rejets:
            self.logger.warning(
                "%d subreddit(s) rejeté(s) pour dépassement de débit — "
                "augmenter REDDIT_MIN_INTERVAL_SECONDS si cela persiste", rejets
            )
        # Tout rejeté = le limiteur est mal réglé ou Reddit a durci sa
        # politique. C'est une panne, pas une absence de discussion : on lève
        # pour que le run soit « failed » et déclenche une alerte, plutôt que
        # d'enregistrer un « succès avec 0 avis » qui passerait inaperçu.
        if targets and rejets == len(targets):
            raise RuntimeError(
                "Toutes les requêtes Reddit ont été rejetées pour dépassement "
                "de débit : augmenter REDDIT_MIN_INTERVAL_SECONDS"
            )

        self.logger.info(
            "%d fil(s) Reddit retenu(s) sur %d subreddit(s)",
            len(all_reviews), len(targets),
        )
        return all_reviews

    # -- Débit ---------------------------------------------------------------

    def _fetch_avec_repli(self, target: dict) -> list[Review]:
        """Interroge un subreddit, avec une seconde chance après ralentissement.

        Même mécanique que GDELT, à une différence près qui la rend plus juste :
        Reddit ANNONCE le délai restant dans `x-ratelimit-reset`. On l'attend
        exactement, au lieu de doubler à l'aveugle.
        """
        try:
            reviews = self._fetch_target(target)
        except _RateLimited as refus:
            self._penaliser(refus.reset_seconds)
            reviews = self._fetch_target(target)   # une seule seconde chance
        # Récompense APRÈS un succès, d'où qu'il vienne — pas seulement après un
        # réessai. Ne relâcher la pénalité que sur le chemin de rattrapage la
        # rendrait définitive : un unique refus en début de run ralentirait tous
        # les subreddits suivants, y compris ceux qui passent du premier coup.
        self._recompenser()
        return reviews

    def _penaliser(self, reset_seconds: Optional[float]) -> None:
        """Allonge le délai, en suivant Reddit quand il donne un chiffre."""
        if reset_seconds and reset_seconds > 0:
            # Reddit dit combien de temps il reste : c'est plus fiable que
            # n'importe quelle heuristique de notre côté.
            self._penalite = min(float(reset_seconds), self.BACKOFF_MAX)
        else:
            self._penalite = min(
                max(self._penalite * 2, self.cfg.min_interval_seconds),
                self.BACKOFF_MAX,
            )
        self.logger.info(
            "Débit Reddit dépassé : attente de %.0f s", self._penalite
        )
        time.sleep(self._penalite)

    def _recompenser(self) -> None:
        """Relâche la pénalité après un succès, sans repartir de zéro."""
        if self._penalite:
            self._penalite = max(0.0, self._penalite / 2)

    def _delai_courant(self) -> float:
        return self.cfg.min_interval_seconds + self._penalite

    def _call_feed(self, target: dict) -> bytes:
        """Récupère un flux en respectant le débit imposé.

        La limitation est appliquée AVANT l'appel et portée par l'instance : les
        requêtes sont donc strictement séquentielles. Paralléliser ce
        collecteur — comme le fait celui de Google News — ferait rejeter la
        quasi-totalité des appels, Reddit n'en tolérant qu'un par minute.
        """
        delai = self._delai_courant()
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < delai:
            time.sleep(delai - elapsed)

        url = (
            f"{self.cfg.base_url}/r/{target['subreddit']}/search.rss"
            f"?q={urllib.parse.quote(target['query'])}"
            f"&restrict_sr=1&sort=new&t={self.cfg.timespan}"
        )
        response = self.session.get(url, timeout=self._request_timeout)
        self._last_call = time.monotonic()

        if response.status_code == 429:
            raise _RateLimited(
                "HTTP 429", self._reset_hint(response.headers)
            )
        response.raise_for_status()

        # Reddit sert une page HTML d'erreur (« you are doing that too much »)
        # avec un code 200 dans certaines fenêtres de bridage. Un flux Atom
        # commence toujours par une déclaration XML : si ce n'est pas le cas,
        # c'est un refus déguisé, et le traiter comme un flux vide ferait
        # enregistrer « 0 fil » au lieu d'une erreur.
        body = response.content.lstrip()
        if not body.startswith(b"<?xml") and b"<feed" not in body[:400]:
            raise _RateLimited(
                body[:120].decode("utf-8", "replace"),
                self._reset_hint(response.headers),
            )
        return response.content

    @staticmethod
    def _reset_hint(headers) -> Optional[float]:
        """Secondes restantes avant réouverture, si Reddit les annonce."""
        for cle in ("x-ratelimit-reset", "retry-after"):
            brut = headers.get(cle)
            if brut:
                try:
                    return float(brut)
                except (TypeError, ValueError):
                    continue
        return None

    # -- Collecte d'un subreddit ---------------------------------------------

    def _fetch_target(self, target: dict) -> list[Review]:
        """Fils d'un subreddit, rattachés aux filiales qu'ils citent."""
        feed = feedparser.parse(self._call_feed(target))
        entries = feed.entries
        reviews: list[Review] = []
        hors_sujet = 0

        for entry in entries:
            issues, pertinent = self._to_reviews(entry, target)
            if not pertinent:
                hors_sujet += 1
            reviews.extend(issues)

        self.logger.debug(
            "r/%s : %d fil(s) lu(s), %d hors sujet, %d avis retenu(s)",
            target["subreddit"], len(entries), hors_sujet, len(reviews),
        )
        if len(entries) >= self.PAGE_SIZE:
            # Le flux est plafonné : il reste probablement des fils non vus.
            # Visible dans le journal plutôt que silencieux, pour que la
            # question « faut-il resserrer la cadence ? » puisse se poser.
            self.logger.info(
                "r/%s : flux plafonné à %d fils, des discussions plus "
                "anciennes n'ont pas été vues", target["subreddit"], self.PAGE_SIZE,
            )
        return reviews

    def _to_reviews(self, entry, target: dict) -> tuple[list[Review], bool]:
        """Un fil → un avis par filiale citée. Rend aussi « était-ce pertinent ».

        Un fil peut légitimement concerner PLUSIEURS filiales (« Airtel et
        Safaricom sont tous les deux en panne ») : on émet alors un avis par
        filiale. La déduplication en base porte sur (entreprise, source, texte),
        ces lignes ne sont donc pas des doublons.
        """
        titre = (getattr(entry, "title", "") or "").strip()
        if not titre:
            return [], False
        corps = self._body(entry)

        # Le vocabulaire d'abord, comme dans `press_attribution.classify` : un
        # fil qui ne parle pas de télécoms n'a pas à être rattaché, même s'il
        # nomme un opérateur.
        if self.cfg.require_telecom_terms and not est_pertinent(titre, corps):
            return [], False

        haystack = normalize(f"{titre} {corps}")
        # `feed_iso2` = le pays du subreddit. Il ne sert que de REPLI : si le
        # fil nomme lui-même un pays de l'opérateur, c'est celui-là qui gagne —
        # un fil de r/southafrica parlant de MTN Nigeria concerne MTN Nigeria.
        filiales = subsidiaries_named(self._matchers, haystack, target["iso2"])
        if not filiales:
            return [], True

        published = self._published(entry)
        auteur = self._author(entry)
        fil_id = (getattr(entry, "id", None) or getattr(entry, "link", "")
                  or titre)

        reviews = []
        for company in dict.fromkeys(filiales):      # dédoublonne, ordre gardé
            cutoff = self.cutoff_for_key(company, SourceEnum.REDDIT.value)
            if self.is_already_known(published, cutoff):
                continue
            review = self._build(
                fil_id, company, titre, corps, target, published, auteur
            )
            if review is not None:
                reviews.append(review)
        return reviews, True

    def _build(self, fil_id: str, company: str, titre: str, corps: str,
               target: dict, published: Optional[datetime],
               auteur: Optional[str]) -> Optional[Review]:
        # Le titre est repris dans le texte : sur Reddit, beaucoup de fils sont
        # de simples liens sans corps, et l'analyse de sentiment n'aurait alors
        # rien à se mettre sous la dent.
        texte = f"{titre}. {corps}".strip() if corps else titre
        try:
            return Review(
                id=f"reddit_{fil_id}"[:255],
                company=company,
                source=SourceEnum.REDDIT,
                title=titre[:500],
                # Le subreddit est joint au texte, comme GDELT y joint le pays
                # source : c'est la provenance de l'avis, et la perdre rendrait
                # les verbatims inexploitables pour qui veut remonter au fil.
                text=f"{texte} [r/{target['subreddit']}]"[:5000],
                # Un fil de forum n'a pas de note. C'est aussi ce qui interdit
                # d'en tirer un taux comparable : voir la migration 011.
                rating=None,
                created_at=published or datetime.now(timezone.utc),
                author=auteur,
            )
        except Exception as e:  # noqa: BLE001
            self.logger.debug("Fil Reddit ignoré (%s) : %s", fil_id, e)
            return None

    # -- Extraction ----------------------------------------------------------

    #: Marqueur de début du pied de page ajouté par Reddit à chaque message.
    _PIED = "submitted by"

    @classmethod
    def _body(cls, entry) -> str:
        """Corps du message, sans le pied de page ajouté par Reddit.

        Le `<content>` d'un flux Reddit n'est pas que le message : il se termine
        par « submitted by /u/x [link] [comments] ». Repris tel quel, ce pied
        entre dans l'analyse de sentiment et pollue les motifs — « link » et
        « comments » finiraient parmi les termes déclenchés de CHAQUE avis
        Reddit du corpus, et l'onglet Motifs deviendrait illisible.

        DEUX CHEMINS, et le second n'est pas du zèle. Le message vit dans
        `<div class="md">`, qu'on isole donc en priorité. Mais si Reddit change
        cette enveloppe, s'en tenir là viderait le corps de TOUS les avis sans
        lever la moindre erreur : on n'aurait plus que des titres, et rien ne le
        signalerait. Le repli découpe donc le texte au pied de page, ce qui rend
        le même résultat par un autre chemin.

        Un corps vide reste possible et normal : c'est un fil de simple lien,
        où le titre porte seul l'information.
        """
        contents = getattr(entry, "content", None) or []
        brut = contents[0].get("value", "") if contents else (
            getattr(entry, "summary", "") or ""
        )
        if not brut:
            return ""

        soup = BeautifulSoup(brut, "html.parser")
        bloc = soup.find("div", class_="md")
        if bloc is not None:
            return bloc.get_text(" ", strip=True)

        # Repli : le pied de page est le seul repère stable dont on dispose.
        texte = soup.get_text(" ", strip=True)
        coupe = texte.find(cls._PIED)
        return (texte[:coupe] if coupe != -1 else texte).strip()

    @staticmethod
    def _author(entry) -> Optional[str]:
        """« /u/pseudo » → « pseudo ». None si absent ou supprimé."""
        brut = (getattr(entry, "author", "") or "").strip()
        if not brut:
            return None
        pseudo = brut.removeprefix("/u/").removeprefix("u/").strip()
        # Reddit rend « [deleted] » pour un compte supprimé : ce n'est pas un
        # auteur, et le stocker ferait apparaître un contributeur fictif dans
        # les agrégats par auteur.
        if not pseudo or pseudo == "[deleted]":
            return None
        return pseudo[:255]

    @staticmethod
    def _published(entry) -> Optional[datetime]:
        """Date de publication en UTC, ou None.

        `published_parsed` est un struct_time déjà normalisé UTC par feedparser ;
        le champ texte est en RFC 3339, que Pydantic accepte mal directement.
        `updated_parsed` sert de repli : Reddit renseigne toujours l'un des deux.
        """
        parsed = (getattr(entry, "published_parsed", None)
                  or getattr(entry, "updated_parsed", None))
        if not parsed:
            return None
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
        except (ValueError, OverflowError, TypeError):
            return None


class _RateLimited(RuntimeError):
    """Reddit a refusé l'appel pour cause de débit trop élevé.

    Porte le délai annoncé par le serveur quand il en donne un : c'est ce qui
    permet d'attendre juste ce qu'il faut au lieu de doubler à l'aveugle.
    """

    def __init__(self, message: str, reset_seconds: Optional[float] = None):
        super().__init__(message)
        self.reset_seconds = reset_seconds
