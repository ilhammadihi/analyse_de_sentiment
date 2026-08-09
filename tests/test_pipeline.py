"""
Test du pipeline en injection de dépendances : aucune BD, aucun réseau.
Illustre le gain de l'architecture — l'orchestration est testable avec des
faux repositories et un faux collecteur.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from reviews.domain.models import Review, SourceEnum, SentimentEnum
from reviews.collectors.base import BaseCollector, CollectorBackoff
from reviews.pipeline import runner
from reviews.pipeline.runner import Pipeline


class FakeCollector(BaseCollector):
    """Collecteur factice qui retourne des avis en dur (aucun réseau)."""

    def __init__(self):
        super().__init__("playstore")

    def collect(self):
        return [
            Review(id="1", company="Moov", source=SourceEnum.GOOGLE_PLAY,
                   text="Service excellent, très rapide"),
            Review(id="2", company="Moov", source=SourceEnum.GOOGLE_PLAY,
                   text="Arnaque, panne totale, horrible"),
        ]


def _fake_settings():
    return SimpleNamespace(
        get_enabled_scrapers=lambda: ["playstore"],
        alerting=None,
    )


def test_dry_run_enriches_sentiment_without_db(monkeypatch):
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)

    pipeline = Pipeline(
        settings=_fake_settings(),
        review_repo=Mock(),
        run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
    )

    run = pipeline.run(dry_run=True)

    assert run.status == "success"
    assert run.total_reviews == 2                       # inséré (simulé) en dry-run
    result = run.scraper_results["playstore"]
    sentiments = {r.sentiment for r in result.reviews}
    # Le sentiment a été recalculé depuis le texte (NLP)
    assert SentimentEnum.POSITIVE in sentiments
    assert SentimentEnum.NEGATIVE in sentiments


def test_dry_run_does_not_touch_repositories(monkeypatch):
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)
    review_repo, run_repo = Mock(), Mock()

    Pipeline(
        settings=_fake_settings(),
        review_repo=review_repo,
        run_repo=run_repo,
        alert_manager=Mock(process=Mock(return_value=[])),
    ).run(dry_run=True)

    review_repo.batch_insert.assert_not_called()
    run_repo.start_run.assert_not_called()
    run_repo.end_run.assert_not_called()


# ---------------------------------------------------------------------------
# Reprise des runs interrompus
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Curseur factice : retient le SQL et les paramètres, sans BD."""

    def __init__(self):
        self.sql = ""
        self.params = ()
        self.rowcount = 3

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params or ()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeDb:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self, **_):
        return self.cur


def test_reclaim_ne_touche_que_les_runs_running_et_hors_delai():
    """Un run vivant ne doit jamais être déclaré échoué.

    La reprise s'applique aux seuls runs « running » ANTÉRIEURS au délai de
    grâce. Sans la borne temporelle, un redémarrage du worker pendant qu'une
    exécution manuelle tourne la marquerait en échec alors qu'elle progresse.
    """
    from reviews.storage.repository import RunRepository

    db = _FakeDb()
    ferme = RunRepository(db).reclaim_interrupted_runs(grace_hours=6)

    sql = " ".join(db.cur.sql.split())
    assert "status = 'running'" in sql          # cible restreinte
    assert "started_at <" in sql                # borne temporelle présente
    assert "status = 'failed'" in sql           # statut lisible par le dashboard
    assert 6 * 3600 in db.cur.params            # délai LIÉ, pas interpolé
    assert ferme == 3


def test_reclaim_conserve_les_compteurs_deja_enregistres():
    """Un run interrompu a pu insérer des avis avant de mourir : ce qu'il a
    réellement collecté doit être conservé, pas remis à zéro."""
    from reviews.storage.repository import RunRepository

    db = _FakeDb()
    RunRepository(db).reclaim_interrupted_runs()
    sql = " ".join(db.cur.sql.split())
    assert "COALESCE(NULLIF(total_reviews, 0)" in sql
    assert "COALESCE(NULLIF(total_duplicates, 0)" in sql


def test_reclaim_ninvente_pas_de_duree():
    """Un run mort à un instant inconnu n'a pas de durée mesurable.

    Calculer « maintenant − départ » lui prêtait la durée de l'ARRÊT : les runs
    refermés affichaient 28 000 s, soit huit heures de travail imaginaires, là
    où le dashboard doit lire « — ».
    """
    from reviews.storage.repository import RunRepository

    db = _FakeDb()
    RunRepository(db).reclaim_interrupted_runs()
    sql = " ".join(db.cur.sql.split())
    assert "duration_seconds =" not in sql
    # Le nombre de paramètres liés doit suivre le nombre de marqueurs.
    assert sql.count("%s") == len(db.cur.params)


# ---------------------------------------------------------------------------
# Un run par source : ce qui permet un job de planification par collecteur
# ---------------------------------------------------------------------------


class _AutreCollecteur(FakeCollector):
    def __init__(self):
        BaseCollector.__init__(self, "gdelt")


def _settings_deux_sources(cadences=None):
    cadences = cadences or {"playstore": 360, "gdelt": 720}
    return SimpleNamespace(
        get_enabled_scrapers=lambda: ["playstore", "gdelt"],
        scraper_interval_minutes=lambda nom: cadences[nom],
        alerting=None,
    )


def test_run_restreint_a_une_source(monkeypatch):
    """`sources=` isole un collecteur : c'est ce qui rend un job par source possible.

    Sans cela, chaque réveil rejouait tout le cycle, et une source lente
    (Google Maps, une dizaine d'heures) empêchait le run d'atteindre sa fin —
    donc l'évaluation des alertes, qui n'a lieu qu'à la dernière ligne.
    """
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)
    monkeypatch.setitem(runner.COLLECTORS, "gdelt", _AutreCollecteur)

    run = Pipeline(
        settings=_settings_deux_sources(),
        review_repo=Mock(), run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
    ).run(dry_run=True, sources=["gdelt"])

    assert list(run.scraper_results) == ["gdelt"]


def test_run_mono_source_porte_le_budget_de_sa_cadence(monkeypatch):
    """Le budget suit la source, pas un seuil global.

    C'est lui qui permet à `slow_run` de distinguer Google Maps — long mais
    dans son rythme — d'une source qui déborde de sa propre cadence.
    """
    monkeypatch.setitem(runner.COLLECTORS, "gdelt", _AutreCollecteur)

    run = Pipeline(
        settings=_settings_deux_sources(),
        review_repo=Mock(), run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
    ).run(dry_run=True, sources=["gdelt"])

    assert run.budget_seconds == 720 * 60


def test_run_sans_restriction_na_pas_de_budget(monkeypatch):
    """Exécution manuelle multi-sources : aucune cadence ne s'applique."""
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)
    monkeypatch.setitem(runner.COLLECTORS, "gdelt", _AutreCollecteur)

    run = Pipeline(
        settings=_settings_deux_sources(),
        review_repo=Mock(), run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
    ).run(dry_run=True)

    assert run.budget_seconds is None
    assert set(run.scraper_results) == {"playstore", "gdelt"}


def test_source_inconnue_ne_fait_pas_echouer_le_run(monkeypatch):
    """Une source désactivée entre-temps ne doit pas produire un run « failed ».

    Un run en échec émet `run_failed`, qui part sur Telegram : une faute de
    configuration silencieuse deviendrait une alerte de panne à chaque cycle.
    """
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)
    alert_manager = Mock(process=Mock(return_value=[]))

    run = Pipeline(
        settings=_settings_deux_sources(),
        review_repo=Mock(), run_repo=Mock(),
        alert_manager=alert_manager,
    ).run(dry_run=True, sources=["source_supprimee"])

    assert run.status == "success"
    assert run.scraper_results == {}


# ---------------------------------------------------------------------------
# Collecte par UNITÉS : file collection_jobs
# ---------------------------------------------------------------------------


class _Unite:
    """Ce que `JobRepository.claim` rend au pipeline."""

    def __init__(self, job_id, cursor=None):
        self.job_id = job_id
        self.company = f"Filiale {job_id}"
        self.operator = "Op"
        self.query = f"Agence Op ville{job_id}"
        self.location = f"ville{job_id}"
        self.cursor = cursor or {}
        self.label = self.company


class _FauxFileJobs:
    """File en mémoire : mêmes méthodes que JobRepository, sans base."""

    def __init__(self, nb_unites):
        self.a_faire = [_Unite(i) for i in range(1, nb_unites + 1)]
        self.catalogue = None
        self.completes, self.echecs, self.curseurs = [], [], []

    def plan(self, source, units, job_type="unit"):
        self.catalogue = units
        return {"planifies": len(units)}

    def reclaim_stale(self, source, lease_minutes=120):
        return 0

    def reschedule_due(self, source, interval_minutes):
        return 0

    def pending_count(self, source):
        return len(self.a_faire)

    def claim(self, source, limit=1, run_id=None):
        return [self.a_faire.pop(0)] if self.a_faire else []

    def save_cursor(self, job_id, cursor):
        self.curseurs.append((job_id, cursor))

    def complete(self, job_id, items_found=0, items_inserted=0):
        self.completes.append(job_id)

    def fail(self, job_id, error, retry_in_minutes=10):
        self.echecs.append((job_id, error))


class _CollecteurUnites(BaseCollector):
    """Collecteur découpé, dont on pilote les échecs unité par unité."""

    SUPPORTS_UNITS = True

    def __init__(self, echoue_sur=(), avis_par_unite=2):
        super().__init__("googlemaps")
        self.echoue_sur = set(echoue_sur)
        self.avis_par_unite = avis_par_unite
        self.sessions = []

    def collect(self):                                   # pragma: no cover
        raise AssertionError("le mode unités ne doit pas appeler collect()")

    def plan_units(self):
        return [{"job_key": f"u{i}", "company": f"Filiale {i}"} for i in (1, 2, 3)]

    def open_session(self):
        self.sessions.append("open")

    def close_session(self):
        self.sessions.append("close")

    def collect_unit(self, job, save_cursor):
        save_cursor({"places_done": ["a"]})
        if job.job_id in self.echoue_sur:
            raise RuntimeError(f"panne sur {job.job_id}")
        return [
            Review(id=f"{job.job_id}-{n}", company="Moov", source=SourceEnum.GOOGLE_MAPS,
                   text="Service correct")
            for n in range(self.avis_par_unite)
        ]


def _settings_unites(budget=60):
    return SimpleNamespace(
        get_enabled_scrapers=lambda: ["googlemaps"],
        scraper_interval_minutes=lambda nom: 1440,
        unit_run_budget_seconds=lambda nom: budget,
        alerting=None,
    )


def _pipeline_unites(collecteur, file, budget=60):
    review_repo = Mock()
    review_repo.batch_insert.return_value = {"inserted": 2, "duplicates": 0, "errors": 0}
    p = Pipeline(
        settings=_settings_unites(budget),
        review_repo=review_repo,
        run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
        job_repo=file,
    )
    return p, review_repo


def test_chaque_unite_est_persistee_separement():
    """LE point du découpage : on n'écrit plus une seule fois à la toute fin.

    Google Maps enchaînait 405 recherches et n'insérait qu'au retour de
    `collect()`. Interrompu — ce qui arrivait tous les jours — il ne laissait
    pas un avis, alors qu'il avait scrapé pendant des heures.
    """
    file = _FauxFileJobs(3)
    p, review_repo = _pipeline_unites(_CollecteurUnites(), file)

    result = p._run_units(_CollecteurUnites(), "googlemaps", "run-1")

    assert review_repo.batch_insert.call_count == 3      # une écriture par unité
    assert file.completes == [1, 2, 3]
    assert result.status == "success"
    assert result.inserted_count == 6


def test_une_unite_en_echec_ne_perd_pas_les_autres():
    """« Si Fès échoue, tu ne recommences pas tout le Maroc. »"""
    file = _FauxFileJobs(3)
    collecteur = _CollecteurUnites(echoue_sur={2})
    p, review_repo = _pipeline_unites(collecteur, file)

    result = p._run_units(collecteur, "googlemaps", "run-1")

    assert file.completes == [1, 3]                      # les saines passent
    assert [j for j, _ in file.echecs] == [2]            # seule la 2 est reprise
    assert review_repo.batch_insert.call_count == 2
    assert result.status == "success"                    # le passage reste utile


def test_le_curseur_est_enregistre_pendant_l_unite():
    """Un curseur écrit seulement à la fin ne servirait à rien : l'interruption
    arrive avant la fin."""
    file = _FauxFileJobs(2)
    collecteur = _CollecteurUnites()
    p, _ = _pipeline_unites(collecteur, file)

    p._run_units(collecteur, "googlemaps", "run-1")

    assert [c for _, c in file.curseurs] == [{"places_done": ["a"]}] * 2


def test_le_passage_est_borne_par_son_budget():
    """Budget nul : aucune unité traitée, la file reste intacte, le run se termine.

    C'est la propriété qui manquait — un passage qui se termine TOUJOURS, donc
    qui atteint l'évaluation des alertes, quel que soit le périmètre restant.
    """
    file = _FauxFileJobs(400)
    collecteur = _CollecteurUnites()
    p, review_repo = _pipeline_unites(collecteur, file, budget=0)

    result = p._run_units(collecteur, "googlemaps", "run-1")

    assert review_repo.batch_insert.call_count == 0
    assert len(file.a_faire) == 400                      # tout reste à faire
    assert result.status == "success"


def test_echec_total_signale_une_panne():
    """Aucune unité réussie alors qu'il y avait du travail : c'est le scraper.

    Distinguer ce cas d'un simple « rien de neuf » est ce qui évite l'échec
    silencieux — une source inaccessible ne doit pas passer pour une absence
    d'avis.
    """
    file = _FauxFileJobs(2)
    collecteur = _CollecteurUnites(echoue_sur={1, 2})
    p, _ = _pipeline_unites(collecteur, file)

    result = p._run_units(collecteur, "googlemaps", "run-1")

    assert result.status == "failed"
    assert "aucune réussie" in result.error_message


def test_la_session_est_toujours_refermee():
    """Playwright laissé ouvert interdit tout démarrage ultérieur dans le thread."""
    file = _FauxFileJobs(2)
    collecteur = _CollecteurUnites(echoue_sur={1, 2})
    p, _ = _pipeline_unites(collecteur, file)

    p._run_units(collecteur, "googlemaps", "run-1")

    assert collecteur.sessions == ["open", "close"]


def test_sans_file_le_collecteur_retombe_sur_collect(monkeypatch):
    """`job_repo=None` : le pipeline reste utilisable sans base, donc testable."""
    monkeypatch.setitem(runner.COLLECTORS, "playstore", FakeCollector)
    p = Pipeline(
        settings=_fake_settings(),
        review_repo=Mock(), run_repo=Mock(),
        alert_manager=Mock(process=Mock(return_value=[])),
        job_repo=None,
    )
    run = p.run(dry_run=True)
    assert run.scraper_results["playstore"].inserted_count == 2


# ---------------------------------------------------------------------------
# Bridage par la source : ce n'est pas un échec de l'unité
# ---------------------------------------------------------------------------


class _FauxFileAvecRelache(_FauxFileJobs):
    def __init__(self, nb_unites):
        super().__init__(nb_unites)
        self.relachees = []

    def release(self, job_id, retry_in_minutes=0):
        self.relachees.append(job_id)


class _CollecteurBride(_CollecteurUnites):
    """Réussit `avant` unités, puis la source bride."""

    def __init__(self, avant=1):
        super().__init__()
        self.avant = avant
        self.appels = 0

    def collect_unit(self, job, save_cursor):
        self.appels += 1
        if self.appels > self.avant:
            raise CollectorBackoff("débit dépassé")
        return super().collect_unit(job, save_cursor)


def test_un_bridage_rend_l_unite_sans_compter_de_tentative():
    """GDELT refuse tout pendant sa fenêtre de bridage.

    Traiter ce refus comme un échec ferait atteindre MAX_ATTEMPTS aux 132
    filiales en un seul passage — donc les sortir de la file pour de bon — pour
    une raison qui n'a rien à voir avec elles.
    """
    file = _FauxFileAvecRelache(5)
    collecteur = _CollecteurBride(avant=2)
    p, _ = _pipeline_unites(collecteur, file)

    result = p._run_units(collecteur, "gdelt", "run-1")

    assert file.completes == [1, 2]          # ce qui est passé est acquis
    assert file.relachees == [3, 4, 5]       # rendues, pas blâmées
    assert file.echecs == []                 # aucune tentative consommée
    assert result.status == "success"


def test_le_passage_sarrete_apres_trois_refus_d_affilee():
    """Tolérance, puis renoncement.

    S'arrêter au PREMIER refus rendait des passages à une seule unité — 135
    passages pour couvrir GDELT. Insister indéfiniment userait le budget en
    attentes pures, puisque la source refuse tout le monde pendant sa fenêtre.
    """
    file = _FauxFileAvecRelache(50)
    collecteur = _CollecteurBride(avant=1)
    p, _ = _pipeline_unites(collecteur, file)

    p._run_units(collecteur, "gdelt", "run-1")

    assert collecteur.appels == 4            # 1 réussie + 3 refus tolérés
    assert len(file.a_faire) == 46           # le reste attend intact


def test_une_unite_reussie_remet_le_compteur_de_refus_a_zero():
    """La tolérance porte sur les refus CONSÉCUTIFS.

    Sans remise à zéro, trois refus étalés sur tout un passage — dont les
    unités passent par ailleurs — suffiraient à l'interrompre.
    """
    file = _FauxFileAvecRelache(9)

    class _Alternant(_CollecteurUnites):
        """Un refus sur deux : jamais trois d'affilée."""

        def __init__(self):
            super().__init__()
            self.appels = 0

        def collect_unit(self, job, save_cursor):
            self.appels += 1
            if self.appels % 2 == 0:
                raise CollectorBackoff("débit dépassé")
            return super().collect_unit(job, save_cursor)

    collecteur = _Alternant()
    p, _ = _pipeline_unites(collecteur, file)
    result = p._run_units(collecteur, "gdelt", "run-1")

    assert len(file.a_faire) == 0            # la file est allée jusqu'au bout
    assert len(file.completes) == 5
    assert result.status == "success"


def test_bridage_des_la_premiere_unite_nest_pas_une_panne():
    """Zéro avis parce que la source demande d'attendre : ce n'est pas un échec,
    mais la trace doit le dire — sinon on lit « aucune actualité »."""
    file = _FauxFileAvecRelache(5)
    collecteur = _CollecteurBride(avant=0)
    p, _ = _pipeline_unites(collecteur, file)

    result = p._run_units(collecteur, "gdelt", "run-1")

    assert result.status == "success"
    assert "bridage" in result.error_message
