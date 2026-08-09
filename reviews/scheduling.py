"""
Planification du pipeline avec APScheduler.

UN JOB PAR COLLECTEUR, et non un cycle global qui les enchaîne.

    La version précédente réveillait un job unique qui parcourait toutes les
    sources à la suite. Le défaut n'était pas théorique : Google Maps demande
    405 recherches, soit une dizaine d'heures mesurées, pour une fenêtre de
    planification de six. Le run n'atteignait jamais sa dernière ligne, donc
    jamais `alert_manager.process()` — et l'alerting s'est tu pendant trois
    jours sans que rien ne le signale, puisque l'alerte qui aurait dû prévenir
    était elle-même derrière la source lente.

    Chaque source a désormais son job, à SA cadence, avec `max_instances=1` :
    une source lente saute sa propre occurrence suivante et ne retient plus
    personne. Chaque run porte alors une seule source et se termine — donc ses
    alertes partent.

Un run qui échoue n'interrompt jamais le planificateur. Utilisé par le service
`worker` dans docker-compose.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from reviews.config import get_settings
from reviews.log_setup import setup_logging
from reviews.storage.db import get_database
from reviews.pipeline.runner import build_pipeline

logger = logging.getLogger("scheduler")


def _safe_run(pipeline, source: str) -> None:
    """Exécute UNE source, sans jamais laisser remonter d'exception.

    `force=False` est conservé alors que le job ne porte qu'une source : c'est
    un filet, pas une redondance. APScheduler peut rattraper une occurrence
    manquée (machine réveillée, conteneur redémarré), et la cadence lue en base
    empêche alors de relancer une collecte qui vient d'avoir lieu.
    """
    try:
        pipeline.run(force=False, sources=[source])
    except Exception as e:  # noqa: BLE001
        logger.error("Collecte %s en échec : %s", source, e, exc_info=True)


def _safe_semantic_pass(settings) -> None:
    """Analyse sémantique des avis nouvellement collectés.

    POURQUOI C'EST UN JOB SÉPARÉ, ET NON UNE ÉTAPE DU PIPELINE
        Le pipeline de collecte doit rester gratuit et autonome : il tourne
        toutes les six heures sans surveillance, et lui greffer une dépendance
        à un fournisseur externe ferait d'une panne d'API une panne de collecte.
        Ici, un échec ne coûte que des aspects manquants, rattrapés au passage
        suivant.

    POURQUOI IL EXISTE MALGRÉ TOUT
        Sans lui, les aspects ne se remplissent que par une commande lancée à la
        main. Sur un environnement de test où personne ne se connecte, l'onglet
        Motifs resterait indéfiniment vide — la fonctionnalité serait livrée
        mais morte.

    Le volume est BORNÉ à chaque passage, et le client refuse de lui-même de
    dépasser le budget quotidien : un quota gratuit ne se consomme donc jamais
    d'un coup, il s'étale sur les passages successifs jusqu'à rattraper le
    corpus.
    """
    if not (settings.llm.enabled and settings.llm.api_key):
        return
    try:
        from reviews.llm.client import get_client
        from reviews.llm.semantic import SemanticAnalyzer

        db = get_database()
        report = SemanticAnalyzer(db, get_client(db)).run(
            limit=settings.llm.scheduled_batch_limit
        )
        logger.info("Analyse sémantique planifiée : %s", report.as_dict())
    except Exception as e:  # noqa: BLE001
        logger.error("Analyse sémantique planifiée en échec : %s", e, exc_info=True)


def run_scheduler() -> None:
    setup_logging()
    settings = get_settings()
    get_database().apply_schema()  # idempotent : garantit le schéma

    pipeline = build_pipeline(settings)
    interval = settings.scheduler.interval_minutes
    sources = settings.get_enabled_scrapers()
    if not sources:
        logger.error("Aucun collecteur activé : le planificateur n'a rien à faire")
        return

    cadences = {nom: settings.scraper_interval_minutes(nom) for nom in sources}

    # Ménage d'ouverture, sur DEUX niveaux : les runs et les unités de la file.
    #
    # Le critère est l'heure de DÉMARRAGE DE CE PROCESSUS, et non un délai de
    # grâce. Tout ce qui était encore « en cours » avant que ce worker existe
    # est nécessairement mort — le processus qui le tenait n'est plus là. C'est
    # exact et immédiat, là où un délai calé sur la plus longue cadence laissait
    # huit runs morts affichés « en cours » pendant douze heures.
    demarrage = datetime.now(timezone.utc)

    orphelins = pipeline.run_repo.reclaim_interrupted_runs(before=demarrage)
    if orphelins:
        logger.warning("%d run(s) interrompu(s) refermé(s) au démarrage", orphelins)

    # Les unités, elles, ne sont pas refermées mais RENDUES à la file, curseur
    # compris : le travail déjà fait à l'intérieur reste acquis. Sans ce passage
    # au démarrage, une unité abandonnée attendait le prochain passage de SA
    # source — jusqu'à six heures — alors que son propriétaire était mort.
    if pipeline.job_repo is not None:
        unites = pipeline.job_repo.reclaim_stale(before=demarrage)
        if unites:
            logger.warning(
                "%d unité(s) de collecte rendue(s) à la file au démarrage", unites
            )

    scheduler = BlockingScheduler(
        timezone=settings.scheduler.timezone,
        executors={"default": ThreadPoolExecutor(settings.scheduler.max_concurrent)},
    )

    # Premier passage : PLANIFIÉ, jamais exécuté avant `scheduler.start()`.
    #
    # L'appel synchrone d'avant bloquait le démarrage pendant toute la durée du
    # premier cycle. Avec Google Maps en tête de liste, le planificateur ne
    # démarrait donc pas de la journée : aucune autre source ne tournait, et le
    # seul run existant était celui qui n'aboutissait pas.
    maintenant = datetime.now(ZoneInfo(settings.scheduler.timezone))
    espacement = settings.scheduler.stagger_minutes

    for rang, nom in enumerate(sources):
        premier = (
            maintenant + timedelta(minutes=rang * espacement)
            if settings.scheduler.run_on_start
            else maintenant + timedelta(minutes=cadences[nom] + rang * espacement)
        )
        scheduler.add_job(
            _safe_run, "interval", minutes=cadences[nom], args=[pipeline, nom],
            id=f"collect:{nom}", max_instances=1, coalesce=True,
            next_run_time=premier,
            # Une occurrence manquée reste valable tant qu'un cycle entier ne
            # s'est pas écoulé : sans cette tolérance, une source dont le
            # créneau tombe pendant un redémarrage attend sa cadence complète.
            misfire_grace_time=int(cadences[nom] * 60),
        )

    logger.info(
        "Planificateur démarré : %d job(s), un par source — %s "
        "(%d collecte(s) simultanée(s) au plus, démarrages espacés de %d min)",
        len(sources),
        ", ".join(f"{nom} {cadences[nom]} min" for nom in sources),
        settings.scheduler.max_concurrent, espacement,
    )

    # Analyse sémantique : job DISTINCT du pipeline, et décalé.
    #
    # Distinct, pour qu'une panne du fournisseur n'entraîne jamais la collecte.
    # Décalé, pour qu'il ne démarre pas pendant que le pipeline écrit encore :
    # il travaillerait sur un corpus incomplet et devrait tout reprendre au
    # passage suivant. Le décalage vaut un quart de cycle, borné à 15 minutes.
    if settings.llm.enabled and settings.llm.api_key:
        decalage = max(1, min(15, interval // 4))
        scheduler.add_job(
            _safe_semantic_pass, "interval", minutes=interval, args=[settings],
            id="semantic", max_instances=1, coalesce=True,
            next_run_time=datetime.now(ZoneInfo(settings.scheduler.timezone))
            + timedelta(minutes=decalage),
        )
        logger.info(
            "Analyse sémantique planifiée (toutes les %d min, premier passage "
            "dans %d min, %d avis par passage)",
            interval, decalage, settings.llm.scheduled_batch_limit,
        )
    elif settings.llm.enabled:
        logger.info(
            "Analyse sémantique inactive : aucune clé LLM_API_KEY configurée. "
            "Le lexique continue de classer tous les avis."
        )

    # Pas d'exécution synchrone ici : `run_on_start` est déjà porté par le
    # `next_run_time` de chaque job. Lancer la collecte avant `start()` rendait
    # le démarrage tributaire de la source la plus lente — le planificateur ne
    # démarrait qu'une fois Google Maps terminé, c'est-à-dire jamais.
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Planificateur arrêté")
