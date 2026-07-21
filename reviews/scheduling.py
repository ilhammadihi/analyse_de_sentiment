"""
Planification du pipeline avec APScheduler.

Lance le pipeline à intervalle régulier (SCHEDULER_INTERVAL_MINUTES). Un run qui
échoue n'interrompt jamais le planificateur. Utilisé par le service `worker`
dans docker-compose.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from reviews.config import get_settings
from reviews.log_setup import setup_logging
from reviews.storage.db import get_database
from reviews.pipeline.runner import build_pipeline

logger = logging.getLogger("scheduler")


def _safe_run(pipeline) -> None:
    try:
        pipeline.run()
    except Exception as e:  # noqa: BLE001
        logger.error("Run planifié en échec : %s", e, exc_info=True)


def run_scheduler() -> None:
    setup_logging()
    settings = get_settings()
    get_database().apply_schema()  # idempotent : garantit le schéma

    pipeline = build_pipeline(settings)
    interval = settings.scheduler.interval_minutes

    scheduler = BlockingScheduler(timezone=settings.scheduler.timezone)
    scheduler.add_job(
        _safe_run, "interval", minutes=interval, args=[pipeline],
        id="pipeline", max_instances=1, coalesce=True,
    )
    logger.info("Planificateur démarré (toutes les %d min)", interval)

    if settings.scheduler.run_on_start:
        logger.info("Exécution initiale du pipeline")
        _safe_run(pipeline)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Planificateur arrêté")
