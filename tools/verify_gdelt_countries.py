"""
Confronte la table FIPS de `countries.py` à l'API GDELT réelle.

POURQUOI CET OUTIL EXISTE
    `sourcecountry:` attend du FIPS 10-4, pas de l'ISO 3166, et sept codes du
    périmètre sont des faux amis : `ZA` est l'ISO2 de l'Afrique du Sud mais le
    FIPS de la ZAMBIE. Une inversion ne lève aucune erreur — GDELT renvoie
    simplement les articles du mauvais pays, qui sont ensuite attribués à la
    mauvaise filiale. Le tableau de bord affiche alors des chiffres crédibles
    et faux.

    Le seul contrôle qui vaille est empirique : interroger, et vérifier que le
    champ `sourcecountry` des articles renvoyés est bien le pays attendu.

USAGE
    python tools/verify_gdelt_countries.py            # tout le périmètre
    python tools/verify_gdelt_countries.py ZA NG SN   # quelques pays

    Comptez ~6 secondes par pays : l'API impose un appel toutes les 5 s et
    refuse les rafales. Le périmètre complet prend environ six minutes.
"""

import sys
import time
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviews.collectors.countries import COUNTRIES  # noqa: E402

API = "https://api.gdeltproject.org/api/v2/doc/doc"
INTERVAL = 6.5          # marge sur les 5 s imposées : la fenêtre est glissante
TIMESPAN = "1m"         # large, pour qu'un petit pays ait quand même des articles


def probe(fips: str) -> tuple[str, list[str]]:
    """Interroge GDELT sur un code pays. Retourne (état, pays observés)."""
    query = urllib.parse.quote(f"telecom sourcecountry:{fips}")
    url = f"{API}?query={query}&mode=artlist&maxrecords=50&format=json&timespan={TIMESPAN}"
    try:
        response = requests.get(url, timeout=60)
    except requests.RequestException as e:
        return f"RESEAU ({type(e).__name__})", []

    body = response.text.lstrip()
    if not body.startswith("{"):
        # Refus de débit : arrive en HTTP 200 avec du texte brut.
        return "DEBIT", []
    try:
        articles = response.json().get("articles") or []
    except ValueError:
        return "JSON ILLISIBLE", []
    if not articles:
        return "AUCUN ARTICLE", []
    return "OK", sorted({a.get("sourcecountry") for a in articles if a.get("sourcecountry")})


def main() -> int:
    demandes = [c.upper() for c in sys.argv[1:]] or sorted(COUNTRIES)
    inconnus = [c for c in demandes if c not in COUNTRIES]
    for code in inconnus:
        print(f"  ISO2 hors table, ignoré : {code}")
    demandes = [c for c in demandes if c in COUNTRIES]

    print(f"{len(demandes)} pays à vérifier, ~{len(demandes) * INTERVAL / 60:.0f} min\n")
    print(f"{'ISO2':5} {'FIPS':5} {'PAYS ATTENDU':28} VERDICT")
    print("-" * 86)

    suspects, indetermines = [], []
    for i, iso2 in enumerate(demandes):
        nom_fr, nom_en, fips = COUNTRIES[iso2]
        etat, observes = probe(fips)

        if etat != "OK":
            verdict = f"~ {etat}"
            indetermines.append(iso2)
        elif nom_en in observes:
            autres = [p for p in observes if p != nom_en]
            verdict = "OK" + (f"  (aussi : {', '.join(autres[:2])})" if autres else "")
        else:
            verdict = f"!! ATTENDU {nom_en!r}, OBSERVE {observes[:3]}"
            suspects.append((iso2, fips, nom_en, observes[:3]))

        print(f"{iso2:5} {fips:5} {nom_fr:28} {verdict}")
        if i < len(demandes) - 1:
            time.sleep(INTERVAL)

    print("\n" + "=" * 86)
    if suspects:
        print(f"{len(suspects)} CODE(S) A CORRIGER dans reviews/collectors/countries.py :")
        for iso2, fips, attendu, observes in suspects:
            print(f"  {iso2} -> FIPS {fips!r} ramene {observes}, pas {attendu!r}")
    else:
        print("Aucun code suspect.")
    if indetermines:
        print(f"\n{len(indetermines)} pays indetermine(s) (trop peu d'articles ou debit) "
              f"— relancer sur ceux-la seuls : {' '.join(indetermines)}")
    return 1 if suspects else 0


if __name__ == "__main__":
    raise SystemExit(main())
