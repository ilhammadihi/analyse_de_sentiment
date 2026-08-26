"""
Configuration centralisée, chargée depuis l'environnement (.env) et validée
avec Pydantic v2.

Rien n'est lu au moment de l'import : la configuration n'est construite que
lors du premier appel à get_settings() (mise en cache). Aucune connexion
réseau/BD n'est ouverte ici.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_ENV = SettingsConfigDict(
    env_file=".env", env_prefix="", extra="ignore", populate_by_name=True
)


class DatabaseConfig(BaseSettings):
    """Configuration PostgreSQL."""
    model_config = _ENV
    host: str = Field(default="localhost", validation_alias="DB_HOST")
    port: int = Field(default=5432, validation_alias="DB_PORT")
    name: str = Field(default="telecom_db", validation_alias="DB_NAME")
    user: str = Field(default="telecom_user", validation_alias="DB_USER")
    password: str = Field(default="telecom_password", validation_alias="DB_PASSWORD")
    ssl_mode: str = Field(default="prefer", validation_alias="DB_SSL_MODE")
    pool_size: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    max_overflow: int = Field(default=10, validation_alias="DB_MAX_OVERFLOW")

    def connection_string(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.name}?sslmode={self.ssl_mode}"
        )


class LoggingConfig(BaseSettings):
    model_config = _ENV
    level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    file: str = Field(default="data/logs/pipeline.log", validation_alias="LOG_FILE")
    format: str = Field(default="json", validation_alias="LOG_FORMAT")  # json | text


class ScrapingConfig(BaseSettings):
    model_config = _ENV
    request_timeout: int = Field(default=30, validation_alias="REQUEST_TIMEOUT")
    retry_max_attempts: int = Field(default=3, validation_alias="RETRY_MAX_ATTEMPTS")
    retry_backoff_factor: float = Field(default=2, validation_alias="RETRY_BACKOFF_FACTOR")
    retry_backoff_max: int = Field(default=120, validation_alias="RETRY_BACKOFF_MAX")


class TrustpilotConfig(BaseSettings):
    model_config = _ENV

    #: DÉSACTIVÉ PAR DÉFAUT, contrairement aux quatre autres collecteurs.
    #:
    #: Mesuré : 50 exécutions, 0 avis inséré. Le collecteur fonctionne — il
    #: navigue, distingue un 404 d'une panne et le journalise correctement —
    #: mais 3 des 4 domaines configurés n'ont aucune fiche Trustpilot et le
    #: quatrième a une fiche vide. La plateforme ne couvre pas les opérateurs
    #: télécoms africains suivis ici.
    #:
    #: Le défaut est à False et non à True parce qu'un déploiement neuf sans
    #: `.env` ne doit pas réactiver un collecteur connu comme sans cible : il
    #: produirait 43 alertes « scraper_zero » par semaine et ferait passer une
    #: absence de fiche pour une panne.
    enabled: bool = Field(default=False, validation_alias="ENABLE_TRUSTPILOT")
    cache_path: str = Field(default="data/state/tp_state.json", validation_alias="TRUSTPILOT_CACHE_PATH")
    max_pages: int = Field(default=10, validation_alias="TRUSTPILOT_MAX_PAGES")


class PlayStoreConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_PLAYSTORE")
    retry_strategies: int = Field(default=3, validation_alias="PLAYSTORE_RETRY_STRATEGIES")

    #: Budget de temps du collecteur ENTIER, tous paquets cumulés.
    #:
    #: Il était pris sur `scraping.request_timeout` (180 s), dimensionné pour
    #: UNE requête. Tant que le périmètre tenait en une centaine d'apps, la
    #: confusion passait inaperçue : 133 s mesurées le 4 août, sous la barre.
    #: À 262 apps, la seule pause de politesse d'une seconde entre apps totalise
    #: 262 s — le budget est dépassé AVANT le premier appel réseau. Le
    #: collecteur ne pouvait donc plus aboutir : trois tentatives, trois
    #: expirations, 543 s et zéro avis, à chaque passage depuis le 6 août.
    #:
    #: 1 200 s couvre le pire cas mesuré (1,20 s de travail + 1 s de pause par
    #: app, soit ~578 s sans repère incrémental) avec le double de marge, et
    #: reste très en deçà de la cadence de 360 min — `slow_run` garde donc son
    #: sens.
    collector_timeout: int = Field(default=1200, validation_alias="PLAYSTORE_TIMEOUT")


class AppStoreConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_APPSTORE")
    max_pages: int = Field(default=5, validation_alias="APPSTORE_MAX_PAGES")
    fallback_country: str = Field(default="fr", validation_alias="APPSTORE_FALLBACK_COUNTRY")

    #: Même raisonnement que côté Play Store, mêmes symptômes : 186 apps,
    #: 1,66 s de travail et 1 s de pause chacune, soit ~494 s mesurés là où le
    #: budget hérité en accordait 180.
    collector_timeout: int = Field(default=1200, validation_alias="APPSTORE_TIMEOUT")


class GoogleMapsConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=False, validation_alias="ENABLE_GOOGLEMAPS")

    #: Avis BRUTS récupérés par agence, avant filtrage.
    #:
    #: Abaissé de 300 à 60 depuis que les avis sont triés du plus récent au plus
    #: ancien (`_sort_by_newest`). L'ordre change tout : sans tri, il fallait
    #: descendre loin pour espérer croiser des avis récents ; trié par date, les
    #: soixante premiers SONT les plus récents.
    #:
    #: 60 et non 10, parce que quatre avis Google Maps sur cinq n'ont pas de
    #: commentaire — juste une note, inexploitable pour l'analyse de sentiment.
    #: Soixante avis bruts donnent donc une douzaine d'avis utilisables, ce qui
    #: correspond à l'objectif retenu : une dizaine d'avis RÉCENTS par agence.
    #:
    #: Effet secondaire recherché : Google Maps était de loin le collecteur le
    #: plus lent (666 s en moyenne, contre 26 s pour Play Store). Moins de
    #: défilement, c'est une collecte plus fréquente, donc des données plus
    #: fraîches — le but même de la manœuvre.
    max_reviews: int = Field(default=60, validation_alias="GOOGLEMAPS_MAX_REVIEWS")
    headless: bool = Field(default=True, validation_alias="GOOGLEMAPS_HEADLESS")

    #: Temps accordé à UN passage, en secondes. Le passage traite autant
    #: d'unités que le budget le permet, puis s'arrête proprement ; les unités
    #: restantes attendent dans `collection_jobs` et repartent au passage
    #: suivant.
    #:
    #: C'EST CE QUI REND LE COLLECTEUR BORNÉ. Sans budget, il tentait ses 405
    #: recherches d'affilée — une dizaine d'heures mesurées — et se faisait tuer
    #: avant la fin, sans jamais rien écrire. Un passage borné se termine
    #: toujours, donc écrit toujours, et l'alerting qui suit la fin du run est
    #: atteint à chaque cycle.
    #:
    #: 3 600 s couvre ~35 recherches (mesuré ~100 s l'une). Le périmètre entier
    #: se couvre donc en une douzaine de passages : abaisser
    #: SCRAPER_INTERVAL_GOOGLEMAPS est désormais SANS DANGER, puisque la durée
    #: d'un passage ne dépend plus du périmètre.
    run_budget_seconds: int = Field(
        default=3600, validation_alias="GOOGLEMAPS_RUN_BUDGET"
    )

    #: Villes interrogées EN PLUS de la requête pays, par filiale et par run.
    #:
    #: La requête générique (« Agence MTN Nigeria ») ne résout qu'UNE fiche
    #: Google, alors qu'un opérateur exploite des dizaines d'agences : c'était
    #: le plus gros gisement inexploité du projet, et il ne demandait aucun
    #: nouveau collecteur. Une requête par ville résout une fiche distincte,
    #: et donne au passage la granularité intra-pays (Lagos contre Kano).
    #:
    #: 2 et non 6, parce que chaque fiche coûte une session navigateur complète
    #: sur le collecteur DÉJÀ le plus lent : 132 filiales × 6 villes feraient
    #: 792 sessions, soit plusieurs heures pour un seul run. À 2, on triple le
    #: nombre de fiches (132 → ~396) pour un run qui reste dans son budget, et
    #: `googlemaps_locations()` fait tourner les villes d'un jour à l'autre :
    #: la couverture complète s'obtient sur la semaine plutôt qu'en une nuit.
    cities_per_run: int = Field(default=2, validation_alias="GOOGLEMAPS_CITIES_PER_RUN")

    #: Agences retenues PAR RECHERCHE.
    #:
    #: Une recherche Google Maps renvoie une liste : mesuré, 10 à 11 résultats
    #: pour « Agence MTN Lagos » ou « Agence Vodacom Johannesburg ». Le
    #: collecteur n'en ouvrait qu'UN (`.first`) et jetait les autres — alors
    #: qu'ils étaient déjà chargés dans la page. Les récupérer ne coûte aucune
    #: recherche supplémentaire, seulement l'ouverture des fiches.
    #:
    #: 5 et non 10 : chaque fiche est une page à ouvrir, faire défiler et
    #: parcourir. Combiné à `cities_per_run`, cela porte le budget théorique à
    #: 132 filiales × 3 recherches × 5 agences = 1 980 fiches — en pratique bien
    #: moins, la déduplication par identifiant de lieu écartant les fiches que
    #: deux recherches se partagent.
    places_per_query: int = Field(
        default=5, validation_alias="GOOGLEMAPS_PLACES_PER_QUERY"
    )


class RSSFeedConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=False, validation_alias="ENABLE_RSS_FEED")

    # Le nombre de flux est le produit filiales x mots-clés : il a explosé en
    # passant de 4 filiales (40 flux) à l'ensemble des opérateurs africains
    # (plusieurs centaines). Le budget de timeout global du collecteur et le
    # parallélisme sont donc réglables indépendamment de scraping.request_timeout,
    # qui reste dimensionné pour une requête unitaire.
    collector_timeout: int = Field(default=300, validation_alias="RSS_COLLECTOR_TIMEOUT")
    max_workers: int = Field(default=16, validation_alias="RSS_MAX_WORKERS")
    keywords: str = Field(
        default="panne,reseau,service,prix,recharge,connexion,4g,5g,internet,données",
        validation_alias="RSS_KEYWORDS",
    )

    def keywords_list(self) -> list[str]:
        return [k.strip() for k in self.keywords.split(",") if k.strip()]


class HelloPeterConfig(BaseSettings):
    """HelloPeter — plateforme d'avis n°1 en Afrique du Sud.

    API JSON publique, sans clé : `api.hellopeter.com/consumer/business/{slug}
    /reviews?page=N`. Avis notés 1-5 avec titre, texte, date et auteur, triés
    du plus récent au plus ancien — donc compatible avec l'arrêt anticipé
    incrémental de BaseCollector.
    """

    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_HELLOPETER")
    base_url: str = Field(
        default="https://api.hellopeter.com/consumer/business",
        validation_alias="HELLOPETER_BASE_URL",
    )

    #: Pages récupérées par filiale et par run. 11 avis par page.
    #:
    #: LE RÉGLAGE LE PLUS SENSIBLE DU PROJET. Les quatre filiales sud-africaines
    #: cumulent plus de 400 000 avis à elles seules, là où le reste du corpus
    #: pan-africain se compte en milliers. Tout aspirer d'un coup ferait de
    #: l'Afrique du Sud 95 % de la base : chaque comparaison entre filiales, qui
    #: est la raison d'être du dashboard, deviendrait une comparaison entre
    #: l'Afrique du Sud et du bruit.
    #:
    #: 20 pages = ~220 avis par filiale et par run. En régime incrémental le
    #: collecteur s'arrête bien avant, dès que le lot est déjà connu ; ce plafond
    #: ne sert qu'au tout premier passage, où il évite d'ingérer un historique
    #: remontant à 2018 en une nuit.
    max_pages: int = Field(default=20, validation_alias="HELLOPETER_MAX_PAGES")

    #: Pause entre deux pages. L'API est ouverte et ne documente aucun quota :
    #: on s'auto-limite plutôt que d'attendre d'être bloqué.
    request_delay: float = Field(default=0.5, validation_alias="HELLOPETER_DELAY")
    collector_timeout: int = Field(default=600, validation_alias="HELLOPETER_TIMEOUT")


class GDELTConfig(BaseSettings):
    """GDELT DOC 2.0 — presse mondiale multilingue, gratuite et sans clé.

    Complète Google News RSS plutôt que de le remplacer : GDELT indexe 100+
    langues et rend le PAYS-SOURCE et la LANGUE de chaque article, deux
    dimensions que le flux Google News ne fournit pas.
    """

    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_GDELT")
    base_url: str = Field(
        default="https://api.gdeltproject.org/api/v2/doc/doc",
        validation_alias="GDELT_BASE_URL",
    )

    #: Délai entre deux requêtes, en secondes. NON NÉGOCIABLE.
    #:
    #: Mesuré : l'API répond en texte brut « Please limit requests to one every
    #: 5 seconds » dès qu'on va plus vite, et le refus est silencieux du point de
    #: vue HTTP (200 avec un corps qui n'est pas du JSON). Un collecteur naïf
    #: enregistrerait donc « 0 article » au lieu d'une erreur.
    #:
    #: 6 s et non 5 : la fenêtre est glissante côté serveur et l'égalité stricte
    #: se fait rejeter. Conséquence assumée : les 132 filiales prennent ~13 min,
    #: d'où un budget de timeout propre à ce collecteur.
    min_interval_seconds: float = Field(
        default=6.0, validation_alias="GDELT_MIN_INTERVAL_SECONDS"
    )

    #: Profondeur d'interrogation. `timespan` accepte 1d, 7d, 1m…
    timespan: str = Field(default="7d", validation_alias="GDELT_TIMESPAN")
    max_records: int = Field(default=100, validation_alias="GDELT_MAX_RECORDS")
    collector_timeout: int = Field(default=1800, validation_alias="GDELT_TIMEOUT")

    #: Temps accordé à UN passage, en secondes (mode unités).
    #:
    #: Le calcul de ~13 min ci-dessus vaut pour le débit NOMINAL de 6 s. Mesuré
    #: en production, GDELT bride et le collecteur monte jusqu'à son plafond de
    #: 60 s : les 132 filiales demandent alors ~145 min, soit cinq fois le
    #: `collector_timeout`. Ce collecteur n'a jamais terminé un seul passage.
    #:
    #: Élargir le budget n'aurait rien réglé — ce n'est pas nous qui sommes
    #: lents, c'est la source qui refuse d'aller plus vite. Découpé en unités,
    #: un passage interroge ce qu'il peut, rend la main dès le premier bridage
    #: dur, et le reste attend en file : la couverture s'étale sur quelques
    #: passages au lieu de ne jamais aboutir.
    run_budget_seconds: int = Field(
        default=1800, validation_alias="GDELT_RUN_BUDGET"
    )


class PressFeedConfig(BaseSettings):
    """Presse tech africaine spécialisée (flux RSS natifs).

    Différent de `rss_feed` : là où Google News est interrogé PAR FILIALE (une
    requête par opérateur), ces flux sont généralistes. On les lit une fois,
    puis on rattache chaque article aux filiales qu'il cite réellement — les
    articles ne citant aucune filiale suivie sont jetés.

    Les dix flux par défaut ont été vérifiés actifs (article de moins de 7
    jours) le 4 août 2026. Deux titres majeurs manquent volontairement :
    Techpoint Africa et MyBroadband répondent 403 derrière Cloudflare et
    exigeraient un navigateur, ce qui n'a pas sa place dans un collecteur HTTP.
    """

    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_PRESS_FEED")

    #: Flux, au format `url` ou `url|ISO2`.
    #:
    #: L'ISO2 optionnel déclare le pays D'ÉDITION du titre, et joue exactement
    #: le rôle du `sourcecountry:` de GDELT : dans Nairametrics, qui ne couvre
    #: que le Nigeria, « MTN » désigne forcément MTN Nigeria. Sans cette
    #: information, l'article est écarté faute de marqueur de pays — mesuré :
    #: 3 articles rattachés sur 178 lus.
    #:
    #: SEULS DEUX TITRES EN PORTENT UN, et c'est délibéré. TechCabal, Jeune
    #: Afrique, ITWeb Africa ou CIO Mag couvrent tout le continent : leur
    #: attribuer un pays d'édition rattacherait un article ghanéen à une
    #: filiale nigériane, c'est-à-dire précisément la faute que le marqueur de
    #: pays sert à empêcher. Mieux vaut rater un article que le mal attribuer.
    feeds: str = Field(
        default=(
            "https://techcabal.com/feed/,"
            "https://disruptafrica.com/feed/,"
            "https://nairametrics.com/feed/|NG,"
            "https://techcentral.co.za/feed|ZA,"
            "https://itweb.africa/rss,"
            "https://www.itnewsafrica.com/feed/,"
            "https://www.digitalbusiness.africa/feed/,"
            "https://cio-mag.com/feed/,"
            "https://www.jeuneafrique.com/feed/,"
            "https://www.financialafrik.com/feed/"
        ),
        validation_alias="PRESS_FEEDS",
    )
    max_workers: int = Field(default=8, validation_alias="PRESS_FEED_MAX_WORKERS")
    fetch_timeout: int = Field(default=20, validation_alias="PRESS_FEED_FETCH_TIMEOUT")
    collector_timeout: int = Field(default=300, validation_alias="PRESS_FEED_TIMEOUT")

    def feeds_list(self) -> list[tuple[str, Optional[str]]]:
        """[(url, iso2_du_titre_ou_None)]."""
        out: list[tuple[str, Optional[str]]] = []
        for raw in self.feeds.split(","):
            raw = raw.strip()
            if not raw:
                continue
            url, _, iso2 = raw.partition("|")
            out.append((url.strip(), iso2.strip().upper() or None))
        return out


class RedditConfig(BaseSettings):
    """Reddit — la parole spontanée des abonnés, hors plateforme d'avis.

    CE QU'ELLE APPORTE QUE LES AUTRES N'APPORTENT PAS
        Les huit autres sources recueillent une parole SOLLICITÉE (on note une
        application, on dépose une plainte) ou RAPPORTÉE (la presse). Reddit est
        la seule où l'abonné parle de lui-même, à ses pairs, sans formulaire :
        c'est là que se décrivent les pannes en cours, les hausses de tarif et
        les contournements, souvent avant qu'un article ne les relaie.

    L'API OFFICIELLE (PRAW), ET RIEN D'AUTRE
        L'accès anonyme est fermé : vérifié le 7 août 2026 depuis le réseau du
        projet, `search.json`, `new.json` et `old.reddit.com` répondent tous
        HTTP 403 « Blocked », y compris avec un en-tête de navigateur. Les flux
        `.rss` restent ouverts mais plafonnent à UNE requête par minute et par
        adresse IP — Reddit l'annonce lui-même (`x-ratelimit-remaining: 0.0`
        juste après un succès), et c'est confirmé : 1 succès sur 5 à 15 s
        d'espacement, 3 sur 3 à 65 s.

        Ce collecteur passe donc par PRAW, le client officiel, avec des
        identifiants OAuth. CONSÉQUENCE À CONNAÎTRE : sans identifiants, il ne
        collecte RIEN. C'est un choix assumé (voir `usable`), pas un oubli.

    CE QUE L'API DONNE ET QUE LE FLUX ATOM NE DONNAIT PAS
        Les COMMENTAIRES. Le flux de recherche ne rendait que des soumissions,
        or l'essentiel du volume d'opinion d'un forum est dans les réactions,
        pas dans le fil d'origine. C'est la raison principale de ce choix.
        Accessoirement : la pagination au-delà de 25, et le score du fil.

    UNE REQUÊTE PAR PAYS, PAS PAR FILIALE
        C'est la leçon déjà tirée par `press_feed` (« dix requêtes au lieu de
        mille trois cents ») : on interroge le subreddit du PAYS avec la
        disjonction de ses opérateurs, puis on rattache chaque fil aux filiales
        qu'il cite. Elle reste valable même sans contrainte de débit — 15
        requêtes valent mieux que 135 pour un résultat identique.

        Le subreddit pays fournit en prime le marqueur de pays exigé par
        `press_matchers()` pour les opérateurs multi-pays — exactement le rôle
        que joue l'annotation `|ISO2` des flux de presse. Dans r/Nigeria,
        « MTN » désigne forcément MTN Nigeria.
    """

    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_REDDIT")

    # --- Identifiants OAuth -------------------------------------------------
    #
    # Application de type « script » à créer sur https://www.reddit.com/prefs/apps
    # (gratuit). Le `client_id` est la chaîne affichée SOUS « personal use
    # script », sans étiquette — c'est le piège classique, beaucoup y recopient
    # le nom de l'application.
    #
    # Aucun identifiant utilisateur n'est nécessaire : on ne lit que des
    # subreddits publics, donc le mode lecture seule (client_credentials) suffit.
    client_id: Optional[str] = Field(default=None, validation_alias="REDDIT_CLIENT_ID")
    client_secret: Optional[str] = Field(
        default=None, validation_alias="REDDIT_CLIENT_SECRET"
    )

    #: Reddit EXIGE un user-agent descriptif et refuse les clients génériques.
    #: Le format recommandé est `plateforme:identifiant:version (by /u/pseudo)`.
    user_agent: str = Field(
        default="script:telecom-sentiment:1.0",
        validation_alias="REDDIT_USER_AGENT",
    )

    # --- Voie Atom (en service tant que le portage PRAW n'est pas terminé) ---
    #
    # PORTAGE EN COURS : les identifiants OAuth ci-dessus sont déclarés, le
    # collecteur les utilisera, mais il fonctionne encore sur le flux Atom. Ces
    # deux réglages lui restent donc nécessaires ; ils disparaîtront avec lui.
    base_url: str = Field(
        default="https://www.reddit.com", validation_alias="REDDIT_BASE_URL"
    )

    #: Délai entre deux requêtes Atom, en secondes. MESURÉ : Reddit accorde une
    #: requête par fenêtre de 60 s et par adresse IP sans authentification —
    #: 1 succès sur 5 à 15 s d'espacement, 3 sur 3 à 65 s. 62 s et non 60, la
    #: fenêtre étant glissante côté serveur. Sans objet une fois passé à PRAW,
    #: qui gère lui-même son quota.
    min_interval_seconds: float = Field(
        default=62.0, validation_alias="REDDIT_MIN_INTERVAL_SECONDS"
    )

    #: Subreddits interrogés, au format `nom|ISO2`.
    #:
    #: L'ISO2 n'est pas décoratif : il DÉSAMBIGUÏSE. Sans lui, un fil de
    #: r/Kenya citant « Airtel » serait rattaché aux dix-huit filiales Airtel du
    #: périmètre à la fois — la faute même que le marqueur de pays existe pour
    #: empêcher (voir `press_feed._filiales_citees`).
    #:
    #: Liste VÉRIFIÉE, pas devinée : chaque entrée a été interrogée le 7 août
    #: 2026 avec la requête réelle du collecteur, et seuls les subreddits ayant
    #: réellement rendu des fils télécom ont été retenus.
    #:
    #: Le défaut est NON VIDE, contrairement à ce qu'on pourrait croire plus
    #: prudent : une liste vide ferait rendre « 0 avis » à chaque run, ce que
    #: l'alerting lit comme une panne. C'est la leçon de Trustpilot, dont
    #: 43 alertes « scraper_zero » par semaine signalaient une absence de cible
    #: et non un incident.
    #: SEUIL RETENU : au moins 4 fils télécom sur un an, soit un par trimestre.
    #: Les 54 pays du périmètre ont été sondés ; 32 subreddits rendent au moins
    #: un fil, 19 n'en rendent aucun (r/Uganda, r/IvoryCoast, r/DRCongo…) et 3
    #: n'existent pas ou sont fermés (r/Chad répond 403, r/SaoTomeAndPrincipe et
    #: r/CentralAfricanRepublic 404).
    #:
    #: Les six subreddits entre 1 et 3 fils par an sont écartés : à 62 s l'appel
    #: et deux runs par jour, chacun coûterait plus de douze heures de collecte
    #: annuelle pour moins de quatre discussions.
    subreddits: str = Field(
        default=(
            "southafrica|ZA,Nigeria|NG,Kenya|KE,ghana|GH,Egypt|EG,"
            "Morocco|MA,algeria|DZ,Tunisia|TN,Ethiopia|ET,Tanzania|TZ,"
            "Zimbabwe|ZW,Zambia|ZM,Rwanda|RW,Malawi|MW,Namibia|NA,"
            "Botswana|BW,Madagascar|MG,mauritius|MU,Somalia|SO,Senegal|SN,"
            "Angola|AO,Cameroon|CM,SierraLeone|SL,Congo|CG,Mauritania|MR,"
            "Seychelles|SC"
        ),
        validation_alias="REDDIT_SUBREDDITS",
    )

    #: Profondeur de recherche : hour, day, week, month, year, all.
    #:
    #: `month` et non `week` : un subreddit pays de taille moyenne produit
    #: quelques fils télécom par mois, pas par semaine. Une fenêtre trop courte
    #: ramènerait zéro résultat sur la majorité des pays et ferait passer un
    #: périmètre calme pour une collecte cassée. Le filtrage incrémental écarte
    #: ensuite sans coût ce qui est déjà en base.
    timespan: str = Field(default="month", validation_alias="REDDIT_TIMESPAN")

    #: Soumissions retenues par subreddit et par run.
    #:
    #: 50 et non 25 : le flux Atom plafonnait à 25, l'API pagine au-delà. Mais
    #: chaque soumission retenue coûte ensuite un appel pour ses commentaires,
    #: donc ce nombre commande le coût du run bien plus que le nombre de
    #: subreddits. Le régime incrémental fait retomber ce coût dès le second run.
    search_limit: int = Field(default=50, validation_alias="REDDIT_SEARCH_LIMIT")

    #: Collecter les commentaires des fils retenus.
    #:
    #: C'EST LA RAISON D'ÊTRE DU PASSAGE À L'API. Le flux Atom ne rendait que
    #: des soumissions ; or sur un forum, l'essentiel de la parole est dans les
    #: réactions. Un fil « MTN est encore en panne » vaut un avis, ses quarante
    #: commentaires en valent quarante.
    with_comments: bool = Field(default=True, validation_alias="REDDIT_WITH_COMMENTS")

    #: Commentaires retenus par fil, les mieux notés d'abord.
    #:
    #: Plafonné parce qu'un fil viral en compte des milliers, et que la queue de
    #: distribution est faite de « lol », « same » et de digressions. Le tri par
    #: score fait remonter les témoignages étayés, qui sont ceux qui portent une
    #: information exploitable.
    max_comments_per_post: int = Field(
        default=40, validation_alias="REDDIT_MAX_COMMENTS"
    )

    #: Longueur minimale d'un commentaire retenu, en caractères.
    #:
    #: « same », « +1 » ou « lol » ne portent aucun sentiment analysable et
    #: pèseraient pourtant autant qu'un témoignage dans les agrégats.
    min_comment_chars: int = Field(
        default=30, validation_alias="REDDIT_MIN_COMMENT_CHARS"
    )

    #: Budget global du collecteur. PRAW gère lui-même l'espacement des appels
    #: sur le quota OAuth ; ce budget ne couvre donc plus une attente imposée
    #: mais le volume : subreddits x (1 recherche + N appels de commentaires).
    collector_timeout: int = Field(default=1800, validation_alias="REDDIT_TIMEOUT")

    #: Exiger du vocabulaire télécom dans le fil, en plus du nom de l'opérateur.
    #:
    #: Le nom seul ne suffit pas sur un forum GÉNÉRALISTE : « orange » y est une
    #: couleur ou un fruit, « free » un adjectif, « telkom » sans contexte peut
    #: être une offre d'emploi. Le contrôle de vocabulaire de
    #: `press_relevance.est_pertinent` est déjà écrit, éprouvé et multilingue —
    #: on le réutilise plutôt que d'inventer une seconde règle qui divergerait.
    require_telecom_terms: bool = Field(
        default=True, validation_alias="REDDIT_REQUIRE_TELECOM_TERMS"
    )

    def usable(self) -> bool:
        """Activé ET en mesure de collecter.

        POURQUOI CE N'EST PAS `enabled` TOUT SEUL
            PRAW ne peut pas passer un seul appel sans identifiants. Un
            collecteur activé mais sans clés rendrait donc « 0 avis » à chaque
            run — et l'alerting lit ce zéro comme une PANNE. C'est exactement ce
            qui s'est produit avec Trustpilot : 43 alertes « scraper_zero » par
            semaine pour signaler non pas un incident, mais une absence de
            cible.

            Sans identifiants, le collecteur n'est donc pas « en échec » : il
            n'est pas lancé. Le fil technique reste lisible, et la seule trace
            est une ligne d'information au démarrage.
        """
        return bool(self.enabled and self.client_id and self.client_secret)

    def subreddits_list(self) -> list[tuple[str, Optional[str]]]:
        """[(nom_du_subreddit, iso2_du_pays_ou_None)]."""
        out: list[tuple[str, Optional[str]]] = []
        for raw in self.subreddits.split(","):
            raw = raw.strip()
            if not raw:
                continue
            nom, _, iso2 = raw.partition("|")
            nom = nom.strip().lstrip("/").removeprefix("r/").strip("/")
            if nom:
                out.append((nom, iso2.strip().upper() or None))
        return out


class SchedulerConfig(BaseSettings):
    """Planification du pipeline (APScheduler)."""
    model_config = _ENV
    enabled: bool = Field(default=False, validation_alias="ENABLE_SCHEDULER")

    #: Cadence de repli, pour une source sans cadence propre déclarée. Ce n'est
    #: PLUS le rythme d'un cycle global : chaque collecteur a son propre job
    #: (voir `run_scheduler`), et donc son propre intervalle.
    interval_minutes: int = Field(default=60, validation_alias="SCHEDULER_INTERVAL_MINUTES")
    run_on_start: bool = Field(default=True, validation_alias="SCHEDULER_RUN_ON_START")
    timezone: str = Field(default="Africa/Casablanca", validation_alias="SCHEDULER_TIMEZONE")

    #: Collecteurs autorisés à tourner EN MÊME TEMPS.
    #:
    #: Un job par source les rend concurrents, ce qui est le but : une source
    #: lente ne doit plus retenir les autres. Mais sans plafond, huit
    #: collecteurs peuvent démarrer à la même minute, dont plusieurs pilotent un
    #: navigateur — de quoi saturer la machine et faire échouer par timeout des
    #: collectes qui réussissaient. 3 laisse tourner les sources rapides pendant
    #: que Google Maps occupe son couloir pendant des heures.
    max_concurrent: int = Field(default=3, validation_alias="SCHEDULER_MAX_CONCURRENT")

    #: Décalage entre les premiers démarrages, en minutes. Sans lui, tous les
    #: jobs se déclenchent à la seconde du lancement et se disputent le plafond
    #: ci-dessus dès la première minute.
    stagger_minutes: int = Field(default=2, validation_alias="SCHEDULER_STAGGER_MINUTES")

    #: Agent de veille : actif, et à quelle heure locale il parle.
    #:
    #: UN RENDEZ-VOUS QUOTIDIEN, PAS UN INTERVALLE. Un briefing toutes les
    #: N heures tombe à 3 h du matin une fois sur deux et perd son statut de
    #: rendez-vous ; une heure fixe se lit comme le journal du matin, et son
    #: absence se remarque. L'heure est locale au fuseau ci-dessus.
    #:
    #: Désactivé par défaut : cet agent ÉCRIT dans un groupe Telegram. Une
    #: activation implicite à la première mise à jour enverrait un message non
    #: sollicité à toute l'équipe.
    agent_enabled: bool = Field(default=False, validation_alias="ENABLE_INSIGHT_AGENT")
    agent_hour: int = Field(default=8, ge=0, le=23, validation_alias="INSIGHT_AGENT_HOUR")

    #: Assistant de campagne : actif, quel jour de la semaine et à quelle heure.
    #:
    #: HEBDOMADAIRE, LÀ OÙ LA VEILLE EST QUOTIDIENNE. Une campagne se prépare,
    #: se valide et se déroule sur plusieurs semaines : en proposer une chaque
    #: matin saturerait la file de validation en trois jours, et l'équipe
    #: prendrait l'habitude de ne plus les lire — exactement ce que le
    #: refroidissement de l'agent existe pour éviter, mais au niveau du rythme.
    #:
    #: Lundi (0) à 9 h : le début de semaine est le seul moment où une équipe
    #: marketing peut encore décider de quelque chose pour cette semaine-là.
    #:
    #: Désactivé par défaut, comme l'agent de veille et pour la même raison : ce
    #: job ÉCRIT dans un groupe Telegram.
    campaign_enabled: bool = Field(
        default=False, validation_alias="ENABLE_CAMPAIGN_AGENT"
    )
    campaign_day: int = Field(
        default=0, ge=0, le=6, validation_alias="CAMPAIGN_AGENT_DAY"
    )
    campaign_hour: int = Field(
        default=9, ge=0, le=23, validation_alias="CAMPAIGN_AGENT_HOUR"
    )

    #: Indicateurs de marché : rafraîchissement MENSUEL, le 1er à 3 h.
    #:
    #: La source publie des données ANNUELLES, révisées en cours d'année. Un
    #: passage quotidien referait 486 appels pour ne rien changer 364 jours sur
    #: 365 ; un passage annuel raterait les révisions. Le mois est le bon pas.
    #:
    #: Actif par défaut, contrairement à l'agent de veille : ce job ne parle à
    #: personne, il ne fait que lire une source publique.
    market_enabled: bool = Field(default=True, validation_alias="ENABLE_MARKET_DATA")
    market_day: int = Field(default=1, ge=1, le=28, validation_alias="MARKET_DATA_DAY")
    market_hour: int = Field(default=3, ge=0, le=23, validation_alias="MARKET_DATA_HOUR")

    #: Abonnés GSM par opérateur (NCC Nigeria) : rafraîchissement MENSUEL lui
    #: aussi, mais la source elle-même est mensuelle — voir
    #: `reviews/collectors/ncc_nigeria.py`. Un jour différent de
    #: `market_day` (2 plutôt que 1) : les deux jobs lisent des sources
    #: publiques indépendantes, rien n'oblige à les faire courir le même jour,
    #: et les séparer facilite la lecture des logs si l'un échoue.
    #:
    #: Actif par défaut, comme les indicateurs de marché pays : lecture seule
    #: d'une source publique, ne parle à personne.
    ncc_nigeria_enabled: bool = Field(default=True, validation_alias="ENABLE_NCC_NIGERIA")
    ncc_nigeria_day: int = Field(default=2, ge=1, le=28, validation_alias="NCC_NIGERIA_DAY")
    ncc_nigeria_hour: int = Field(default=3, ge=0, le=23, validation_alias="NCC_NIGERIA_HOUR")

    #: Abonnés mobile par opérateur (ANRT Maroc) : la source elle-même est
    #: TRIMESTRIELLE, mais un passage mensuel ne coûte rien de plus qu'un
    #: appel HTTP inutile deux mois sur trois — voir `market_enabled` sur le
    #: même raisonnement pour la source pays.
    anrt_maroc_enabled: bool = Field(default=True, validation_alias="ENABLE_ANRT_MAROC")
    anrt_maroc_day: int = Field(default=3, ge=1, le=28, validation_alias="ANRT_MAROC_DAY")
    anrt_maroc_hour: int = Field(default=3, ge=0, le=23, validation_alias="ANRT_MAROC_HOUR")

    #: Abonnés mobile actifs par opérateur (ARCEP Bénin) : la source est
    #: ANNUELLE et son URL change à chaque édition (voir
    #: `reviews/collectors/arcep_benin.py`) — un passage mensuel ne trouvera
    #: donc rien de neuf onze mois sur douze tant que le lien n'est pas
    #: recontrôlé à la main, mais reste inoffensif (upsert idempotent).
    arcep_benin_enabled: bool = Field(default=True, validation_alias="ENABLE_ARCEP_BENIN")
    arcep_benin_day: int = Field(default=4, ge=1, le=28, validation_alias="ARCEP_BENIN_DAY")
    arcep_benin_hour: int = Field(default=3, ge=0, le=23, validation_alias="ARCEP_BENIN_HOUR")

    #: Abonnements voix mobile par opérateur (NCA Ghana) : la source publie un
    #: bulletin TRIMESTRIEL, comme ANRT Maroc — même raisonnement sur le
    #: passage mensuel qui ne trouve rien de neuf deux mois sur trois, sans
    #: coût réel. URL datée par édition, comme ARCEP Bénin — voir
    #: `reviews/collectors/nca_ghana.py`.
    nca_ghana_enabled: bool = Field(default=True, validation_alias="ENABLE_NCA_GHANA")
    nca_ghana_day: int = Field(default=5, ge=1, le=28, validation_alias="NCA_GHANA_DAY")
    nca_ghana_hour: int = Field(default=3, ge=0, le=23, validation_alias="NCA_GHANA_HOUR")


class AlertingConfig(BaseSettings):
    """Alerting temps réel : seuils + canaux de notification."""
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_MONITORING")
    alert_zero_reviews: bool = Field(default=True, validation_alias="ALERT_ZERO_REVIEWS")

    # --- Seuils métier : détection d'un PIC d'insatisfaction ---------------
    #
    # Un pic est une VARIATION sur une COURTE PÉRIODE, pas un niveau.
    # La règle précédente mesurait la part de négatifs parmi les avis d'un run,
    # ce qui ne pouvait pas fonctionner : un run collecte des avis publiés de
    # 1970 à aujourd'hui, et son ratio ne décrit donc aucune période. Elle n'a
    # produit qu'une seule alerte sur 216.

    #: Fenêtre d'observation, en jours. Comparée aux `spike_window_days`
    #: précédents, de durée égale — comparer 7 jours à 30 produirait un écart dû
    #: à la seule durée.
    spike_window_days: int = Field(default=7, validation_alias="ALERT_SPIKE_WINDOW_DAYS")

    #: Hausse minimale de la part de négatifs, EN POINTS, pour parler de pic.
    #:
    #: CALIBRÉ SUR LES DONNÉES, pas choisi a priori. Les hausses réellement
    #: observées sur le corpus plafonnent à +10,0 pts sur 7 jours, +14,3 sur
    #: 30 et +12,5 sur 90. Une valeur de 15 aurait donc été au-dessus du
    #: maximum jamais atteint : la règle n'aurait jamais déclenché — exactement
    #: le défaut de l'ancienne (50 % exigés pour une moyenne de corpus à 17 %).
    #:
    #: 10 points retient les dégradations franches — MTN Ouganda passant de
    #: 6,5 % à 20,8 % en un mois, soit un triplement — sans réagir aux
    #: oscillations de quelques points.
    #:
    #: À revoir si le volume d'avis augmente fortement : plus il y a d'avis,
    #: plus les taux sont stables, et plus un même écart devient significatif.
    spike_delta_points: float = Field(default=10.0, validation_alias="ALERT_SPIKE_DELTA_POINTS")

    #: Niveau absolu déclenchant une alerte même sans hausse mesurable.
    #:
    #: Filet de sécurité de la règle de variation : une filiale durablement
    #: mauvaise ne monte plus, elle ne produirait donc jamais de « pic ». Sans
    #: ce seuil, elle resterait invisible indéfiniment.
    #:
    #: 40 % — soit plus du double de la moyenne du corpus (17 %) — et aucune
    #: filiale ne l'atteint aujourd'hui sur une fenêtre glissante. Le seuil est
    #: donc silencieux en régime normal et ne se déclenchera que sur un
    #: décrochage réel, ce qui est le comportement voulu d'un filet.
    negative_ratio_threshold: float = Field(default=0.40, validation_alias="ALERT_NEGATIVE_RATIO")

    #: Avis clients exigés sur la fenêtre pour qu'un taux soit crédible.
    min_reviews_for_ratio: int = Field(default=10, validation_alias="ALERT_MIN_REVIEWS_FOR_RATIO")

    #: Délai avant de ré-alerter sur la MÊME filiale pour le même motif.
    #: Sans lui, le pipeline tournant plusieurs fois par jour republierait la
    #: même alerte à chaque passage, et le fil métier deviendrait aussi
    #: illisible que le fil technique — exactement ce qu'on cherche à éviter.
    spike_cooldown_hours: int = Field(default=24, validation_alias="ALERT_SPIKE_COOLDOWN_HOURS")

    # Canal e-mail (SMTP)
    smtp_host: Optional[str] = Field(default=None, validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=587, validation_alias="SMTP_PORT")
    smtp_user: Optional[str] = Field(default=None, validation_alias="SMTP_USER")
    smtp_password: Optional[str] = Field(default=None, validation_alias="SMTP_PASSWORD")
    smtp_from: Optional[str] = Field(default=None, validation_alias="SMTP_FROM")
    alert_email: Optional[str] = Field(default=None, validation_alias="ALERT_EMAIL")

    # Canal webhook (Slack / Discord / Teams)
    webhook_url: Optional[str] = Field(default=None, validation_alias="ALERT_WEBHOOK_URL")

    # Canal Telegram.
    #
    # Les DEUX valeurs sont nécessaires : le jeton identifie le robot, l'
    # identifiant de conversation dit à qui écrire. Un robot Telegram ne peut
    # pas démarrer une conversation — c'est le destinataire qui doit lui parler
    # en premier, sans quoi l'envoi échoue avec « chat not found ». C'est la
    # cause d'échec la plus fréquente à la mise en place, et elle n'a rien d'un
    # défaut de configuration.
    telegram_bot_token: Optional[str] = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_chat_id: Optional[str] = Field(
        default=None, validation_alias="TELEGRAM_CHAT_ID"
    )

    #: Gravités transmises à Telegram. Un canal poussé sur un téléphone n'a pas
    #: la même tolérance qu'une boîte mail : recevoir chaque « info » apprend à
    #: ignorer les notifications, donc à manquer les critiques. Par défaut, seul
    #: ce qui appelle une réaction est poussé.
    telegram_min_severity: str = Field(
        default="warning", validation_alias="TELEGRAM_MIN_SEVERITY"
    )

    #: Types d'alertes poussés sur Telegram, séparés par des virgules. Vide =
    #: la liste par défaut du notifieur (métier + pannes franches) ; `*` =
    #: aucun filtre.
    #:
    #: La gravité seule ne suffit pas à trier : `scraper_zero` est un
    #: « warning » et représente à lui seul les trois quarts des alertes en
    #: base. Voir TelegramNotifier._TYPES_PAR_DEFAUT pour le raisonnement.
    telegram_alert_types: str = Field(
        default="", validation_alias="TELEGRAM_ALERT_TYPES"
    )


class ChatConfig(BaseSettings):
    """Assistant conversationnel Telegram.

    Répond à des questions posées en langage naturel, en traduisant chacune en
    PARAMÈTRES validés contre le contrat de filtre. Le modèle ne compose jamais
    de requête : voir `agents/questions.py`.
    """

    model_config = _ENV

    #: DÉSACTIVÉ PAR DÉFAUT, même raison que l'agent de veille : cette boucle
    #: ÉCRIT dans une conversation Telegram. Une activation implicite à la
    #: première mise à jour ferait apparaître un robot qui répond dans un groupe
    #: où personne ne l'attend.
    #:
    #: S'y ajoute une contrainte propre au long polling : un seul processus peut
    #: interroger `getUpdates` pour un robot donné. Démarrer la boucle sans
    #: l'avoir décidé volerait ses messages à une instance déjà en écoute — et
    #: les deux répondraient une question sur deux.
    enabled: bool = Field(default=False, validation_alias="ENABLE_CHAT_AGENT")

    #: Secondes pendant lesquelles Telegram garde la connexion ouverte à
    #: attendre un message.
    #:
    #: 25 s et non 0 : à zéro, la boucle interrogerait l'API en continu pour
    #: n'obtenir presque toujours rien. Le long polling rend la réponse
    #: quasi immédiate tout en tenant trois requêtes par minute. Au-delà de
    #: 50 s, les coupures d'intermédiaires réseau deviennent fréquentes.
    poll_timeout: int = Field(default=25, ge=0, le=50, validation_alias="CHAT_POLL_TIMEOUT")

    #: Questions autorisées par personne sur 24 heures glissantes.
    #:
    #: LE BUDGET DE MODÈLE EST PARTAGÉ : 200 appels par jour couvrent aussi
    #: l'analyse sémantique (jusqu'à 200 appels programmés) et le briefing
    #: quotidien. Une question en coûte deux — traduction puis rédaction. Vingt
    #: questions plafonnent donc une personne à 40 appels, soit un cinquième du
    #: budget : de quoi explorer sérieusement sans que la veille du lendemain en
    #: pâtisse.
    daily_questions_per_user: int = Field(
        default=20, ge=1, validation_alias="CHAT_DAILY_QUESTIONS"
    )

    #: Conversations autorisées à interroger le robot, séparées par des virgules.
    #:
    #: Vide = on retombe sur `TELEGRAM_CHAT_ID`, jamais sur « tout le monde » :
    #: le nom d'un robot est public et sa conversation privée est ouverte à
    #: quiconque le trouve. Voir `telegram_chat.build_boucle`.
    #:
    #: Un identifiant de conversation privée est celui de la PERSONNE (positif) ;
    #: un identifiant de groupe est négatif. Les deux se mettent ici.
    allowed_chat_ids: str = Field(default="", validation_alias="CHAT_ALLOWED_CHAT_IDS")

    def allowed_chat_ids_list(self) -> list[int]:
        """Identifiants valides de la liste blanche.

        Les entrées illisibles sont IGNORÉES avec un avertissement plutôt que de
        faire échouer le démarrage : une virgule en trop ne doit pas empêcher le
        robot de répondre aux conversations correctement déclarées.
        """
        out: list[int] = []
        for brut in self.allowed_chat_ids.split(","):
            brut = brut.strip()
            if not brut:
                continue
            try:
                out.append(int(brut))
            except ValueError:
                logger.warning("CHAT_ALLOWED_CHAT_IDS : « %s » ignoré (entier attendu)", brut)
        return out


class LLMConfig(BaseSettings):
    """Analyse sémantique et synthèses en langage naturel.

    ENTIÈREMENT OPTIONNEL. Sans `LLM_API_KEY`, la couche est inerte : le
    lexique continue de classer tous les avis, le dashboard fonctionne, et les
    écrans concernés affichent la raison au lieu d'une erreur.

    Les valeurs par défaut visent le niveau GRATUIT de Gemini, à travers sa
    couche compatible OpenAI. C'est ce qui rend le fournisseur interchangeable
    sans toucher au code : Groq, Mistral, OpenRouter ou un modèle servi
    localement parlent le même dialecte, et se substituent en changeant
    `LLM_BASE_URL` et `LLM_MODEL`.
    """

    model_config = _ENV

    enabled: bool = Field(default=True, validation_alias="ENABLE_LLM")

    #: Cloison de comptabilité dans `llm_usage`, et RIEN D'AUTRE.
    #:
    #: Ce n'est pas le nom du fournisseur : changer `LLM_BASE_URL` de Gemini
    #: vers Groq ne doit pas scinder l'historique du jour en deux lignes ni
    #: remettre le budget à zéro en milieu de journée. Ce champ dit à quel
    #: USAGE le budget est adossé.
    #:
    #: Non lié à une variable d'environnement, délibérément : il est fixé par
    #: le code qui construit le client (voir `quality_llm_config`), jamais par
    #: l'exploitant. Un profil réglable par `.env` permettrait à une faute de
    #: frappe de faire repartir un compteur à zéro — c'est-à-dire de désactiver
    #: silencieusement le garde-fou qu'il sert à tenir.
    profil: str = "defaut"

    #: Clé d'API. Absente = fonctionnalité dormante, jamais une erreur.
    api_key: Optional[str] = Field(default=None, validation_alias="LLM_API_KEY")

    base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai",
        validation_alias="LLM_BASE_URL",
    )

    #: Modèle de classification, appelé sur des MILLIERS d'avis : il doit être
    #: le moins cher et le plus rapide de la gamme, la tâche étant du
    #: rangement dans une liste fermée, pas de la rédaction.
    model: str = Field(default="gemini-2.5-flash-lite", validation_alias="LLM_MODEL")

    #: Modèle des synthèses. Quelques appels par jour, mais un texte lu par un
    #: décideur : un modèle un cran au-dessus est justifié ici, et seulement
    #: ici. Vide = on réutilise `model`.
    synthesis_model: Optional[str] = Field(
        default=None, validation_alias="LLM_SYNTHESIS_MODEL"
    )

    timeout: int = Field(default=60, validation_alias="LLM_TIMEOUT")
    max_retries: int = Field(default=3, validation_alias="LLM_MAX_RETRIES")
    retry_backoff_max: float = Field(default=30.0, validation_alias="LLM_RETRY_BACKOFF_MAX")
    max_tokens: int = Field(default=1600, validation_alias="LLM_MAX_TOKENS")

    #: Température basse et non nulle : on veut une réponse reproductible. Une
    #: synthèse est mise en cache et relue par d'autres — deux lecteurs de la
    #: même carte ne doivent pas obtenir deux textes différents.
    temperature: float = Field(default=0.2, validation_alias="LLM_TEMPERATURE")

    #: Délai minimal entre deux appels, en secondes.
    #:
    #: Le niveau gratuit se compte en appels par MINUTE autant que par jour, et
    #: Google ne publie plus ces valeurs — elles se consultent par projet dans
    #: AI Studio et ont déjà été réduites sans préavis. 6 s tient une dizaine
    #: d'appels par minute, ce qui reste sous les seuils gratuits connus tout en
    #: laissant un backfill avancer.
    min_interval_seconds: float = Field(
        default=6.0, validation_alias="LLM_MIN_INTERVAL_SECONDS"
    )

    #: Plafond d'appels par jour, tous usages confondus. Compté EN BASE, donc
    #: il survit à un redémarrage du worker. C'est le garde-fou qui empêche une
    #: boucle défaillante de consommer un quota entier pendant la nuit.
    daily_call_budget: int = Field(default=200, validation_alias="LLM_DAILY_CALL_BUDGET")

    #: Avis envoyés par appel lors de l'analyse sémantique.
    #:
    #: Le levier principal du coût : à 20 avis par appel, 3 300 avis tiennent en
    #: 165 appels. Monter trop haut dégrade la qualité (le modèle survole) et
    #: risque de tronquer la réponse ; descendre trop bas multiplie les appels,
    #: donc épuise le quota gratuit.
    batch_size: int = Field(default=20, validation_alias="LLM_BATCH_SIZE")

    #: Caractères d'avis transmis au modèle. Au-delà, on tronque : la fin d'un
    #: pavé de 4 000 caractères n'apporte plus d'aspect nouveau et fait payer
    #: des jetons pour rien.
    max_review_chars: int = Field(default=700, validation_alias="LLM_MAX_REVIEW_CHARS")

    #: Avis analysés à chaque passage du planificateur.
    #:
    #: Sans ce passage automatique, les aspects ne se rempliraient que par une
    #: commande lancée à la main — donc jamais sur un environnement de test où
    #: personne ne se connecte, et l'onglet Motifs resterait vide.
    #:
    #: PORTÉ DE 300 À 1000 SUR MESURE. Le lexique et le LLM ont été comparés sur
    #: les 3 760 avis que les deux avaient jugés, avec la note comme vérité
    #: terrain : 68,2 % d'exactitude pour le lexique, 85,2 % pour le LLM.
    #: Dix-sept points d'écart.
    #:
    #: Pire que l'écart global, son INÉGALITÉ : le lexique tient 79 % en Afrique
    #: australe (anglophone) mais tombe à 62 % en Afrique du Nord et 61 % en
    #: Afrique centrale. Or ce tableau de bord existe pour comparer des filiales
    #: entre pays — un classifieur dont la qualité varie de 18 points selon la
    #: langue fait passer du bruit de mesure pour de l'écart de satisfaction.
    #:
    #: À 300, quatre passages quotidiens traitaient 1 200 avis, soit 60 appels :
    #: le plafond bridait bien en deçà du budget, qui en autorise 200. À 1000,
    #: les quatre passages consomment exactement les 200 appels disponibles et
    #: rattrapent 4 000 avis par jour. Le budget quotidien reste la borne dure.
    scheduled_batch_limit: int = Field(
        default=1000, validation_alias="LLM_SCHEDULED_BATCH_LIMIT"
    )

    def effective_synthesis_model(self) -> str:
        return self.synthesis_model or self.model


class QualityLLMConfig(BaseSettings):
    """Fournisseur de modèle PROPRE à l'Agent 3.

    POURQUOI UN SECOND PROFIL PLUTÔT QUE LE CLIENT EXISTANT
        Le budget par défaut est de 200 appels par jour, et il est DÉJÀ
        entièrement engagé : l'analyse sémantique en programme jusqu'à 200, le
        briefing quotidien en consomme quelques-uns, l'assistant
        conversationnel en autorise 40 par personne. Brancher un quatrième
        consommateur sur ce compteur ne prendrait pas « un peu de marge » : il
        assécherait l'analyse sémantique, dont dépendent les aspects, donc les
        motifs, donc la ligne la plus utile du briefing de l'Agent 1.

        D'où un profil distinct, avec sa clé, son fournisseur et son budget.
        La cloison est tenue en base par `llm_usage.profil` (migration 022).

    CE QUI A ÉTÉ VÉRIFIÉ SUR OLLAMA CLOUD, ET NON SUPPOSÉ (17 août 2026)
        - L'endpoint compatible OpenAI EXISTE : une requête sans clé sur
          `https://ollama.com/v1/chat/completions` rend HTTP 401, et non 404.
          C'est décisif : le `LLMClient` de ce projet parle déjà ce dialecte,
          donc Ollama Cloud est enfichable SANS UNE LIGNE DE CODE — deux
          variables suffisent.
        - Un palier gratuit à 0 $ existe réellement, avec un modèle cloud à la
          fois et un usage qualifié de « léger ».
        - MAIS les quotas gratuits ne sont PAS publiés en nombres : ils se
          comptent en TEMPS GPU, avec remise à zéro par fenêtres de 5 h et de
          7 jours. Aucune limite chiffrée ne peut donc être inscrite ici de
          bonne foi.

        Conséquence directe sur la conception : `daily_call_budget` reste la
        borne dure et locale. On ne peut pas faire confiance à un quota qu'on
        ne peut pas lire, donc on ne s'y adosse pas — on se plafonne soi-même.

    LE REPLI EST EXPLICITE, JAMAIS AUTOMATIQUE ET SILENCIEUX
        Sans clé propre, l'Agent 3 n'emprunte PAS la clé Gemini de son propre
        chef : il fonctionne en mode déterministe (règles seules) et le dit.
        C'est la même règle que partout ailleurs ici — sans clé, la couche est
        dormante, jamais en erreur. Emprunter le quota d'un autre agent en
        silence serait exactement la panne que ce profil existe pour empêcher.
        Le repli sur Gemini doit être DEMANDÉ (`LLM3_FALLBACK_GEMINI=true`).
    """

    model_config = _ENV

    enabled: bool = Field(default=True, validation_alias="ENABLE_QUALITY_LLM")

    #: Fournisseur, à titre DOCUMENTAIRE et pour le diagnostic affiché.
    #:
    #: Le code ne s'en sert pas pour router : c'est `base_url` qui décide, et
    #: c'est voulu. Un aiguillage sur ce nom obligerait à modifier le code pour
    #: chaque nouveau fournisseur, alors que le dialecte est le même partout.
    #: Valeurs d'usage : ollama_cloud, ollama_local, gemini, groq, zai, autre.
    provider: str = Field(default="ollama_cloud", validation_alias="LLM3_PROVIDER")

    api_key: Optional[str] = Field(default=None, validation_alias="LLM3_API_KEY")

    #: Repères VÉRIFIÉS, à recopier dans `.env` selon le fournisseur retenu :
    #:
    #:   ollama_cloud  https://ollama.com/v1                gpt-oss:20b
    #:   ollama_local  http://localhost:11434/v1            qwen2.5:7b
    #:   gemini        https://generativelanguage.googleapis.com/v1beta/openai
    #:   groq          https://api.groq.com/openai/v1
    #:   zai (GLM)     https://api.z.ai/api/paas/v4
    base_url: str = Field(
        default="https://ollama.com/v1", validation_alias="LLM3_BASE_URL"
    )

    #: `gpt-oss:20b` et non `120b` : la tâche de l'Agent 3 est du RANGEMENT
    #: dans une liste fermée (cet avis est-il du spam ? parle-t-il de cet
    #: opérateur ?), pas de la rédaction. Le petit modèle y suffit, et sur un
    #: palier gratuit compté en temps GPU, le modèle le plus léger est
    #: littéralement celui qui rend le plus d'appels par semaine.
    model: str = Field(default="gpt-oss:20b", validation_alias="LLM3_MODEL")

    #: Autoriser le repli sur la configuration LLM principale (Gemini) quand
    #: aucune clé propre n'est renseignée.
    #:
    #: FAUX PAR DÉFAUT. Voir la docstring : un repli implicite ferait de
    #: l'Agent 3 le consommateur du quota qu'il devait épargner, et personne ne
    #: s'en apercevrait avant de voir l'analyse sémantique s'arrêter.
    fallback_gemini: bool = Field(
        default=False, validation_alias="LLM3_FALLBACK_GEMINI"
    )

    #: Budget propre. 60 et non 200 : l'Agent 3 n'a pas besoin de volume. Il
    #: n'appelle le modèle que sur des cas DÉJÀ triés par des règles
    #: déterministes — quelques dizaines d'avis douteux par passage, pas le
    #: corpus. Un budget large ne le rendrait pas meilleur, il le rendrait
    #: seulement capable de brûler un quota sur une boucle défaillante.
    daily_call_budget: int = Field(
        default=60, validation_alias="LLM3_DAILY_CALL_BUDGET"
    )

    timeout: int = Field(default=90, validation_alias="LLM3_TIMEOUT")

    #: 3 s et non 6 : sur un fournisseur dédié, la contrainte par minute n'est
    #: pas partagée avec l'analyse sémantique. Reste espacé — un palier gratuit
    #: se compte toujours aussi par minute, même quand il ne le dit pas.
    min_interval_seconds: float = Field(
        default=3.0, validation_alias="LLM3_MIN_INTERVAL_SECONDS"
    )

    #: Avis envoyés par appel de validation. Plus petit que les 20 de l'analyse
    #: sémantique : ici le modèle doit rendre un VERDICT argumenté par avis
    #: (pertinent, opérateur, filiale, spam, sujet, raison), soit une sortie
    #: nettement plus verbeuse. À 20, la réponse se tronque et le lot est perdu
    #: — la panne exactement mesurée sur le backfill sémantique (4 % de lots
    #: perdus, voir `_TOKENS_PER_REVIEW`).
    batch_size: int = Field(default=8, validation_alias="LLM3_BATCH_SIZE")

    max_review_chars: int = Field(
        default=600, validation_alias="LLM3_MAX_REVIEW_CHARS"
    )


class QualityConfig(BaseSettings):
    """Agent 3 — seuils de diagnostic et pondérations du score de qualité.

    LES POIDS SONT ICI, EXPLICITES ET MODIFIABLES, parce que l'énoncé l'exige
    et parce qu'un score composite dont les poids sont enfouis dans le code est
    indéfendable : la première question posée devant « 42 % » est « pourquoi
    42 ? », et il faut pouvoir y répondre sans ouvrir un fichier Python.
    """

    model_config = _ENV

    #: DÉSACTIVÉ PAR DÉFAUT, comme les agents 1 et 2 et pour la même raison :
    #: ce passage ÉCRIT dans un groupe Telegram. Une activation implicite à la
    #: première mise à jour enverrait un message non sollicité à toute l'équipe.
    enabled: bool = Field(default=False, validation_alias="ENABLE_QUALITY_AGENT")
    hour: int = Field(default=7, ge=0, le=23, validation_alias="QUALITY_AGENT_HOUR")

    #: Le garde-fou consulté par les Agents 1 et 2 avant de parler.
    #:
    #: ACTIF PAR DÉFAUT, contrairement au passage de l'agent lui-même — et la
    #: dissymétrie est voulue. Le passage ÉCRIT dans un groupe Telegram, donc
    #: il s'active à la demande. Le garde-fou, lui, n'écrit nulle part : il
    #: n'ajoute qu'une phrase de réserve à des briefings qui partaient déjà.
    #: Le laisser à faux par défaut reviendrait à livrer un garde-fou installé
    #: mais débranché, ce qui est la pire des deux options : on croit être
    #: protégé.
    #:
    #: Tant qu'aucun instantané n'existe, il rend `INDETERMINE` et LAISSE
    #: PASSER : activer ce drapeau sur une base neuve ne change donc rien
    #: jusqu'au premier passage de l'agent.
    gate_enabled: bool = Field(default=True, validation_alias="ENABLE_QUALITY_GATE")

    # --- Seuils de couverture ---------------------------------------------

    #: En dessous, une filiale est « sous-couverte » : ses taux ne sont pas
    #: comparables à ceux d'une filiale à quatre cents avis.
    #:
    #: 10 et non 30 : aligné sur `ALERT_MIN_REVIEWS_FOR_RATIO`, déjà le seuil
    #: retenu par l'alerting pour juger un taux crédible. Deux seuils
    #: différents pour la même question produiraient un écran annonçant
    #: « données insuffisantes » à côté d'une alerte fondée sur ces données.
    min_reviews: int = Field(default=10, validation_alias="QUALITY_MIN_REVIEWS")

    #: Sources d'avis clients attendues pour qu'une filiale soit dite diverse.
    #:
    #: 2 et non 3, et c'est une mesure et non une opinion : sur les 135
    #: filiales, Google Maps est la SEULE source de 130 d'entre elles, et
    #: 86 seulement déclarent une application. Exiger trois sources
    #: déclarerait la majorité du périmètre en défaut pour une raison qui ne
    #: dépend pas de nous — les plateformes n'existent pas dans ces pays.
    min_sources: int = Field(default=2, validation_alias="QUALITY_MIN_SOURCES")

    # --- Seuils de fraîcheur ----------------------------------------------

    #: Multiple de la cadence déclarée au-delà duquel une source est « stale ».
    #:
    #: Un multiple, jamais un délai fixe : les cadences vont de 6 h (flux RSS)
    #: à 24 h (Google Maps). « Pas de collecte depuis 48 h » est normal pour
    #: l'un et grave pour l'autre. Un seuil absolu produirait donc soit du
    #: bruit sur les sources lentes, soit de la cécité sur les rapides.
    #:
    #: 3 : deux occurrences manquées peuvent être un redémarrage, trois est un
    #: motif. C'est le même raisonnement que `misfire_grace_time`.
    stale_factor: float = Field(default=3.0, validation_alias="QUALITY_STALE_FACTOR")

    # --- Détection de volume anormal --------------------------------------

    #: Facteur d'augmentation à partir duquel un volume journalier est jugé
    #: anormal, comparé à la médiane des jours précédents.
    #:
    #: MÉDIANE ET NON MOYENNE : la collecte est par nature en dents de scie
    #: (un passage Google Maps insère des centaines d'avis d'un coup, le
    #: suivant zéro). Une moyenne serait tirée par ces pics et ne
    #: déclencherait jamais ; la médiane décrit la journée typique.
    #:
    #: 10 : l'exemple de l'énoncé (200 → 25 000) vaut 125. Un facteur 10 attrape
    #: donc largement le cas visé sans réagir à un rattrapage de deux jours.
    volume_spike_factor: float = Field(
        default=10.0, validation_alias="QUALITY_VOLUME_SPIKE_FACTOR"
    )

    #: Plancher de volume sous lequel on ne parle jamais d'anomalie. Sans lui,
    #: passer de 1 à 12 avis déclencherait une alerte de « volume anormal ».
    volume_min_baseline: int = Field(
        default=20, validation_alias="QUALITY_VOLUME_MIN_BASELINE"
    )

    #: Longueur en deçà de laquelle un texte d'avis est jugé non exploitable.
    #:
    #: Aligné sur `REDDIT_MIN_COMMENT_CHARS` (30) : « same », « +1 » ou « ok »
    #: ne portent aucun sentiment analysable. Deux seuils différents pour la
    #: même notion se contrediraient d'un écran à l'autre.
    min_text_chars: int = Field(default=30, validation_alias="QUALITY_MIN_TEXT_CHARS")

    # --- Pondérations du score global -------------------------------------
    #
    # SOMME ATTENDUE : 1,0. La normalisation est faite par le code (voir
    # `score.poids_normalises`) plutôt qu'imposée par une validation : un
    # exploitant qui veut doubler le poids de la couverture ne doit pas avoir à
    # recalculer les cinq autres pour que ça marche.
    #
    # L'ORDRE DE GRANDEUR EST LE MESSAGE : la couverture pèse presque le double
    # de tout le reste, parce que c'est la seule composante dont la défaillance
    # rend les autres SANS OBJET. La fraîcheur d'une filiale sans aucun avis
    # n'est pas une question — et un score qui la noterait à 100 % sur cinq
    # composantes vides afficherait « 83 % » sur une filiale sans données.

    #: Y a-t-il assez d'avis, et sur assez de sources attendues ?
    weight_coverage: float = Field(default=0.30, validation_alias="QUALITY_W_COVERAGE")
    #: Les données arrivent-elles encore ?
    weight_freshness: float = Field(default=0.20, validation_alias="QUALITY_W_FRESHNESS")
    #: Les avis sont-ils exploitables (texte, note, date) ?
    weight_completeness: float = Field(
        default=0.15, validation_alias="QUALITY_W_COMPLETENESS"
    )
    #: Combien de constats de qualité non résolus pèsent sur cette filiale ?
    weight_consistency: float = Field(
        default=0.15, validation_alias="QUALITY_W_CONSISTENCY"
    )
    #: Le signal repose-t-il sur une seule source ?
    weight_diversity: float = Field(default=0.10, validation_alias="QUALITY_W_DIVERSITY")
    #: Les collecteurs réussissent-ils ?
    weight_reliability: float = Field(
        default=0.10, validation_alias="QUALITY_W_RELIABILITY"
    )

    # --- Seuils de confiance (Data Trust Status) --------------------------
    #
    # CE SONT LES SEULS CHIFFRES QUE LISENT LES AGENTS 1 ET 2. Ils ne
    # comparent pas des scores : ils lisent un mot. Un seuil unique, lu au même
    # endroit par tous, vaut mieux que trois comparaisons numériques dispersées
    # qui finiraient par diverger.
    trusted_at: float = Field(default=0.75, validation_alias="QUALITY_TRUSTED_AT")
    acceptable_at: float = Field(default=0.55, validation_alias="QUALITY_ACCEPTABLE_AT")
    degraded_at: float = Field(default=0.30, validation_alias="QUALITY_DEGRADED_AT")

    # --- Retenue ------------------------------------------------------------

    #: Sujets remontés au maximum par notification. Même raison que l'Agent 1 :
    #: un briefing de dix sujets n'est pas lu, et un briefing qu'on ne lit pas
    #: est pire qu'aucun — il donne le sentiment d'être couvert.
    max_sujets: int = Field(default=3, validation_alias="QUALITY_MAX_SUJETS")

    #: Jours pendant lesquels on ne redit pas la même chose sur la même filiale.
    #:
    #: 7 et non 3 : un trou de couverture ne se comble pas en trois jours. Le
    #: signaler tous les trois jours apprendrait à ignorer l'agent, sans que le
    #: destinataire ait rien pu faire entre-temps.
    cooldown_jours: int = Field(default=7, validation_alias="QUALITY_COOLDOWN_JOURS")

    #: Chute de score (en points sur 100) faisant reparler malgré le
    #: refroidissement. Même dissymétrie que l'Agent 1 : « déjà dit » fait
    #: taire, « déjà dit mais pire » fait parler.
    aggravation_points: float = Field(
        default=10.0, validation_alias="QUALITY_AGGRAVATION_POINTS"
    )

    # --- Découverte de sources ---------------------------------------------

    #: Sonder réellement les URL candidates en HTTP.
    #:
    #: C'est ce qui sépare une proposition d'une hypothèse. Désactivable pour
    #: les tests et pour un environnement sans accès sortant, auquel cas les
    #: candidates restent au statut CANDIDATE avec `accessibility='inconnu'` —
    #: jamais présentées comme vérifiées.
    probe_enabled: bool = Field(default=True, validation_alias="QUALITY_PROBE_ENABLED")
    probe_timeout: int = Field(default=15, validation_alias="QUALITY_PROBE_TIMEOUT")

    #: Candidates proposées au maximum par filiale. Trois suffisent : une liste
    #: de quinze pistes non instruites n'est pas un travail, c'est un report de
    #: travail sur le lecteur.
    max_candidates: int = Field(default=3, validation_alias="QUALITY_MAX_CANDIDATES")

    def poids(self) -> dict[str, float]:
        """Pondérations telles que configurées, avant normalisation."""
        return {
            "coverage": self.weight_coverage,
            "freshness": self.weight_freshness,
            "completeness": self.weight_completeness,
            "consistency": self.weight_consistency,
            "diversity": self.weight_diversity,
            "reliability": self.weight_reliability,
        }


class APIConfig(BaseSettings):
    """Configuration du service API (FastAPI)."""
    model_config = _ENV
    host: str = Field(default="0.0.0.0", validation_alias="API_HOST")
    port: int = Field(default=8000, validation_alias="API_PORT")
    cors_origins: str = Field(default="*", validation_alias="API_CORS_ORIGINS")  # CSV

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


#: Intervalle propre à chaque collecteur, en minutes. `None` = suivre
#: l'intervalle global du planificateur.
#:
#: POURQUOI CE RÉGLAGE EXISTE
#:     Les sources n'ont ni le même coût ni le même rythme de renouvellement.
#:     Mesuré sur `run_metrics` : Google Maps tourne à 1 030 s de moyenne et
#:     3 332 s au pire, contre 37 s pour un flux RSS — un facteur 28. Les faire
#:     tourner à la même fréquence, c'est soit gaspiller des heures de
#:     navigateur sur des fiches qui reçoivent un avis par mois, soit brider les
#:     sources rapides au rythme de la plus lente.
#:
#:     Le rythme suit donc la RÉALITÉ de chaque source : une agence Google Maps
#:     reçoit ~1,7 avis par mois, la repasser toutes les six heures ne rapporte
#:     rien ; une plateforme de plaintes ou un flux de presse bougent chaque
#:     heure.
_SCRAPER_INTERVALS: dict[str, Optional[int]] = {
    # 24 h : le collecteur le plus lent du projet, sur des fiches qui bougent
    # lentement. C'est aussi ce qui permet d'aller chercher PLUSIEURS agences
    # par recherche sans faire exploser la durée d'un cycle.
    "googlemaps": 1440,
    # 12 h : l'API impose 6 s entre deux appels, soit ~13 min pour 132 filiales.
    # La presse ne se renouvelle pas assez vite pour justifier davantage.
    "gdelt": 720,
    # Les autres suivent l'intervalle global (6 h par défaut) : ils coûtent
    # moins d'une minute et leurs sources bougent en continu.
    "trustpilot": None,
    "playstore": None,
    "appstore": None,
    "rss_feed": None,
    "hellopeter": None,
    "press_feed": None,
    # 12 h, pour la même raison que GDELT et avec une contrainte plus dure
    # encore : Reddit n'accorde qu'UNE requête par minute, soit ~20 min pour la
    # liste de subreddits. Un fil de forum se commente pendant des jours, le
    # repasser toutes les six heures ne ramènerait presque rien de neuf.
    "reddit": 720,
}


class Settings(BaseSettings):
    """Configuration globale de l'application."""

    model_config = _ENV

    debug: bool = Field(default=False, validation_alias="DEBUG")
    base_dir: Path = Path(__file__).resolve().parent.parent
    data_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "data"
    )

    # Sous-configurations (default_factory => rien n'est lu tant que Settings
    # n'est pas construit)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    trustpilot: TrustpilotConfig = Field(default_factory=TrustpilotConfig)
    playstore: PlayStoreConfig = Field(default_factory=PlayStoreConfig)
    appstore: AppStoreConfig = Field(default_factory=AppStoreConfig)
    googlemaps: GoogleMapsConfig = Field(default_factory=GoogleMapsConfig)
    rss_feed: RSSFeedConfig = Field(default_factory=RSSFeedConfig)
    hellopeter: HelloPeterConfig = Field(default_factory=HelloPeterConfig)
    gdelt: GDELTConfig = Field(default_factory=GDELTConfig)
    press_feed: PressFeedConfig = Field(default_factory=PressFeedConfig)
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    quality_llm: QualityLLMConfig = Field(default_factory=QualityLLMConfig)

    @field_validator("data_dir", mode="before")
    @classmethod
    def create_data_dir(cls, v):
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        (path / "state").mkdir(exist_ok=True)
        (path / "logs").mkdir(exist_ok=True)
        return path

    def get_enabled_scrapers(self) -> list[str]:
        mapping = {
            "trustpilot": self.trustpilot.enabled,
            "playstore": self.playstore.enabled,
            "appstore": self.appstore.enabled,
            "googlemaps": self.googlemaps.enabled,
            "rss_feed": self.rss_feed.enabled,
            "hellopeter": self.hellopeter.enabled,
            "gdelt": self.gdelt.enabled,
            "press_feed": self.press_feed.enabled,
            # `enabled` tant que le collecteur tourne sur le flux Atom, qui ne
            # demande aucun identifiant. À BASCULER SUR `self.reddit.usable()`
            # en même temps que le passage à PRAW : celui-ci ne peut rien
            # collecter sans clés, et un collecteur activé mais muet rendrait
            # « 0 avis » à chaque run — que l'alerting prendrait pour une panne.
            "reddit": self.reddit.enabled,
        }
        return [name for name, enabled in mapping.items() if enabled]

    def scraper_interval_minutes(self, name: str) -> int:
        """Cadence propre à un collecteur, en minutes.

        Surchargeable par variable d'environnement, une par collecteur :
        `SCRAPER_INTERVAL_GOOGLEMAPS=2880` pour repasser Google Maps tous les
        deux jours. Sans surcharge, la valeur de `_SCRAPER_INTERVALS`, et à
        défaut l'intervalle global.
        """
        surcharge = os.environ.get(f"SCRAPER_INTERVAL_{name.upper()}")
        if surcharge:
            try:
                valeur = int(surcharge)
                if valeur > 0:
                    return valeur
            except ValueError:
                logger.warning(
                    "SCRAPER_INTERVAL_%s=%r ignoré : entier attendu",
                    name.upper(), surcharge,
                )
        return _SCRAPER_INTERVALS.get(name) or self.scheduler.interval_minutes

    def unit_run_budget_seconds(self, name: str) -> int:
        """Temps accordé à UN passage d'un collecteur piloté par unités.

        Le passage traite ce qu'il peut dans ce budget et laisse le reste dans
        `collection_jobs` : il se termine donc toujours, quel que soit le
        périmètre. C'est ce qui garantit que la fin du run — et donc
        l'évaluation des alertes — est atteinte à chaque cycle.

        Le repli vaut la moitié de la cadence de la source : un passage qui
        déborderait de sa propre cadence rendrait le découpage inutile.
        """
        budgets = {
            "googlemaps": self.googlemaps.run_budget_seconds,
            "gdelt": self.gdelt.run_budget_seconds,
        }
        return budgets.get(name) or max(
            60, self.scraper_interval_minutes(name) * 60 // 2
        )


@lru_cache
def get_settings() -> Settings:
    """Retourne l'instance unique de configuration (construite à la demande)."""
    return Settings()


def quality_llm_config(settings: Optional[Settings] = None) -> Optional[LLMConfig]:
    """Configuration LLM de l'Agent 3, ou None s'il doit rester déterministe.

    RÉSOUT LE PROFIL EN UNE SEULE FONCTION, et c'est le point : la règle de
    repli doit exister à UN endroit. Dispersée entre la CLI, le planificateur
    et l'API, elle divergerait — et l'agent n'appellerait pas le même
    fournisseur selon la façon dont on le lance, ce qui rend tout écart de
    résultat impossible à expliquer.

    Trois cas, dans cet ordre :

      1. Une clé propre est renseignée -> on l'utilise, sur `LLM3_BASE_URL`.
      2. Pas de clé propre, mais `LLM3_FALLBACK_GEMINI=true` -> on emprunte la
         configuration principale, en CONSERVANT le profil « qualite » : les
         appels restent comptés à part, donc le budget de l'analyse sémantique
         reste protégé même quand la clé est partagée. C'est là tout l'intérêt
         d'avoir séparé le profil du fournisseur.
      3. Ni l'un ni l'autre -> None. L'agent tourne alors en règles seules et
         l'annonce. Il n'emprunte JAMAIS le quota d'un autre agent en silence.

    Renvoie un `LLMConfig` plutôt qu'un client : la construction du client
    demande une base de données, que ce module n'a pas à connaître.
    """
    settings = settings or get_settings()
    cfg = settings.quality_llm

    if not cfg.enabled:
        return None

    if cfg.api_key:
        return LLMConfig(
            enabled=True,
            profil="qualite",
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            # Le modèle de synthèse est le MÊME : l'Agent 3 ne rédige pas de
            # texte lu par un décideur, il classe. Payer un modèle supérieur
            # pour ranger dans une liste fermée n'achèterait rien.
            synthesis_model=cfg.model,
            timeout=cfg.timeout,
            max_tokens=settings.llm.max_tokens,
            temperature=settings.llm.temperature,
            min_interval_seconds=cfg.min_interval_seconds,
            daily_call_budget=cfg.daily_call_budget,
            batch_size=cfg.batch_size,
            max_review_chars=cfg.max_review_chars,
        )

    if cfg.fallback_gemini and settings.llm.enabled and settings.llm.api_key:
        logger.info(
            "Agent 3 : aucune clé LLM3_API_KEY, repli EXPLICITE sur la "
            "configuration principale (budget cloisonné sous le profil « qualite »)."
        )
        return settings.llm.model_copy(
            update={
                "profil": "qualite",
                "daily_call_budget": cfg.daily_call_budget,
                "batch_size": cfg.batch_size,
                "max_review_chars": cfg.max_review_chars,
            }
        )

    logger.info(
        "Agent 3 : aucun modèle configuré (LLM3_API_KEY absente et "
        "LLM3_FALLBACK_GEMINI=false). La validation sémantique est inactive ; "
        "les règles déterministes continuent de tourner."
    )
    return None
