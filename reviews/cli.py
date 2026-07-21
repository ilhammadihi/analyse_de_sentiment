"""
Interface en ligne de commande unifiée.

    python -m reviews init-db        # crée/vérifie le schéma
    python -m reviews run [--dry-run] # exécute le pipeline une fois
    python -m reviews serve           # lance l'API FastAPI
    python -m reviews schedule        # lance le pipeline en boucle (APScheduler)
"""

import argparse
import logging
import os
import sys

from reviews.log_setup import setup_logging


def _cmd_init_db() -> int:
    from reviews.storage.db import get_database
    logging.getLogger("cli").info("Initialisation de la base de données…")
    get_database().apply_schema()
    print("✓ Base de données initialisée")
    return 0


def _cmd_run(dry_run: bool) -> int:
    from reviews.storage.db import get_database
    from reviews.pipeline.runner import build_pipeline
    from reviews.pipeline.reporting import print_summary

    if not dry_run:
        get_database().apply_schema()  # idempotent

    pipeline = build_pipeline()
    try:
        run = pipeline.run(dry_run=dry_run)
    finally:
        get_database().close_all()
    print_summary(run)
    return 0 if run.status == "success" else 1


def _cmd_serve(host: str | None, port: int | None) -> int:
    import uvicorn
    from reviews.config import get_settings
    settings = get_settings()
    uvicorn.run(
        "reviews.api.main:app",
        host=host or settings.api.host,
        port=port or settings.api.port,
    )
    return 0


def _cmd_schedule() -> int:
    from reviews.scheduling import run_scheduler
    run_scheduler()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviews",
        description="Plateforme de collecte et d'analyse de sentiment des avis clients",
    )
    parser.add_argument("--verbose", action="store_true", help="Logs détaillés (DEBUG)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Créer/vérifier le schéma de la base")

    p_run = sub.add_parser("run", help="Exécuter le pipeline une fois")
    p_run.add_argument("--dry-run", action="store_true", help="Sans insertion en BD")

    p_serve = sub.add_parser("serve", help="Lancer l'API FastAPI")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    sub.add_parser("schedule", help="Lancer le pipeline en boucle (APScheduler)")

    args = parser.parse_args(argv)

    if args.verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"
    setup_logging()

    try:
        if args.command == "init-db":
            return _cmd_init_db()
        if args.command == "run":
            return _cmd_run(args.dry_run)
        if args.command == "serve":
            return _cmd_serve(args.host, args.port)
        if args.command == "schedule":
            return _cmd_schedule()
    except KeyboardInterrupt:
        logging.getLogger("cli").info("Interrompu par l'utilisateur")
        return 130
    except Exception as e:  # noqa: BLE001
        logging.getLogger("cli").error("Erreur fatale : %s", e, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
