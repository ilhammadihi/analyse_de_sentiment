"""
Scraper Google Play Store.
Collecte les avis des apps Moov Africa.
"""

import logging
from datetime import datetime, timedelta
import time
from google_play_scraper import Sort, reviews
from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import playstore_apps
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig, execute_with_retry

logger = logging.getLogger(__name__)


class PlayStoreScraper(BaseCollector):
    """Scraper pour Google Play Store."""
    
    # Apps à scraper : chargées depuis config/operators.json (targets.py).

    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            # Budget du collecteur ENTIER, et non d'une requête : `collect()`
            # parcourt 262 paquets à la suite. `scraping.request_timeout` est
            # dimensionné pour un appel unitaire — l'employer ici faisait
            # expirer chaque tentative avant la fin du parcours.
            timeout=settings.playstore.collector_timeout,
        )
        super().__init__("playstore", retry_config)
    
    def collect(self) -> list[Review]:
        """Collecte tous les avis des apps."""
        all_reviews = []
        apps = playstore_apps()
        self.logger.info("%d app(s) Play Store déclarée(s) en configuration", len(apps))

        for app in apps:
            self.logger.info(f"Scraping {app['name']}")
            try:
                app_reviews = self._fetch_app_reviews(app)
                all_reviews.extend(app_reviews)
            except Exception as e:
                self.logger.error(f"Erreur scraping {app['name']} : {e}")
                continue
            
            time.sleep(1)  # Pause entre les apps
        
        return all_reviews
    
    #: Langues tentées successivement, TOUJOURS sur la boutique du pays.
    #:
    #: Les trois langues officielles dominantes du périmètre africain suivi.
    #: Faire varier la langue élargit la couverture d'un même marché ; faire
    #: varier le PAYS changerait de marché, ce qui est interdit ici.
    _LANGS = (None, "fr", "en", "pt")

    def _fetch_app_reviews(self, app: dict) -> list[Review]:
        """Récupère les avis d'une app, sur la boutique de SON pays uniquement.

        Un repli « global » (sans pays) existait ici : il ramenait les avis
        toutes zones confondues et les rattachait à la filiale interrogée, si
        bien qu'un avis indien pouvait être compté comme sénégalais. Il fausse
        exactement ce que le dashboard sert à comparer, et de façon invisible
        puisque la collecte réussissait. Il a été retiré.

        Ne subsiste que la variation de LANGUE, qui reste dans le même pays :
        sur un marché où l'on écrit en français, en anglais et en portugais, une
        seule langue interrogée laisse de côté une partie réelle des clients.
        """
        for lang in self._LANGS:
            label = f"store {app['country'].upper()}" + (f" / {lang}" if lang else "")
            result = self._try_fetch(
                app["package_id"],
                country=app["country"],
                lang=lang,
                label=label,
                company=app["name"],
            )
            if result:
                return self._parse_reviews(result, app)

        self.logger.info(
            "Aucun avis sur la boutique %s pour %s — aucun repli hors pays, "
            "un avis d'un autre marché ne doit pas être attribué à cette filiale.",
            app["country"].upper(), app["name"],
        )
        return []
    
    #: Taille d'un lot, et nombre maximal d'avis récupérés pour une app.
    #:
    #: Le collecteur demandait auparavant 100 avis en UN appel, à chaque
    #: passage, quoi qu'il ait déjà en base — d'où 11 377 doublons pour 2 315
    #: insertions sur sept jours, soit 83 % de trafic inutile, le pire des cinq
    #: collecteurs.
    #:
    #: En lots avec jeton de continuation, le comportement s'adapte :
    #:   - PREMIER passage sur une app : on descend jusqu'à BATCH_MAX pour
    #:     constituer un historique, soit plus qu'avant ;
    #:   - passages SUIVANTS : on s'arrête au premier lot entièrement connu,
    #:     donc en général après un seul appel.
    BATCH_SIZE = 40
    BATCH_MAX = 200

    def _try_fetch(self, package_id: str, country: str = None, lang: str = None,
                   label: str = "", company: str = None) -> list:
        """Récupère les avis par lots, du plus récent au plus ancien.

        `Sort.NEWEST` est ce qui rend l'arrêt anticipé légitime : un lot
        entièrement antérieur au repère garantit que tout ce qui suit l'est
        aussi. Sur une source non triée, s'arrêter ferait perdre des avis.
        """
        # Repère propre à CE paquet : voir le commentaire équivalent côté
        # App Store. Une filiale suit plusieurs applications.
        cutoff = (self.cutoff_for_key(company, "google_play", str(package_id))
                  if company else None)
        collectes: list = []
        token = None

        try:
            self.logger.debug(f"Tentative {label}")

            while len(collectes) < self.BATCH_MAX:
                kwargs = {
                    "app_id": package_id,
                    "sort": Sort.NEWEST,
                    "count": self.BATCH_SIZE,
                }
                if country:
                    kwargs["country"] = country
                if lang:
                    kwargs["lang"] = lang
                if token:
                    kwargs["continuation_token"] = token

                lot, token = reviews(**kwargs)
                if not lot:
                    break
                collectes.extend(lot)

                if self.batch_fully_known([r.get("at") for r in lot], cutoff):
                    self.logger.debug(
                        "Arrêt après %d avis : lot entièrement déjà connu",
                        len(collectes),
                    )
                    break

                # Plus de jeton = fin des avis disponibles pour cette app.
                if token is None:
                    break

            if collectes:
                self.logger.info(f"✓ {len(collectes)} avis trouvés ({label})")
                return collectes

            return None

        except Exception as e:
            self.logger.debug(f"Échec {label} : {e}")
            # Un lot déjà récupéré avant l'erreur reste exploitable : le perdre
            # obligerait à tout retélécharger au passage suivant.
            return collectes or None
    
    def _parse_reviews(self, raw_reviews: list, app: dict) -> list[Review]:
        """Parse les avis bruts en objets Review."""
        parsed = []
        
        for rv in raw_reviews:
            try:
                review = Review(
                    id=rv.get("reviewId"),
                    company=app["name"],
                    source=SourceEnum.GOOGLE_PLAY,
                    title=None,  # Play Store ne fournit pas de titre
                    text=rv.get("content", ""),
                    rating=rv.get("score"),
                    author=rv.get("userName"),
                    verified=True,  # Supposé vérifié sur Play Store
                    likes=rv.get("thumbsUpCount", 0),
                    created_at=rv.get("at"),
                    # Identité de la sous-cible : voir le repère par app.
                    target_id=str(app.get("package_id") or "") or None,
                    target_name=app.get("app_label") or None,
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue
        
        return parsed