"""
Analyse sémantique des avis déjà en base : sentiment relu et aspects métier.

POURQUOI UN OUTIL SÉPARÉ, ET NON UNE ÉTAPE DU PIPELINE
    Le pipeline de collecte doit rester autonome et gratuit : il tourne toutes
    les six heures sans surveillance, et une dépendance à un fournisseur externe
    y introduirait un point de panne pour une fonctionnalité d'analyse. Le
    lexique continue donc de classer chaque avis à l'insertion ; cette couche
    passe après, quand on décide de la faire passer.

CONÇU POUR UN QUOTA GRATUIT QU'ON NE MAÎTRISE PAS
    Google ne publie plus les limites du niveau gratuit de Gemini et les a déjà
    réduites sans préavis. L'outil est donc REPRENABLE : il ne traite que les
    lignes non analysées, s'arrête proprement quand le budget est atteint, et la
    prochaine exécution repart exactement où celle-ci s'est arrêtée. Le rejouer
    trois jours de suite est une manière parfaitement valable de traiter le
    corpus.

UTILISATION
    python -m tools.backfill_aspects --dry-run        # ce qui serait fait
    python -m tools.backfill_aspects --limit 200      # un premier lot
    python -m tools.backfill_aspects --all            # jusqu'au budget du jour

    En Docker :
    docker compose exec api python -m tools.backfill_aspects --limit 200
"""

import argparse
import logging
import sys

from reviews.config import get_settings
from reviews.domain.aspects import ASPECT_VERSION
from reviews.llm.client import get_client
from reviews.llm.semantic import SemanticAnalyzer
from reviews.log_setup import setup_logging
from reviews.storage.db import get_database

logger = logging.getLogger("backfill_aspects")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyse sémantique (aspects métier) des avis en base."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=400,
        help="Nombre maximal d'avis traités sur cette exécution (défaut : 400).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Traite tout ce que le budget quotidien permet, sans autre borne.",
    )
    parser.add_argument(
        "--source-kind",
        default="customer_review",
        help="Type de source à analyser. « all » pour ne pas restreindre. "
        "Par défaut les avis clients seulement : le vocabulaire de la presse "
        "n'est pas une plainte de client, et la taxonomie ne le décrit pas.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'appelle rien : affiche l'état, le reste à faire et le coût estimé.",
    )
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    db = get_database()
    client = get_client(db)
    analyzer = SemanticAnalyzer(db, client)

    source_kind = None if args.source_kind == "all" else args.source_kind

    pending = analyzer.pending_count(source_kind)
    coverage = analyzer.coverage(source_kind)
    usage = client.usage_today()
    batch = max(1, settings.llm.batch_size)

    print("=" * 70)
    print(f"Analyse sémantique — taxonomie v{ASPECT_VERSION}")
    print("=" * 70)
    print(f"  Périmètre           : {args.source_kind}")
    print(f"  Déjà analysés       : {coverage['analyses']} / {coverage['total']}"
          f"  ({coverage['part'] if coverage['part'] is not None else '—'} %)")
    print(f"  Restant à analyser  : {pending}")
    print(f"  Modèle              : {settings.llm.model}")
    print(f"  Avis par appel      : {batch}")
    print(f"  Appels consommés    : {usage['calls']} / {settings.llm.daily_call_budget} aujourd'hui")
    print(f"  Appels restants     : {client.remaining_budget()}")
    print("-" * 70)

    if pending == 0:
        print("Rien à faire : tout le périmètre est analysé sous cette taxonomie.")
        return 0

    if args.dry_run:
        # Le plafond réel est le plus contraignant des deux : le budget du jour
        # ou la borne demandée. Annoncer « 165 appels » alors que 40 restent au
        # budget donnerait une estimation fausse et un backfill à moitié fait.
        limit = pending if args.all else min(args.limit, pending)
        appels = -(-limit // batch)  # division entière par excès
        budget = client.remaining_budget()
        print(f"  Traiterait          : {limit} avis")
        print(f"  Soit               ~: {appels} appels")
        if appels > budget:
            traitable = budget * batch
            print(f"  ATTENTION           : le budget du jour n'autorise que {budget} "
                  f"appels, soit ~{traitable} avis.")
            print(f"                        Le reste ({limit - traitable} avis) "
                  "sera traité aux exécutions suivantes.")
        raison = client.unavailable_reason()
        if raison:
            print(f"  BLOQUÉ              : {raison}")
        print("\n(--dry-run : aucun appel n'a été passé.)")
        return 0

    raison = client.unavailable_reason()
    if raison:
        print(f"IMPOSSIBLE : {raison}")
        return 2

    limit = pending if args.all else min(args.limit, pending)
    report = analyzer.run(limit=limit, source_kind=source_kind)

    print(f"  Candidats           : {report.candidats}")
    print(f"  Analysés            : {report.analyses}")
    print(f"  Lots                : {report.lots} (dont {report.lots_en_echec} en échec)")
    if report.arret:
        print(f"  Arrêt               : {report.arret}")
    print("-" * 70)

    reste = analyzer.pending_count(source_kind)
    apres = analyzer.coverage(source_kind)
    print(f"  Couverture          : {apres['part']} %")
    print(f"  Restant             : {reste}")
    if reste:
        print("\n  Relancer la même commande pour poursuivre — l'outil reprend "
              "où il s'est arrêté.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
