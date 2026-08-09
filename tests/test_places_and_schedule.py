"""
Tests des deux chantiers de la vague 2 : couverture réelle des agences Google
Maps, et cadence propre à chaque source. Aucun appel réseau.

Les cas couverts viennent tous d'observations faites sur la vraie recherche
Google Maps, pas d'hypothèses : le premier résultat qui appartient à une autre
enseigne, la même fiche renvoyée par deux requêtes différentes, et les dix
résultats déjà chargés dont un seul était exploité.
"""

from datetime import datetime, timedelta, timezone

from reviews.collectors.google_maps import GoogleMapsScraper
from reviews.collectors.targets import googlemaps_locations
from reviews.config import Settings
from reviews.domain.models import Review, SourceEnum


# ---------------------------------------------------------------------------
# Identité de l'agence
# ---------------------------------------------------------------------------

class TestIdentiteAgence:
    def test_identifiant_extrait_de_l_url(self):
        """L'identifiant hexadécimal survit aux changements de nom du lieu."""
        s = GoogleMapsScraper()
        url = ("https://www.google.com/maps/place/Vodacom+Shop/"
               "@-26.1,28.0,17z/data=!3m1!4b1!4m6!3m5!1s0x1e95a3f2b:0x9d4c1a7e!8m2")
        assert s._place_id_from_url(url) == "0x1e95a3f2b:0x9d4c1a7e"

    def test_repli_sur_le_segment_de_nom_si_pas_d_identifiant(self):
        """Google ne met pas toujours l'identifiant dans le lien de la liste.

        Le repli suffit à dédupliquer à l'intérieur d'un run, ce qui est le
        besoin réel — mieux qu'un `None` qui ferait revisiter la même fiche.
        """
        s = GoogleMapsScraper()
        url = "https://www.google.com/maps/place/MTN+Service+Center/@6.5,3.3,17z"
        assert s._place_id_from_url(url) == "MTN+Service+Center"

    def test_normalisation_ignore_accents_et_casse(self):
        s = GoogleMapsScraper()
        assert s._normalize("Sénégal") == s._normalize("SENEGAL")
        assert s._normalize("Côte d'Ivoire") == "cote d'ivoire"


# ---------------------------------------------------------------------------
# Le filtre d'enseigne — LE correctif de fond
# ---------------------------------------------------------------------------

class _FaussePage:
    """Page Playwright réduite à ce dont `_search_places` a besoin."""

    def __init__(self, resultats, url="https://www.google.com/maps/search/x"):
        self._resultats = resultats
        self.url = url

    def goto(self, *a, **k):
        return None

    def title(self):
        return "Fiche"

    def query_selector_all(self, *a, **k):
        return []

    def locator(self, selector):
        page = self

        class _Loc:
            def evaluate_all(self, _script):
                return page._resultats

        return _Loc()


class TestFiltreEnseigne:
    """« Agence Vodacom Johannesburg » remonte Cellucity en PREMIER résultat.

    Observé en direct. Cellucity est un revendeur tiers : ses avis étaient
    enregistrés comme des avis Vodacom South Africa, et rien en base ne
    permettait de s'en apercevoir.
    """

    RESULTATS_JOHANNESBURG = [
        {"name": "Cellucity - Bedfordview",
         "url": "https://www.google.com/maps/place/Cellucity/data=!1s0xaa:0xbb"},
        {"name": "Vodacom Shop Ghandi Square",
         "url": "https://www.google.com/maps/place/VS1/data=!1s0x11:0x22"},
        {"name": "Vodacom Shop Rosebank Mall",
         "url": "https://www.google.com/maps/place/VS2/data=!1s0x33:0x44"},
    ]

    def _scraper(self):
        s = GoogleMapsScraper()
        s._seen_places.clear()
        return s

    def test_le_revendeur_tiers_est_ecarte(self):
        s = self._scraper()
        page = _FaussePage(self.RESULTATS_JOHANNESBURG)
        places = s._search_places(
            page, {"query": "Agence Vodacom Johannesburg",
                   "name": "Vodacom South Africa", "operator": "Vodacom"}
        )
        noms = [p["name"] for p in places]
        assert "Cellucity - Bedfordview" not in noms
        assert len(noms) == 2

    def test_toutes_les_agences_de_l_enseigne_sont_retenues(self):
        """RÉGRESSION — le collecteur n'ouvrait QUE le premier résultat.

        Les dix autres étaient déjà chargés dans la page : les jeter revenait à
        suivre une agence par filiale au lieu de plusieurs dizaines.
        """
        s = self._scraper()
        page = _FaussePage(self.RESULTATS_JOHANNESBURG)
        places = s._search_places(
            page, {"query": "q", "name": "Vodacom South Africa",
                   "operator": "Vodacom"}
        )
        assert len(places) > 1, "une seule agence retenue : le `.first` est revenu"

    def test_une_meme_agence_n_est_pas_visitee_deux_fois(self):
        """« Agence MTN Lagos » et « Agence MTN Nigeria » renvoient la MÊME fiche.

        Vérifié en direct. Sans déduplication, la densification par ville
        dépensait des minutes de navigateur pour zéro avis nouveau.
        """
        s = self._scraper()
        resultats = [{"name": "MTN Nigeria Communications Limited",
                      "url": "https://www.google.com/maps/place/MTN/data=!1s0x1:0x2"}]
        cible = {"query": "q", "name": "MTN Nigeria", "operator": "MTN"}

        premier = s._search_places(_FaussePage(resultats), cible)
        second = s._search_places(_FaussePage(resultats), cible)
        assert len(premier) == 1
        assert second == [], "la même fiche a été retenue deux fois"

    def test_le_plafond_par_recherche_est_respecte(self):
        s = self._scraper()
        resultats = [
            {"name": f"Vodacom Shop {i}",
             "url": f"https://www.google.com/maps/place/V{i}/data=!1s0x{i}:0x{i}"}
            for i in range(20)
        ]
        places = s._search_places(
            _FaussePage(resultats),
            {"query": "q", "name": "Vodacom South Africa", "operator": "Vodacom"},
        )
        from reviews.config import get_settings
        assert len(places) == get_settings().googlemaps.places_per_query

    def test_sans_operateur_declare_rien_n_est_ecarte(self):
        """Un filtre qui se déclenche sans référence viderait la collecte."""
        s = self._scraper()
        places = s._search_places(
            _FaussePage(self.RESULTATS_JOHANNESBURG),
            {"query": "q", "name": "X", "operator": ""},
        )
        assert len(places) == 3


class TestCiblesPortentLOperateur:
    def test_chaque_cible_declare_son_operateur(self):
        """Sans lui, le filtre d'enseigne n'a aucune référence à comparer."""
        cibles = googlemaps_locations(cities_per_run=2)
        assert cibles
        assert all(c.get("operator") for c in cibles), (
            "une cible sans opérateur laisserait passer les revendeurs tiers"
        )


# ---------------------------------------------------------------------------
# Déduplication : le lieu entre dans le checksum
# ---------------------------------------------------------------------------

class TestChecksumParAgence:
    @staticmethod
    def _avis(**kw):
        base = dict(id="x", company="Vodacom South Africa",
                    source=SourceEnum.GOOGLE_MAPS, text="Bon service")
        base.update(kw)
        return Review(**base)

    def test_deux_agences_meme_texte_ne_sont_pas_un_doublon(self):
        """Deux clients de deux boutiques écrivant « Bon service » sont deux avis.

        Sans le lieu dans le hash, le second serait écarté : plus on couvre
        d'agences, plus on perdrait d'avis courts, et la densification se
        saborderait elle-même.
        """
        a = self._avis(id="r1", target_id="0x1:0x2")
        b = self._avis(id="r2", target_id="0x3:0x4")
        assert a.get_checksum() != b.get_checksum()

    def test_meme_agence_meme_texte_reste_un_doublon(self):
        a = self._avis(id="r1", target_id="0x1:0x2")
        b = self._avis(id="r2", target_id="0x1:0x2")
        assert a.get_checksum() == b.get_checksum()

    def test_les_sources_sans_lieu_gardent_leur_checksum_historique(self):
        """RÉGRESSION CRITIQUE.

        Si le lieu était inséré au milieu de la clé — même vide — le hash de
        TOUTES les lignes déjà en base changerait, et le prochain run les
        réinsérerait en masse comme si elles étaient neuves.
        """
        import hashlib
        avis = Review(id="p1", company="MTN Nigeria",
                      source=SourceEnum.GOOGLE_PLAY, text="Bonne app")
        attendu = hashlib.sha256(
            "MTN Nigeria:google_play:Bonne app".encode()
        ).hexdigest()
        assert avis.get_checksum() == attendu


# ---------------------------------------------------------------------------
# Cadence par source
# ---------------------------------------------------------------------------

class _FauxRunRepo:
    def __init__(self, derniers):
        self._derniers = derniers

    def last_attempt_by_scraper(self):
        return self._derniers


class TestEchecEnregistre:
    """RÉGRESSION — `run_metrics` ne contenait QUE des succès : 420 lignes, 0 échec.

    Un collecteur en échec ne laissait aucune trace. GDELT n'avait pas une seule
    ligne alors qu'il avait bien tourné. La cadence par source lit ce journal :
    sans ligne, un collecteur en échec passe pour « jamais exécuté » et repart à
    chaque cycle — Google Maps, qui met jusqu'à 55 min avant d'échouer,
    monopoliserait le worker.
    """

    def _pipeline(self, enregistres):
        from reviews.pipeline.runner import Pipeline

        class _RunRepo:
            def record_metric(self, run_id, result):
                enregistres.append((result.scraper_name, result.status))

        p = Pipeline.__new__(Pipeline)
        p.settings = Settings()
        p.run_repo = _RunRepo()
        return p

    def test_un_collecteur_en_echec_laisse_une_trace(self, monkeypatch):
        from reviews.domain.models import ScraperResult

        class _CollecteurCasse:
            def __init__(self):
                self.since = {}

            def run(self):
                return ScraperResult(
                    scraper_name="googlemaps", started_at=datetime.utcnow(),
                    ended_at=datetime.utcnow(), status="failed",
                    error_message="Google Maps inaccessible",
                )

        monkeypatch.setitem(
            __import__("reviews.collectors", fromlist=["COLLECTORS"]).COLLECTORS,
            "googlemaps", _CollecteurCasse,
        )
        enregistres: list = []
        p = self._pipeline(enregistres)
        p._run_collector("googlemaps", "run-1", dry_run=False)
        assert ("googlemaps", "failed") in enregistres, (
            "l'échec n'est pas journalisé : la cadence le croira jamais exécuté"
        )

    def test_un_collecteur_sans_avis_laisse_aussi_une_trace(self, monkeypatch):
        from reviews.domain.models import ScraperResult

        class _CollecteurVide:
            def __init__(self):
                self.since = {}

            def run(self):
                return ScraperResult(
                    scraper_name="gdelt", started_at=datetime.utcnow(),
                    ended_at=datetime.utcnow(), status="success", reviews=[],
                )

        monkeypatch.setitem(
            __import__("reviews.collectors", fromlist=["COLLECTORS"]).COLLECTORS,
            "gdelt", _CollecteurVide,
        )
        enregistres: list = []
        p = self._pipeline(enregistres)
        p._run_collector("gdelt", "run-1", dry_run=False)
        assert ("gdelt", "success") in enregistres


class TestCadenceParSource:
    def _pipeline(self, derniers):
        from reviews.pipeline.runner import Pipeline
        p = Pipeline.__new__(Pipeline)
        p.settings = Settings()
        p.run_repo = _FauxRunRepo(derniers)
        return p

    def test_google_maps_tourne_moins_souvent_que_les_flux(self):
        s = Settings()
        assert s.scraper_interval_minutes("googlemaps") == 1440
        assert s.scraper_interval_minutes("gdelt") == 720
        # Les sources rapides suivent l'intervalle global.
        assert (s.scraper_interval_minutes("hellopeter")
                == s.scheduler.interval_minutes)

    def test_surcharge_par_variable_d_environnement(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_INTERVAL_GOOGLEMAPS", "2880")
        assert Settings().scraper_interval_minutes("googlemaps") == 2880

    def test_surcharge_invalide_ignoree_sans_planter(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_INTERVAL_GOOGLEMAPS", "deux jours")
        assert Settings().scraper_interval_minutes("googlemaps") == 1440

    def test_un_collecteur_jamais_execute_est_toujours_du(self):
        """Au premier démarrage aucun n'a d'historique : tout doit partir."""
        p = self._pipeline({})
        dus, reportes = p._split_by_schedule(["googlemaps", "hellopeter"])
        assert set(dus) == {"googlemaps", "hellopeter"}
        assert reportes == []

    def test_google_maps_est_reporte_tant_que_son_cycle_court(self):
        maintenant = datetime.now(timezone.utc)
        p = self._pipeline({
            "googlemaps": maintenant - timedelta(hours=6),    # < 24 h
            "hellopeter": maintenant - timedelta(hours=7),    # > 6 h
        })
        dus, reportes = p._split_by_schedule(["googlemaps", "hellopeter"])
        assert dus == ["hellopeter"]
        assert any("googlemaps" in r for r in reportes)

    def test_google_maps_repart_apres_24h(self):
        maintenant = datetime.now(timezone.utc)
        p = self._pipeline({"googlemaps": maintenant - timedelta(hours=25)})
        dus, _ = p._split_by_schedule(["googlemaps"])
        assert dus == ["googlemaps"]

    def test_marge_d_une_minute_sur_le_creneau(self):
        """Le planificateur se réveille à heure fixe et le run précédent a duré.

        Sans marge, un collecteur à 1 440 min manque systématiquement son
        créneau et ne tourne qu'un jour sur deux.
        """
        maintenant = datetime.now(timezone.utc)
        p = self._pipeline({
            "googlemaps": maintenant - timedelta(minutes=1439, seconds=30)
        })
        dus, _ = p._split_by_schedule(["googlemaps"])
        assert dus == ["googlemaps"]

    def test_date_naive_ne_leve_pas(self):
        """La base peut renvoyer une date sans fuseau selon la configuration."""
        p = self._pipeline({"googlemaps": datetime.utcnow() - timedelta(hours=30)})
        dus, _ = p._split_by_schedule(["googlemaps"])
        assert dus == ["googlemaps"]

    def test_panne_de_lecture_relance_tout_plutot_que_rien(self):
        """Plus coûteux, jamais faux : c'est le comportement d'avant la cadence."""
        class _RepoCasse:
            def last_attempt_by_scraper(self):
                raise RuntimeError("base injoignable")

        p = self._pipeline({})
        p.run_repo = _RepoCasse()
        dus, reportes = p._split_by_schedule(["googlemaps", "gdelt"])
        assert dus == ["googlemaps", "gdelt"]
        assert reportes == []


# ---------------------------------------------------------------------------
# Registre de migrations — le retour arrière silencieux
# ---------------------------------------------------------------------------

class _FauxCurseur:
    """Curseur minimal : mémorise le SQL exécuté, sert le registre demandé."""

    def __init__(self, registre, executes):
        self._registre = registre
        self._executes = executes
        self._dernier = ""

    def execute(self, sql, params=None):
        self._dernier = sql
        self._executes.append((sql, params))
        if "INSERT INTO schema_migrations" in sql and params:
            self._registre.add(params[0])

    def fetchall(self):
        if "SELECT filename FROM schema_migrations" in self._dernier:
            return [(nom,) for nom in sorted(self._registre)]
        return []


class _FausseDb:
    def __init__(self, deja_appliquees=()):
        self.registre = set(deja_appliquees)
        self.executes: list[tuple] = []

    def cursor(self, dict_rows=False):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield _FauxCurseur(self.registre, self.executes)

        return _ctx()


class TestRegistreDeMigrations:
    """RÉGRESSION — panne observée en production, sans aucun message d'erreur.

    Les migrations sont EMBARQUÉES dans l'image Docker (`build: .`), pas
    montées. Le conteneur `worker`, construit avant l'ajout des migrations 006 à
    008, ne connaissait que 001 à 005 — et les rejouait à chaque démarrage. Or
    la 005 fait `DROP VIEW v_reviews_enriched CASCADE` puis la recrée SANS
    `source_comparable`, colonne ajoutée par la 007.

    Résultat : un simple redémarrage de conteneur ANNULAIT la migration 007. La
    satisfaction cessait d'être calculée, et le seul symptôme visible était une
    ligne « Détection de pic indisponible » dans les journaux.
    """

    def _db(self, deja=()):
        from reviews.storage.db import Database
        db = Database.__new__(Database)
        faux = _FausseDb(deja)
        db.cursor = faux.cursor
        return db, faux

    def test_une_migration_deja_appliquee_n_est_pas_rejouee(self):
        """C'EST le correctif : rejouer une ancienne migration revient en arrière."""
        from pathlib import Path
        toutes = sorted(p.name for p in Path("migrations").glob("*.sql"))
        db, faux = self._db(deja=toutes)
        db.apply_schema()

        rejouees = [
            sql for sql, _ in faux.executes
            if "CREATE VIEW" in sql or "CREATE TABLE IF NOT EXISTS reviews" in sql
        ]
        assert not rejouees, (
            "une migration déjà appliquée a été rejouée : un DROP VIEW CASCADE "
            "d'une ancienne migration annulerait une plus récente"
        )

    def test_une_migration_nouvelle_est_appliquee_et_enregistree(self):
        from pathlib import Path
        toutes = sorted(p.name for p in Path("migrations").glob("*.sql"))
        db, faux = self._db(deja=toutes[:-1])   # la dernière manque
        db.apply_schema()
        assert toutes[-1] in faux.registre

    def test_registre_vide_applique_tout(self):
        """Base neuve : rien n'est enregistré, tout doit passer."""
        from pathlib import Path
        toutes = sorted(p.name for p in Path("migrations").glob("*.sql"))
        db, faux = self._db(deja=())
        db.apply_schema()
        assert faux.registre == set(toutes)

    def test_image_plus_ancienne_que_la_base_est_signalee(self, caplog):
        """Un conteneur périmé doit le DIRE, pas travailler en silence."""
        import logging
        db, _ = self._db(deja=("001_init_schema.sql", "099_du_futur.sql"))
        with caplog.at_level(logging.WARNING):
            db.apply_schema()
        # getMessage() applique les arguments : `record.message` n'existe qu'après
        # formatage par un handler, et n'est donc pas fiable ici.
        messages = [r.getMessage() for r in caplog.records]
        assert any("plus ancienne que la base" in m for m in messages)
        assert any("099_du_futur.sql" in m for m in messages)


# ---------------------------------------------------------------------------
# Planification : UN JOB PAR SOURCE
# ---------------------------------------------------------------------------


class _FauxScheduler:
    """Enregistre les jobs au lieu de les exécuter.

    `start()` lève KeyboardInterrupt, que `run_scheduler` attrape déjà : la
    fonction rend la main sans jamais bloquer le test.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.jobs = []

    def add_job(self, func, trigger=None, **kw):
        self.jobs.append({"func": func, "trigger": trigger, **kw})

    def start(self):
        raise KeyboardInterrupt


class TestUnJobParSource:
    """Le cycle unique retenait tout derrière la source la plus lente.

    Google Maps demande 405 recherches, une dizaine d'heures mesurées, pour une
    fenêtre de planification de six. Le run n'atteignait jamais sa fin, donc
    jamais `alert_manager.process()` : l'alerting s'est tu pendant trois jours
    sans que rien ne le signale — l'alerte qui aurait prévenu était elle-même
    derrière la source lente.
    """

    CADENCES = {"googlemaps": 1440, "gdelt": 720, "rss_feed": 360}

    def _settings(self, **surcharges):
        from types import SimpleNamespace

        base = dict(run_on_start=True, timezone="UTC", max_concurrent=3,
                    stagger_minutes=2, interval_minutes=360)
        base.update(surcharges)
        return SimpleNamespace(
            scheduler=SimpleNamespace(**base),
            llm=SimpleNamespace(enabled=False, api_key=None,
                                scheduled_batch_limit=1000),
            get_enabled_scrapers=lambda: list(self.CADENCES),
            scraper_interval_minutes=lambda nom: self.CADENCES[nom],
        )

    def _lancer(self, monkeypatch, **surcharges):
        from unittest.mock import Mock

        from reviews import scheduling

        faux = _FauxScheduler()
        pipeline = Mock()
        pipeline.run_repo.reclaim_interrupted_runs.return_value = 0
        pipeline.job_repo.reclaim_stale.return_value = 0

        monkeypatch.setattr(scheduling, "setup_logging", lambda: None)
        monkeypatch.setattr(scheduling, "get_settings",
                            lambda: self._settings(**surcharges))
        monkeypatch.setattr(scheduling, "get_database", lambda: Mock())
        monkeypatch.setattr(scheduling, "build_pipeline", lambda s: pipeline)
        monkeypatch.setattr(scheduling, "BlockingScheduler",
                            lambda **kw: faux.__init__(**kw) or faux)

        scheduling.run_scheduler()
        return faux, pipeline

    def test_un_job_par_collecteur_a_sa_propre_cadence(self, monkeypatch):
        faux, _ = self._lancer(monkeypatch)
        cadences = {j["id"]: j["minutes"] for j in faux.jobs}
        assert cadences == {
            "collect:googlemaps": 1440,
            "collect:gdelt": 720,
            "collect:rss_feed": 360,
        }

    def test_une_source_lente_ne_bloque_pas_les_autres(self, monkeypatch):
        """`max_instances=1` par job : chacun saute SA propre occurrence.

        Google Maps qui déborde de son créneau ne retarde plus personne — c'est
        toute la raison du découpage.
        """
        faux, _ = self._lancer(monkeypatch)
        assert all(j["max_instances"] == 1 for j in faux.jobs)
        assert all(j["coalesce"] is True for j in faux.jobs)

    def test_aucune_collecte_avant_le_demarrage_du_planificateur(self, monkeypatch):
        """RÉGRESSION : `run_on_start` appelait le pipeline AVANT `start()`.

        Le démarrage devenait tributaire de la source la plus lente. Avec Google
        Maps en tête de liste, `scheduler.start()` n'était pas atteint de la
        journée : aucune autre source ne tournait, et le seul run existant était
        celui qui n'aboutissait pas. Le premier passage est désormais porté par
        `next_run_time`, pas par un appel synchrone.
        """
        _, pipeline = self._lancer(monkeypatch, run_on_start=True)
        pipeline.run.assert_not_called()

    def test_les_premiers_passages_sont_echelonnes(self, monkeypatch):
        """Sans décalage, tous les jobs se disputent le plafond à la même seconde."""
        faux, _ = self._lancer(monkeypatch)
        departs = sorted(j["next_run_time"] for j in faux.jobs)
        ecarts = {(b - a).total_seconds() for a, b in zip(departs, departs[1:])}
        assert ecarts == {120.0}          # 2 min entre chaque

    def test_sans_run_on_start_le_premier_passage_attend_la_cadence(self, monkeypatch):
        faux, _ = self._lancer(monkeypatch, run_on_start=False)
        par_id = {j["id"]: j for j in faux.jobs}
        # rss_feed est en 3e position : 360 min de cadence + 2 × 2 min de décalage
        attente = (par_id["collect:rss_feed"]["next_run_time"]
                   - par_id["collect:googlemaps"]["next_run_time"]).total_seconds()
        assert attente == (360 - 1440 + 4) * 60

    def test_les_collectes_simultanees_sont_plafonnees(self, monkeypatch):
        """Huit collecteurs lancés ensemble, dont plusieurs pilotent un navigateur,
        saturent la machine et font échouer par timeout des collectes qui
        passaient."""
        faux, _ = self._lancer(monkeypatch)
        assert "default" in faux.kwargs["executors"]

    def test_le_menage_d_ouverture_se_cale_sur_le_demarrage_du_processus(self, monkeypatch):
        """Le critère est l'heure de démarrage, pas un délai de grâce.

        Tout ce qui était « en cours » avant que ce worker existe est
        nécessairement mort. Le délai de grâce, lui, se calait sur la plus
        longue cadence — douze heures depuis que chaque source a la sienne — et
        laissait huit runs morts affichés « en cours » toute une soirée.
        """
        avant = datetime.now(timezone.utc)
        _, pipeline = self._lancer(monkeypatch)
        apres = datetime.now(timezone.utc)

        appel = pipeline.run_repo.reclaim_interrupted_runs.call_args
        assert "grace_hours" not in appel.kwargs
        assert avant <= appel.kwargs["before"] <= apres

    def test_les_unites_abandonnees_sont_rendues_a_la_file_au_demarrage(self, monkeypatch):
        """Sans ce passage, une unité attendait le prochain tour de SA source.

        Une unité Google Maps abandonnée à 17 h 29 patientait jusqu'au passage
        de 23 h 27 — six heures — alors que son propriétaire était mort. Le bail
        de 120 min ne sert à rien s'il n'est vérifié qu'au passage suivant.
        """
        _, pipeline = self._lancer(monkeypatch)
        appel = pipeline.job_repo.reclaim_stale.call_args
        # Toutes sources confondues : le worker qui vient de mourir en tenait
        # peut-être plusieurs.
        assert appel.kwargs.get("source") is None
        assert appel.kwargs["before"] is not None
