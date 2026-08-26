"""
Backfill du score de sentiment et des termes déclenchés (migration 004).

POURQUOI UN SCRIPT PYTHON ET PAS DU SQL
    Le lexique de sentiment vit en Python (reviews/domain/sentiment.py) : la
    négation, les intensificateurs et les frontières de clause ne se rejouent pas
    en SQL. Le texte est donc relu ici, ré-analysé, et seules les colonnes
    dérivées sont réécrites — jamais le texte, jamais le label existant.

CE QUE LE SCRIPT NE FAIT PAS
    Il ne touche pas à `reviews.sentiment`. Le label a été produit à la collecte
    et sert de référence historique ; le remplacer changerait rétroactivement des
    chiffres déjà présentés. Le script n'écrit que les colonnes ajoutées par la
    migration 004 (score, termes, version du lexique).

    Exception explicite : `--resync-labels` recalcule aussi le label. À n'utiliser
    qu'après une évolution assumée du lexique, en sachant que les séries
    historiques du dashboard bougeront.

REPRISE
    Seules les lignes dont `lexicon_version` est NULL ou inférieure à la version
    courante sont traitées. Le script est donc interruptible et relançable sans
    refaire le travail déjà fait — sur 20 000 lignes ça compte peu, mais le
    corpus est fait pour grossir.

USAGE
    python -m tools.backfill_sentiment_terms                 # traite le retard
    python -m tools.backfill_sentiment_terms --all           # force tout
    python -m tools.backfill_sentiment_terms --dry-run       # ne réécrit rien
    python -m tools.backfill_sentiment_terms --batch-size 2000
"""

import argparse
import logging
import sys
from pathlib import Path

# Exécutable directement depuis la racine du dépôt, sans installation préalable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg2.extras import execute_values  # noqa: E402

from reviews.domain.sentiment import (  # noqa: E402
    LEXICON_VERSION, analyze_sentiment,
)
from reviews.log_setup import setup_logging  # noqa: E402
from reviews.storage.db import get_database  # noqa: E402

logger = logging.getLogger("backfill")


def count_pending(db, process_all: bool) -> int:
    """Nombre de lignes à traiter, pour annoncer l'ampleur avant de commencer."""
    clause = "" if process_all else "WHERE r.lexicon_version IS NULL OR r.lexicon_version < %s"
    params = () if process_all else (LEXICON_VERSION,)
    with db.cursor() as cur:
        # Alias `r` obligatoire : la clause est partagée avec `fetch_batch`, qui
        # joint dim_source et doit donc qualifier ses colonnes.
        cur.execute(f"SELECT COUNT(*) FROM reviews r {clause}", params)
        return cur.fetchone()[0]


def fetch_batch(db, process_all: bool, size: int, after: str = "") -> list[tuple]:
    """Lot suivant de (review_id, texte, type de source) à analyser.

    PAGINATION PAR CLÉ (`review_id > after`), et non par décalage.

    La version précédente relisait « le premier lot restant » à chaque tour, en
    supposant que les lignes traitées sortaient du périmètre du WHERE. C'est
    vrai en mode incrémental — leur `lexicon_version` devient à jour — mais
    FAUX avec `--all`, qui n'a aucun filtre : les mêmes 1 000 premières lignes
    étaient relues et réécrites indéfiniment. La boucle ne s'arrêtait jamais, et
    le garde-fou anti-boucle ne voyait rien puisque chaque écriture réussissait.
    Symptôme observé : exactement 1 000 lignes migrées sur 21 710, à chaque
    tentative.

    Un OFFSET ne conviendrait pas davantage en mode incrémental : les lignes
    quittant le filtre au fur et à mesure, il en sauterait. La pagination par
    clé est correcte dans LES DEUX modes, et reste efficace grâce à l'index de
    clé primaire.
    """
    conditions = []
    params: list = []
    if not process_all:
        conditions.append("(r.lexicon_version IS NULL OR r.lexicon_version < %s)")
        params.append(LEXICON_VERSION)
    if after:
        conditions.append("r.review_id > %s")
        params.append(after)
    clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with db.cursor() as cur:
        # Le TYPE DE SOURCE est lu avec le texte : il détermine quel lexique
        # appliquer. Sans lui, le rejeu réintroduirait l'abus de domaine que la
        # version 5 du lexique corrige.
        cur.execute(
            f"""SELECT r.review_id, r.text, COALESCE(s.kind, 'customer_review')
                FROM reviews r
                LEFT JOIN dim_source s ON s.source_id = r.source_id
                {clause}
                ORDER BY r.review_id LIMIT %s""",
            params + [size],
        )
        return cur.fetchall()


def write_batch(db, rows: list[tuple], resync_labels: bool) -> int:
    """Écrit un lot de résultats d'analyse.

    UPDATE ... FROM (VALUES %s) : une seule requête par lot au lieu d'un UPDATE
    par ligne. Sur 20 000 avis, la différence se compte en minutes.

    `page_size=len(rows)` force execute_values à n'émettre QU'UNE instruction.
    Avec la pagination par défaut (100), `cur.rowcount` ne rapporte que la
    dernière page : le journal annonçait « 100 lignes écrites » pour un lot de
    1 000, et le garde-fou anti-boucle en aurait été faussé.
    """
    label_assignment = "sentiment = v.sentiment," if resync_labels else ""
    with db.cursor() as cur:
        execute_values(
            cur,
            f"""
            UPDATE reviews r SET
                {label_assignment}
                sentiment_score = v.score::real,
                pos_terms       = v.pos_terms::text[],
                neg_terms       = v.neg_terms::text[],
                lexicon_version = v.version::smallint
            FROM (VALUES %s) AS v(review_id, sentiment, score, pos_terms,
                                  neg_terms, version)
            WHERE r.review_id = v.review_id::text
            """,
            rows,
            page_size=len(rows),
        )
        return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Retraite toutes les lignes, y compris celles déjà à jour.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse et affiche un aperçu, sans aucune écriture.",
    )
    parser.add_argument(
        "--resync-labels",
        action="store_true",
        help="Recalcule aussi reviews.sentiment. Modifie les séries historiques "
        "du dashboard : à n'utiliser qu'après une évolution assumée du lexique.",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    setup_logging()
    db = get_database()

    pending = count_pending(db, args.all)
    logger.info(
        "Backfill : %d ligne(s) à analyser (lexique v%d)%s",
        pending,
        LEXICON_VERSION,
        " — DRY-RUN, aucune écriture" if args.dry_run else "",
    )
    if pending == 0:
        logger.info("Rien à faire : toutes les lignes sont à jour.")
        return 0

    processed = 0
    with_terms = 0
    # Curseur de pagination par clé : dernier review_id traité.
    dernier_id = ""

    while True:
        batch = fetch_batch(db, args.all, args.batch_size, dernier_id)
        if not batch:
            break
        dernier_id = batch[-1][0]

        rows = []
        for review_id, text, kind in batch:
            score = analyze_sentiment(text, domain=kind)
            if score.negative_terms or score.positive_terms:
                with_terms += 1
            rows.append(
                (
                    review_id,
                    score.sentiment.value,
                    score.score,
                    score.positive_terms,
                    score.negative_terms,
                    LEXICON_VERSION,
                )
            )

        if args.dry_run:
            for review_id, sentiment, score, pos, neg, _ in rows[:5]:
                logger.info(
                    "  %s → %s (%.3f) | neg=%s | pos=%s",
                    review_id[:40], sentiment, score, neg[:5], pos[:5],
                )
            processed += len(rows)
            logger.info("Dry-run : %d ligne(s) analysée(s), arrêt après un lot.", processed)
            break

        written = write_batch(db, rows, args.resync_labels)
        processed += len(rows)
        logger.info("  %d / %d lignes écrites (%d dans ce lot)", processed, pending, written)

        # La pagination par clé garantit la progression : `dernier_id` croît
        # strictement à chaque tour, donc la boucle se termine même si une
        # écriture échoue. Une écriture nulle reste anormale et mérite d'être
        # signalée, sans pour autant interrompre le rejeu.
        if written == 0:
            logger.warning(
                "Lot de %d ligne(s) lu mais aucune écrite (dernier id : %s). "
                "Vérifier la migration 004.", len(rows), dernier_id,
            )

    logger.info(
        "Terminé : %d ligne(s) traitée(s), dont %d avec au moins un terme du lexique "
        "(%.1f %%). Les autres n'emploient aucun mot connu du lexique : c'est "
        "attendu pour la presse, qui décrit sans juger.",
        processed,
        with_terms,
        100.0 * with_terms / processed if processed else 0.0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
