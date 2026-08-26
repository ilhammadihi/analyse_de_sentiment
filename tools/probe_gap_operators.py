"""Teste, boutique en main, si les opérateurs manquants existent vraiment.

LE PROBLÈME QUE CET OUTIL RÉSOUT
    `verify_operator_coverage.py` signale ~80 réseaux assignés en E.212 mais
    absents du périmètre. On ne peut pas les ajouter tels quels : la table
    E.212 garde des opérateurs ÉTEINTS (en Côte d'Ivoire, `KoZ`, `GreenN` et
    `café` y sont « Operational » des années après leur fermeture). Les ajouter
    en bloc peuplerait le tableau de bord de filiales sans le moindre avis, et
    ferait chuter artificiellement la couverture de chaque pays concerné.

    La confirmation qui fait foi vient du régulateur national. Mais elle est
    lente — 54 sites à lire — et une preuve empirique se trouve avant :

        UNE APPLICATION VIVANTE, ÉDITÉE PAR L'OPÉRATEUR, AVEC DES AVIS SUR LA
        BOUTIQUE DE SON PAYS, EST UN OPÉRATEUR QUI EXISTE.

    Personne ne maintient une application self-care pour un réseau éteint. Ce
    test-là est automatisable, immédiat, et il trie les ~80 candidats en deux
    tas : ceux qu'on peut instruire tout de suite, et ceux qui exigent vraiment
    la lecture du régulateur.

CE QUE LE TEST NE DIT PAS
    L'absence d'application ne prouve RIEN. Beaucoup d'opérateurs africains
    parfaitement actifs n'en publient aucune — c'est déjà vrai de plusieurs
    filiales du périmètre actuel, qui ne vivent que par la presse et Google
    Maps. Un candidat sans app n'est donc pas rejeté : il est renvoyé au
    régulateur, ce qui est son sort normal.

USAGE
    python -m tools.probe_gap_operators                 # les ~80 candidats
    python -m tools.probe_gap_operators --pays ZM UG    # quelques pays
    python -m tools.probe_gap_operators --limit 10      # test rapide
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

RACINE = Path(__file__).resolve().parent.parent
ECART = RACINE / "config" / "couverture_ecart.json"
SORTIE = RACINE / "config" / "gap_operators_probe.json"

#: L'API iTunes refuse les rafales. Même cadence que discover_apps.py.
PAUSE = 1.2

#: Au-delà, l'application n'est pas celle d'un opérateur national africain.
#:
#: Repère mesuré sur le périmètre : la plus grosse application d'opérateur
#: recensée est « My e& » avec 1,59 million d'avis. Un résultat à 61 millions
#: (Clash of Clans, remonté pour « Supercell » en RD Congo) ou 240 millions
#: (WhatsApp, remonté pour « AT » en Algérie) est nécessairement autre chose.
PLAFOND_AVIS = 2_000_000

#: Marques qui sont aussi des mots courants. Pour elles, AUCUN test automatique
#: ne conclut : « rain » (Afrique du Sud) trouve « Rain Rain Sleep Sounds »,
#: « Sure » (Sainte-Hélène) trouve « Sure Petcare », « café » (Côte d'Ivoire)
#: trouve « My Cafe — Restaurant Game ». Les signaler comme INDÉCIDABLES est la
#: seule réponse honnête : un VIVANT faux coûte plus cher qu'un aveu d'échec,
#: puisqu'il ferait entrer au périmètre une filiale qui n'existe pas.
MOTS_COURANTS = {
    "rain", "sure", "cafe", "busy", "blu", "smart", "access", "swift", "one",
    "muni", "chili", "star", "next", "click", "pay", "green", "free", "now",
    "life", "connect", "link", "wave", "sky", "air", "net", "call", "talk",
    "at", "we", "tc", "up", "go",
}


def _mot(terme: str, texte: str) -> bool:
    """Le terme apparaît-il comme un MOT ENTIER ?

    `terme in texte` cherchait une sous-chaîne, ce qui rendait le test presque
    inopérant : « at » se trouve dans « whatsapp », « access » dans
    « Accessibility Suite », « swift » dans « SwiftKey ». Sur 78 candidats,
    ce seul défaut produisait 45 « VIVANT » dont l'immense majorité étaient
    des jeux et des utilitaires sans aucun rapport.
    """
    import re as _re
    if _re.search(rf"\b{_re.escape(terme)}\b", texte):
        return True
    # Les opérateurs collent volontiers leur marque au préfixe « My » :
    # « MyComium », « MyZamtel », « MyTelesol » ne contiennent aucun mot
    # « comium », et la seule recherche par mot les perdait tous. Une
    # sous-chaîne n'est admise qu'à partir de CINQ caractères, longueur en
    # dessous de laquelle elle redevient dangereuse (« at » dans « whatsapp »).
    return len(terme) >= 5 and terme in texte


def _retenir(a_nom: str, a_editeur: str, marque: str, iso2: str,
             avis: int) -> bool:
    """Cette application peut-elle appartenir à l'opérateur recherché ?"""
    jetons = [j for j in brand_tokens(marque) if j]
    if not jetons:
        return False
    nom_n, editeur_n = norm(a_nom), norm(a_editeur)
    haystack = f"{nom_n} {editeur_n}"

    if avis <= 0 or avis > PLAFOND_AVIS:
        return False
    if not all(_mot(j, haystack) for j in jetons):
        return False
    if country_verdict(haystack, iso2) == "conflict":
        return False
    # La marque doit figurer dans le NOM de l'application, pas seulement chez
    # l'éditeur : « Brawl Stars » est publiée par une société nommée Supercell,
    # homonyme de l'opérateur congolais. L'éditeur seul ne prouve rien.
    # Exiger un jeton d'au moins trois lettres écartait « AT Mobile App », la
    # vraie application d'AT Ghana : ses jetons sont « at » et « ghana », et le
    # nom ne porte que le premier. N'importe quel jeton suffit donc.
    if not any(_mot(j, nom_n) for j in jetons):
        return False
    return True


def cherche_appstore(marque: str, iso2: str) -> list[dict]:
    """Applications de cette marque sur la vitrine nationale, avec avis."""
    terme = urllib.parse.quote(marque)
    url = (f"https://itunes.apple.com/search?term={terme}"
           f"&country={iso2.lower()}&entity=software&limit=12")
    data = http_json(url)
    if not data:
        return []

    retenues = []
    for a in data.get("results", []):
        nom, editeur = a.get("trackName", ""), a.get("artistName", "")
        avis = a.get("userRatingCount") or 0
        if not _retenir(nom, editeur, marque, iso2, avis):
            continue
        retenues.append({
            "app_id": str(a.get("trackId")),
            "nom": nom,
            "editeur": editeur,
            "avis": avis,
        })
    return sorted(retenues, key=lambda x: -x["avis"])[:4]


def cherche_playstore(marque: str, iso2: str) -> list[dict]:
    """Idem côté Google Play. `search()` de google_play_scraper étant en panne,
    on interroge la page de recherche publique et on confirme chaque paquet
    par l'API `app()`, qui, elle, répond."""
    import re
    import requests
    from google_play_scraper import app as gp_app

    try:
        r = requests.get(
            "https://play.google.com/store/search",
            params={"q": marque, "c": "apps", "gl": iso2.upper(), "hl": "en"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=40,
        )
        paquets = list(dict.fromkeys(
            re.findall(r"/store/apps/details\?id=([A-Za-z0-9_.]+)", r.text)))[:8]
    except Exception:
        return []

    retenues = []
    for pkg in paquets:
        try:
            a = gp_app(pkg, lang="en", country=iso2.lower())
        except Exception:
            continue
        avis = a.get("ratings") or 0
        if not _retenir(a.get("title", ""), a.get("developer", ""),
                        marque, iso2, avis):
            continue
        retenues.append({
            "package_id": pkg,
            "nom": a.get("title", ""),
            "editeur": a.get("developer", ""),
            "avis": avis,
        })
    return sorted(retenues, key=lambda x: -x["avis"])[:4]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pays", nargs="*", metavar="ISO2")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not ECART.exists():
        print("config/couverture_ecart.json absent — lancer d'abord "
              "tools/verify_operator_coverage.py --json config/couverture_ecart.json",
              file=sys.stderr)
        return 2

    manquants = json.loads(ECART.read_text(encoding="utf-8"))["manquants"]
    filtre = {c.upper() for c in (args.pays or [])}
    if filtre:
        manquants = [m for m in manquants if m["iso2"] in filtre]
    if args.limit:
        manquants = manquants[: args.limit]

    print(f"{len(manquants)} candidat(s) à tester en boutique…\n")
    resultats = []
    for i, m in enumerate(manquants, 1):
        marque = m["brand"] or m["exploitant"]
        ios = cherche_appstore(marque, m["iso2"])
        time.sleep(PAUSE)
        android = cherche_playstore(marque, m["iso2"])

        # Une marque qui est aussi un mot courant ne peut pas être tranchée par
        # une recherche textuelle, quels que soient les garde-fous : le test
        # dirait « vivant » pour un jeu ou un réveil homonyme. On le DIT, au
        # lieu de produire un verdict que rien ne soutient.
        ambigu = any(j in MOTS_COURANTS for j in brand_tokens(marque))
        if not (ios or android):
            verdict = "SANS APP"
        elif ambigu:
            verdict = "INDECIDABLE"
        else:
            verdict = "PISTE"
        resultats.append({**m, "marque_testee": marque, "verdict": verdict,
                          "appstore": ios, "playstore": android})

        detail = ""
        if ios:
            detail += f" iOS:{ios[0]['nom'][:22]}({ios[0]['avis']})"
        if android:
            detail += f" AND:{android[0]['nom'][:22]}({android[0]['avis']})"
        print(f"  [{i:>3}/{len(manquants)}] {m['iso2']} {marque[:22]:22} "
              f"{verdict:8}{detail}")

    vivants = [r for r in resultats if r["verdict"] == "PISTE"]
    indecidables = [r for r in resultats if r["verdict"] == "INDECIDABLE"]
    SORTIE.write_text(json.dumps(
        {"_meta": {"teste_le": time.strftime("%Y-%m-%d"),
                   "candidats": len(resultats), "vivants": len(vivants),
                   "regle": "PISTE = une app plausible a ete trouvee, ce qui reste a "
                            "confirmer a l'oeil ; INDECIDABLE = marque trop banale "
                            "pour qu'une recherche textuelle conclue ; SANS APP = ne "
                            "prouve rien, beaucoup d'operateurs actifs n'en publient pas"},
         "candidats": resultats}, ensure_ascii=False, indent=1), encoding="utf-8")

    sans = len(resultats) - len(vivants) - len(indecidables)
    print(f"\n{'=' * 70}")
    print(f"  {len(vivants):3d}  PISTE       — app plausible, A CONFIRMER A L'OEIL")
    print(f"  {len(indecidables):3d}  INDECIDABLE — marque trop banale, à trancher à l'œil")
    print(f"  {sans:3d}  SANS APP    — ne prouve rien, renvoyés au régulateur")
    print(f"\nÉcrit : {SORTIE.relative_to(RACINE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
