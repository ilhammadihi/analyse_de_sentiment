"""
Intègre dans config/operators.json les identifiants VÉRIFIÉS empiriquement.

DIFFÉRENCE AVEC merge_discovered.py
    `merge_discovered.py` fusionne des candidats jugés sur leur NOM, et rejette
    par précaution tout identifiant partagé entre plusieurs filiales. Cette
    précaution était justifiée tant que rien n'était mesuré : une même app
    attribuée à treize pays ressemble à une erreur d'appariement.

    `tools/verify_identifiers.py` a levé le doute par la mesure. Pour l'app
    « My Airtel Africa » (id 1462268018), les flux d'avis des boutiques
    tanzanienne, zambienne, malgache et seychelloise ont été comparés :
    49, 49, 15 et 2 avis, et ZÉRO avis en commun entre deux boutiques
    quelconques. La boutique App Store est donc réellement segmentée par pays,
    et partager un identifiant entre filiales est légitime — chaque filiale
    reçoit les avis de SON marché.

    Ce script fusionne donc sur les verdicts de la vérification, pas sur les
    noms, et conserve le partage en le marquant explicitement.

RÈGLES DE SÛRETÉ CONSERVÉES
  1. Seuls les verdicts `valide` et `valide_sans_avis` sont intégrés. Un
     verdict `valide_sans_avis` signifie que l'app et l'éditeur sont confirmés
     dans la boutique du pays, mais qu'elle n'a pas encore d'avis : c'est un
     appariement correct, à conserver pour les collectes futures.
  2. Un identifiant DÉJÀ présent dans la config n'est jamais écrasé — il a été
     vérifié manuellement en amont, et cette vérification prime.
  3. La provenance est enregistrée avec chaque identifiant (`_verified_*`), pour
     qu'on puisse toujours savoir sur quoi la décision reposait.

USAGE
    python -m tools.merge_verified --dry-run    # montre sans écrire
    python -m tools.merge_verified
"""

import argparse
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "operators.json"
VERIFIED = ROOT / "config" / "verified.json"

#: Clé de l'identifiant technique, par source.
ID_KEY = {"appstore": "app_id", "playstore": "package_id"}

#: Verdicts acceptés. `rejete` ne l'est jamais : un identifiant douteux est pire
#: que pas d'identifiant, il attribue des avis à la mauvaise filiale et fausse
#: silencieusement tous les agrégats.
ACCEPTED = {"valide", "valide_sans_avis"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien")
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    verified = json.loads(VERIFIED.read_text(encoding="utf-8"))
    by_name = {s["subsidiary_name"]: s for s in config["subsidiaries"]}

    added, skipped_existing, rejected, collisions = [], 0, 0, []

    # Packages Play DÉJÀ attribués dans la configuration, toutes passes
    # confondues.
    #
    # La vérification ne contrôle l'exclusivité qu'ENTRE candidats d'une même
    # passe : un identifiant intégré lors d'une passe antérieure lui est
    # invisible. C'est ainsi que `com.mtn1app`, attribué à MTN Ouganda, a failli
    # l'être aussi à MTN Guinée-Bissau. Comme Google Play ne segmente pas ses
    # avis par pays — mesuré : les mêmes 20 avis pour TZ, ZM, NG, KE et UG — les
    # deux filiales auraient reçu les mêmes avis.
    #
    # Ce contrôle est ici, au dernier verrou avant écriture, précisément parce
    # que c'est le seul endroit qui voit l'état COMPLET de la configuration.
    packages_pris = {
        s["sources"]["playstore"]["package_id"]: s["subsidiary_name"]
        for s in config["subsidiaries"]
        if s["sources"].get("playstore", {}) and
        s["sources"]["playstore"].get("package_id")
    }

    for r in verified:
        if r["verdict"] not in ACCEPTED:
            rejected += 1
            continue

        sub = by_name.get(r["subsidiary"])
        if sub is None:
            continue

        source = r["source"]
        if sub["sources"].get(source):
            # Déjà renseigné : vérification manuelle antérieure, on n'y touche pas.
            skipped_existing += 1
            continue

        if source == "playstore":
            package = r.get("package_id")
            proprietaire = packages_pris.get(package)
            if proprietaire and proprietaire != r["subsidiary"]:
                collisions.append((r["subsidiary"], package, proprietaire))
                continue
            packages_pris[package] = r["subsidiary"]

        entry: dict = {ID_KEY[source]: r.get(ID_KEY[source])}
        if source == "appstore":
            # La boutique nationale fait partie de l'identifiant : c'est elle
            # qui détermine de quel marché viennent les avis. Sans elle, un
            # app_id partagé serait ambigu.
            entry["store_country"] = r["store_country"]

        entry["_verified_app"] = r.get("app_name")
        entry["_verified_publisher"] = r.get("publisher")
        entry["_verified_on"] = date.today().isoformat()
        entry["_verified_reviews"] = r.get("reviews", 0)
        if r.get("_shared_app"):
            # Marqué explicitement : un même identifiant sur plusieurs filiales
            # est légitime (app panafricaine, boutiques disjointes vérifiées),
            # mais ne doit pas passer plus tard pour une faute de saisie.
            entry["_shared_app"] = True

        sub["sources"][source] = entry
        added.append((r["subsidiary"], source, r.get(ID_KEY[source]), r["verdict"]))

    # `verified_entries` compte les filiales disposant d'au moins un
    # identifiant de boutique — c'est ce que le collecteur peut réellement
    # interroger, et donc le seul chiffre utile pour suivre l'avancement.
    with_id = sum(
        1
        for s in config["subsidiaries"]
        if s["sources"].get("appstore") or s["sources"].get("playstore")
    )
    config["_meta"]["verified_entries"] = with_id
    config["_meta"]["last_verification"] = date.today().isoformat()

    print(f"{len(added)} identifiant(s) intégré(s) :")
    for name, source, ident, verdict in added:
        flag = "" if verdict == "valide" else "  (aucun avis pour l'instant)"
        print(f"  + {name:<28} {source:<10} {ident}{flag}")
    if collisions:
        print(f"\n{len(collisions)} collision(s) de package Play écartée(s) :")
        for filiale, package, proprietaire in collisions:
            print(f"  ! {filiale:<28} {package} déjà attribué à {proprietaire}")

    print(
        f"\n{skipped_existing} déjà présent(s) et préservé(s) · "
        f"{rejected} rejeté(s) par la vérification"
    )
    print(f"Filiales interrogeables par au moins une boutique : {with_id} / {len(config['subsidiaries'])}")

    if args.dry_run:
        print("\nDRY-RUN : config/operators.json non modifié.")
        return 0

    CONFIG.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nÉcrit dans {CONFIG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
