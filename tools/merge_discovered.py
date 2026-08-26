"""
Fusionne les identifiants découverts dans config/operators.json.

Règles de sûreté (un identifiant douteux est pire que pas d'identifiant :
il attribue des avis à la mauvaise filiale et fausse tous les agrégats) :
  1. confiance 'match' uniquement — le pays doit être confirmé dans le libellé
     de l'app ou de l'éditeur ; 'neutral' est rejeté (cas réels observés :
     "Orange Money Jordan" proposé pour le Congo, "My Airtel" panafricaine
     proposée pour 9 pays à la fois).
  2. identifiant non nul.
  3. identifiant non partagé entre plusieurs filiales.
  4. les identifiants Moov déjà vérifiés manuellement en production ne sont
     jamais écrasés.
"""

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "operators.json"
DISCOVERED = ROOT / "config" / "discovered.json"

SRC_KEYS = {"appstore": "app_id", "playstore": "package_id"}


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    discovered = {
        r["subsidiary_name"]: r["found"]
        for r in json.loads(DISCOVERED.read_text(encoding="utf-8"))
    }

    # Seule la confiance "match" (pays confirmé dans le libellé de l'app ou de
    # l'éditeur) est acceptée.
    #
    # Un assouplissement a été tenté pour les opérateurs mono-pays, en partant
    # de l'idée qu'il n'y a chez eux aucune confusion inter-pays possible. Il a
    # été retiré : sur 10 identifiants ainsi acceptés, 4 étaient faux —
    # « Mobilis » remontait une app d'un développeur particulier, « Telma »
    # une application de restauration et le portail d'une personne homonyme,
    # « 9mobile » un utilitaire tiers. Une marque courte suffit à faire
    # correspondre n'importe quoi ; le nom du pays est le seul garde-fou fiable.
    def accepted(_sub: dict, hit: dict) -> bool:
        return hit.get("_confidence") == "match"

    # Repère les identifiants partagés par plusieurs filiales -> ambigus
    shared: dict[str, set] = {}
    for src, key in SRC_KEYS.items():
        counts = defaultdict(set)
        by_name = {s["subsidiary_name"]: s for s in config["subsidiaries"]}
        for name, found in discovered.items():
            hit = found.get(src) or {}
            sub = by_name.get(name)
            if sub and hit.get(key) and accepted(sub, hit):
                counts[hit[key]].add(name)
        shared[src] = {k for k, v in counts.items() if len(v) > 1}

    stats = defaultdict(int)
    for sub in config["subsidiaries"]:
        found = discovered.get(sub["subsidiary_name"], {})

        for src, key in SRC_KEYS.items():
            if sub["sources"].get(src):
                stats[f"{src}_deja_verifie"] += 1
                continue  # ne jamais écraser un identifiant validé en prod
            hit = found.get(src) or {}
            if not accepted(sub, hit):
                stats[f"{src}_rejete_confiance"] += 1
                continue
            if not hit.get(key):
                stats[f"{src}_rejete_vide"] += 1
                continue
            if hit[key] in shared[src]:
                stats[f"{src}_rejete_partage"] += 1
                continue
            entry = {key: hit[key]}
            if src == "appstore":
                entry["store_country"] = hit["store_country"]
            entry["_verified_app"] = hit.get("_app_name")
            entry["_verified_publisher"] = hit.get("_seller") or hit.get("_developer")
            # Trace la règle qui a fait accepter l'identifiant : "match" = pays
            # confirmé dans le libellé, "mono-pays" = opérateur d'un seul pays,
            # donc sans risque de confusion. Utile pour re-auditer plus tard.
            entry["_match_rule"] = (
                "pays-confirme" if hit.get("_confidence") == "match" else "mono-pays"
            )
            sub["sources"][src] = entry
            stats[f"{src}_retenu"] += 1
            if entry["_match_rule"] == "mono-pays":
                stats[f"{src}_retenu_mono_pays"] += 1

        # Google Maps : requête texte libre, aucun identifiant à vérifier
        if not sub["sources"].get("google_maps") and found.get("google_maps"):
            sub["sources"]["google_maps"] = found["google_maps"]
            stats["google_maps_retenu"] += 1

    total = len(config["subsidiaries"])
    filled = sum(
        1
        for s in config["subsidiaries"]
        if s["sources"].get("appstore") or s["sources"].get("playstore")
    )
    config["_meta"]["verified_entries"] = filled
    config["_meta"]["total_entries"] = total
    CONFIG.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Config mise à jour : {CONFIG}")
    for k in sorted(stats):
        print(f"  {k:32s} : {stats[k]}")
    print(f"\n  Filiales avec au moins une app (store) : {filled}/{total}")
    print(f"  Filiales avec RSS (aucun identifiant requis) : {total}/{total}")


if __name__ == "__main__":
    main()
