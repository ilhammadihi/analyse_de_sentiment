"""
Découverte de TOUTES les applications d'une filiale, sur les deux boutiques.

CE QUI CHANGE PAR RAPPORT À `discover_identifiers.py`
    L'outil précédent renvoyait UNE app par filiale et par boutique
    (`return hit`). C'est le même défaut que le collecteur Google Maps corrigé
    en amont : on prenait le premier résultat et on jetait les autres.

    Or un opérateur télécom africain publie plusieurs applications par pays, et
    ce n'est pas anecdotique — mesuré sur les boutiques réelles :

        MTN Nigeria      myMTN NG            41 832 notes
                         MoMo PSB            10 802 notes   <- absent de la config
        Safaricom Kenya  MySafaricom        100 947 notes   <- RIEN n'était configuré
                         M-PESA for Business  3 145 notes
        Vodacom ZA       VodaPay            178 150 notes   <- aucune app iOS configurée

    L'application de paiement pèse souvent autant que l'application self-care,
    parfois davantage : le mobile money est le premier usage numérique du
    continent. N'en retenir qu'une, c'est laisser l'essentiel de côté.

CE QUI NE CHANGE PAS
    Les garde-fous de `discover_identifiers.py` sont réutilisés tels quels, et
    c'est délibéré : ils ont été écrits après un vrai incident (l'identifiant
    1612447230 pointait vers un jeu, « Claustrophobia »). Une app n'est retenue
    que si TOUS les jetons de la marque apparaissent dans son nom ou celui de
    son éditeur, et si son libellé ne désigne pas un AUTRE pays du périmètre.

    S'y ajoute une vérification que l'outil précédent ne faisait pas : l'app
    doit avoir des avis DANS CETTE BOUTIQUE. L'App Store segmente ses avis par
    vitrine nationale — une app présente partout peut n'avoir aucun avis au
    Sénégal — et une app sans avis est une cible de collecte inutile.

Usage :
    python -m tools.discover_apps                    # les 132 filiales
    python -m tools.discover_apps --limit 5          # test rapide
    python -m tools.discover_apps --missing-only     # seulement celles sans app
    python -m tools.discover_apps --store appstore   # une seule boutique
"""

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.discover_identifiers import (  # noqa: E402
    brand_tokens, country_verdict, http_json, norm,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "operators.json"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "config" / "discovered_apps.json"

#: L'API iTunes tolère ~20 appels par minute. Au-delà elle renvoie des réponses
#: vides SANS erreur HTTP : la découverte semblerait alors ne rien trouver.
ITUNES_DELAY = 3.2

#: Termes essayés par filiale. Le mobile money mérite sa propre recherche : il
#: est souvent édité sous une marque distincte (« MoMo », « M-PESA », « Max
#: it ») qui ne remonte pas sur le seul nom de l'opérateur.
def search_terms(sub: dict) -> list[str]:
    operator = sub["operator"]
    return list(dict.fromkeys([
        operator,
        f"{operator} {sub['country']}",
        f"My {operator}",
        f"{operator} Money",
    ]))


def _load_all_operators() -> list[str]:
    """Marques de TOUS les opérateurs suivis, normalisées.

    Sert à repérer les applications tierces : un utilitaire qui cite quatre
    opérateurs dans son titre (« USSDSA - vodacom, MTN, Cell C… ») n'appartient
    à aucun d'eux.
    """
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    marques = {norm(s["operator"]) for s in config["subsidiaries"] if s.get("operator")}
    return sorted(m for m in marques if len(m) >= 3)


_ALL_BRANDS: list[str] = []


def appartient_a_la_marque(nom: str, editeur: str, operator: str) -> bool:
    """L'application appartient-elle vraiment à cet opérateur ?

    TROIS RÈGLES, chacune née d'un faux positif observé.

    1. La marque doit apparaître EN UN SEUL BLOC, pas jeton par jeton.
       « Cell C » se décompose en `cell` + `c`, et un `c` isolé se retrouve dans
       n'importe quelle chaîne : la recherche remontait « Clash of Clans » et
       « Brawl Stars » comme applications de Cell C. C'est la reprise exacte de
       l'incident « Claustrophobia » qui avait motivé ces garde-fous, amplifiée
       par le passage à plusieurs résultats par filiale.

    2. Une application citant DEUX opérateurs suivis ou plus est un outil
       tiers, jamais l'application officielle de l'un d'eux — observé :
       « USSDSA - vodacom, MTN, Cell C ».

    3. La marque doit être un MOT, pas un fragment : `\\b` évite que « Vodacom »
       matche sur « Vodacomm » ou « MTN » sur « Amtnex ».
    """
    import re

    marque = norm(operator)
    if not marque:
        return False

    hay_nom = norm(nom)
    hay_editeur = norm(editeur)
    motif = re.compile(rf"\b{re.escape(marque)}\b")

    # L'éditeur suffit à lui seul : c'est la preuve la plus forte d'appartenance.
    # On ne peut pas l'EXIGER en revanche — Orange Sénégal publie sous « SONATEL
    # S.A. », sa raison sociale, qui ne contient pas « Orange ».
    if motif.search(hay_editeur):
        return True
    if not motif.search(hay_nom):
        return False

    autres = [m for m in _ALL_BRANDS
              if m != marque and re.search(rf"\b{re.escape(m)}\b", hay_nom)]
    return not autres


# --------------------------------------------------------------- App Store

def itunes_search(term: str, country: str, limit: int = 25) -> list[dict]:
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "country": country, "entity": "software", "limit": limit}
    )
    data = http_json(url)
    time.sleep(ITUNES_DELAY)
    return (data or {}).get("results", [])


def appstore_reviews_count(app_id: str, country: str) -> int:
    """Avis réellement présents dans CETTE vitrine.

    Le flux RSS public d'Apple, sans clé. Une app peut exister sur la vitrine
    et n'y avoir aucun avis : la collecter serait une cible morte.
    """
    url = (f"https://itunes.apple.com/{country}/rss/customerreviews/"
           f"id={app_id}/sortBy=mostRecent/json")
    data = http_json(url)
    time.sleep(0.6)
    entries = ((data or {}).get("feed") or {}).get("entry") or []
    # La première entrée du flux décrit l'app elle-même, pas un avis.
    return max(0, len(entries) - 1) if entries else 0


def discover_appstore(sub: dict) -> list[dict]:
    """Toutes les apps de la marque présentes sur la vitrine du pays."""
    country = sub["iso2"].lower()
    tokens = brand_tokens(sub["operator"])
    if not tokens:
        return []

    trouvees: dict[str, dict] = {}
    for term in search_terms(sub):
        for item in itunes_search(term, country):
            app_id = str(item.get("trackId") or "")
            if not app_id or app_id in trouvees:
                continue
            nom, editeur = item.get("trackName", ""), item.get("sellerName", "")
            if not appartient_a_la_marque(nom, editeur, sub["operator"]):
                continue
            if country_verdict(norm(f"{nom} {editeur}"), sub["iso2"]) == "conflict":
                continue  # app d'un autre pays du périmètre : rejet
            trouvees[app_id] = {
                "app_id": app_id,
                "store_country": country,
                "_country_verdict": country_verdict(
                    norm(f"{nom} {editeur}"), sub["iso2"]),
                "_name": item.get("trackName"),
                "_seller": item.get("sellerName"),
                "_global_ratings": item.get("userRatingCount") or 0,
            }

    retenues = []
    for app in trouvees.values():
        app["_store_reviews"] = appstore_reviews_count(app["app_id"], country)
        if app["_store_reviews"] > 0:
            retenues.append(app)
    retenues.sort(key=lambda a: -a["_store_reviews"])
    return retenues


# -------------------------------------------------------------- Play Store

def discover_playstore(sub: dict) -> list[dict]:
    """Toutes les apps de la marque trouvées sur Google Play pour ce pays."""
    try:
        from google_play_scraper import search as gp_search
    except ImportError:
        return []

    country = sub["iso2"].lower()
    tokens = brand_tokens(sub["operator"])
    if not tokens:
        return []

    trouvees: dict[str, dict] = {}
    for term in search_terms(sub):
        try:
            results = gp_search(term, lang="fr", country=country, n_hits=20)
        except Exception:
            continue
        for item in results:
            pkg = item.get("appId")
            if not pkg or pkg in trouvees:
                continue
            nom, dev = item.get("title", ""), item.get("developer", "")
            if not appartient_a_la_marque(f"{nom} {pkg}", dev, sub["operator"]):
                continue
            if country_verdict(norm(f"{nom} {dev} {pkg}"), sub["iso2"]) == "conflict":
                continue
            trouvees[pkg] = {
                "package_id": pkg,
                # Google Play NE SEGMENTE PAS ses avis par pays (mesuré, cf.
                # mémoire projet) : un package partagé entre filiales servirait
                # LES MÊMES avis à chacune. Le verdict pays est donc conservé
                # ici, et la fusion s'en sert pour n'attribuer un package
                # partagé qu'aux filiales qu'il nomme explicitement.
                "_country_verdict": country_verdict(
                    norm(f"{nom} {dev} {pkg}"), sub["iso2"]),
                "_name": item.get("title"),
                "_developer": item.get("developer"),
                "_score": item.get("score"),
            }
        time.sleep(0.5)
    return list(trouvees.values())


# --------------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="n'traiter que N filiales")
    parser.add_argument("--missing-only", action="store_true",
                        help="seulement les filiales sans aucune app configurée")
    parser.add_argument("--store", choices=["appstore", "playstore"],
                        help="restreindre à une boutique")
    parser.add_argument("--resume", action="store_true",
                        help="reprendre là où une exécution précédente s'est arrêtée")
    args = parser.parse_args()

    # La console Windows est en cp1252 : afficher le nom d'une application
    # coréenne, arabe ou simplement accentuée d'une façon inattendue faisait
    # planter l'outil sur un UnicodeEncodeError — au bout de 57 filiales, après
    # une heure de collecte, et en perdant la progression en cours.
    #
    # `errors="replace"` plutôt qu'un try/except autour de chaque print : le
    # nom d'une application ne vaut pas d'interrompre un parcours d'une heure,
    # et un caractère de remplacement à l'écran n'altère en rien le JSON écrit,
    # qui est en UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    global _ALL_BRANDS
    _ALL_BRANDS = _load_all_operators()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    subs = config["subsidiaries"]

    if args.missing_only:
        subs = [
            s for s in subs
            if not (s["sources"].get("appstore") or s["sources"].get("playstore"))
        ]
    # REPRISE — AVANT le plafond `--limit`, et l'ordre compte.
    #
    # Le parcours complet dure plus d'une heure : le débit imposé par l'API
    # iTunes fixe le plancher, et deux interruptions ont déjà tout fait
    # recommencer. Les résultats acquis sont rechargés et leurs filiales sautées,
    # si bien que relancer trois fois de suite couvre le périmètre.
    #
    # Appliquer `--limit` d'abord découperait dans la liste COMPLÈTE, dont le
    # début est justement déjà traité : `--resume --limit 10` ne ferait alors
    # rien du tout. C'est ce qui s'est produit au premier essai.
    resultats: dict = {}
    if args.resume and OUTPUT_PATH.exists():
        try:
            resultats = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            resultats = {}
        avant = len(subs)
        subs = [s for s in subs if s["subsidiary_name"] not in resultats]
        print(f"Reprise : {avant - len(subs)} filiale(s) déjà traitée(s), "
              f"{len(subs)} restante(s)")

    if args.limit:
        subs = subs[: args.limit]

    if not subs:
        print("Rien à faire : toutes les filiales sont déjà traitées.")
        return 0

    stores = [args.store] if args.store else ["appstore", "playstore"]
    print(f"{len(subs)} filiale(s), boutique(s) : {', '.join(stores)}")
    print(f"~{len(subs) * len(search_terms(subs[0])) * ITUNES_DELAY / 60:.0f} min "
          f"pour l'App Store (débit imposé)\n", flush=True)

    total_apps = 0
    for i, sub in enumerate(subs, 1):
        nom = sub["subsidiary_name"]
        entree = {}
        if "appstore" in stores:
            entree["appstore"] = discover_appstore(sub)
        if "playstore" in stores:
            entree["playstore"] = discover_playstore(sub)

        n = sum(len(v) for v in entree.values())
        total_apps += n
        resultats[nom] = entree

        detail = ""
        for boutique, apps in entree.items():
            for a in apps[:3]:
                cle = a.get("app_id") or a.get("package_id")
                detail += f"\n      [{boutique[:4]}] {cle:<28} {str(a.get('_name'))[:34]}"
        print(f"[{i:3}/{len(subs)}] {nom:28} {n} app(s){detail}", flush=True)

        OUTPUT_PATH.write_text(
            json.dumps(resultats, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    avec = sum(1 for v in resultats.values() if any(v.values()))
    print(f"\n{total_apps} app(s) trouvée(s) sur {len(subs)} filiale(s) "
          f"({avec} en ont au moins une)")
    print(f"Écrit dans {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
