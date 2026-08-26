"""
Orchestrateur du pipeline (injection de dépendances).

Enchaîne, pour chaque source activée :
    collecte → sentiment (NLP) → déduplication/persistance → métriques
puis, en fin de run : évaluation des alertes et notifications.

Les dépendances (repositories, alert manager) sont injectées : le pipeline est
testable sans BD réelle (mocks), et rien ne se connecte au moment de l'import.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from reviews.config import Settings, get_settings
from reviews.domain.models import PipelineRun, ScraperResult
from reviews.domain.sentiment import (
    LEXICON_VERSION, analyze_sentiment, domain_for_source,
)
from reviews.collectors import COLLECTORS
from reviews.collectors.base import CollectorBackoff
from reviews.alerting.manager import AlertManager
from reviews.storage.db import get_database
from reviews.storage.stats_repository import StatsRepository
from reviews.storage.repository import ReviewRepository, RunRepository, AlertRepository
from reviews.storage.jobs_repository import JobRepository

logger = logging.getLogger("pipeline")


class Pipeline:
    """Orchestrateur principal."""

    def __init__(
        self,
        settings: Settings,
        review_repo: ReviewRepository,
        run_repo: RunRepository,
        alert_manager: AlertManager,
        job_repo: Optional[JobRepository] = None,
    ):
        self.settings = settings
        self.review_repo = review_repo
        self.run_repo = run_repo
        self.alert_manager = alert_manager
        #: File `collection_jobs`. Facultative : sans elle, les collecteurs qui
        #: savent travailler par unités retombent sur `collect()`. C'est ce qui
        #: garde le pipeline testable sans base.
        self.job_repo = job_repo

    def run(
        self,
        dry_run: bool = False,
        force: bool = True,
        sources: Optional[list[str]] = None,
    ) -> PipelineRun:
        """Exécute un cycle de collecte.

        Args:
            dry_run: collecte et enrichit sans rien persister.
            force: ignore les cadences par source et lance TOUS les collecteurs
                activés. Vrai par défaut, pour qu'une exécution manuelle fasse
                ce qu'on lui demande ; le planificateur, lui, passe False.
            sources: restreint le run à ces collecteurs. C'est ce qui permet au
                planificateur de donner UN JOB PAR SOURCE plutôt qu'un cycle
                unique qui les enchaîne. Sans cette séparation, Google Maps —
                une dizaine d'heures pour 405 recherches — retenait tout le run
                derrière lui : la fin n'était jamais atteinte, donc
                `alert_manager.process()` non plus, et l'alerting se taisait
                pendant des jours sans que rien ne le signale.
        """
        run_id = str(uuid.uuid4())
        run = PipelineRun(run_id=run_id, started_at=datetime.utcnow(), status="running")
        if sources:
            logger.info("Démarrage du pipeline", extra={"extra_data": {
                "run_id": run_id, "sources": sources}})
        else:
            logger.info("Démarrage du pipeline", extra={"extra_data": {"run_id": run_id}})

        if not dry_run:
            self.run_repo.start_run(run_id)

        try:
            enabled = self.settings.get_enabled_scrapers()
            if not enabled:
                raise ValueError("Aucun collecteur activé")

            if sources is not None:
                demandes = set(sources)
                inconnus = demandes - set(enabled)
                if inconnus:
                    # Désactivé entre-temps, ou nom erroné : on le dit sans
                    # faire échouer le run des autres sources demandées.
                    logger.warning(
                        "Source(s) ignorée(s), non activée(s) : %s",
                        ", ".join(sorted(inconnus)),
                    )
                enabled = [n for n in enabled if n in demandes]
                if not enabled:
                    logger.info("Aucune source activée parmi %s : rien à faire",
                                sorted(demandes))
                    run.ended_at = datetime.utcnow()
                    run.status = "success"
                    if not dry_run:
                        self.run_repo.end_run(run_id, run.status,
                                              run.model_dump(mode="json"))
                    return run

                # Budget de temps : calculé APRÈS validation, jamais avant.
                # Le demander pour une source inexistante ferait échouer le run
                # sur une faute de configuration — donc émettre `run_failed`,
                # donc sonner sur Telegram, pour une source simplement
                # désactivée. Une seule source : son budget est sa cadence
                # (cf. PipelineRun.budget_seconds).
                if len(enabled) == 1:
                    run.budget_seconds = (
                        self.settings.scraper_interval_minutes(enabled[0]) * 60
                    )

            if force:
                due = enabled
            else:
                due, reportes = self._split_by_schedule(enabled)
                if reportes:
                    logger.info("Reportés (cadence non écoulée) : %s",
                                ", ".join(reportes))
                if not due:
                    # AUCUNE ERREUR : c'est le fonctionnement normal d'un
                    # planificateur qui passe toutes les six heures alors que
                    # Google Maps tourne toutes les vingt-quatre. Lever ici
                    # ferait échouer trois cycles sur quatre et noierait
                    # l'alerting sous des pannes imaginaires.
                    logger.info("Aucun collecteur dû à cette heure : rien à faire")
                    run.ended_at = datetime.utcnow()
                    run.status = "success"
                    if not dry_run:
                        self.run_repo.end_run(run_id, run.status,
                                              run.model_dump(mode="json"))
                    return run
            enabled = due
            logger.info("Collecteurs de ce cycle : %s", enabled)

            # Repère de collecte incrémentale, lu UNE fois pour tout le run.
            # C'est le pipeline qui interroge la base et transmet le résultat :
            # les collecteurs restent sans accès BD (cf. collectors/base.py).
            watermarks = self._load_watermarks()

            for name in enabled:
                run.scraper_results[name] = self._run_collector(
                    name, run_id, dry_run, watermarks
                )

            run.total_reviews = sum(r.inserted_count for r in run.scraper_results.values())
            run.total_duplicates = sum(r.duplicate_count for r in run.scraper_results.values())
            run.total_errors = sum(r.error_count for r in run.scraper_results.values())
            run.ended_at = datetime.utcnow()
            run.status = "success"

            if not dry_run:
                self.run_repo.end_run(run_id, run.status, run.model_dump(mode="json"))

            self.alert_manager.process(run)
            logger.info("Pipeline terminé", extra={"extra_data": {
                "run_id": run_id, "inserted": run.total_reviews,
                "duplicates": run.total_duplicates, "errors": run.total_errors}})
            return run

        except Exception as e:
            logger.error("Erreur pipeline : %s", e, exc_info=True)
            run.ended_at = datetime.utcnow()
            run.status = "failed"
            run.error_message = str(e)
            if not dry_run:
                self.run_repo.end_run(run_id, "failed", {"error": str(e)})
            self.alert_manager.process(run)
            raise

    def _split_by_schedule(self, enabled: list[str]) -> tuple[list[str], list[str]]:
        """Sépare les collecteurs dus de ceux dont la cadence n'est pas écoulée.

        Un collecteur jamais exécuté est TOUJOURS dû : au premier démarrage,
        aucun n'a d'historique, et les reporter tous laisserait une base vide
        sans que rien ne l'explique.

        Un échec de lecture ne bloque rien : on retombe sur « tout est dû »,
        c'est-à-dire le comportement d'avant la planification par source. Plus
        coûteux, jamais faux.
        """
        try:
            derniers = self.run_repo.last_attempt_by_scraper()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Cadences par source indisponibles (%s) : tout est lancé", e
            )
            return enabled, []

        maintenant = datetime.now(timezone.utc)
        dus, reportes = [], []
        for name in enabled:
            dernier = derniers.get(name)
            if dernier is None:
                dus.append(name)
                continue
            if dernier.tzinfo is None:
                dernier = dernier.replace(tzinfo=timezone.utc)
            interval = self.settings.scraper_interval_minutes(name)
            ecoule = (maintenant - dernier).total_seconds() / 60
            # Marge d'une minute : le planificateur se réveille à intervalle
            # fixe, et l'exécution précédente a duré. Sans elle, un collecteur
            # à 1 440 min manquerait systématiquement son créneau et ne
            # tournerait qu'un jour sur deux.
            if ecoule >= interval - 1:
                dus.append(name)
            else:
                reportes.append(f"{name} ({ecoule:.0f}/{interval} min)")
        return dus, reportes

    def _load_watermarks(self) -> dict:
        """Date du dernier avis connu par (entreprise, source).

        Un échec ici ne doit pas interrompre le run : on retombe sur une
        collecte complète, plus coûteuse mais correcte.
        """
        try:
            marks = self.review_repo.latest_review_dates()
            logger.info("Repère incrémental chargé : %d couple(s) (entreprise, source)",
                        len(marks))
            return marks
        except Exception as e:  # noqa: BLE001
            logger.warning("Repère incrémental indisponible (%s) : collecte complète", e)
            return {}

    def _run_collector(
        self, name: str, run_id: str, dry_run: bool, watermarks: Optional[dict] = None
    ) -> ScraperResult:
        """Collecte, enrichit (sentiment) et persiste une source."""
        collector_cls = COLLECTORS.get(name)
        if collector_cls is None:
            logger.error("Collecteur inconnu : %s", name)
            return ScraperResult(scraper_name=name, started_at=datetime.utcnow(),
                                 ended_at=datetime.utcnow(), status="failed",
                                 error_message="collecteur inconnu")

        collector = collector_cls()
        # Injecté sur l'instance (et non via __init__) pour ne pas modifier la
        # signature des cinq collecteurs existants.
        collector.since = watermarks or {}

        # Collecteur découpé en unités : le pipeline les enchaîne lui-même et
        # persiste après chacune. En dry-run on garde le chemin classique — il
        # n'écrit rien, donc la file n'aurait rien à suivre.
        if (getattr(collector, "SUPPORTS_UNITS", False)
                and self.job_repo is not None and not dry_run):
            return self._run_units(collector, name, run_id)

        result = collector.run()                # collecte SEULE (avec retry)
        if result.status == "failed" or not result.reviews:
            # L'ÉCHEC EST ENREGISTRÉ, LUI AUSSI.
            #
            # Il ne l'était pas, et cela se voyait dans les chiffres :
            # `run_metrics` comptait 420 lignes, toutes en « success ». Un
            # collecteur qui échouait ne laissait aucune trace — GDELT n'y avait
            # pas une seule ligne alors qu'il avait bien tourné.
            #
            # Deux conséquences, l'une visible et l'autre non :
            #   * l'onglet Collecte présentait une chaîne sans le moindre échec,
            #     ce qui est exactement l'inverse de ce qu'un écran de
            #     supervision doit montrer ;
            #   * surtout, la cadence par source lit ce journal. Sans ligne, un
            #     collecteur en échec passe pour « jamais exécuté », donc dû à
            #     chaque cycle : Google Maps, qui met jusqu'à 55 minutes avant
            #     d'échouer, monopoliserait le worker toutes les six heures au
            #     lieu de respecter ses vingt-quatre.
            if not dry_run:
                self.run_repo.record_metric(run_id, result)
            return result

        self._enrich(result.reviews)

        if dry_run:
            result.inserted_count = len(result.reviews)
            logger.info("Dry-run : %d avis (pas d'insertion) pour %s",
                        len(result.reviews), name)
            return result

        stats = self.review_repo.batch_insert(run_id, result.reviews)
        result.inserted_count = stats["inserted"]
        result.duplicate_count = stats["duplicates"]
        result.error_count += stats["errors"]
        self.run_repo.record_metric(run_id, result)
        return result

    def _enrich(self, reviews: list) -> None:
        """Sentiment NLP à partir du texte, en place.

        On conserve la sortie complète du moteur (score continu + termes
        déclenchés), pas seulement le label : c'est ce qui alimente l'onglet
        « Motifs d'insatisfaction » et le contexte des futurs agents IA.
        Re-analyser plus tard coûterait un passage sur toute la table.
        """
        for review in reviews:
            # Le DOMAINE conditionne le lexique employé : les poids appris sur
            # des avis d'applications n'ont aucun sens sur un article de presse
            # (« la fibre ARRIVE » y devenait une mauvaise nouvelle).
            score = analyze_sentiment(
                review.text, domain=domain_for_source(review.source)
            )
            review.sentiment = score.sentiment.value
            review.sentiment_score = score.score
            review.pos_terms = score.positive_terms
            review.neg_terms = score.negative_terms
            review.lexicon_version = LEXICON_VERSION

    # ------------------------------------------------------------------
    # Collecte par UNITÉS (file `collection_jobs`)
    # ------------------------------------------------------------------

    #: Refus d'affilée tolérés avant d'abandonner le passage.
    #:
    #: Mesuré sur GDELT : s'arrêter au PREMIER refus rendait des passages à une
    #: seule unité, soit 135 passages pour couvrir le périmètre. Une fenêtre de
    #: bridage est souvent passagère, et le collecteur attend déjà entre deux
    #: essais. Trois refus consécutifs, en revanche, disent que la fenêtre est
    #: bien ouverte : continuer ne ferait qu'user le budget en attentes pures.
    BRIDAGES_TOLERES = 3

    def _run_units(self, collector, name: str, run_id: str) -> ScraperResult:
        """Enchaîne les unités d'un collecteur, en persistant après chacune.

        TROIS PROPRIÉTÉS, et c'est pour elles que ce chemin existe.

        1. UN ÉCHEC NE COÛTE QUE SON UNITÉ. Avant, une erreur à la 200e des
           405 recherches faisait échouer le collecteur entier et perdait les
           199 réussies — alors qu'aucune ne dépend des autres.
        2. LE PASSAGE EST BORNÉ. Il traite ce que le budget permet puis
           s'arrête ; le reste attend en base. Le run se termine donc toujours,
           et l'alerting qui suit sa fin est atteint à chaque cycle. C'est
           exactement ce qui manquait : Google Maps tentait ses dix heures et se
           faisait tuer avant d'avoir rien écrit.
        3. LA REPRISE EST FINE. Le curseur d'une unité interrompue survit en
           base : la recherche reprend à la 4e fiche, pas à la première.
        """
        debut = time.monotonic()
        budget = self.settings.unit_run_budget_seconds(name)
        result = ScraperResult(scraper_name=name, started_at=datetime.utcnow(),
                               status="running")

        # Catalogue réinscrit à chaque passage : idempotent sur `job_key`, il
        # absorbe les villes qui tournent d'un jour à l'autre sans toucher à
        # l'état des unités déjà connues.
        catalogue = self.job_repo.plan(name, collector.plan_units())
        repris = self.job_repo.reclaim_stale(name)
        remis = self.job_repo.reschedule_due(
            name, self.settings.scraper_interval_minutes(name)
        )
        logger.info(
            "File %s : %d unité(s) au catalogue, %d bail(s) repris, "
            "%d remise(s) en file, %d en attente — budget %d s",
            name, catalogue["planifies"], repris, remis,
            self.job_repo.pending_count(name), budget,
        )

        faites = echouees = bridages = consecutifs = 0
        collector.open_session()
        try:
            while time.monotonic() - debut < budget:
                jobs = self.job_repo.claim(name, limit=1, run_id=run_id)
                if not jobs:
                    logger.info("File %s vide : rien de plus à collecter", name)
                    break
                job = jobs[0]
                try:
                    avis = collector.collect_unit(
                        job,
                        save_cursor=lambda c, jid=job.job_id: (
                            self.job_repo.save_cursor(jid, c)
                        ),
                    )
                    avis = collector.drop_already_known(avis)
                    self._enrich(avis)
                    stats = (self.review_repo.batch_insert(run_id, avis) if avis
                             else {"inserted": 0, "duplicates": 0, "errors": 0})

                    self.job_repo.complete(
                        job.job_id, items_found=len(avis),
                        items_inserted=stats["inserted"],
                    )
                    result.inserted_count += stats["inserted"]
                    result.duplicate_count += stats["duplicates"]
                    result.error_count += stats["errors"]
                    faites += 1
                    # Une unité qui passe prouve que la fenêtre de bridage
                    # s'est refermée : le compteur repart de zéro.
                    consecutifs = 0
                except CollectorBackoff as e:
                    # La SOURCE bride : ce n'est pas la faute de l'unité. On la
                    # rend à la file SANS compter de tentative — sinon les 132
                    # filiales de GDELT atteindraient MAX_ATTEMPTS en un seul
                    # passage, pour une raison étrangère à chacune d'elles.
                    bridages += 1
                    consecutifs += 1
                    self.job_repo.release(job.job_id)

                    # On tolère quelques refus avant de renoncer : une fenêtre
                    # de bridage est souvent passagère, et s'arrêter au premier
                    # refus rendrait un passage à une seule unité — 135 passages
                    # pour couvrir GDELT. Mais insister indéfiniment gaspille le
                    # budget en attentes pures, puisque la source refuse tout le
                    # monde pendant sa fenêtre.
                    if consecutifs >= self.BRIDAGES_TOLERES:
                        logger.info(
                            "Passage %s interrompu : %d refus d'affilée de la "
                            "source (%s) — %d unité(s) traitée(s), le reste "
                            "attend en file", name, consecutifs, e, faites,
                        )
                        break
                    continue
                except Exception as e:  # noqa: BLE001
                    # L'unité est remise en file avec son curseur : le travail
                    # déjà fait à l'intérieur n'est pas perdu non plus.
                    echouees += 1
                    result.error_count += 1
                    logger.warning("Unité %s en échec : %s", job.label, e)
                    self.job_repo.fail(job.job_id, str(e))
        finally:
            collector.close_session()

        result.ended_at = datetime.utcnow()
        restantes = self.job_repo.pending_count(name)

        # « success » même avec des unités en attente : le passage a fait ce
        # qu'on lui demandait dans son budget. Le marquer « failed » ferait
        # sonner `run_failed` à chaque cycle alors que tout fonctionne.
        # N'est un échec que le passage qui n'a RIEN réussi alors qu'il avait
        # du travail — signature d'une panne du scraper, pas d'un manque d'avis.
        result.status = "failed" if (faites == 0 and echouees > 0) else "success"
        if result.status == "failed":
            result.error_message = (
                f"{echouees} unité(s) tentée(s), aucune réussie — "
                f"source inaccessible ou structure de page modifiée"
            )
        elif bridages and faites == 0:
            # Bridé sans avoir rien collecté : ce n'est pas une panne — la
            # source nous demande d'attendre — mais la trace doit le dire,
            # sinon un passage à zéro avis passe pour une absence d'actualité.
            result.error_message = "Passage interrompu par le bridage de la source"

        logger.info(
            "File %s : %d unité(s) traitée(s), %d en échec, %d bridage(s), "
            "%d avis insérés, %d unité(s) restantes",
            name, faites, echouees, bridages, result.inserted_count, restantes,
        )
        self.run_repo.record_metric(run_id, result)
        return result


def build_pipeline(settings: Optional[Settings] = None) -> Pipeline:
    """Assemble un Pipeline câblé sur la BD réelle (composition root partagée)."""
    settings = settings or get_settings()
    db = get_database()
    return Pipeline(
        settings=settings,
        review_repo=ReviewRepository(db),
        run_repo=RunRepository(db),
        job_repo=JobRepository(db),
        # stats_repo : l'alerting métier mesure les pics sur l'historique en
        # base, pas sur le contenu du run (voir AlertManager._business_alerts).
        alert_manager=AlertManager(
            settings.alerting,
            AlertRepository(db),
            stats_repo=StatsRepository(db),
        ),
    )
