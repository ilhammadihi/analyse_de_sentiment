"""
Mesure ce que la règle de pertinence ferait au corpus de presse — SANS RIEN
MODIFIER.

Un filtre qui écarte un tiers d'un corpus doit être regardé avant d'être
appliqué : il peut aussi bien retirer le bruit que supprimer le signal. Ce
script montre les deux côtés — ce qui sortirait, ce qui resterait — et donne
les termes qui ont fait pencher chaque décision.

Usage :
    python -m tools.audit_press_relevance
    python -m tools.audit_press_relevance --exemples 25
"""

import argparse
import sys
from collections import Counter

from reviews.domain.press_relevance import RELEVANCE_VERSION, termes_trouves
from reviews.storage.db import get_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Audite la pertinence du corpus de presse.")
    parser.add_argument("--exemples", type=int, default=15)
    args = parser.parse_args()

    db = get_database()
    with db.cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT v.review_id, v.title, v.text, v.operator, v.country, v.source,
                   COALESCE(v.created_at, v.collected_at) AS occurred_at
            FROM v_reviews_enriched v
            WHERE v.source_kind = 'press'
            """
        )
        articles = [dict(r) for r in cur.fetchall()]

    if not articles:
        print("Aucun article de presse en base.")
        return 1

    gardes, ecartes = [], []
    termes_compte = Counter()
    for a in articles:
        trouves = termes_trouves(a["title"], a["text"])
        if trouves:
            gardes.append((a, trouves))
            termes_compte.update(trouves)
        else:
            ecartes.append(a)

    total = len(articles)
    print("=" * 74)
    print(f"AUDIT DE PERTINENCE — règle v{RELEVANCE_VERSION} — {total} articles")
    print("=" * 74)
    print(f"  Conservés : {len(gardes):>5}  ({100 * len(gardes) / total:.1f} %)")
    print(f"  Écartés   : {len(ecartes):>5}  ({100 * len(ecartes) / total:.1f} %)")

    print(f"\n--- {min(args.exemples, len(ecartes))} articles ÉCARTÉS "
          f"(vérifiez qu'aucun ne devrait rester) ---")
    for a in ecartes[: args.exemples]:
        print(f"  [{a['operator'] or '—'} · {a['country'] or '—'}] {(a['title'] or '')[:95]}")

    print(f"\n--- {min(args.exemples, len(gardes))} articles CONSERVÉS "
          f"(vérifiez qu'aucun ne devrait sortir) ---")
    for a, trouves in gardes[: args.exemples]:
        print(f"  [{a['operator'] or '—'} · {a['country'] or '—'}] {(a['title'] or '')[:78]}")
        print(f"      → {', '.join(trouves[:6])}")

    print("\n--- termes les plus déclencheurs ---")
    for terme, nb in termes_compte.most_common(15):
        print(f"  {terme:<18} {nb}")

    # Un terme qui déclenche à lui seul sur une grande part du corpus mérite
    # d'être regardé : c'est le profil d'un mot trop général qui laisserait
    # passer du bruit.
    solitaires = Counter(t[0] for _, t in gardes if len(t) == 1)
    if solitaires:
        print("\n--- articles retenus sur UN SEUL terme (les plus fragiles) ---")
        for terme, nb in solitaires.most_common(10):
            print(f"  {terme:<18} {nb}")

    print(f"\nEffet sur le compteur affiché : {total} → {len(gardes)} articles de presse.")
    print("Aucune donnée n'a été modifiée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
