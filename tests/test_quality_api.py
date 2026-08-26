"""
Agent 3 exposé : découverte de sources, orchestration, notification, API.

CE QUE CES TESTS PROTÈGENT
    Le raisonnement de l'agent est couvert par `test_quality_agent.py`. Ce
    module ne teste QUE ce qui l'entoure, et exclusivement les fautes qui ne
    lèveraient aucune erreur :

      - une source candidate proposée sans avoir été sondée : une piste
        présentée comme un fait, et personne pour s'en apercevoir ;
      - une recherche de sources déclenchée sur un collecteur en panne : on
        contourne la panne au lieu de la corriger, et elle devient permanente ;
      - un passage à blanc qui écrit malgré tout : les instantanés que lisent
        les Agents 1 et 2 seraient modifiés par une simple mise au point ;
      - une notification partie sans échappement : l'API Telegram rejette tout
        le message, et l'alerte disparaît sans trace.

AUCUN RÉSEAU, AUCUN MODÈLE, AUCUNE BASE. Les doubles rendent ce qu'on leur donne.
"""

from contextlib import contextmanager

import pytest

from reviews.agents.quality.couverture import CouvertureFiliale, EtatSource
from reviews.agents.quality.decouverte import Candidate, DecouverteSources
from reviews.agents.quality.diagnostic import Cas, diagnostiquer


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Curseur:
    """Curseur qui retient le SQL exécuté et rend des lignes déclarées."""

    def __init__(self, lignes=None):
        self.lignes = lignes or []
        self.sql = None
        self.params = None
        self.executions = []

    def execute(self, sql, params=None):
        self.sql, self.params = sql, list(params or [])
        self.executions.append((sql, self.params))

    def executemany(self, sql, lignes):
        self.sql, self.params = sql, lignes
        self.executions.append((sql, lignes))

    def fetchall(self):
        return self.lignes

    def fetchone(self):
        return self.lignes[0] if self.lignes else None

    @property
    def rowcount(self):
        return len(self.lignes)


class _Base:
    def __init__(self, lignes=None):
        self.curseur = _Curseur(lignes)

    def cursor(self, dict_rows: bool = False):
        @contextmanager
        def _ouvrir():
            yield self.curseur

        return _ouvrir()


class _Canal:
    """Notifieur Telegram factice : retient ce qui aurait été envoyé."""

    def __init__(self, ok=True):
        self.ok = ok
        self.envois = []

    def send_text(self, corps):
        self.envois.append(corps)
        return self.ok


def _couverture(**kw):
    base = dict(
        subsidiary_id=1, subsidiary="Comores Telecom", operator="Comores Telecom",
        country="Comores", iso2="KM",
    )
    base.update(kw)
    return CouvertureFiliale(**base)


def _source(code="google_maps", **kw):
    etat = EtatSource(code=code)
    for k, v in kw.items():
        setattr(etat, k, v)
    return etat


# ===========================================================================
# 1. Découverte — la sonde décide, jamais le catalogue
# ===========================================================================


def test_une_url_absente_est_rejetee_et_jamais_proposee():
    """Proposer une piste qu'on sait morte fait perdre le crédit de toutes les
    autres. Le 404 est mesuré, il n'est pas discutable."""
    d = DecouverteSources(
        sonde=lambda url: {
            "http": 404, "accessibility": "absent", "vocabulaire": False,
        }
    )
    candidate = Candidate(source_name="X", url="https://absent.example")
    d._instruire(candidate)

    assert candidate.status == "REJECTED"
    assert candidate.confidence == 0.0


def test_une_page_qui_repond_sans_vocabulaire_telecom_reste_une_piste():
    """Un code 200 ne prouve rien : beaucoup d'hébergeurs rendent 200 sur une
    page de parking. Sans vocabulaire, on ne crédite pas — mais on ne rejette
    pas non plus, la page pouvant charger son contenu en JavaScript."""
    d = DecouverteSources(
        sonde=lambda url: {
            "http": 200, "accessibility": "http_ouvert", "vocabulaire": False,
        }
    )
    candidate = Candidate(source_name="X", url="https://vide.example")
    d._instruire(candidate)

    assert candidate.status == "CANDIDATE"
    assert candidate.estimated_relevance == "low"
    assert candidate.confidence < 0.5


def test_une_page_qui_repond_et_parle_telecom_est_verifiee():
    """LE SEUL CAS OÙ L'AGENT AFFIRME QUELQUE CHOSE, et il repose sur TROIS
    faits mesurés, pas un : ça répond, ça parle de télécoms, et ça cite
    l'opérateur cherché (sinon une page générique passerait pour vérifiée)."""
    d = DecouverteSources(
        sonde=lambda url: {
            "http": 200, "accessibility": "http_ouvert", "vocabulaire": True,
            "texte": "avis clients sur mtn — réseau mobile",
            "url_finale": "https://ok.example",
        }
    )
    candidate = Candidate(source_name="X", url="https://ok.example", operator="MTN")
    d._instruire(candidate)

    assert candidate.status == "VERIFIED"
    assert candidate.estimated_relevance == "high"
    assert candidate.confidence >= 0.8
    # La preuve doit porter le code HTTP : c'est ce qui rend la proposition
    # reproductible par qui veut la vérifier.
    sonde = [e for e in candidate.evidence if e.get("type") == "sonde"]
    assert sonde and sonde[0]["http"] == 200


def test_une_page_generique_qui_ne_cite_pas_loperateur_nest_pas_verifiee():
    """LE PIÈGE COMPLAINTSBOARD : une recherche mal formée peut rediriger vers
    une page d'accueil générique qui parle de télécoms sans jamais mentionner
    l'opérateur cherché. Sans ce garde-fou, elle serait créditée VERIFIED."""
    d = DecouverteSources(
        sonde=lambda url: {
            "http": 200, "accessibility": "http_ouvert", "vocabulaire": True,
            "texte": "plaintes résolues : hp, etihad airways, t-mobile...",
            "url_finale": "https://www.complaintsboard.com",
        }
    )
    candidate = Candidate(source_name="X", url="https://ok.example", operator="MTN")
    d._instruire(candidate)

    assert candidate.status == "CANDIDATE"
    assert candidate.confidence < 0.5


def test_un_blocage_est_une_information_et_non_un_echec():
    """C'est ce qui a fait écarter Techpoint Africa et MyBroadband des flux de
    presse : la source existe, elle exigera un navigateur."""
    d = DecouverteSources(
        sonde=lambda url: {
            "http": 403, "accessibility": "bloque", "vocabulaire": False,
        }
    )
    candidate = Candidate(source_name="MyBroadband", url="https://mybroadband.example")
    d._instruire(candidate)

    assert candidate.status == "CANDIDATE"
    assert candidate.connector_required is True
    assert "navigateur" in candidate.apport


def test_sans_sonde_une_candidate_n_est_jamais_presentee_comme_verifiee():
    """La différence entre une piste et un fait doit rester visible, y compris
    quand l'environnement n'a pas d'accès sortant."""
    d = DecouverteSources(probe_enabled=False)
    candidate = Candidate(source_name="X", url="https://x.example")
    d._instruire(candidate)

    assert candidate.status == "CANDIDATE"
    assert candidate.accessibility == "inconnu"
    assert candidate.confidence <= 0.3


def test_une_sonde_qui_leve_ne_fait_pas_tomber_le_passage():
    def _explose(url):
        raise RuntimeError("réseau coupé")

    d = DecouverteSources(sonde=_explose)
    candidate = Candidate(source_name="X", url="https://x.example")
    d._instruire(candidate)  # ne doit pas lever

    assert candidate.status == "CANDIDATE"


def test_le_format_de_sortie_respecte_le_contrat_demande():
    """Les clés du §7 de l'énoncé sont un contrat : le dashboard et le rapport
    s'appuient dessus."""
    attendu = {
        "source_name", "url", "country", "operator", "subsidiary",
        "estimated_relevance", "reason", "source_type", "accessibility",
        "evidence",
    }
    assert attendu <= set(Candidate(source_name="X", url="u").as_dict())


# ===========================================================================
# 2. Le garde-fou d'ordre — on ne cherche pas ailleurs pour masquer une panne
# ===========================================================================


@pytest.mark.parametrize(
    "cas_attendu, enrichissable",
    [
        (Cas.COLLECTEUR_EN_ECHEC, False),
        (Cas.JAMAIS_TENTE, False),
        (Cas.MAPPING_SUSPECT, False),
        (Cas.AUCUNE_SOURCE_EXPLOITABLE, True),
    ],
)
def test_seuls_les_cas_instruits_autorisent_la_recherche_de_sources(
    cas_attendu, enrichissable
):
    """LA RÈGLE DU §5, vérifiée cas par cas. Chercher une source externe pour
    contourner un collecteur en panne rendrait la panne permanente."""
    fabriques = {
        Cas.COLLECTEUR_EN_ECHEC: (
            {"google_maps": _source(attendue=True, unites_jamais_reussies=3)},
            None,
        ),
        Cas.JAMAIS_TENTE: (
            {"google_maps": _source(attendue=True, unites_attente=4)},
            None,
        ),
        Cas.MAPPING_SUSPECT: (
            {"google_maps": _source(attendue=True, unites_deja_reussies=2)},
            [{"kind": "alias_manquant"}],
        ),
        Cas.AUCUNE_SOURCE_EXPLOITABLE: (
            {"google_maps": _source(attendue=True, unites_deja_reussies=6)},
            None,
        ),
    }
    sources, indices = fabriques[cas_attendu]
    d = diagnostiquer(_couverture(sources=sources), indices_mapping=indices)

    assert d.cas is cas_attendu
    assert d.enrichissable is enrichissable


# ===========================================================================
# 3. Orchestration — le passage à blanc n'écrit rien
# ===========================================================================


def test_le_passage_a_blanc_n_ecrit_ni_ne_notifie(monkeypatch):
    """UN PASSAGE RÉEL ÉCRIT LES INSTANTANÉS QUE LISENT LES AGENTS 1 ET 2. Une
    simple mise au point ne doit pas pouvoir les modifier, ni réveiller le
    groupe Telegram — c'est la raison d'être du mode à blanc."""
    from reviews.agents.quality import guardian as module
    from reviews.config import get_settings

    canal = _Canal()
    agent = module.QualityGuardian(_Base(), get_settings(), notifier=canal)

    ecritures = []
    for nom in (
        "enregistrer_constats", "enregistrer_candidates",
        "enregistrer_scores", "enregistrer_affirmations",
    ):
        monkeypatch.setattr(
            agent.depot, nom,
            lambda *a, _n=nom, **k: ecritures.append(_n) or 0,
        )
    monkeypatch.setattr(
        module.MoniteurCouverture, "analyser",
        lambda self: [
            _couverture(
                sources={"google_maps": _source(attendue=True, unites_deja_reussies=6)}
            )
        ],
    )
    monkeypatch.setattr(module.DetecteurMapping, "analyser", lambda self: [])
    monkeypatch.setattr(module.ControlesQualite, "analyser", lambda self: [])
    monkeypatch.setattr(
        module.ControlesFraicheur, "analyser", lambda self, c: []
    )
    monkeypatch.setattr(module, "completude_par_filiale", lambda db: {})

    passage = agent.run(dry_run=True)

    assert ecritures == [], f"le mode à blanc a écrit : {ecritures}"
    assert canal.envois == []
    assert passage.filiales == 1


def test_une_couverture_illisible_rend_un_passage_muet_et_non_une_exception(
    monkeypatch,
):
    """Un agent muet vaut mieux qu'un crash : APScheduler désactive un job qui
    lève trop souvent, et un gardien désactivé silencieusement laisserait les
    deux autres agents raisonner sans contrôle."""
    from reviews.agents.quality import guardian as module
    from reviews.config import get_settings

    def _explose(self):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(module.MoniteurCouverture, "analyser", _explose)
    passage = module.QualityGuardian(_Base(), get_settings()).run()

    assert passage.raison_silence is not None
    assert "couverture" in passage.raison_silence


# ===========================================================================
# 4. Notification — échappement et retenue
# ===========================================================================


def test_la_notification_echappe_les_caracteres_interdits_par_telegram():
    """Sans échappement, un nom contenant « & » ou « < » fait rejeter TOUT
    l'envoi par l'API — pas seulement le caractère fautif. L'alerte disparaît
    alors sans trace, exactement le mode de panne qui a fait taire l'alerting
    pendant trois jours."""
    from reviews.agents.quality.guardian import QualityGuardian
    from reviews.agents.quality.score import ScoreQualite
    from reviews.config import get_settings

    canal = _Canal()
    agent = QualityGuardian(_Base(), get_settings(), notifier=canal)
    couverture = _couverture(subsidiary="Orange <Mali> & Co")
    score = ScoreQualite(
        subsidiary_id=1, subsidiary=couverture.subsidiary,
        global_score=0.2, statut="UNTRUSTED",
    )

    assert agent._envoyer([(couverture, score, "texte")]) is True
    (corps,) = canal.envois
    assert "&lt;Mali&gt;" in corps and "&amp;" in corps
    # La structure HTML volontaire, elle, doit rester intacte.
    assert "<b>" in corps


def test_sans_canal_configure_l_agent_journalise_sans_echouer():
    from reviews.agents.quality.guardian import QualityGuardian
    from reviews.agents.quality.score import ScoreQualite
    from reviews.config import get_settings

    agent = QualityGuardian(_Base(), get_settings(), notifier=None)
    score = ScoreQualite(
        subsidiary_id=1, subsidiary="X", global_score=0.2, statut="UNTRUSTED"
    )
    assert agent._envoyer([(_couverture(), score, "t")]) is False


# ===========================================================================
# 5. Persistance — l'idempotence protège le travail humain
# ===========================================================================


def test_un_constat_deja_instruit_ne_repasse_pas_en_attente():
    """SUR CONFLIT, ON MET À JOUR LA RAISON, JAMAIS LE STATUT. Sans cette
    règle, un constat instruit et passé à ACCEPTED redeviendrait FLAGGED au
    passage suivant : la file d'instruction ne se viderait jamais et l'écran
    redemanderait éternellement le même arbitrage."""
    from reviews.storage.quality_repository import QualityRepository

    db = _Base()
    QualityRepository(db).enregistrer_constats(
        [{"kind": "doublon_semantique", "scope": "review",
          "subject_key": "r1", "reason": "test"}]
    )
    sql = db.curseur.sql

    assert "ON CONFLICT (kind, scope, subject_key) DO UPDATE" in sql
    assert "reason" in sql
    assert "status" not in sql.split("DO UPDATE")[1]


def test_une_source_integree_n_est_jamais_redegradee_par_la_sonde():
    """INTEGRATED est un état posé par un humain. Une source intégrée qui
    répond mal est un incident de collecte, pas une candidate à re-proposer."""
    from reviews.storage.quality_repository import QualityRepository

    db = _Base()
    QualityRepository(db).enregistrer_candidates(
        [{"source_name": "X", "url": "u", "status": "VERIFIED"}]
    )
    sql = db.curseur.sql

    assert "INTEGRATED" in sql
    assert "CASE WHEN source_candidates.status = 'INTEGRATED'" in sql


def test_le_contrat_de_trust_porte_les_trois_scores_attendus():
    """Ce sont les clés que lisent les Agents 1 et 2 : les changer casse
    silencieusement leur retenue."""
    from reviews.storage.quality_repository import QualityRepository

    db = _Base([
        {
            "subsidiary_id": 3, "subsidiary": "MTN Ghana", "operator": "MTN",
            "country": "Ghana", "iso2": "GH", "coverage": 0.91,
            "freshness": 0.8, "reliability": 0.82, "global_score": 0.87,
            "status": "TRUSTED", "diagnostic": "couvert", "computed_at": None,
        }
    ])
    (ligne,) = QualityRepository(db).trust()

    assert ligne["coverage_score"] == 0.91
    assert ligne["quality_score"] == 0.87
    assert ligne["reliability_score"] == 0.82
    assert ligne["overall_confidence"] == 0.87
    assert ligne["status"] == "TRUSTED"


# ===========================================================================
# 6. API — le défaut d'une route déclenchable par accident
# ===========================================================================


def test_la_route_de_passage_est_a_blanc_par_defaut():
    """Une route HTTP est appelable par erreur — rechargement, outil de test,
    lien partagé. Le défaut doit être l'action qui ne fait rien
    d'irréversible : un passage réel notifie toute l'équipe."""
    import inspect

    from reviews.api.routes.quality import run

    defaut = inspect.signature(run).parameters["dry_run"].default
    assert defaut.default is True


def test_le_compteur_de_non_corrobores_dit_la_meme_chose_que_l_agent():
    """DEUX DÉFINITIONS SOUS UN MÊME NOM, ET C'EST L'ÉCRAN QUI PERD.

    `Affirmation.exploitable` n'autorise que CONFIRMED et CORROBORATED : c'est
    ce qui commande la retenue des Agents 1 et 2. Un compteur d'écran qui ne
    retiendrait que UNCONFIRMED afficherait 0 sur un corpus portant quatre
    affirmations PLAUSIBLE non relayables — « rien à surveiller » précisément
    là où il faut regarder. Mesuré sur le corpus réel du 17 août.
    """
    from reviews.storage.quality_repository import QualityRepository

    # Le double rend la MÊME ligne à toutes les requêtes du résumé : elle porte
    # donc les clés de chacune, sans quoi `resume()` échouerait avant d'arriver
    # à la requête que ce test observe.
    db = _Base([{"n": 0, "status": "TRUSTED", "filiales": 0, "moyenne": None}])
    QualityRepository(db).resume()
    sql_claims = [
        sql for sql, _ in db.curseur.executions if "data_claims" in sql
    ]
    assert sql_claims, "aucune requête sur data_claims"
    assert "NOT IN ('CONFIRMED', 'CORROBORATED')" in sql_claims[0]


def test_les_lectures_de_qualite_sont_en_get():
    """Contrairement à `/insights/*`, aucune de ces routes n'appelle de modèle :
    ce sont des lectures de tables, idempotentes et sans coût. Seul `/run` est
    un acte, donc en POST."""
    from reviews.api.routes.quality import router

    methodes = {
        r.path: r.methods for r in router.routes if hasattr(r, "methods")
    }
    assert methodes["/quality/trust"] == {"GET"}
    assert methodes["/quality/overview"] == {"GET"}
    assert methodes["/quality/run"] == {"POST"}
