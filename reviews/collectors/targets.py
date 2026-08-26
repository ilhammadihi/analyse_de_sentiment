"""
Cibles de collecte, chargées depuis config/operators.json.

Une seule source de vérité pour « quelles filiales collecter, et avec quel
identifiant sur quelle plateforme ». Ajouter un opérateur est désormais une
modification de données (le JSON), plus une modification de code dans les
cinq collecteurs.

Une filiale dont la source vaut `null` est simplement ignorée par le
collecteur concerné : toutes les filiales ne sont pas présentes sur toutes
les plateformes (Moov Centrafrique n'a aucune app, par exemple).
"""

import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

from reviews.collectors.countries import country_names

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
CONFIG_PATH = _CONFIG_DIR / "operators.json"
CITIES_PATH = _CONFIG_DIR / "cities.json"


@lru_cache
def load_subsidiaries() -> list[dict]:
    """Toutes les filiales déclarées (lu une seule fois, puis mis en cache)."""
    if not CONFIG_PATH.exists():
        logger.error("Config des opérateurs introuvable : %s", CONFIG_PATH)
        return []
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Config des opérateurs illisible (%s) : %s", CONFIG_PATH, e)
        return []
    return data.get("subsidiaries", [])


@lru_cache
def load_cities() -> dict[str, list[str]]:
    """Principales villes par ISO2. Vide si le fichier manque.

    Absence tolérée : sans villes, la collecte Google Maps retombe sur la
    requête générique par pays, c'est-à-dire exactement le comportement
    d'avant. Un fichier manquant dégrade la densité, il ne casse rien.
    """
    if not CITIES_PATH.exists():
        logger.warning("config/cities.json absent : Google Maps reste au niveau pays")
        return {}
    try:
        data = json.loads(CITIES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("config/cities.json illisible : %s", e)
        return {}
    return data.get("cities", {})


def _targets_for(source: str) -> list[tuple[dict, dict]]:
    """(filiale, config_source) pour les filiales où la source est renseignée."""
    out = []
    for sub in load_subsidiaries():
        cfg = (sub.get("sources") or {}).get(source)
        if cfg:
            out.append((sub, cfg))
    return out


def _apps_of(cfg: dict) -> list[dict]:
    """Applications déclarées pour une boutique, quel que soit le format.

    DEUX FORMATS ACCEPTÉS, et c'est délibéré :

        {"app_id": "123", ...}                     <- historique, UNE app
        {"apps": [{"app_id": "123"}, {...}]}       <- actuel, PLUSIEURS apps

    Un opérateur publie plusieurs applications par pays — self-care, mobile
    money, TV — et l'application de paiement pèse souvent autant que l'autre :
    mesuré, VodaPay compte 178 150 notes et MoMo PSB 10 802. N'en déclarer
    qu'une laissait l'essentiel de côté.

    Accepter les deux formes évite une migration en bloc de 132 entrées de
    configuration : celles qui n'ont pas encore été redécouvertes continuent de
    fonctionner à l'identique.
    """
    if not cfg:
        return []
    apps = cfg.get("apps")
    if isinstance(apps, list):
        return [a for a in apps if isinstance(a, dict)]
    return [cfg]      # forme historique : la configuration EST l'application


def appstore_apps() -> list[dict]:
    """[{app_id, name, country, app_label}] — attendu par AppStoreScraper.

    Une entrée PAR APPLICATION, et plusieurs peuvent porter le même `name` :
    c'est le nom de la FILIALE, qui reste la clé de rattachement dimensionnel.
    La distinction entre applications passe par `app_id`.
    """
    out = []
    for sub, cfg in _targets_for("appstore"):
        for app in _apps_of(cfg):
            if not app.get("app_id"):
                continue
            out.append({
                "app_id": str(app["app_id"]),
                "name": sub["subsidiary_name"],
                "country": app.get("store_country", sub["iso2"].lower()),
                "app_label": app.get("_name"),
            })
    return out


def playstore_apps() -> list[dict]:
    """[{package_id, name, country, app_label}] — attendu par PlayStoreScraper."""
    out = []
    for sub, cfg in _targets_for("playstore"):
        for app in _apps_of(cfg):
            if not app.get("package_id"):
                continue
            out.append({
                "package_id": app["package_id"],
                "name": sub["subsidiary_name"],
                "country": sub["iso2"].lower(),
                "app_label": app.get("_name"),
            })
    return out


def googlemaps_locations(cities_per_run: int = 0,
                         today: date | None = None) -> list[dict]:
    """[{query, name}] — forme attendue par GoogleMapsScraper.

    La requête générique pays (« Agence MTN Nigeria ») ne résout QU'UNE fiche
    Google, alors qu'un opérateur exploite des dizaines d'agences. On y ajoute
    donc des requêtes par ville, qui résolvent chacune une fiche distincte.

    `cities_per_run` borne le nombre de villes visitées par filiale et par run.
    Sans borne, 132 filiales × 6 villes feraient 792 sessions navigateur : ce
    collecteur est déjà le plus lent du lot, un run entier n'y suffirait pas.

    Les villes tournent d'un jour à l'autre, par une fenêtre glissante indexée
    sur le quantième. Le choix est DÉTERMINISTE et sans état : tous les runs
    d'une même journée visitent les mêmes agences — ce qui laisse la collecte
    incrémentale faire son travail — et la couverture complète s'obtient sur
    quelques jours au lieu d'un seul run interminable.
    """
    cities_by_iso = load_cities()
    day = (today or date.today()).timetuple().tm_yday
    out: list[dict] = []

    for sub, cfg in _targets_for("google_maps"):
        query = cfg.get("query")
        if not query:
            continue
        name = sub["subsidiary_name"]
        # `operator` accompagne chaque cible : le collecteur s'en sert pour
        # écarter les établissements d'une AUTRE enseigne. Google classe par
        # pertinence commerciale, et remonte volontiers un revendeur tiers
        # (« Cellucity ») en tête d'une recherche « Agence Vodacom ».
        operator = sub.get("operator") or ""
        iso2 = (sub.get("iso2") or "").upper()
        # `country` et `city` accompagnent la requête pour que la file
        # `collection_jobs` soit lisible telle quelle — « quelles unités du
        # Nigeria échouent ? » sans avoir à redécouper la chaîne de requête.
        # `city=None` marque la requête au niveau pays.
        out.append({"query": query, "name": name, "operator": operator,
                    "country": iso2, "city": None})

        if cities_per_run <= 0:
            continue
        cities = cities_by_iso.get(iso2, [])
        if not cities:
            continue
        # Fenêtre glissante : on repart d'un décalage différent chaque jour.
        start = day % len(cities)
        selection = [cities[(start + i) % len(cities)]
                     for i in range(min(cities_per_run, len(cities)))]
        out.extend(
            {"query": f"Agence {operator} {city}", "name": name,
             "operator": operator, "country": iso2, "city": city}
            for city in selection
        )

    return out


def hellopeter_companies() -> list[dict]:
    """[{slug, name}] — forme attendue par HelloPeterScraper."""
    return [
        {"slug": cfg["slug"], "name": sub["subsidiary_name"]}
        for sub, cfg in _targets_for("hellopeter")
        if cfg.get("slug")
    ]


def gdelt_targets() -> list[dict]:
    """[{term, iso2, name}] — forme attendue par GDELTScraper.

    `term` est l'OPÉRATEUR seul, pas la filiale : c'est le nom sous lequel la
    presse le désigne (« MTN », jamais « MTN Nigeria » dans un journal
    nigérian, où le pays va de soi). C'est le filtre pays de GDELT
    (`sourcecountry:`) qui distingue ensuite les filiales entre elles.
    """
    return [
        {
            "term": cfg.get("term") or sub["operator"],
            "iso2": sub["iso2"].upper(),
            "name": sub["subsidiary_name"],
        }
        # Même déclaration que la presse Google News : une filiale suivie par
        # l'une l'est par l'autre, inutile d'ajouter une clé `gdelt` à 132
        # entrées de configuration pour dire deux fois la même chose.
        for sub, cfg in _targets_for("rss")
        if sub.get("operator") and sub.get("iso2")
    ]


def reddit_targets(
    subreddits: list[tuple[str, str | None]]
) -> list[dict]:
    """[{subreddit, iso2, operators, query}] — attendu par RedditScraper.

    UNE CIBLE PAR SUBREDDIT, PAS PAR FILIALE. Chaque requête Reddit coûte une
    minute (voir `RedditConfig.min_interval_seconds`) : interroger les 135
    filiales séparément demanderait 2 h 15 par run. On interroge donc chaque
    subreddit pays UNE fois, avec la disjonction des opérateurs présents dans
    ce pays, et le rattachement fil → filiale se fait ensuite sur le texte.

    La requête est bâtie sur l'OPÉRATEUR et non sur la filiale, pour la raison
    déjà exposée par `gdelt_targets()` : dans r/Nigeria on écrit « MTN », jamais
    « MTN Nigeria » — le pays y va de soi. C'est le subreddit qui porte le pays.

    Les noms composés sont mis entre guillemets (`"Cell C"`), sans quoi Reddit
    les découpe en deux termes et « C » ramènerait n'importe quoi.

    Un subreddit dont l'ISO2 n'a aucune filiale déclarée est IGNORÉ plutôt
    qu'interrogé sans filtre : une requête vide ramènerait tout le subreddit,
    soit une minute dépensée pour du bruit intégral.
    """
    par_pays: dict[str, set[str]] = {}
    for sub in load_subsidiaries():
        iso2 = (sub.get("iso2") or "").upper()
        operator = (sub.get("operator") or "").strip()
        # Les noms non reconnaissables sont exclus de la REQUÊTE autant que du
        # rattachement : chercher « WE » dans r/Egypt ramène tout l'anglais du
        # subreddit, pour des résultats qu'aucune règle ne pourra ensuite
        # attribuer. C'est une minute de run dépensée en pur bruit.
        if iso2 and operator and operator.lower() not in _NOMS_NON_RECONNAISSABLES:
            par_pays.setdefault(iso2, set()).add(operator)

    cibles = []
    for nom, iso2 in subreddits:
        operateurs = sorted(par_pays.get((iso2 or "").upper(), ()))
        if not operateurs:
            logger.warning(
                "Reddit : r/%s ignoré (aucune filiale déclarée pour l'ISO2 %r)",
                nom, iso2,
            )
            continue
        termes = [f'"{o}"' if " " in o else o for o in operateurs]
        cibles.append({
            "subreddit": nom,
            "iso2": (iso2 or "").upper(),
            "operators": operateurs,
            "query": " OR ".join(termes),
        })
    return cibles


#: Opérateurs qu'on RENONCE à reconnaître par leur nom en texte libre.
#:
#: MESURÉ, PAS SUPPOSÉ. « WE » est l'opérateur historique égyptien, et il est
#: MONO-PAYS : la règle ci-dessous ne lui impose donc aucun marqueur de pays, et
#: `\bwe\b` mord sur le pronom anglais partout. Vérifié :
#:
#:     « We need better broadband in Lagos »   -> ['WE Égypte']
#:
#: Un fil nigérian attribué à une filiale égyptienne, sans le moindre signe
#: extérieur. Et le marqueur de pays ne sauverait rien ici : dans r/Egypt, le
#: pays du support fournit le marqueur, et n'importe quel « we » égyptien
#: deviendrait un avis sur WE — alors que le fil parle peut-être de Vodafone.
#:
#: On applique donc le principe déjà retenu partout ailleurs dans ce module :
#: MIEUX VAUT RATER QUE MAL ATTRIBUER. Ces filiales restent collectées par les
#: boutiques d'applications, Google Maps et HelloPeter — sources où la cible est
#: désignée par un identifiant, pas reconnue dans du texte. Elles ne perdent que
#: la presse et les forums.
#:
#: CRITÈRE D'AJOUT à cette liste : le nom est un mot courant d'une des langues
#: du corpus (fr/en/pt), ou une abréviation d'usage massif dans un autre domaine.
_NOMS_NON_RECONNAISSABLES = {
    # Pronom anglais. Le cas mesuré ci-dessus.
    "we",
    # `re.escape("e&")` entouré de `\b` ne peut de toute façon jamais
    # s'apparier : `\b` après « & » exige un caractère de mot juste derrière.
    # L'exclure explicitement transforme un trou silencieux en trou documenté.
    "e&",
    # Ticker universel du Bitcoin. L'opérateur botswanais s'appelle BTC, et un
    # fil crypto citant « data » suffirait à lui attribuer un avis.
    "btc",
}


@lru_cache
def press_matchers() -> list[dict]:
    """Règles de rattachement d'un article de presse à une filiale.

    Un flux de presse généraliste ne se demande pas « parle-moi de MTN
    Nigeria » : il livre tout, et c'est à nous de reconnaître qui est cité.

    Deux régimes, selon que l'opérateur est présent dans un seul pays ou dans
    plusieurs :

    * **mono-pays** (Djezzy, Cell C, Hormuud…) : le seul nom de l'opérateur
      suffit, il n'y a aucune ambiguïté possible ;
    * **multi-pays** (MTN, Orange, Airtel…) : le nom de l'opérateur NE SUFFIT
      PAS. Il faut aussi un marqueur de pays — nom français ou anglais — sans
      quoi un article sur MTN Ghana serait attribué aux dix-sept filiales MTN
      du périmètre à la fois. C'est précisément le défaut que le collecteur
      Play Store documente et refuse : rattacher à une filiale un avis venu
      d'un autre marché.
    """
    subs = load_subsidiaries()
    per_operator: dict[str, int] = {}
    for sub in subs:
        op = (sub.get("operator") or "").strip()
        if op:
            per_operator[op] = per_operator.get(op, 0) + 1

    matchers = []
    for sub in subs:
        operator = (sub.get("operator") or "").strip()
        if not operator:
            continue
        if operator.lower() in _NOMS_NON_RECONNAISSABLES:
            logger.warning(
                "Presse : %s ignorée — « %s » est un mot courant, le "
                "reconnaître en texte libre produirait de fausses attributions",
                sub.get("subsidiary_name"), operator,
            )
            continue
        multi = per_operator.get(operator, 0) > 1
        markers = country_names(sub.get("iso2", "")) if multi else []
        if multi and not markers:
            # Opérateur multi-pays dont le pays n'est pas dans la table : on
            # ne peut pas lever l'ambiguïté, donc on ne rattache rien plutôt
            # que d'attribuer au hasard.
            logger.warning(
                "Presse : %s ignorée (opérateur multi-pays, ISO2 %r inconnu)",
                sub.get("subsidiary_name"), sub.get("iso2"),
            )
            continue
        matchers.append({
            "name": sub["subsidiary_name"],
            "operator": operator,
            "iso2": (sub.get("iso2") or "").upper(),
            "country_markers": markers,
        })
    return matchers


def trustpilot_companies() -> list[dict]:
    """[{domain, name}] — forme attendue par TrustpilotScraper."""
    return [
        {"domain": cfg["domain"], "name": sub["subsidiary_name"]}
        for sub, cfg in _targets_for("trustpilot")
        if cfg.get("domain")
    ]


def rss_search_terms() -> list[str]:
    """Termes de recherche presse — une entrée par filiale."""
    return [
        cfg["search_term"]
        for _, cfg in _targets_for("rss")
        if cfg.get("search_term")
    ]
