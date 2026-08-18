"""
Ré-attribue le corpus de presse déjà collecté, selon ce que chaque article
NOMME plutôt que selon le terme de recherche qui l'a fait remonter.

À BLANC PAR DÉFAUT. Rien n'est écrit sans `--apply`. L'opération touche
plusieurs milliers de lignes ; elle doit pouvoir être lue avant d'être faite.

CE QUI EST ÉCRIT, ET CE QUI NE L'EST JAMAIS
    Aucune ligne n'est supprimée. Le bruit est MARQUÉ (`attribution='noise'`),
    ce qui le sort de `v_reviews_enriched` donc de tous les écrans, tout en le
    laissant consultable dans `reviews`. Si la règle se révèle fautive, un
    second passage rétablit tout : c'est la raison d'être de
    `attribution_version`.

LES CAS À PLUSIEURS FILIALES
    « RD Congo : MTN Rwanda accusé d'utilisation illégale des fréquences »
    nomme trois filiales MTN. Le schéma n'en accepte qu'une par ligne, et en
    choisir une au hasard inventerait une information. L'article est donc
    classé GROUP, rattaché à l'opérateur commun quand il y en a un — ce qui est
    exactement ce qu'on sait de lui : c'est du MTN, sans pays déterminé.

Usage :
    python -m tools.reattribute_press
    python -m tools.reattribute_press --apply
"""

import argparse
import logging
import sys
from collections import Counter

from reviews.collectors.targets import press_matchers
from reviews.domain.press_attribution import (
    ATTRIBUTION_VERSION,
    CONFIRMED,
    GROUP,
    NOISE,
    REATTRIBUTED,
    classify,
    compile_matchers,
)
from reviews.log_setup import setup_logging
from reviews.storage.db import get_database

logger = logging.getLogger("reattribute_press")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ré-attribue les articles de presse.")
    parser.add_argument("--apply", action="store_true", help="Écrit réellement en base.")
    parser.add_argument("--exemples", type=int, default=8)
    args = parser.parse_args()

    setup_logging()
    db = get_database()
    groupes = compile_matchers(press_matchers())

    with db.cursor(dict_rows=True) as cur:
        cur.execute("SELECT subsidiary_id, name, operator_id FROM dim_subsidiary")
        subs = {r["name"]: r for r in cur.fetchall()}
        cur.execute("SELECT operator_id, name FROM dim_operator")
        ops = {r["name"]: r["operator_id"] for r in cur.fetchall()}

        # La vue masque déjà le bruit d'un passage précédent : on interroge la
        # TABLE, sans quoi un second passage ne verrait plus ce qu'il a écarté
        # et ne pourrait jamais le réhabiliter.
        cur.execute(
            """
            SELECT r.review_id, r.title, r.text, r.subsidiary_id, s.name AS subsidiary
            FROM reviews r
            JOIN dim_source src ON src.source_id = r.source_id
            LEFT JOIN dim_subsidiary s ON s.subsidiary_id = r.subsidiary_id
            WHERE src.kind = 'press'
            """
        )
        articles = [dict(r) for r in cur.fetchall()]

    if not articles:
        logger.error("Aucun article de presse en base.")
        return 1

    plan: list[tuple[str, str, object, object]] = []  # (id, etat, sub_id, op_id)
    compte = Counter()
    exemples: dict[str, list] = {REATTRIBUTED: [], GROUP: [], NOISE: []}

    for a in articles:
        etat, filiales, operateurs = classify(
            groupes, a["title"], a["text"], a["subsidiary"]
        )

        sub_id, op_id = a["subsidiary_id"], None

        if etat == REATTRIBUTED:
            connues = [f for f in filiales if f in subs]
            if len(connues) == 1:
                sub_id = subs[connues[0]]["subsidiary_id"]
            elif connues:
                # Plusieurs filiales : on ne tranche pas, on remonte d'un cran.
                operateurs_des_filiales = {subs[f]["operator_id"] for f in connues}
                etat = GROUP
                sub_id = None
                op_id = (
                    operateurs_des_filiales.pop()
                    if len(operateurs_des_filiales) == 1
                    else None
                )
            else:
                etat = NOISE
        elif etat == GROUP:
            sub_id = None
            op_id = ops.get(operateurs[0]) if len(set(operateurs)) == 1 else None

        compte[etat] += 1
        if etat in exemples and len(exemples[etat]) < args.exemples:
            exemples[etat].append((a, filiales or operateurs))
        plan.append((a["review_id"], etat, sub_id, op_id))

    total = len(articles)
    print("=" * 74)
    print(f"RÉ-ATTRIBUTION — règle v{ATTRIBUTION_VERSION} — {total} articles")
    print("=" * 74)
    for etat in (CONFIRMED, REATTRIBUTED, GROUP, NOISE):
        n = compte[etat]
        print(f"  {etat:<14} {n:>5}  ({100 * n / total:.1f} %)")

    for etat, titre in (
        (REATTRIBUTED, "RÉATTRIBUÉS"),
        (GROUP, "ACTUALITÉ DE GROUPE"),
        (NOISE, "BRUIT (marqués, non supprimés)"),
    ):
        if not exemples[etat]:
            continue
        print(f"\n--- {titre} ---")
        for a, cibles in exemples[etat]:
            print(f"  {(a['title'] or '')[:80]}")
            print(f"      {a['subsidiary'] or '—'}  →  {', '.join(cibles[:3]) or '—'}")

    if not args.apply:
        print("\nPassage à blanc — aucune écriture. Ajoutez --apply pour appliquer.")
        return 0

    with db.cursor() as cur:
        cur.executemany(
            """
            UPDATE reviews
               SET subsidiary_id = %s,
                   operator_id = %s,
                   attribution = %s,
                   attribution_version = %s
             WHERE review_id = %s
            """,
            [(sub_id, op_id, etat, ATTRIBUTION_VERSION, rid)
             for rid, etat, sub_id, op_id in plan],
        )

    logger.info("%d article(s) mis à jour.", len(plan))
    print(f"\n{len(plan)} article(s) mis à jour.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
