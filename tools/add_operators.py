"""
Ajoute des opérateurs manquants à config/operators.json.

Contexte : 13 pays n'avaient qu'un seul opérateur suivi (le Maroc n'affichait
qu'Orange alors qu'il compte aussi Maroc Telecom et Inwi), ce qui donnait
l'impression d'une couverture lacunaire sur la carte.

Chaque entrée est créée avec le SEUL champ qui n'exige aucune vérification
préalable : le terme de recherche presse. Les identifiants d'applications sont
laissés à null et seront renseignés par tools/discover_identifiers.py, qui les
vérifie contre les APIs officielles. Aucun identifiant n'est écrit à la main.

Le rattachement opérateur × pays vient de connaissances publiques générales :
le secteur connaît des fusions et changements de marque fréquents, cette liste
demande donc une relecture métier avant tout usage officiel.

Usage :  python -m tools.add_operators [--dry-run]
"""

import argparse
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "operators.json"

# (opérateur, pays, iso2, nom de filiale, terme de recherche presse)
NEW = [
    # --- Pays qui n'avaient qu'un seul opérateur -------------------------
    ("Maroc Telecom", "Maroc", "MA", "Maroc Telecom", "Maroc Telecom"),
    ("Inwi", "Maroc", "MA", "Inwi", "Inwi Maroc"),
    ("Mobilis", "Algérie", "DZ", "Mobilis", "Mobilis Algérie"),
    ("Djezzy", "Algérie", "DZ", "Djezzy", "Djezzy Algérie"),
    ("Free Sénégal", "Sénégal", "SN", "Free Sénégal", "Free Sénégal opérateur"),
    ("Expresso", "Sénégal", "SN", "Expresso Sénégal", "Expresso Sénégal"),
    ("Ethio Telecom", "Éthiopie", "ET", "Ethio Telecom", "Ethio Telecom"),
    ("Togocom", "Togo", "TG", "Togocom", "Togocom"),
    ("Africell", "Sierra Leone", "SL", "Africell Sierra Leone", "Africell Sierra Leone"),
    ("TNM", "Malawi", "MW", "TNM Malawi", "TNM Malawi"),
    ("Tmcel", "Mozambique", "MZ", "Tmcel", "Tmcel Mozambique"),
    ("Movitel", "Mozambique", "MZ", "Movitel", "Movitel Mozambique"),
    ("Econet", "Lesotho", "LS", "Econet Lesotho", "Econet Telecom Lesotho"),
    ("Eswatini Mobile", "eSwatini", "SZ", "Eswatini Mobile", "Eswatini Mobile"),
    ("Cable & Wireless", "Seychelles", "SC", "Cable & Wireless Seychelles", "Cable Wireless Seychelles"),
    ("Telecel", "Centrafrique", "CF", "Telecel Centrafrique", "Telecel Centrafrique"),
    ("Sotel Tchad", "Tchad", "TD", "Sotel Tchad", "Sotel Tchad"),
    # --- Renforcement des grands marchés --------------------------------
    ("Glo", "Nigeria", "NG", "Glo Nigeria", "Globacom Nigeria"),
    ("9mobile", "Nigeria", "NG", "9mobile", "9mobile Nigeria"),
    ("AirtelTigo", "Ghana", "GH", "AirtelTigo Ghana", "AirtelTigo Ghana"),
    ("Cell C", "Afrique du Sud", "ZA", "Cell C", "Cell C Afrique du Sud"),
    ("Telma", "Madagascar", "MG", "Telma", "Telma Madagascar"),
    ("Halotel", "Tanzanie", "TZ", "Halotel Tanzanie", "Halotel Tanzania"),
    ("Celtiis", "Bénin", "BJ", "Celtiis", "Celtiis Bénin"),
    ("Africell", "RD Congo", "CD", "Africell RDC", "Africell RDC"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    subs = config["subsidiaries"]
    existing = {(s["operator"], s["iso2"]) for s in subs}

    added, skipped = [], []
    for operator, country, iso2, name, search in NEW:
        if (operator, iso2) in existing:
            skipped.append(name)
            continue
        subs.append(
            {
                "operator": operator,
                "country": country,
                "iso2": iso2,
                "subsidiary_name": name,
                "sources": {
                    "appstore": None,
                    "playstore": None,
                    "google_maps": {"query": f"Agence {operator} {country}"},
                    "trustpilot": None,
                    "rss": {"search_term": search},
                },
            }
        )
        added.append(name)

    config["_meta"]["total_entries"] = len(subs)

    if args.dry_run:
        print(f"[simulation] {len(added)} ajout(s), {len(skipped)} déjà présent(s)")
    else:
        CONFIG.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Config mise à jour : {CONFIG}")

    for name in added:
        print(f"  + {name}")
    for name in skipped:
        print(f"  = {name} (déjà présent)")
    print(f"\nTotal filiales : {len(subs)}")


if __name__ == "__main__":
    main()
