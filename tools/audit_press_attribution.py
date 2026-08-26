"""
Que deviendrait le corpus de presse si chaque article était rattaché à ce
qu'il NOMME, plutôt qu'au terme de recherche qui l'a fait remonter ?

SANS RIEN MODIFIER. Cet audit précède une migration qui touchera 7 718 lignes ;
il faut voir ce qu'elle ferait avant de la faire.

CE QU'ON MESURE
    `rss_feed` écrit `company = <terme de recherche>`. Google News étant un
    moteur flou, ce terme n'a aucun rapport garanti avec le contenu : mesuré,
    seuls 57,4 % des articles nomment l'opérateur auquel ils sont attribués, et
    Telesom comme TN Mobile tombent à 0 % sur 526 articles.

    On rejoue donc sur chaque article la reconnaissance déjà écrite pour
    `press_feed` — limites de mots, et marqueur de pays obligatoire pour les
    opérateurs multi-pays — et on classe le résultat en trois cas :

      CONFIRMÉ      l'article nomme bien la filiale à laquelle il est rattaché
      À RÉATTRIBUER il en nomme une (ou plusieurs) autre(s)
      ORPHELIN      il n'en nomme aucune du périmètre

    Les orphelins ne sont pas tous du bruit : « Internet mobile au Bénin :
    colère après la fin des forfaits illimités » ne nomme aucun opérateur et
    décrit pourtant un vrai événement de marché. C'est pourquoi la migration
    les MARQUERA au lieu de les supprimer.

Usage :
    python -m tools.audit_press_attribution
    python -m tools.audit_press_attribution --exemples 20
"""

import argparse
import re
import sys
from collections import Counter

from reviews.collectors.press_feed import _normalize
from reviews.collectors.targets import press_matchers
from reviews.storage.db import get_database


def compiler_matchers() -> dict[str, dict]:
    """Même construction que `press_feed._compile_matchers`, sans instancier le
    collecteur (qui ouvrirait une session HTTP dont l'audit n'a que faire)."""
    groupes: dict[str, dict] = {}
    for m in press_matchers():
        groupe = groupes.setdefault(
            m["operator"],
            {
                "operator": re.compile(r"\b" + re.escape(_normalize(m["operator"])) + r"\b"),
                "filiales": [],
            },
        )
        groupe["filiales"].append(
            {
                "name": m["name"],
                "iso2": m["iso2"],
                "countries": [
                    re.compile(r"\b" + re.escape(_normalize(c)) + r"\b")
                    for c in m["country_markers"]
                ],
            }
        )
    return groupes


def operateurs_cites(groupes: dict[str, dict], haystack: str) -> list[str]:
    """Opérateurs nommés, SANS exiger de marqueur de pays.

    Sert à séparer deux populations que le décompte d'orphelins confondait :

      - « MTN et Ericsson unissent leurs forces en Afrique » nomme un opérateur
        du périmètre mais aucun pays. C'est une actualité de GROUPE, réelle et
        exploitable, qu'aucune filiale ne peut pourtant s'attribuer sans mentir.
      - « Les oranges, un luxe pour les Marocains » ne nomme personne. C'est du
        bruit de moteur de recherche.

    Les additionner donnerait un taux de déchet surévalué, et supprimer le tout
    perdrait de la vraie information.
    """
    return [nom for nom, g in groupes.items() if g["operator"].search(haystack)]


def filiales_citees(groupes: dict[str, dict], haystack: str) -> list[str]:
    """Filiales réellement nommées par l'article.

    Reprend la règle de `press_feed._filiales_citees`, moins le repli sur le
    pays d'édition : un article Google News n'a pas de flux d'origine
    identifiable, donc ce repli n'a rien sur quoi s'appuyer ici.
    """
    trouvees: list[str] = []
    for groupe in groupes.values():
        if not groupe["operator"].search(haystack):
            continue
        filiales = groupe["filiales"]
        mono = [f["name"] for f in filiales if not f["countries"]]
        if mono:
            trouvees.extend(mono)
            continue
        trouvees.extend(
            f["name"] for f in filiales
            if any(rx.search(haystack) for rx in f["countries"])
        )
    return trouvees


def main() -> int:
    parser = argparse.ArgumentParser(description="Audite l'attribution du corpus de presse.")
    parser.add_argument("--exemples", type=int, default=12)
    args = parser.parse_args()

    groupes = compiler_matchers()
    print(f"{len(groupes)} opérateur(s), "
          f"{sum(len(g['filiales']) for g in groupes.values())} filiale(s) reconnaissable(s)\n")

    db = get_database()
    with db.cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT v.review_id, v.title, v.text, v.subsidiary, v.operator, v.country
            FROM v_reviews_enriched v
            WHERE v.source_kind = 'press'
            """
        )
        articles = [dict(r) for r in cur.fetchall()]

    confirmes, reattribuer, groupe_seul, orphelins = [], [], [], []
    for a in articles:
        haystack = _normalize(f"{a['title'] or ''} {a['text'] or ''}")
        citees = filiales_citees(groupes, haystack)
        if citees:
            if a["subsidiary"] in citees:
                confirmes.append(a)
            else:
                reattribuer.append((a, citees))
        elif operateurs_cites(groupes, haystack):
            groupe_seul.append((a, operateurs_cites(groupes, haystack)))
        else:
            orphelins.append(a)

    total = len(articles)
    print("=" * 76)
    print(f"AUDIT D'ATTRIBUTION — {total} articles de presse")
    print("=" * 76)
    for libelle, lot in (
        ("Confirmés            ", confirmes),
        ("À réattribuer        ", reattribuer),
        ("Groupe, sans pays    ", groupe_seul),
        ("Orphelins (bruit)    ", orphelins),
    ):
        print(f"  {libelle} : {len(lot):>5}  ({100 * len(lot) / total:.1f} %)")

    print(f"\n--- {min(args.exemples, len(groupe_seul))} ACTUALITÉS DE GROUPE "
          f"(opérateur nommé, aucun pays — inattribuables en l'état) ---")
    for a, ops in groupe_seul[: args.exemples]:
        print(f"  [{', '.join(ops[:2])}] {(a['title'] or '')[:82]}")

    print(f"\n--- {min(args.exemples, len(reattribuer))} À RÉATTRIBUER ---")
    for a, citees in reattribuer[: args.exemples]:
        print(f"  {(a['title'] or '')[:82]}")
        print(f"      actuel : {a['subsidiary']}")
        print(f"      réel   : {', '.join(citees[:3])}")

    print(f"\n--- {min(args.exemples, len(orphelins))} ORPHELINS "
          f"(aucune filiale nommée — à marquer, pas à supprimer) ---")
    for a in orphelins[: args.exemples]:
        print(f"  [{a['subsidiary']}] {(a['title'] or '')[:88]}")

    perdants = Counter(a["subsidiary"] for a in orphelins)
    print("\n--- filiales dont l'actualité est la plus douteuse ---")
    for nom, nb in perdants.most_common(10):
        total_f = sum(1 for a in articles if a["subsidiary"] == nom)
        print(f"  {nom:<28} {nb:>4} orphelins sur {total_f}")

    print("\nAucune donnée n'a été modifiée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
