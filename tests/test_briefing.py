"""Tests du résumé de période et du diagnostic de cause racine.

Aucun appel réseau, aucune base. L'essentiel porte sur `_lectures()` : c'est la
fonction qui empêche le modèle de conclure une panne devant n'importe quelle
concentration, et elle est entièrement déterministe — donc entièrement testable.

Le reste couvre les cas où la réponse doit être un REFUS chiffré plutôt qu'une
phrase : trop peu d'avis, analyse sémantique pas encore passée, quota épuisé.
Dans tous ces cas le dashboard doit recevoir les chiffres, jamais une erreur.
"""

import json
from datetime import datetime, timezone

import pytest

from reviews.llm.briefing import (
    DIAGNOSIS,
    DIGEST,
    MIN_AVIS,
    REFRESH_HOURS,
    BriefingService,
    _diagnosis_fields,
    _first_text,
    _lectures,
    _time_bucket,
)
from reviews.llm.cache import CachedText, scope_hash
from reviews.storage.filters import StatsFilter


# ---------------------------------------------------------------------------
# Fabriques de signaux
# ---------------------------------------------------------------------------


def _conc(principal, part, groupes=1):
    return {"principal": principal, "part": part, "groupes": groupes, "top": []}


def _signaux(**surcharges):
    base = {
        "aspect": _conc("coupures_pannes", 45.0, 6),
        "geographique": _conc("Nigeria", 45.0, 3),
        "filiale": _conc("MTN Nigeria", 40.0, 4),
        "source": _conc("google_play", 40.0, 4),
        "temporelle": _conc("2026-08-05", 20.0, 20),
        "anteriorite": {
            "motif": "coupures_pannes",
            "avis_periode_precedente": 12,
            "total_periode_precedente": 200,
            "nouveau": False,
        },
    }
    base.update(surcharges)
    return base


def _texte(lectures):
    return " || ".join(lectures)


# ---------------------------------------------------------------------------
# Les garde-fous : ce que les volumes IMPOSENT au modèle
# ---------------------------------------------------------------------------


class TestLectures:
    def test_motif_diffus_interdit_de_designer_une_cause(self):
        """LE garde-fou central.

        Devant un mécontentement réparti sur dix-sept motifs, un modèle laissé
        libre en choisit un et le présente comme LA cause — c'est la réponse la
        plus utile en apparence, et la plus fausse.
        """
        lectures = _lectures(_signaux(aspect=_conc("service_client", 15.9, 17)),
                             {"avis": 2000})
        texte = _texte(lectures)
        assert "DIFFUS" in texte
        assert "PAS désigner une cause unique" in texte

    def test_motif_dominant_recentre_le_diagnostic(self):
        lectures = _lectures(_signaux(aspect=_conc("coupures_pannes", 72.0, 4)),
                             {"avis": 500})
        assert "DOMINE" in _texte(lectures)

    def test_source_unique_declenche_l_avertissement_d_artefact(self):
        """Une flambée visible sur UNE plateforme est d'abord suspecte.

        Sans ce signal, un backfill HelloPeter ou l'activation d'une nouvelle
        source se lit comme une dégradation du réseau.
        """
        lectures = _lectures(_signaux(source=_conc("hellopeter", 88.0, 3)),
                             {"avis": 500})
        texte = _texte(lectures)
        assert "une seule source" in texte
        assert "hellopeter" in texte
        assert "biais" in texte

    def test_source_repartie_ne_declenche_pas_l_avertissement(self):
        """Le garde-fou doit rester silencieux quand les sources se recoupent."""
        lectures = _lectures(_signaux(source=_conc("google_play", 35.0, 5)),
                             {"avis": 500})
        assert "biais de cette" not in _texte(lectures)

    def test_pic_sur_un_jour_et_motif_nouveau_autorise_l_hypothese_de_panne(self):
        lectures = _lectures(
            _signaux(
                temporelle=_conc("2026-08-05", 84.0, 7),
                anteriorite={"motif": "coupures_pannes",
                             "avis_periode_precedente": 0,
                             "total_periode_precedente": 150, "nouveau": True},
            ),
            {"avis": 500},
        )
        texte = _texte(lectures)
        assert "INCIDENT PONCTUEL" in texte
        assert "panne" in texte

    def test_motif_deja_present_interdit_de_parler_de_panne_soudaine(self):
        """RÉGRESSION À PRÉVENIR — le contresens le plus coûteux.

        Un motif chronique présenté comme une panne récente envoie une équipe
        chercher un incident qui n'existe pas.
        """
        lectures = _lectures(_signaux(), {"avis": 500})
        texte = _texte(lectures)
        assert "chronique" in texte
        assert "ne parle ni de panne soudaine" in texte

    def test_un_meme_signal_ne_peut_pas_dire_incident_ET_chronique(self):
        """Les deux lectures s'excluent : elles orientent vers l'inverse."""
        incident = _texte(_lectures(
            _signaux(temporelle=_conc("2026-08-05", 90.0, 3),
                     anteriorite={"motif": "x", "avis_periode_precedente": 0,
                                  "total_periode_precedente": 100, "nouveau": True}),
            {"avis": 500},
        ))
        assert "INCIDENT PONCTUEL" in incident
        assert "chronique" not in incident

    def test_phenomene_localise_autorise_une_action_ciblee(self):
        lectures = _lectures(_signaux(geographique=_conc("Nigeria", 91.0, 4)),
                             {"avis": 500})
        assert "LOCALISÉ" in _texte(lectures)

    def test_phenomene_reparti_ecarte_la_cause_locale(self):
        lectures = _lectures(_signaux(geographique=_conc("Nigeria", 12.0, 30)),
                             {"avis": 500})
        texte = _texte(lectures)
        assert "RÉPARTI" in texte
        assert "cause locale est peu probable" in texte

    def test_un_seul_pays_ne_declenche_aucune_lecture_geographique(self):
        """Filtrer sur un pays rend la concentration à 100 % : elle ne dit rien."""
        lectures = _lectures(_signaux(geographique=_conc("Afrique du Sud", 100.0, 1)),
                             {"avis": 500})
        assert "LOCALISÉ" not in _texte(lectures)

    def test_faible_volume_impose_la_prudence(self):
        lectures = _lectures(_signaux(), {"avis": 60})
        assert "reste prudent" in _texte(lectures)

    def test_aucun_signal_franc_donne_quand_meme_une_consigne(self):
        """Une liste vide laisserait le modèle sans garde-fou du tout."""
        neutre = {
            "aspect": _conc("x", 45.0, 5),
            "geographique": _conc("y", 45.0, 3),
            "filiale": _conc("z", 40.0, 3),
            "source": _conc("s", 40.0, 4),
            "temporelle": _conc("j", 20.0, 20),
            "anteriorite": {},
        }
        lectures = _lectures(neutre, {"avis": 500})
        assert lectures
        assert "piste" in _texte(lectures)


# ---------------------------------------------------------------------------
# Cache : borner le coût sans figer le contenu
# ---------------------------------------------------------------------------


class TestCache:
    def test_la_tranche_est_stable_dans_la_fenetre(self):
        a = _time_bucket(datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc))
        b = _time_bucket(datetime(2026, 8, 7, 6 + REFRESH_HOURS - 1, 59,
                                  tzinfo=timezone.utc))
        assert a == b

    def test_la_tranche_change_a_la_fenetre_suivante(self):
        """Sans cela, un résumé « des dernières 24 h » vieillirait d'une journée."""
        a = _time_bucket(datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc))
        b = _time_bucket(datetime(2026, 8, 7, REFRESH_HOURS, 0, tzinfo=timezone.utc))
        assert a != b

    def test_l_empreinte_ignore_l_ordre_des_cles(self):
        """Le même écran rouvert ne doit pas repayer un appel."""
        assert scope_hash({"a": 1, "b": [2, 3]}) == scope_hash({"b": [2, 3], "a": 1})

    def test_deux_perimetres_differents_ne_partagent_pas_une_phrase(self):
        """La pire erreur possible ici : un texte juste sous d'autres chiffres."""
        assert scope_hash({"scope": {"countries": ["NG"]}}) != scope_hash(
            {"scope": {"countries": ["GH"]}}
        )


# ---------------------------------------------------------------------------
# Tolérance au format de réponse
# ---------------------------------------------------------------------------


class TestReponseDuModele:
    def test_recommandations_en_chaine_sont_acceptees(self):
        """Les petits modèles rendent parfois une chaîne au lieu d'une liste.

        Rejeter le lot pour cette seule raison perdrait un appel de quota.
        """
        champs = _diagnosis_fields(
            {"cause_probable": "c", "recommandations": "Vérifier les antennes"}
        )
        assert champs["recommandations"] == ["Vérifier les antennes"]

    def test_les_recommandations_sont_plafonnees_a_trois(self):
        """Un modèle bavard ne doit pas imposer dix actions au dashboard."""
        champs = _diagnosis_fields(
            {"cause_probable": "c", "recommandations": [f"a{i}" for i in range(9)]}
        )
        assert len(champs["recommandations"]) == 3

    def test_recommandations_absentes_donnent_une_liste_vide(self):
        assert _diagnosis_fields({"cause_probable": "c"})["recommandations"] == []

    def test_reponse_non_dict_ne_leve_pas(self):
        assert _diagnosis_fields("du texte") is None

    def test_texte_recupere_quel_que_soit_le_nom_du_champ(self):
        assert _first_text({"synthese": "a"}, ("synthese", "summary")) == "a"
        assert _first_text({"summary": "b"}, ("synthese", "summary")) == "b"
        assert _first_text("brut", ("synthese",)) == "brut"
        assert _first_text({"synthese": "   "}, ("synthese",)) is None


# ---------------------------------------------------------------------------
# Service : les refus doivent rester chiffrés
# ---------------------------------------------------------------------------


class _FauxClient:
    def __init__(self, reason=None, answer=None):
        self._reason = reason
        self._answer = answer or {}
        self.appels = 0

        class _Cfg:
            def effective_synthesis_model(self):
                return "modele-test"

        self.cfg = _Cfg()

    def unavailable_reason(self):
        return self._reason

    def complete_json(self, system, user, max_tokens):
        self.appels += 1
        return self._answer


class _FauxBriefingRepo:
    def __init__(self, avis=500, aspect="coupures_pannes"):
        self._avis = avis
        self._aspect = aspect

    def volumes(self, f):
        return {"avis": self._avis, "negatifs": self._avis // 2,
                "filiales": 3, "pays": 2, "par_source": []}

    def pain_points(self, f, limit=8):
        return [{"iso2": "NG", "country": "Nigeria",
                 "aspect": "coupures_pannes", "avis": 40, "nb_filiales": 2}]

    def signals(self, f):
        return _signaux(aspect=_conc(self._aspect, 70.0, 4)) if self._aspect else \
            _signaux(aspect=_conc(None, None, 0))

    def verbatims_for_aspect(self, f, aspect, limit=6):
        return ["le reseau tombe tous les soirs"]


class _FauxStatsRepo:
    def semantic_coverage(self, f):
        return {"total": 100, "analyses": 95, "part": 95.0, "version": 1}


class _FauxCache:
    def __init__(self, entree=None):
        self.entree = entree
        self.ecritures = 0

    def read(self, kind, digest):
        return self.entree

    def write(self, kind, digest, scope, entry):
        self.ecritures += 1


def _service(client, repo=None, cache=None):
    service = BriefingService(
        db=None, briefing_repo=repo or _FauxBriefingRepo(),
        stats_repo=_FauxStatsRepo(), client=client,
    )
    service.cache = cache or _FauxCache()
    return service


class TestService:
    F = StatsFilter(days=30)

    def test_trop_peu_d_avis_refuse_sans_appeler_le_modele(self):
        """Sous le seuil, « la principale plainte » désignerait deux personnes."""
        client = _FauxClient()
        service = _service(client, repo=_FauxBriefingRepo(avis=MIN_AVIS - 1))
        r = service.digest(self.F)
        assert r["available"] is False
        assert client.appels == 0
        assert r["payload"]["volumes"]["avis"] == MIN_AVIS - 1

    def test_diagnostic_sans_aspect_analyse_refuse_proprement(self):
        """L'analyse sémantique n'est pas passée : il n'y a rien à diagnostiquer."""
        client = _FauxClient()
        service = _service(client, repo=_FauxBriefingRepo(aspect=None))
        r = service.diagnose(self.F)
        assert r["available"] is False
        assert "sémantique" in r["reason"]
        assert client.appels == 0

    def test_quota_epuise_rend_les_chiffres_malgre_tout(self):
        """L'écran doit rester utilisable sans modèle : seule la phrase manque."""
        client = _FauxClient(reason="Budget quotidien atteint.")
        r = _service(client).digest(self.F)
        assert r["available"] is False
        assert r["reason"] == "Budget quotidien atteint."
        assert r["payload"]["motifs_par_pays"], "les chiffres doivent être là"
        assert client.appels == 0

    def test_le_cache_evite_un_appel(self):
        client = _FauxClient(answer={"synthese": "neuf"})
        cache = _FauxCache(
            CachedText(text="déjà écrit", kind=DIGEST, cached=True)
        )
        r = _service(client, cache=cache).digest(self.F)
        assert r["text"] == "déjà écrit"
        assert r["cached"] is True
        assert client.appels == 0

    def test_refresh_ignore_le_cache(self):
        client = _FauxClient(answer={"synthese": "neuf", "fiabilite": "haute"})
        cache = _FauxCache(CachedText(text="vieux", kind=DIGEST, cached=True))
        r = _service(client, cache=cache).digest(self.F, use_cache=False)
        assert r["text"] == "neuf"
        assert client.appels == 1

    def test_le_diagnostic_expose_sa_reponse_structuree(self):
        """Le dashboard doit pouvoir faire deux widgets sans redécouper une phrase."""
        client = _FauxClient(answer={
            "cause_probable": "Panne régionale probable",
            "elements_a_verifier": "Journaux réseau",
            "recommandations": ["Vérifier les antennes", "Communiquer"],
            "fiabilite": "moyenne",
        })
        r = _service(client).diagnose(self.F)
        assert r["available"] is True
        assert r["reliability"] == "moyenne"
        reponse = r["payload"]["_reponse"]
        assert reponse["cause_probable"] == "Panne régionale probable"
        assert len(reponse["recommandations"]) == 2

    def test_le_resume_annonce_que_sa_liste_est_tronquee(self):
        """RÉGRESSION MESURÉE — le modèle a écrit « exclusivement ».

        Sur sept jours, les huit premiers motifs étaient tous sud-africains
        alors que neuf pays étaient concernés. Présentée comme complète, la
        liste tronquée a produit « l'insatisfaction se concentre EXCLUSIVEMENT
        sur l'Afrique du Sud » : une conclusion fausse, tirée honnêtement d'un
        extrait. Une liste tronquée doit annoncer qu'elle l'est.
        """
        class _RepoLarge(_FauxBriefingRepo):
            def pain_points(self, f, limit=8):
                return [
                    {"iso2": "ZA", "country": "Afrique du Sud",
                     "aspect": "service_client", "avis": 50 - i, "nb_filiales": 2}
                    for i in range(8)
                ] + [
                    {"iso2": "EG", "country": "Égypte",
                     "aspect": "app_bugs", "avis": 5, "nb_filiales": 1}
                ]

        contexte = _service(_FauxClient(), repo=_RepoLarge())._digest_context(self.F)
        assert contexte["liste_tronquee"] is True
        assert contexte["pays_concernes_au_total"] == 2
        assert len(contexte["motifs_par_pays"]) == 8

    def test_une_liste_complete_n_est_pas_annoncee_tronquee(self):
        """Le signal doit rester juste dans les deux sens."""
        contexte = _service(_FauxClient())._digest_context(self.F)
        assert contexte["liste_tronquee"] is False

    def test_le_contexte_transmet_les_contraintes_au_modele(self):
        """Sans elles, le prompt n'a plus aucun garde-fou déterministe."""
        client = _FauxClient(answer={"cause_probable": "c"})
        service = _service(client)
        contexte = service._diagnosis_context(self.F)
        assert contexte["contraintes"], "aucune contrainte calculée"

    def test_reponse_illisible_refuse_au_lieu_de_planter(self):
        client = _FauxClient(answer={"rien": "du tout"})
        r = _service(client).digest(self.F)
        assert r["available"] is False
        assert "exploitable" in r["reason"]

    def test_une_reponse_produite_est_mise_en_cache(self):
        client = _FauxClient(answer={"synthese": "texte", "fiabilite": "haute"})
        cache = _FauxCache()
        _service(client, cache=cache).digest(self.F)
        assert cache.ecritures == 1

    @pytest.mark.parametrize("kind", [DIGEST, DIAGNOSIS])
    def test_les_deux_types_sont_declares(self, kind):
        from reviews.llm.briefing import BRIEFING_KINDS
        assert kind in BRIEFING_KINDS


def test_aucun_code_technique_d_aspect_n_atteint_le_modele():
    """RÉGRESSION VÉCUE : un diagnostic est parti en écrivant « le motif
    'app_bugs' constitue un problème chronique ».

    `motif_dominant` était traduit, mais `signaux` transportait le code brut à
    côté — et c'est celui-là que le modèle a repris. Un identifiant de base de
    données sous les yeux d'un responsable métier laisse penser que le reste
    est tout aussi peu relu.
    """
    from reviews.domain.aspects import label as aspect_label
    from reviews.llm.briefing import BriefingService

    class _Briefing:
        def volumes(self, f):
            return {"avis": 500, "negatifs": 300}

        def signals(self, f):
            return {
                "aspect": {"principal": "app_bugs", "part": 62.0, "groupes": 4},
                "pays": {}, "filiale": {}, "source": {}, "temps": {},
            }

        def verbatims_for_aspect(self, f, aspect, limit):
            # Le code BRUT reste nécessaire ici : c'est une clé de base.
            assert aspect == "app_bugs"
            return []

    class _Stats:
        def semantic_coverage(self, f):
            return {"total": 500, "analyses": 500, "part": 100.0}

    svc = BriefingService.__new__(BriefingService)
    svc.briefing, svc.stats = _Briefing(), _Stats()
    ctx = svc._diagnosis_context(StatsFilter(days=90))

    blob = json.dumps(ctx, ensure_ascii=False, default=str)
    assert "app_bugs" not in blob, "code technique transmis au modèle"
    assert aspect_label("app_bugs") in blob
