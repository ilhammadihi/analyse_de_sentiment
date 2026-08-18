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


def _safe_market_data(settings) -> None:
    """Rafraîchissement mensuel des indicateurs de marché.

    Job à part, comme l'analyse sémantique et l'agent : il dépend d'une API
    externe, et son échec ne doit jamais toucher la collecte d'avis. Une
    donnée annuelle non rafraîchie ce mois-ci reste juste ; un pipeline d'avis
    interrompu perd des avis pour de bon.
    """
    from reviews.collectors.market_data import MarketDataCollector
    from reviews.storage.db import get_database
    from reviews.storage.market_repository import MarketRepository

    try:
        db = get_database()
        with db.cursor() as cur:
            cur.execute(
                "SELECT iso3, country_id FROM dim_country WHERE iso3 IS NOT NULL"
            )
            pays = {r[0]: r[1] for r in cur.fetchall()}
        lignes, erreurs = MarketDataCollector().collect(pays)
        ecrites = MarketRepository(db).upsert(lignes)
        logger.info(
            "Indicateurs de marché : %d mesure(s) écrite(s), %d appel(s) en échec",
            ecrites, len(erreurs),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Collecte des indicateurs de marché en échec : %s", e, exc_info=True)


def _safe_insight_agent(settings) -> None:
    """Passage quotidien de l'agent de veille satisfaction.

    JOB SÉPARÉ DE LA COLLECTE, pour la même raison que l'analyse sémantique :
    l'agent dépend d'un fournisseur de modèle et de l'API Telegram. Greffé au
    pipeline, une panne de l'un ou de l'autre deviendrait une panne de
    collecte — soit le contraire de la hiérarchie voulue, où la donnée prime
    toujours sur son commentaire.

    Les exceptions sont avalées ici comme pour les autres jobs : APScheduler
    désactive un job qui lève trop souvent, et un agent muet parce qu'il a été
    désactivé silencieusement est exactement le mode de panne qui a fait taire
    l'alerting trois jours durant.
    """
    from reviews.agents.insight_agent import build_agent
    from reviews.storage.db import get_database

    try:
        passage = build_agent(get_database(), settings).run()
        logger.info("Agent de veille : %s", passage.resume())
    except Exception as e:  # noqa: BLE001
        logger.error("Agent de veille en échec : %s", e, exc_info=True)


def _safe_campaign_agent(settings) -> None:
    """Passage hebdomadaire de l'assistant de campagne.

    JOB DISTINCT DE CELUI DE LA VEILLE, alors que les deux agents partagent leur
    infrastructure. Ils n'ont ni la même cadence — quotidienne contre
    hebdomadaire — ni le même destinataire : la veille prévient qui exploite,
    la campagne s'adresse à qui communique. Les fondre obligerait à retenir le
    plus lent des deux rythmes, et une panne de l'un ferait taire l'autre.

    Comme les autres jobs, les exceptions sont avalées : APScheduler désactive un
    job qui lève trop souvent, et un agent désactivé silencieusement est le mode
    de panne qu'on ne détecte qu'en s'apercevant, des semaines plus tard, qu'il
    ne dit plus rien.
    """
    from reviews.agents.campaign_agent import build_campaign_agent
    from reviews.storage.db import get_database

    try:
        campagne = build_campaign_agent(get_database(), settings).run()
        logger.info("Assistant de campagne : %s", campagne.resume())
    except Exception as e:  # noqa: BLE001
        logger.error("Assistant de campagne en échec : %s", e, exc_info=True)


def _safe_quality_agent(settings) -> None:
    """Passage quotidien du gardien de la qualité (Agent 3).

    PLANIFIÉ AVANT L'AGENT DE VEILLE, et l'ordre compte. L'Agent 1 doit pouvoir
    lire un DATA TRUST STATUS À JOUR avant de décider de quoi il parle : lancé
    après, il commenterait la satisfaction de filiales dont la qualité de
    données vient seulement d'être réévaluée, sur l'instantané de la veille.

    Une heure d'écart suffit : le passage complet est une poignée de requêtes
    agrégées sur 135 filiales, pas une collecte.

    Exceptions avalées comme les autres jobs : APScheduler désactive un job qui
    lève trop souvent, et un gardien désactivé silencieusement laisserait les
    deux autres agents raisonner sur des données dont plus rien ne vérifie la
    qualité — sans que rien ne le signale.
    """
    from reviews.agents.quality.guardian import build_quality_agent
    from reviews.storage.db import get_database

    try:
        passage = build_quality_agent(get_database(), settings).run()
        logger.info("Agent qualité : %s", passage.resume())
    except Exception as e:  # noqa: BLE001
        logger.error("Agent qualité en échec : %s", e, exc_info=True)


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

    # Agent de veille : rendez-vous quotidien à heure fixe, jamais un intervalle.
    #
    # `cron` et non `interval` parce qu'un briefing est un rendez-vous : à heure
    # fixe on remarque son absence, à intervalle il finit par tomber la nuit et
    # se lit comme une notification de plus. `misfire_grace_time` d'une heure —
    # au-delà, un briefing du matin rattrapé à midi commenterait une journée
    # déjà entamée.
    if settings.scheduler.agent_enabled:
        scheduler.add_job(
            _safe_insight_agent, "cron", hour=settings.scheduler.agent_hour, minute=0,
            args=[settings], id="insight-agent", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "Agent de veille planifié : tous les jours à %02d h 00 (%s)",
            settings.scheduler.agent_hour, settings.scheduler.timezone,
        )

    # Gardien de la qualité : rendez-vous quotidien, AVANT l'agent de veille.
    #
    # `cron` et non `interval`, même raison que le briefing. L'heure par défaut
    # (7 h) précède celle de la veille (8 h) pour que l'Agent 1 lise un statut
    # de confiance calculé le matin même — voir `_safe_quality_agent`.
    if settings.quality.enabled:
        scheduler.add_job(
            _safe_quality_agent, "cron", hour=settings.quality.hour, minute=0,
            args=[settings], id="quality-agent", max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "Agent qualité planifié : tous les jours à %02d h 00 (%s)",
            settings.quality.hour, settings.scheduler.timezone,
        )

    # Assistant de campagne : rendez-vous HEBDOMADAIRE.
    #
    # `day_of_week` et non `interval` pour la même raison que le briefing : un
    # rendez-vous se remarque quand il manque. La tolérance de rattrapage est
    # portée à six heures — contrairement à un briefing du matin, une
    # proposition de campagne reste exploitable en fin de journée, et la perdre
    # ferait attendre une semaine entière.
    if settings.scheduler.campaign_enabled:
        scheduler.add_job(
            _safe_campaign_agent, "cron",
            day_of_week=settings.scheduler.campaign_day,
            hour=settings.scheduler.campaign_hour, minute=0,
            args=[settings], id="campaign-agent", max_instances=1, coalesce=True,
            misfire_grace_time=6 * 3600,
        )
        logger.info(
            "Assistant de campagne planifié : jour %d à %02d h 00 (%s)",
            settings.scheduler.campaign_day, settings.scheduler.campaign_hour,
            settings.scheduler.timezone,
        )

    # Indicateurs de marché : mensuel. La source est annuelle et révisée en
    # cours d'année — voir `SchedulerConfig.market_enabled`.
    if settings.scheduler.market_enabled:
        scheduler.add_job(
            _safe_market_data, "cron",
            day=settings.scheduler.market_day, hour=settings.scheduler.market_hour,
            minute=0, args=[settings], id="market-data",
            max_instances=1, coalesce=True, misfire_grace_time=6 * 3600,
        )
        logger.info(
            "Indicateurs de marché planifiés : le %d de chaque mois à %02d h 00",
            settings.scheduler.market_day, settings.scheduler.market_hour,
        )

    # Pas d'exécution synchrone ici : `run_on_start` est déjà porté par le
    # `next_run_time` de chaque job. Lancer la collecte avant `start()` rendait
    # le démarrage tributaire de la source la plus lente — le planificateur ne
    # démarrait qu'une fois Google Maps terminé, c'est-à-dire jamais.
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Planificateur arrêté")
