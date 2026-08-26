"""
Agent 3 intégré : garde-fou des Agents 1 et 2, orphelins, profil de modèle.

CE QUE CES TESTS PROTÈGENT, ET POURQUOI CE N'EST PAS ÉVIDENT
    Toutes les fautes couvertes ici sont SILENCIEUSES. Aucune ne lève, aucune
    ne remplit un journal d'erreurs :

      - un garde-fou qui bloque au lieu de laisser passer fait TAIRE les deux
        autres agents, et un agent muet ne se remarque pas ;
      - un garde-fou qui laisse passer un UNTRUSTED fait publier une
        recommandation d'action sur un taux calculé sur quatre avis ;
      - une réattribution d'orphelin appliquée sans trace rend l'opération
        irréversible, et personne ne s'en aperçoit avant d'en avoir besoin ;
      - un repli implicite sur Gemini fait consommer par l'Agent 3 le quota
        qu'il devait épargner.

AUCUN APPEL DE MODÈLE, AUCUN RÉSEAU, AUCUNE BASE. Les doubles rendent ce qu'on
leur donne — les tests ne doivent jamais consommer le quota d'Ollama Cloud.
"""

from contextlib import contextmanager

import pytest

from reviews.agents.quality.garde import (
    INDETERMINE,
    GardeQualite,
    NEUTRE,
    Verdict,
    construire_garde,
)
from reviews.agents.quality.orphelins import (
    AUTO_SAFE,
    HIGH_CONFIDENCE,
    REVIEW_REQUIRED,
    UNRESOLVED,
    Proposition,
    ResolveurOrphelins,
    _IndexFiliales,
    normaliser,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Curseur:
    def __init__(self, lignes=None):
        self.lignes = list(lignes or [])
        self.sql = None
        self.params = None
        self.executions = []

    def execute(self, sql, params=None):
        self.sql, self.params = sql, list(params or [])
        self.executions.append((sql, self.params))

    def executemany(self, sql, lignes):
        self.sql, self.params = sql, list(lignes)
        self.executions.append((sql, list(lignes)))

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


class _BaseQuiLeve:
    """Base indisponible : toute ouverture de curseur échoue."""

    def cursor(self, dict_rows: bool = False):
        raise RuntimeError("base indisponible")


def _index(*filiales):
    """Index de rapprochement depuis (id, nom, [alias…])."""
    return _IndexFiliales(
        [
            {"subsidiary_id": sid, "name": nom, "aliases": list(alias)}
            for sid, nom, *reste in filiales
            for alias in [reste[0] if reste else []]
        ]
    )


# ===========================================================================
# 1. Le garde-fou — la dissymétrie qui le rend sûr
# ===========================================================================


def test_le_garde_desactive_laisse_tout_passer():
    """`ENABLE_QUALITY_GATE=false` doit restituer EXACTEMENT le comportement
    d'avant l'intégration : aucune mention, rien de bloqué."""
    garde = GardeQualite(_Base(), enabled=False)
    v = garde.verdict(12)

    assert v.statut == INDETERMINE
    assert v.fiable is True
    assert v.mention is None


def test_une_base_indisponible_laisse_passer_au_lieu_de_bloquer():
    """LE CHOIX LE PLUS IMPORTANT DU MODULE, et il est contre-intuitif.

    Un garde-fou qui se ferme quand il ne peut pas lire son propre score ferait
    taire les Agents 1 et 2 — c'est-à-dire provoquerait une panne plus grave que
    celle qu'il prévient. Un agent muet ne se remarque pas ; c'est exactement le
    mode de panne qui a fait taire l'alerting trois jours durant. Une réserve
    manquante, elle, se voit.
    """
    garde = GardeQualite(_BaseQuiLeve(), enabled=True)
    v = garde.verdict(12)

    assert v.statut == INDETERMINE
    assert v.fiable is True


def test_une_filiale_jamais_evaluee_laisse_passer():
    """Sur une base neuve, l'agent n'a pas encore tourné. Bloquer reviendrait à
    exiger l'Agent 3 pour que les deux autres fonctionnent."""
    garde = GardeQualite(_Base([]), enabled=True)
    assert garde.verdict(12).statut == INDETERMINE


@pytest.mark.parametrize(
    "statut, fiable, a_une_mention",
    [
        ("TRUSTED", True, False),
        ("ACCEPTABLE", True, False),
        ("DEGRADED", True, True),
        ("UNTRUSTED", False, True),
    ],
)
def test_chaque_statut_produit_le_bon_verdict(statut, fiable, a_une_mention):
    """`DEGRADED` reste FIABLE et porte une réserve : renoncer dès la
    dégradation viderait le briefing de son intérêt sur les filiales les moins
    couvertes — justement celles dont personne ne parle jamais."""
    garde = GardeQualite(
        _Base([{"global_score": 0.4, "status": statut, "diagnostic": "sous_couvert"}]),
        enabled=True,
    )
    v = garde.verdict(12)

    assert v.statut == statut
    assert v.fiable is fiable
    assert bool(v.mention) is a_une_mention


def test_le_verdict_est_mis_en_cache_pour_un_passage():
    """Les agents interrogent la même filiale plusieurs fois par passage
    (candidat, rédaction, envoi). Sans cache, autant de requêtes."""
    base = _Base([{"global_score": 0.9, "status": "TRUSTED", "diagnostic": None}])
    garde = GardeQualite(base, enabled=True)
    garde.verdict(12)
    garde.verdict(12)
    garde.verdict(12)

    assert len(base.curseur.executions) == 1


def test_un_identifiant_illisible_ne_leve_pas():
    garde = GardeQualite(_Base(), enabled=True)
    assert garde.verdict("pas-un-entier").statut == INDETERMINE
    assert garde.verdict(None) is NEUTRE


def test_le_garde_se_construit_meme_sur_une_configuration_incomplete():
    """Un double de test ou une configuration antérieure ne doit pas
    désactiver silencieusement un garde-fou."""
    from types import SimpleNamespace

    assert construire_garde(None, SimpleNamespace()).enabled is True
    assert construire_garde(
        None, SimpleNamespace(quality=SimpleNamespace(gate_enabled=False))
    ).enabled is False


# ===========================================================================
# 2. Agent 1 — la recommandation disparaît, pas seulement le ton
# ===========================================================================


def test_agent1_ne_formule_aucune_recommandation_sur_une_filiale_non_fiable(
    monkeypatch,
):
    """CE N'EST PAS UN AVERTISSEMENT AJOUTÉ À UNE RECOMMANDATION : c'est
    l'absence de recommandation.

    « À faire : relancer une campagne de réassurance » sous un taux calculé sur
    quatre avis envoie travailler sur du bruit, et la réserve en dessous ne
    rattrape rien — c'est l'action que le lecteur retient.
    """
    from reviews.agents.insight_agent import InsightAgent
    from reviews.agents.arbitrage import Candidat

    agent = InsightAgent.__new__(InsightAgent)
    agent.briefing = object()  # présent : sans le garde, il serait consulté
    agent.garde = GardeQualite(
        _Base([{"global_score": 0.2, "status": "UNTRUSTED", "diagnostic": "source_vide"}]),
        enabled=True,
    )
    monkeypatch.setattr(
        InsightAgent, "_signal", lambda self, c: "69,6 % des avis sont négatifs."
    )

    candidat = Candidat(
        level="subsidiary", key="12", label="Comores Telecom", pays="Comores",
        delta_negatifs=20.0, part_negatifs=70.0,
        avis_clients=4, avis_clients_avant=3,
    )
    redaction = agent._rediger(candidat, dry_run=False)

    # La MESURE doit rester : on retire seulement l'insight et l'action.
    assert "69,6 %" in redaction.signal
    assert redaction.insight is None
    assert redaction.action is None
    assert "insuffisantes" in (redaction.reserve or "")
    assert "À faire" not in redaction.texte()


def test_agent1_conserve_son_comportement_quand_le_garde_est_muet(monkeypatch):
    """NON-RÉGRESSION. Garde désactivé ou filiale non évaluée : le briefing doit
    être exactement celui d'avant l'intégration."""
    from reviews.agents.insight_agent import InsightAgent
    from reviews.agents.arbitrage import Candidat

    agent = InsightAgent.__new__(InsightAgent)
    agent.briefing = None
    agent.garde = GardeQualite(None, enabled=False)
    monkeypatch.setattr(InsightAgent, "_signal", lambda self, c: "Texte factuel.")
    monkeypatch.setattr(InsightAgent, "_insight_repli", lambda self, c: None)

    candidat = Candidat(
        level="subsidiary", key="12", label="X", pays="Mali",
        delta_negatifs=20.0, part_negatifs=70.0,
        avis_clients=100, avis_clients_avant=100,
    )
    assert agent._rediger(candidat, dry_run=True).texte() == "Texte factuel."


# ===========================================================================
# 3. Agent 2 — la campagne reste possible, la réserve devient visible
# ===========================================================================


def test_agent2_affiche_la_reserve_sans_renoncer_a_la_campagne():
    """UNE FILIALE MAL COUVERTE EST SOUVENT CELLE DONT PERSONNE NE S'OCCUPE.

    Refuser de proposer la rendrait définitivement invisible. On propose, on dit
    ce que vaut le socle, et la décision reste à l'équipe.
    """
    from reviews.agents.campaign_agent import Campagne
    from reviews.domain.marketing import CANAUX, OBJECTIFS, SEGMENTS
    from reviews.agents.campagne import Cible

    campagne = Campagne(
        cible=Cible(
            level="subsidiary", key="12", label="Orange Mali", pays="Mali",
            iso2="ML", avis_clients=40, positifs=5, negatifs=30,
            part_negatifs=75.0, part_positifs=12.5,
        ),
        segment=list(SEGMENTS.values())[0],
        objectif=list(OBJECTIFS.values())[0],
        canal=list(CANAUX.values())[0],
        nom="Réassurance", probleme="75 % d'avis négatifs.",
        accroche="A", message="M", taille_segment=30,
        qualite_donnees=Verdict(
            statut="UNTRUSTED", score=0.2,
            mention="⚠️ Les données disponibles sont insuffisantes.",
        ).as_dict(),
    )
    texte = campagne.texte()

    assert "insuffisantes" in texte
    assert "ne doit pas être présentée comme fondée" in texte
    # La campagne EXISTE toujours : son nom et son message sont là.
    assert "Réassurance" in texte and "Message : M" in texte


def test_agent2_sans_verdict_produit_le_texte_d_avant():
    """NON-RÉGRESSION : `qualite_donnees` absent = aucune ligne ajoutée."""
    from reviews.agents.campaign_agent import Campagne
    from reviews.domain.marketing import CANAUX, OBJECTIFS, SEGMENTS
    from reviews.agents.campagne import Cible

    campagne = Campagne(
        cible=Cible(
            level="subsidiary", key="12", label="Orange Mali", pays="Mali",
            iso2="ML", avis_clients=400, positifs=50, negatifs=300,
            part_negatifs=75.0, part_positifs=12.5,
        ),
        segment=list(SEGMENTS.values())[0],
        objectif=list(OBJECTIFS.values())[0],
        canal=list(CANAUX.values())[0],
        nom="N", probleme="P", accroche="A", message="M", taille_segment=30,
    )
    assert "⚠️ Les données" not in campagne.texte()


# ===========================================================================
# 4. Orphelins — la chaîne de résolution, du sûr au refus
# ===========================================================================


def test_une_egalite_stricte_avec_un_alias_est_appliquable_d_office():
    """C'est un REJEU, pas une décision : l'avis aurait dû être rattaché à la
    collecte. Mesuré : 1 202 des 1 215 orphelins sont dans ce cas."""
    r = ResolveurOrphelins(_Base())
    p = r._resoudre(
        {"review_id": "r1", "company": "MTN Nigeria", "source_code": "rss_feed"},
        _index((7, "MTN Nigeria", ["MTN Nigeria"])),
    )

    assert p.status == AUTO_SAFE
    assert p.method == "alias_exact"
    assert p.proposed_subsidiary_id == 7
    assert p.applicable is True


def test_une_correspondance_apres_accent_reste_a_valider():
    """Les 13 « Orange Senegal » du corpus. HIGH_CONFIDENCE et non AUTO_SAFE :
    la règle de repli est très probablement juste, mais personne ne l'a
    validée, et déplacer des avis change des taux publiés."""
    r = ResolveurOrphelins(_Base())
    p = r._resoudre(
        {"review_id": "r1", "company": "Orange Senegal", "source_code": "google_maps"},
        _index((9, "Orange Sénégal", ["Orange Sénégal"])),
    )

    assert p.status == HIGH_CONFIDENCE
    assert p.method == "alias_normalise"
    assert p.proposed_subsidiary_id == 9
    # Pas applicable d'office : c'est tout l'intérêt de la distinction.
    assert p.applicable is False


def test_un_libelle_ambigu_n_est_jamais_tranche():
    """Deux alias qui se recouvrent sont un défaut de NOTRE configuration. Un
    modèle n'a aucune information pour lever l'ambiguïté — il produirait une
    réponse plausible et invérifiable."""
    r = ResolveurOrphelins(_Base())
    p = r._resoudre(
        {"review_id": "r1", "company": "Orange", "source_code": "rss_feed"},
        _index((1, "Orange Mali", ["Orange"]), (2, "Orange Niger", ["Orange"])),
    )

    assert p.status == REVIEW_REQUIRED
    assert p.method == "ambigu"
    assert p.proposed_subsidiary_id is None
    assert len(p.evidence) == 2


def test_un_libelle_inconnu_reste_non_resolu():
    r = ResolveurOrphelins(_Base())
    p = r._resoudre(
        {"review_id": "r1", "company": "Zamtel Zambie", "source_code": "rss_feed"},
        _index((1, "Orange Mali", ["Orange Mali"])),
    )

    assert p.status == UNRESOLVED
    assert p.proposed_subsidiary_id is None


def test_un_avis_sans_libelle_ne_leve_pas():
    r = ResolveurOrphelins(_Base())
    p = r._resoudre({"review_id": "r1", "company": None}, _index((1, "X", ["X"])))
    assert p.status == UNRESOLVED


def test_la_normalisation_replie_accents_et_casse():
    assert normaliser("Orange Sénégal") == normaliser("ORANGE  SENEGAL")
    assert normaliser("Côte d'Ivoire") == "cote d'ivoire"
    assert normaliser(None) == ""


def test_seul_le_deterministe_est_ecrit_par_defaut():
    """`appliquer()` sans drapeau ne doit toucher QUE les AUTO_SAFE."""
    base = _Base()
    r = ResolveurOrphelins(base)
    propositions = [
        Proposition(review_id="a", company="X", source_code="s",
                    proposed_subsidiary_id=1, status=AUTO_SAFE),
        Proposition(review_id="b", company="Y", source_code="s",
                    proposed_subsidiary_id=2, status=HIGH_CONFIDENCE),
        Proposition(review_id="c", company="Z", source_code="s", status=UNRESOLVED),
    ]
    assert r.appliquer(propositions) == 1
    assert r.appliquer(propositions, inclure_haute_confiance=True) == 2


def test_l_ecriture_ne_peut_pas_ecraser_un_rattachement_existant():
    """`subsidiary_id IS NULL` dans le WHERE : garde-fou contre une
    ré-application concurrente. On ne peut pas savoir laquelle des deux
    attributions serait la bonne, et écraser en silence serait la pire réponse."""
    base = _Base()
    ResolveurOrphelins(base).appliquer(
        [Proposition(review_id="a", company="X", source_code="s",
                     proposed_subsidiary_id=1, status=AUTO_SAFE)]
    )
    sql_reviews = [s for s, _ in base.curseur.executions if "UPDATE reviews" in s]

    assert sql_reviews
    assert "subsidiary_id IS NULL" in sql_reviews[0]


def test_l_application_est_tracee_donc_reversible():
    """Sans trace, l'opération serait irréversible — et personne ne s'en
    apercevrait avant d'en avoir besoin."""
    base = _Base()
    ResolveurOrphelins(base).appliquer(
        [Proposition(review_id="a", company="X", source_code="s",
                     proposed_subsidiary_id=1, status=AUTO_SAFE)]
    )
    sql = [s for s, _ in base.curseur.executions if "orphan_resolutions" in s]

    assert sql and "applied_at = now()" in sql[0]


def test_le_depot_ne_remet_jamais_applied_at_a_nul():
    """Une proposition déjà appliquée doit garder sa date : c'est elle qui
    distingue une analyse d'une modification de données."""
    from reviews.storage.quality_repository import QualityRepository

    base = _Base()
    QualityRepository(base).enregistrer_propositions(
        [{"review_id": "a", "method": "alias_exact", "status": AUTO_SAFE}]
    )
    apres_conflit = base.curseur.sql.split("DO UPDATE")[1]

    assert "applied_at" not in apres_conflit


# ===========================================================================
# 5. Profil de modèle — le cloisonnement, et le repli qui ne s'improvise pas
# ===========================================================================


def test_sans_cle_propre_et_sans_repli_l_agent_reste_deterministe(monkeypatch):
    """LE GARDE-FOU DE §17. Sans cette règle, l'Agent 3 emprunterait le quota
    de Gemini — celui-là même qu'il devait épargner — et personne ne le verrait
    avant que l'analyse sémantique ne s'arrête."""
    from types import SimpleNamespace

    from reviews.config import quality_llm_config

    settings = SimpleNamespace(
        quality_llm=SimpleNamespace(
            enabled=True, api_key=None, fallback_gemini=False,
            provider="ollama_cloud", base_url="https://ollama.com/v1",
            model="gpt-oss:20b", timeout=90, min_interval_seconds=3.0,
            daily_call_budget=60, batch_size=8, max_review_chars=600,
        ),
        llm=SimpleNamespace(enabled=True, api_key="clef-gemini"),
    )
    assert quality_llm_config(settings) is None


def test_le_repli_gemini_demande_conserve_le_profil_cloisonne():
    """Même en empruntant la clé principale, les appels restent comptés sous
    « qualite » : c'est ce qui protège le budget de l'analyse sémantique."""
    from types import SimpleNamespace

    from reviews.config import LLMConfig, quality_llm_config

    settings = SimpleNamespace(
        quality_llm=SimpleNamespace(
            enabled=True, api_key=None, fallback_gemini=True,
            provider="gemini", base_url="x", model="y", timeout=90,
            min_interval_seconds=3.0, daily_call_budget=60,
            batch_size=8, max_review_chars=600,
        ),
        llm=LLMConfig(enabled=True, api_key="clef-gemini"),
    )
    cfg = quality_llm_config(settings)

    assert cfg is not None
    assert cfg.profil == "qualite"
    assert cfg.daily_call_budget == 60


def test_une_cle_propre_pointe_sur_ollama_sans_toucher_au_profil_principal():
    from types import SimpleNamespace

    from reviews.config import quality_llm_config

    settings = SimpleNamespace(
        quality_llm=SimpleNamespace(
            enabled=True, api_key="clef-ollama", fallback_gemini=False,
            provider="ollama_cloud", base_url="https://ollama.com/v1",
            model="gpt-oss:20b", timeout=90, min_interval_seconds=3.0,
            daily_call_budget=60, batch_size=8, max_review_chars=600,
        ),
        llm=SimpleNamespace(max_tokens=1600, temperature=0.2),
    )
    cfg = quality_llm_config(settings)

    assert cfg.base_url == "https://ollama.com/v1"
    assert cfg.model == "gpt-oss:20b"
    assert cfg.profil == "qualite"


def test_la_comptabilite_du_modele_est_cloisonnee_par_profil():
    """`llm_usage` a pour clé (day, profil) depuis la migration 022. Sans le
    prédicat, l'Agent 3 lirait — et incrémenterait — le compteur de Gemini."""
    from reviews.config import LLMConfig
    from reviews.llm.client import LLMClient

    base = _Base([{"calls": 3, "tokens_in": 0, "tokens_out": 0, "errors": 0}])
    client = LLMClient(LLMConfig(profil="qualite", api_key="x"), db=base)
    client.usage_today()

    assert "profil = %s" in base.curseur.sql
    assert "qualite" in base.curseur.params


def test_le_budget_de_jetons_provisionne_le_raisonnement():
    """MESURÉ : `gpt-oss:20b` rend `reasoning` ET `content`, tous deux facturés
    en jetons de sortie. À 32 jetons, `content` revient VIDE — le lot entier est
    perdu. Le plafond doit donc couvrir le raisonnement, sinon la panne des 4 %
    de lots perdus revient par une autre porte."""
    from reviews.llm import quality_validator as qv

    assert qv._TOKENS_RAISONNEMENT >= 300
    budget = (
        qv._TOKENS_ENVELOPPE + qv._TOKENS_RAISONNEMENT + qv._TOKENS_PAR_AVIS * 8
    )
    assert budget > 1500


# ===========================================================================
# 6. Résilience — une sous-étape en échec ne fait pas tomber le passage
# ===========================================================================


@pytest.mark.parametrize(
    "etape",
    ["mapping", "controles", "decouverte", "validation", "affirmations", "telegram"],
)
def test_une_sous_etape_en_echec_ne_fait_pas_tomber_le_passage(monkeypatch, etape):
    """LE PRINCIPE DE L'ÉTAPE 9 : Agent 3 échoue → les données principales
    continuent de fonctionner.

    Chaque sous-étape dépend d'une ressource qui peut disparaître — un modèle
    injoignable, une URL qui ne répond plus, l'API Telegram en panne. Aucune ne
    doit emporter le diagnostic ni le score, qui sont la seule chose que
    l'Agent 3 sait produire sans aucune dépendance externe.
    """
    from reviews.agents.quality import guardian as module
    from reviews.config import get_settings

    def _explose(*a, **k):
        raise RuntimeError(f"panne simulée : {etape}")

    couverture = _CouvertureFactice()
    monkeypatch.setattr(
        module.MoniteurCouverture, "analyser", lambda self: [couverture]
    )
    monkeypatch.setattr(
        module.DetecteurMapping, "analyser",
        _explose if etape == "mapping" else (lambda self: []),
    )
    monkeypatch.setattr(
        module.ControlesQualite, "analyser",
        _explose if etape == "controles" else (lambda self: []),
    )
    monkeypatch.setattr(module.ControlesFraicheur, "analyser", lambda self, c: [])
    monkeypatch.setattr(module, "completude_par_filiale", lambda db: {})

    agent = module.QualityGuardian(_Base(), get_settings())
    for nom in ("enregistrer_constats", "enregistrer_candidates",
                "enregistrer_scores", "enregistrer_affirmations",
                "constats_ouverts_par_filiale"):
        monkeypatch.setattr(agent.depot, nom, lambda *a, **k: {} if "par_filiale" in nom else 0)

    if etape == "decouverte":
        monkeypatch.setattr(agent.decouverte, "pour", _explose)
    if etape == "validation":
        agent.validateur = type("V", (), {"valider": _explose})()
        monkeypatch.setattr(agent.depot, "avis_a_valider",
                            lambda **k: [{"flag_id": 1, "review_id": "r"}])
    if etape == "affirmations":
        monkeypatch.setattr(
            module.VerificateurAffirmations, "analyser", _explose
        )
    if etape == "telegram":
        agent.notifier = type("N", (), {"send_text": _explose})()

    passage = agent.run()  # ne doit PAS lever

    # Le diagnostic a été posé malgré la panne : c'est l'invariant.
    assert passage.filiales == 1
    assert passage.diagnostics


class _CouvertureFactice:
    """Couverture minimale : une filiale sans source déclarée."""

    subsidiary_id = 1
    subsidiary = "Filiale test"
    operator = "Op"
    country = "Pays"
    iso2 = "ML"
    avis_clients = 0
    avis_recents = 0
    articles_presse = 0
    derniere_collecte = None
    dernier_avis = None
    sources: dict = {}
    sources_attendues: list = []
    sources_actives: list = []
    sources_muettes: list = []
    sources_en_erreur: list = []
    sources_jamais_tentees: list = []
    taux_couverture_sources = None


# ===========================================================================
# 7. API — le contrat des orphelins
# ===========================================================================


def test_la_route_des_orphelins_est_en_lecture_seule():
    """La réattribution modifie `reviews`, la seule table que tout le reste de
    l'Agent 3 s'interdit de toucher. Elle ne doit pas être déclenchable par une
    requête web — d'où une CLI, et un GET ici."""
    from reviews.api.routes.quality import router

    methodes = {r.path: r.methods for r in router.routes if hasattr(r, "methods")}
    assert methodes["/quality/orphans"] == {"GET"}
