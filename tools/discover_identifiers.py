"""
Découverte automatique des identifiants de collecte pour chaque filiale.

Ne DEVINE jamais un identifiant : interroge les vraies APIs publiques
(iTunes Search, Google Play, Trustpilot) et ne retient un résultat que s'il
passe un contrôle de correspondance avec la marque de l'opérateur. Un
identifiant inventé produirait un collecteur qui échoue en silence ou pointe
vers la mauvaise entité (bug déjà rencontré : 1612447230 -> "Claustrophobia").

Usage :
    python -m tools.discover_identifiers            # toutes les filiales
    python -m tools.discover_identifiers --limit 5  # test rapide
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "operators.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
TIMEOUT = 20


def norm(text: str) -> str:
    """Minuscules sans accents ni ponctuation, pour comparer des marques."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def brand_tokens(operator: str) -> list[str]:
    """Jetons de marque significatifs (on ignore 'africa', trop générique)."""
    return [t for t in norm(operator).split() if t not in {"africa", "group"}]


# Indices textuels d'un pays : nom normalisé + code ISO2 + abréviations usuelles.
# Sert de garde-fou : une app dont le libellé désigne un AUTRE pays que celui
# recherché est rejetée (cas réel : "Moov Money TG" remonté pour le Bénin).
COUNTRY_HINTS = {
    "BJ": ["benin", "bj"], "BF": ["burkina", "bf"], "ML": ["mali", "ml"],
    "CF": ["centrafrique", "centrafricaine", "cf"], "CI": ["cote d ivoire", "ivoire", "ci"],
    "TG": ["togo", "tg"], "NE": ["niger", "ne"], "GA": ["gabon", "ga"],
    "SN": ["senegal", "sn"], "GN": ["guinee", "gn"], "GW": ["guinee bissau", "gw"],
    "SL": ["sierra leone", "sl"], "LR": ["liberia", "lr"], "CM": ["cameroun", "cameroon", "cm"],
    "CD": ["rdc", "congo kinshasa", "drc", "cd"], "CG": ["congo brazzaville", "cg"],
    "MG": ["madagascar", "mg"], "EG": ["egypte", "egypt", "eg"], "MA": ["maroc", "morocco", "ma"],
    "TN": ["tunisie", "tunisia", "tn"], "NG": ["nigeria", "ng"], "GH": ["ghana", "gh"],
    "RW": ["rwanda", "rw"], "UG": ["ouganda", "uganda", "ug"], "ZM": ["zambie", "zambia", "zm"],
    "ZA": ["afrique du sud", "south africa", "za"], "SD": ["soudan", "sudan", "sd"],
    "SS": ["soudan du sud", "south sudan", "ss"], "SZ": ["eswatini", "swaziland", "sz"],
    "TD": ["tchad", "chad", "td"], "KE": ["kenya", "ke"], "TZ": ["tanzanie", "tanzania", "tz"],
    "MW": ["malawi", "mw"], "SC": ["seychelles", "sc"], "MZ": ["mozambique", "mz"],
    "LS": ["lesotho", "ls"], "ET": ["ethiopie", "ethiopia", "et"], "DZ": ["algerie", "algeria", "dz"],
}


def country_verdict(haystack: str, iso2: str) -> str:
    """'match' si le libellé désigne le bon pays, 'conflict' s'il en désigne un
    autre, 'neutral' si aucun pays n'est mentionné."""
    hay_tokens = set(haystack.split())
    for hint in COUNTRY_HINTS.get(iso2.upper(), []):
        if (" " in hint and hint in haystack) or hint in hay_tokens:
            return "match"
    for other_iso, hints in COUNTRY_HINTS.items():
        if other_iso == iso2.upper():
            continue
        for hint in hints:
            if len(hint) <= 2:
                continue  # code ISO seul : trop ambigu pour conclure à un conflit
            if hint in haystack:
                return "conflict"
    return "neutral"


def http_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def http_status(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ---------------------------------------------------------------- App Store

def discover_appstore(sub: dict) -> dict | None:
    """iTunes Search API : renvoie {app_id, store_country, app_name} ou None."""
    country = sub["iso2"].lower()
    tokens = brand_tokens(sub["operator"])
    for term in (sub["operator"], sub["subsidiary_name"]):
        url = (
            "https://itunes.apple.com/search?"
            + urllib.parse.urlencode(
                {"term": term, "country": country, "entity": "software", "limit": 25}
            )
        )
        data = http_json(url)
        time.sleep(0.4)  # l'API iTunes limite à ~20 appels/min
        if not data:
            continue
        best_neutral = None
        for item in data.get("results", []):
            hay = norm(f"{item.get('trackName','')} {item.get('sellerName','')}")
            # toutes les composantes de la marque doivent apparaître
            if not all(t in hay for t in tokens):
                continue
            verdict = country_verdict(hay, sub["iso2"])
            if verdict == "conflict":
                continue  # app d'un autre pays : on rejette
            hit = {
                "app_id": str(item["trackId"]),
                "store_country": country,
                "_app_name": item.get("trackName"),
                "_seller": item.get("sellerName"),
                "_confidence": verdict,
            }
            if verdict == "match":
                return hit
            best_neutral = best_neutral or hit
        if best_neutral:
            return best_neutral
    return None


# --------------------------------------------------------------- Play Store

def discover_playstore(sub: dict) -> dict | None:
    """google_play_scraper.search : renvoie {package_id, _app_name} ou None."""
    try:
        from google_play_scraper import search as gp_search
    except ImportError:
        return None
    tokens = brand_tokens(sub["operator"])
    country = sub["iso2"].lower()
    for term in (sub["subsidiary_name"], sub["operator"]):
        try:
            results = gp_search(term, lang="fr", country=country, n_hits=15)
        except Exception:
            continue
        time.sleep(0.3)
        best_neutral = None
        for item in results:
            hay = norm(f"{item.get('title','')} {item.get('developer','')}")
            if not all(t in hay for t in tokens):
                continue
            verdict = country_verdict(hay, sub["iso2"])
            if verdict == "conflict":
                continue
            hit = {
                "package_id": item["appId"],
                "_app_name": item.get("title"),
                "_developer": item.get("developer"),
                "_confidence": verdict,
            }
            if verdict == "match":
                return hit
            best_neutral = best_neutral or hit
        if best_neutral:
            return best_neutral
    return None


# --------------------------------------------------------------- Trustpilot

def discover_trustpilot(sub: dict) -> dict | None:
    """Teste des domaines plausibles ; ne retient qu'une page réellement servie."""
    op = norm(sub["operator"]).replace(" ", "")
    iso = sub["iso2"].lower()
    candidates = [
        f"{op}.{iso}",
        f"www.{op}.{iso}",
        f"{op}.com",
        f"{op}.co.{iso}",
        f"{op}.{iso}.com",
    ]
    for domain in candidates:
        code = http_status(f"https://www.trustpilot.com/review/{domain}")
        time.sleep(0.3)
        if code == 200:
            return {"domain": domain}
    return None


# -------------------------------------------------------------- Google Maps

def build_maps_query(sub: dict) -> dict:
    """Requête texte libre : pas d'identifiant à vérifier, mais on la construit
    de façon cohérente (agence + marque + pays)."""
    return {"query": f"Agence {sub['operator']} {sub['country']}"}


# --------------------------------------------------------------------- main

def process(sub: dict, sources: set[str]) -> dict:
    name = sub["subsidiary_name"]
    found = {}
    if "appstore" in sources:
        found["appstore"] = discover_appstore(sub)
    if "playstore" in sources:
        found["playstore"] = discover_playstore(sub)
    if "trustpilot" in sources:
        found["trustpilot"] = discover_trustpilot(sub)
    if "maps" in sources:
        found["google_maps"] = build_maps_query(sub)
    hits = [k for k, v in found.items() if v]
    print(f"  {name:38s} -> {', '.join(hits) if hits else '(rien)'}", flush=True)
    return {"subsidiary_name": name, "found": found}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="n premières filiales")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--sources",
        default="appstore,playstore,trustpilot,maps",
        help="sources à interroger (CSV)",
    )
    ap.add_argument("--out", default="config/discovered.json")
    args = ap.parse_args()

    sources = {s.strip() for s in args.sources.split(",") if s.strip()}
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    subs = config["subsidiaries"]
    if args.limit:
        subs = subs[: args.limit]

    print(f"Découverte sur {len(subs)} filiales | sources : {sorted(sources)}\n")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, s, sources): s for s in subs}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                print(f"  ERREUR {futures[f]['subsidiary_name']} : {e}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tally = {k: 0 for k in ("appstore", "playstore", "trustpilot", "google_maps")}
    for r in results:
        for k, v in r["found"].items():
            if v:
                tally[k] += 1
    print(f"\nRésultat écrit dans {out}")
    print(f"Filiales traitées : {len(results)}")
    for k, n in tally.items():
        print(f"  {k:12s} : {n} identifiant(s) trouvé(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
