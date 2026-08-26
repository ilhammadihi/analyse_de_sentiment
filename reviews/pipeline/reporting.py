"""Affichage lisible d'un résultat de pipeline (CLI)."""

from reviews.domain.models import PipelineRun


def print_summary(run: PipelineRun) -> None:
    print("\n" + "=" * 60)
    print("RÉSUMÉ DE L'EXÉCUTION")
    print("=" * 60)
    print(f"Run ID     : {run.run_id}")
    print(f"Status     : {run.status}")
    print(f"Total avis : {run.total_reviews}")
    print(f"Doublons   : {run.total_duplicates}")
    print(f"Erreurs    : {run.total_errors}")
    if run.duration_seconds is not None:
        print(f"Durée      : {run.duration_seconds:.2f}s")
    print("=" * 60)

    for name, result in run.scraper_results.items():
        print(f"\n[{name.upper()}]")
        print(f"  Status    : {result.status}")
        print(f"  Insérés   : {result.inserted_count}")
        print(f"  Doublons  : {result.duplicate_count}")
        print(f"  Erreurs   : {result.error_count}")
        if result.duration_seconds is not None:
            print(f"  Durée     : {result.duration_seconds:.2f}s")
        if result.error_message:
            print(f"  Erreur    : {result.error_message}")
    print()
