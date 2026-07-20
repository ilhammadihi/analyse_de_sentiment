"""
Validation du moteur de sentiment (sentiment_analyzer.py).

Compare le sentiment déduit du texte (NLP) au sentiment déduit de la note
(1-5 étoiles) sur les avis déjà en base, comme proxy de référence en
l'absence de données annotées manuellement. Un désaccord n'est pas
forcément une erreur du NLP (ex. avis 5 étoiles mais texte sarcastique),
mais un fort taux de désaccord signale un problème.

Usage: python validate_sentiment.py [--limit N] [--show-disagreements N]
"""

import argparse
from collections import Counter

from database import db
from sentiment_analyzer import analyze_sentiment


def rating_to_sentiment(rating: int) -> str:
    """Réplique la logique de Review.compute_sentiment (models.py)."""
    if rating >= 4:
        return "positive"
    elif rating == 3:
        return "neutral"
    return "negative"


def main():
    parser = argparse.ArgumentParser(description="Valide le moteur de sentiment NLP")
    parser.add_argument("--limit", type=int, default=5000, help="Nombre max d'avis à analyser")
    parser.add_argument("--show-disagreements", type=int, default=10, help="Nombre d'exemples de désaccord à afficher")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT review_id, company, text, rating
                FROM reviews
                WHERE rating IS NOT NULL AND text IS NOT NULL AND text != ''
                ORDER BY collected_at DESC
                LIMIT %s
                """,
                (args.limit,),
            )
            rows = cur.fetchall()
    finally:
        db.return_connection(conn)

    if not rows:
        print("Aucun avis avec note en base à valider.")
        return

    confusion = Counter()
    agreements = 0
    disagreements = []

    for review_id, company, text, rating in rows:
        reference = rating_to_sentiment(rating)
        predicted = analyze_sentiment(text).sentiment.value

        confusion[(reference, predicted)] += 1
        if reference == predicted:
            agreements += 1
        else:
            disagreements.append((review_id, company, rating, reference, predicted, text))

    total = len(rows)
    agreement_rate = agreements / total * 100

    print("=" * 70)
    print("VALIDATION DU MOTEUR DE SENTIMENT")
    print("=" * 70)
    print(f"Avis analysés          : {total}")
    print(f"Accord NLP vs note      : {agreements}/{total} ({agreement_rate:.1f}%)")
    print()

    labels = ["positive", "neutral", "negative"]
    print(f"{'Référence (note) \\ NLP':<28}" + "".join(f"{l:>12}" for l in labels))
    for ref in labels:
        row = [confusion.get((ref, pred), 0) for pred in labels]
        print(f"{ref:<28}" + "".join(f"{v:>12}" for v in row))
    print()

    if args.show_disagreements and disagreements:
        print(f"--- {min(args.show_disagreements, len(disagreements))} exemples de désaccord ---")
        for review_id, company, rating, reference, predicted, text in disagreements[: args.show_disagreements]:
            snippet = (text or "")[:80]
            print(f"[{company}] note={rating} | référence={reference} | NLP={predicted} | {snippet!r}")

    print()
    print(f"Total désaccords : {len(disagreements)} ({len(disagreements) / total * 100:.1f}%)")


if __name__ == "__main__":
    main()
