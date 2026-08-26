"""
Vérification EMPIRIQUE des identifiants candidats, boutique par boutique.

POURQUOI CE SCRIPT EXISTE
    `discover_identifiers.py` propose des candidats à partir du NOM de l'app.
    C'est insuffisant, et la liste des propositions en attente le montre : elle
    contient « Orange Money Jordan » pour Orange Congo et « My Vodafone (PNG) »
    — Papouasie-Nouvelle-Guinée — pour Vodafone Ghana. Un nom qui contient la
    marque ne prouve rien.

    Ce script ne lit plus les noms : il INTERROGE la boutique du pays visé et
    regarde ce qu'elle répond réellement. Trois questions, trois mesures :

      1. l'app existe-t-elle dans cette boutique nationale ?
      2. l'éditeur correspond-il à la marque de l'opérateur ?
      3. la boutique renvoie-t-elle des avis pour cette app ?

    Un candidat n'est retenu que s'il passe les trois. C'est la différence entre
    « le nom ressemble » et « la source répond ».

LE CAS DES APPS PANAFRICAINES
    Airtel publie UNE app (« My Airtel Africa ») pour treize pays, MTN de même.
    Ce n'est pas une erreur d'appariement : l'API App Store est segmentée par
    boutique nationale, donc le MÊME app_id interrogé sur la boutique nigériane
    renvoie les avis nigérians. Le partage est donc légitime — à condition que
    la boutique du pays réponde vraiment, ce que ce script vérifie.

    Le fichier de sortie marque ces cas `_shared_app`, pour qu'on sache plus
    tard qu'un même identifiant sert plusieurs filiales et qu'on ne prenne pas
    cette duplication pour une faute de saisie.

USAGE
    python -m tools.verify_identifiers                 # tous les candidats
    python -m tools.verify_identifiers --limit 10      # test rapide
    python -m tools.verify_identifiers --sources appstore
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Réutilisé et non redupliqué : la table des indices de pays et la règle de
# conflit sont la propriété de l'outil de découverte. Deux copies divergeraient,
# et un pays corrigé d'un côté resterait faux de l'autre.
from tools.discover_identifiers import country_verdict  # noqa: E402

CONFIG = ROOT / "config" / "operators.json"
DISCOVERED = ROOT / "config" / "discovered.json"
OUT = ROOT / "config" / "verified.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
TIMEOUT = 20

#: Éditeurs dont le nom ne contient PAS la marque mais qui la publient
#: légitimement. Liste explicite plutôt que correspondance floue : chaque entrée
#: est une décision tracée, pas une heuristique qui laisserait passer un tiers.
PUBLISHER_ALIASES = {
    "airtel": ["bharti airtel", "airtel africa"],
    "mtn": ["mtn group", "mtn"],
    "orange": ["orange", "sonatel"],
    "vodacom": ["vodacom"],
    "vodafone": ["vodafone", "safaricom"],
    "moov": ["moov", "maroc telecom", "atlantique telecom", "etisalat"],
    "safaricom": ["safaricom"],
    "telkom": ["telkom"],
    "ooredoo": ["ooredoo"],
    "zain": ["zain"],
    "glo": ["globacom"],
    "africell": ["africell"],
    "telma": ["telma"],
    "tnm": ["telekom networks"],
    "camtel": ["camtel"],
    "movitel": ["movitel"],
    "expresso": ["expresso"],
    "econet": ["econet"],
    "cell c": ["cell c"],
    "celtiis": ["celtiis", "benin telecoms"],
    "inwi": ["inwi"],
    "mobilis": ["mobilis", "algerie telecom"],
    "togocom": ["togocom"],
    "sotel": ["sotel"],
    "telecel": ["telecel"],
    "free": ["free", "hayo"],
    # Opérateurs des 16 pays ajoutés. Beaucoup sont des SIGLES dont l'éditeur
    # publie sous sa raison sociale complète : sans ces alias, « BTC » est jugé
    # étranger à « Botswana Telecommunications Corporation », qui est pourtant
    # la même entité. C'était un faux rejet, pas une protection.
    "unitel": ["unitel", "angola telecom"],
    "movicel": ["movicel"],
    "mascom": ["mascom"],
    "btc": ["botswana telecommunications", "btc"],
    "mtc": ["mtc", "mobile telecommunications"],
    "tn mobile": ["telecom namibia", "tn mobile"],
    "netone": ["netone", "net one"],
    "emtel": ["emtel"],
    "mauritius telecom": ["mauritius telecom", "my.t"],
    "comores telecom": ["comores telecom", "comores cables"],
    "djibouti telecom": ["djibouti telecom", "evatis"],
    "hormuud": ["hormuud"],
    "somtel": ["somtel", "dahabshiil"],
    "telesom": ["telesom"],
    "eritel": ["eritel", "eritrea telecommunication"],
    "lumitel": ["lumitel", "viettel"],
    "onatel": ["onatel", "maroc telecom"],
    "qcell": ["qcell", "q group"],
    "gamcel": ["gamcel", "gamtel"],
    "cvmovel": ["cvmovel", "cabo verde telecom", "cv movel"],
    "mauritel": ["mauritel", "maroc telecom"],
    "chinguitel": ["chinguitel", "sudatel"],
    "mattel": ["mattel", "tunisie telecom"],
    "getesa": ["getesa", "orange"],
    "cst": ["cst", "companhia santomense"],
    "libyana": ["libyana", "libyan post"],
    "almadar": ["almadar", "al madar", "libyan post"],
}


#: Marqueurs de marchés HORS Afrique rencontrés dans les propositions.
#:
#: `country_verdict` ne connaît que les pays du périmètre africain : une app
#: papouane ou jordanienne lui paraît donc « neutre ». Ces cas sont réels et
#: non théoriques — « My Vodafone (PNG) » a été proposé pour le Ghana et
#: « Orange Money Jordan » pour le Congo.
FOREIGN_MARKETS = [
    "png", "papua", "jordan", "jordanie", "india", "inde", "pakistan",
    "bangladesh", "nepal", "iraq", "irak", "yemen", "oman", "qatar",
    "bahrain", "kuwait", "koweit", "arabie", "saudi", "liban", "lebanon",
    "syrie", "syria", "turquie", "turkey", "roumanie", "romania",
    "espagne", "spain", "portugal", "belgique", "belgium", "suisse",
    "luxembourg", "moldova", "moldavie", "slovaquie", "slovakia", "pologne",
    "poland", "uk", "ireland", "irlande", "australia", "australie",
    "new zealand", "fiji", "vanuatu", "samoa", "tonga",
]

#: Codes ISO alpha-2 de tous les pays suivis, pour repérer un code isolé dans
#: un identifiant de paquet (`bf.moovmoney.hwmm`) ou un nom d'app (`MOOV MONEY BF`).
_ALL_ISO2 = {
    "bj", "bf", "ml", "cf", "ci", "tg", "ne", "ga", "sn", "gn", "gw", "sl",
    "lr", "cm", "cd", "cg", "mg", "eg", "ma", "tn", "ng", "gh", "rw", "ug",
    "zm", "za", "sd", "ss", "sz", "td", "ke", "tz", "mw", "sc", "mz", "ls",
    "et", "dz", "fr", "us", "gb", "in", "pt", "es", "be",
    # Les 16 pays ajoutés à l'extension continentale. Les OUBLIER ici rendait le
    # contrôle aveugle sur eux : `ao.unitel.MyAppUnitel` — l'app ANGOLAISE — a
    # ainsi été acceptée pour Sao Tomé, faute de reconnaître le préfixe « ao ».
    # Cette table doit rester alignée sur le périmètre de config/operators.json.
    "ao", "bw", "na", "zw", "mu", "km", "dj", "so", "er", "bi",
    "gm", "cv", "mr", "gq", "st", "ly",
}


#: Catégories de boutique incompatibles avec une app d'opérateur télécom.
#:
#: Contrôle piloté par la donnée plutôt que par une liste de mots interdits :
#: la boutique classe elle-même ses apps, et cette classification est plus
#: fiable qu'un nom. Cas réel écarté par ce seul contrôle : « القرآن الكريم Zain
#: by », un Coran publié par Zain Group — éditeur authentique, marque présente,
#: catégorie « Books & Reference ». Aucun autre test ne l'attrapait.
#:
#: « Finance » est VOLONTAIREMENT absent de cette liste : le mobile money
#: (MTN MoMo, VodaPay, mkesh, Orange Money) est un service central des
#: opérateurs africains, et l'insatisfaction qu'il suscite est une
#: insatisfaction envers l'opérateur. L'exclure amputerait le signal.
IRRELEVANT_GENRES = {
    "books & reference", "education", "games", "game", "health & fitness",
    "medical", "music & audio", "sports", "travel & local", "food & drink",
    "photography", "art & design", "beauty", "dating", "parenting",
    "weather", "auto & vehicles", "house & home", "libraries & demo",
    "personalization", "comics", "entertainment",
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def foreign_market(haystack: str) -> str | None:
    """Le libellé désigne-t-il un marché hors du périmètre africain ?"""
    tokens = set(haystack.split())
    for marker in FOREIGN_MARKETS:
        if (" " in marker and marker in haystack) or marker in tokens:
            return marker
    return None


def iso_conflict(identifier: str, app_name: str, iso2: str) -> str | None:
    """Un code pays à deux lettres désigne-t-il un AUTRE pays ?

    `country_verdict` ignore délibérément les codes de deux lettres, jugés trop
    ambigus dans une phrase. Ils ne le sont plus du tout dans un identifiant de
    paquet ni en fin de nom d'app, où ils sont la convention de nommage
    dominante : `bf.moovmoney.hwmm` est l'app du Burkina, `fr.ap7.telma` celle
    d'un éditeur français. Ce contrôle-ci les lit donc là où ils sont fiables —
    segments pointés et jetons isolés — et nulle part ailleurs.
    """
    target = iso2.lower()
    segments = {s.lower() for s in re.split(r"[.\-_]", identifier or "") if s}
    tokens = set(norm(app_name).split())
    for code in segments | tokens:
        if len(code) == 2 and code in _ALL_ISO2 and code != target:
            return code
    return None


def publisher_matches(operator: str, publisher: str) -> bool:
    """L'éditeur de l'app appartient-il bien à l'opérateur ?

    C'est le contrôle qui écarte « Settlo Technologies Limited » proposé pour
    Vodacom Mozambique, ou « Rêveurs Professionnels » pour MTN Congo : des
    tiers qui publient une app dont le nom cite la marque.
    """
    pub = norm(publisher)
    op = norm(operator)
    if not pub:
        return False
    for brand, aliases in PUBLISHER_ALIASES.items():
        if brand in op:
            return any(a in pub for a in aliases)
    # Opérateur hors liste : on exige que la marque figure chez l'éditeur.
    first = op.split()[0] if op.split() else op
    return first in pub


def has_latin(text: str) -> bool:
    """Le libellé contient-il des caractères latins ?

    Un nom d'app entièrement en arabe — « المدار الجديد » pour Almadar Aljadid —
    ne peut pas contenir une marque écrite en alphabet latin. Exiger la
    correspondance dans ce cas rejette une app parfaitement légitime, et exclut
    de fait les opérateurs des cinq pays arabophones du périmètre.
    """
    return any("a" <= c.lower() <= "z" for c in text or "")


def brand_in_app(operator: str, app_label: str) -> bool:
    """La marque figure-t-elle dans le nom de l'app (ou son identifiant) ?

    Contrôle complémentaire de `publisher_matches`, et non redondant : un
    éditeur homonyme suffit à tromper le test d'éditeur. « Portal FEI » publié
    par « Telma Cunha » — une personne — passait ainsi pour l'app de
    l'opérateur malgache Telma. Exiger la marque des DEUX côtés ferme ce cas.
    """
    hay = norm(app_label)
    tokens = [t for t in norm(operator).split() if t not in {"africa", "group", "telecom", "telecoms"}]
    return any(t in hay for t in tokens) if tokens else False


def http_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


# ---------------------------------------------------------------- App Store


def probe_appstore(app_id: str, iso2: str, operator: str) -> dict:
    """Interroge la boutique nationale : présence, éditeur, avis."""
    country = iso2.lower()
    result = {
        "app_id": app_id,
        "store_country": country,
        "in_storefront": False,
        "publisher_ok": False,
        "reviews": 0,
        "app_name": None,
        "publisher": None,
        "verdict": "rejete",
        "reason": "",
    }

    # 1. L'app est-elle publiée dans cette boutique ? `lookup` renvoie 0 résultat
    #    si l'app n'est pas distribuée dans le pays — c'est le contrôle le plus
    #    discriminant, et il ne coûte qu'un appel.
    lookup = http_json(
        f"https://itunes.apple.com/lookup?id={app_id}&country={country}"
    )
    time.sleep(0.35)
    if not lookup or not lookup.get("results"):
        result["reason"] = f"app absente de la boutique {country.upper()}"
        return result

    item = lookup["results"][0]
    result["in_storefront"] = True
    result["app_name"] = item.get("trackName")
    result["publisher"] = item.get("sellerName")

    # 2. Le libellé désigne-t-il un AUTRE pays ?
    #    Contrôle indispensable et non couvert par l'éditeur : « Orange Money
    #    Jordan » publié par « Orange Jordan » passe le test de marque — le mot
    #    « orange » y figure — alors qu'il s'agit d'un tout autre marché.
    hay = norm(f"{item.get('trackName','')} {item.get('sellerName','')}")
    if country_verdict(hay, iso2) == "conflict":
        result["reason"] = f"libelle {item.get('trackName')!r} designe un autre pays"
        return result
    foreign = foreign_market(hay)
    if foreign:
        result["reason"] = f"marche hors perimetre detecte ({foreign})"
        return result
    conflict = iso_conflict(app_id, item.get("trackName", ""), iso2)
    if conflict:
        result["reason"] = f"code pays {conflict.upper()} dans le libelle, attendu {iso2}"
        return result
    # La marque doit apparaitre dans le NOM de l'app, pas seulement chez
    # l'editeur : « Portal FEI » publie par « Telma Cunha » satisfait le test
    # d'editeur alors que ce dernier est une personne homonyme.
    # Contrôle de marque réservé aux libellés LATINS : voir has_latin().
    libelle = item.get("trackName", "")
    if has_latin(libelle) and not brand_in_app(operator, libelle):
        result["reason"] = f"marque absente du nom d'app {item.get('trackName')!r}"
        return result
    genre = (item.get("primaryGenreName") or "").lower()
    result["genre"] = item.get("primaryGenreName")
    if genre in IRRELEVANT_GENRES:
        result["reason"] = f"categorie {item.get('primaryGenreName')!r} etrangere au telecom"
        return result

    # 3. L'éditeur appartient-il à l'opérateur ?
    if not publisher_matches(operator, item.get("sellerName", "")):
        result["reason"] = (
            f"editeur {item.get('sellerName')!r} etranger a {operator!r}"
        )
        return result
    result["publisher_ok"] = True

    # 3. La boutique renvoie-t-elle des avis ? Un appariement valide mais sans
    #    aucun avis n'apporte rien au dashboard : on le signale à part plutôt
    #    que de le retenir comme un succès.
    feed = http_json(
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"id={app_id}/sortBy=mostRecent/json"
    )
    time.sleep(0.35)
    entries = ((feed or {}).get("feed") or {}).get("entry") or []
    # La première entrée du flux décrit l'app elle-même, pas un avis.
    result["reviews"] = max(0, len(entries) - 1) if entries else 0

    if result["reviews"] == 0:
        result["verdict"] = "valide_sans_avis"
        result["reason"] = "app confirmee, mais aucun avis sur cette boutique"
    else:
        result["verdict"] = "valide"
        result["reason"] = f"{result['reviews']} avis sur la boutique {country.upper()}"
    return result


# --------------------------------------------------------------- Play Store


def probe_playstore(package_id: str, iso2: str, operator: str) -> dict:
    """Interroge Google Play pour le pays visé : existence, éditeur, avis."""
    country = iso2.lower()
    result = {
        "package_id": package_id,
        "country": country,
        "in_storefront": False,
        "publisher_ok": False,
        "reviews": 0,
        "app_name": None,
        "publisher": None,
        "verdict": "rejete",
        "reason": "",
    }
    try:
        from google_play_scraper import app as gp_app, reviews as gp_reviews
    except ImportError:
        result["reason"] = "google_play_scraper absent"
        return result

    try:
        info = gp_app(package_id, lang="en", country=country)
    except Exception as e:  # noqa: BLE001
        result["reason"] = f"app introuvable sur Play {country.upper()} ({type(e).__name__})"
        return result
    time.sleep(0.25)

    result["in_storefront"] = True
    result["app_name"] = info.get("title")
    result["publisher"] = info.get("developer")

    hay = norm(f"{info.get('title','')} {info.get('developer','')}")
    if country_verdict(hay, iso2) == "conflict":
        result["reason"] = f"libelle {info.get('title')!r} designe un autre pays"
        return result
    foreign = foreign_market(f"{hay} {norm(package_id)}")
    if foreign:
        result["reason"] = f"marche hors perimetre detecte ({foreign})"
        return result
    conflict = iso_conflict(package_id, info.get("title", ""), iso2)
    if conflict:
        result["reason"] = f"code pays {conflict.upper()} dans l'identifiant, attendu {iso2}"
        return result
    # Contrôle de marque réservé aux libellés LATINS : voir has_latin().
    libelle = f"{info.get('title','')} {package_id}"
    if has_latin(libelle) and not brand_in_app(operator, libelle):
        result["reason"] = f"marque absente du nom d'app {info.get('title')!r}"
        return result
    genre = (info.get("genre") or "").lower()
    result["genre"] = info.get("genre")
    if genre in IRRELEVANT_GENRES:
        result["reason"] = f"categorie {info.get('genre')!r} etrangere au telecom"
        return result

    if not publisher_matches(operator, info.get("developer", "")):
        result["reason"] = f"editeur {info.get('developer')!r} etranger a {operator!r}"
        return result
    result["publisher_ok"] = True

    try:
        got, _ = gp_reviews(package_id, lang="en", country=country, count=5)
        result["reviews"] = len(got)
    except Exception:
        result["reviews"] = 0
    time.sleep(0.25)

    if result["reviews"] == 0:
        result["verdict"] = "valide_sans_avis"
        result["reason"] = "app confirmee, mais aucun avis renvoye pour ce pays"
    else:
        result["verdict"] = "valide"
        result["reason"] = f"{result['reviews']} avis renvoyes pour {country.upper()}"
    return result


# ------------------------------------------------- Résolution d'identifiants


def resolve_play_package(app_name: str, developer: str, iso2: str) -> str | None:
    """Retrouve le package_id d'une app dont on connaît déjà le nom et l'éditeur.

    POURQUOI C'EST NÉCESSAIRE
        Une passe de découverte antérieure a enregistré, pour 32 filiales, un
        nom d'app et un éditeur CORRECTS et validés (« Orange Max it – Mali »
        publié par « Orange Mali ») mais sans le package_id. L'identification
        est donc acquise ; seule la clé technique manque. Ces entrées ne sont pas
        des échecs de découverte, et les traiter comme tels reviendrait à
        abandonner 32 filiales déjà identifiées.

    On ne devine rien : on cherche dans la boutique du pays et l'on n'accepte
    qu'une correspondance EXACTE du titre et de l'éditeur, tous deux normalisés.
    Une correspondance approximative rouvrirait la porte au « Claustrophobia »
    qui a motivé la règle de vérification du projet.
    """
    try:
        from google_play_scraper import search as gp_search
    except ImportError:
        return None

    target_title = norm(app_name)
    target_dev = norm(developer)
    for country in (iso2.lower(), "us"):
        try:
            hits = gp_search(app_name, lang="en", country=country, n_hits=20)
        except Exception:
            continue
        time.sleep(0.3)
        for h in hits:
            if norm(h.get("title", "")) == target_title and norm(h.get("developer", "")) == target_dev:
                return h.get("appId")
    return None


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="n'examiner que N filiales")
    ap.add_argument(
        "--sources",
        default="appstore,playstore",
        help="sources à vérifier, séparées par des virgules",
    )
    ap.add_argument(
        "--include-existing",
        action="store_true",
        help="revérifier aussi les identifiants déjà présents dans la config",
    )
    args = ap.parse_args()
    wanted = {s.strip() for s in args.sources.split(",")}

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    disc = json.loads(DISCOVERED.read_text(encoding="utf-8"))
    by_name = {s["subsidiary_name"]: s for s in cfg["subsidiaries"]}

    todo = []
    for d in disc:
        name = d["subsidiary_name"]
        sub = by_name.get(name)
        if not sub:
            continue
        found = d.get("found") or {}
        for source in ("appstore", "playstore"):
            if source not in wanted:
                continue
            candidate = found.get(source)
            if not candidate:
                continue
            if sub["sources"].get(source) and not args.include_existing:
                continue  # déjà vérifié lors d'une passe précédente
            todo.append((sub, source, candidate))

    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(todo)} candidat(s) à vérifier contre les vraies boutiques.\n")

    results = []
    resolved = 0
    for i, (sub, source, candidate) in enumerate(todo, 1):
        name, iso2, operator = sub["subsidiary_name"], sub["iso2"], sub["operator"]

        if source == "appstore":
            if not candidate.get("app_id"):
                continue
            probe = probe_appstore(candidate["app_id"], iso2, operator)
        else:
            package = candidate.get("package_id")
            if not package:
                # Nom et éditeur déjà validés, clé technique manquante :
                # on la retrouve avant de rejeter la filiale.
                package = resolve_play_package(
                    candidate.get("_app_name") or "",
                    candidate.get("_developer") or "",
                    iso2,
                )
                if not package:
                    results.append({
                        "subsidiary": name, "iso2": iso2, "operator": operator,
                        "source": source, "verdict": "rejete",
                        "app_name": candidate.get("_app_name"),
                        "publisher": candidate.get("_developer"),
                        "reason": "package_id introuvable pour ce nom d'app",
                        "reviews": 0, "in_storefront": False, "publisher_ok": False,
                    })
                    print(f"[{i:>3}/{len(todo)}] NON {name:<26} {source:<10} package_id introuvable")
                    continue
                resolved += 1
            probe = probe_playstore(package, iso2, operator)

        probe.update(subsidiary=name, iso2=iso2, operator=operator, source=source)
        results.append(probe)

        mark = {"valide": "OK ", "valide_sans_avis": "~  ", "rejete": "NON"}[probe["verdict"]]
        print(f"[{i:>3}/{len(todo)}] {mark} {name:<26} {source:<10} {probe['reason']}")

    # ------------------------------------------------------------------
    # Partage d'identifiant : la règle DIFFÈRE selon la boutique, parce que
    # les deux boutiques ne se comportent pas de la même façon. Mesuré, pas
    # supposé :
    #
    #   App Store — flux comparés pour l'app « My Airtel Africa » (1462268018)
    #   sur TZ / ZM / MG / SC : 49, 49, 15 et 2 avis, ZÉRO avis en commun entre
    #   deux boutiques. La segmentation par pays est réelle, le partage est donc
    #   légitime et chaque filiale reçoit les avis de son marché.
    #
    #   Google Play — mêmes 20 avis renvoyés à l'identique pour TZ, ZM, NG, KE
    #   et UG (intersection complète). Le paramètre `country` ne filtre PAS les
    #   avis. Partager un package entre filiales dupliquerait donc les mêmes
    #   avis sur autant de pays, en gonflant les volumes et en attribuant à
    #   chaque pays le sentiment de tous les autres.
    #
    # Conséquence : un package Play partagé n'est conservé que pour la filiale
    # dont le code pays figure dans l'identifiant (`com.consumerug` -> Ouganda).
    # Sans ce rattachement explicite, il est écarté partout.
    # ------------------------------------------------------------------
    keys: dict = {}
    for r in results:
        if r["verdict"].startswith("valide"):
            key = (r["source"], r.get("app_id") or r.get("package_id"))
            keys.setdefault(key, []).append(r)

    for (source, ident), holders in keys.items():
        shared = len(holders) > 1
        for r in holders:
            r["_shared_app"] = shared
        if not shared or source != "playstore":
            continue
        segments = {s.lower() for s in re.split(r"[.\-_]", ident or "") if s}
        for r in holders:
            iso = r["iso2"].lower()
            owns = any(seg == iso or seg.endswith(iso) for seg in segments)
            if not owns:
                r["verdict"] = "rejete"
                r["reason"] = (
                    f"package partage par {len(holders)} filiales et Play ne "
                    f"segmente pas les avis par pays"
                )

    OUT.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    counts = {"valide": 0, "valide_sans_avis": 0, "rejete": 0}
    for r in results:
        counts[r["verdict"]] += 1
    print(
        f"\nValides avec avis : {counts['valide']}"
        f" | valides sans avis : {counts['valide_sans_avis']}"
        f" | rejetes : {counts['rejete']}"
    )
    if resolved:
        print(f"{resolved} package_id retrouve(s) a partir du nom d'app deja valide.")
    shared = sorted({k for k, v in keys.items() if len(v) > 1})
    if shared:
        print(f"{len(shared)} identifiant(s) partages entre plusieurs filiales (apps panafricaines).")
    print(f"Résultats écrits dans {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
