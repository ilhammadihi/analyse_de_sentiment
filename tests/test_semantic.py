"""
Tests de la couche sémantique — purs, sans base ni réseau.

Ce module cible les défauts qui produisent des données FAUSSES SANS lever
d'erreur : un aspect inventé par le modèle qui entre en base, un lot dont la
réponse est mal alignée sur les avis envoyés, une synthèse servie depuis le
cache d'un AUTRE périmètre. Aucun de ces cas ne provoque de 500 — ils
s'affichent tranquillement, ce qui est exactement ce qui les rend dangereux.
"""

import json

import pytest

from reviews.domain.aspects import (
    ASPECT_VERSION,
    MAX_ASPECTS_PER_POLARITY,
    OTHER,
    VALID_ASPECTS,
    label,
    normalize,
    taxonomy_for_prompt,
)
from reviews.llm.client import LLMError, extract_json
from reviews.llm.insights import PROMPT_VERSION, _delta, _scope_to
from reviews.llm.semantic import (
    SemanticAnalyzer,
    _clean_aspects,
    _index_results,
)
from reviews.storage.filters import StatsFilter


# ---------------------------------------------------------------------------
# Taxonomie
# ---------------------------------------------------------------------------


def test_taxonomy_rejects_invented_aspects():
    """Un aspect hors taxonomie est REFUSÉ, pas rangé approximativement.

    C'est la garantie centrale de la liste fermée : accepter « probleme_reseau »
    à côté de « reseau_couverture » rouvrirait la taxonomie et ramènerait le
    nuage de formulations non agrégeables que ces aspects existent pour éviter.
    """
    assert normalize("reseau_couverture") == "reseau_couverture"
    assert normalize("probleme_reseau") is None
    assert normalize("network") is None
    assert normalize("") is None
    assert normalize(None) is None


def test_taxonomy_tolerates_cosmetic_variations():
    """Casse, espaces et tirets ne doivent pas faire perdre un aspect valide."""
    assert normalize("  Reseau_Couverture ") == "reseau_couverture"
    assert normalize("app-bugs") == "app_bugs"
    assert normalize("app bugs") == "app_bugs"


def test_every_aspect_has_a_definition_in_the_prompt():
    """La liste envoyée au modèle est GÉNÉRÉE, jamais recopiée.

    Une taxonomie tenue à deux endroits diverge : le modèle produirait alors des
    aspects que `normalize()` rejette, et les avis perdraient silencieusement
    leur classement.
    """
    prompt = taxonomy_for_prompt()
    for key in VALID_ASPECTS:
        assert f"- {key} :" in prompt


def test_other_is_part_of_the_taxonomy():
    """Le repli doit être une valeur ACCEPTÉE, pas une valeur rejetée.

    Sans lui, un avis hors périmètre (« merci », une insulte) force le modèle à
    choisir un aspect au hasard, qui pollue un motif réel.
    """
    assert OTHER in VALID_ASPECTS
    assert normalize(OTHER) == OTHER


def test_label_falls_back_to_the_key():
    assert label("coupures_pannes") == "Coupures & pannes"
    assert label("inconnu") == "inconnu"


# ---------------------------------------------------------------------------
# Nettoyage des sorties du modèle
# ---------------------------------------------------------------------------


def test_clean_aspects_filters_deduplicates_and_caps():
    """Trois garanties en une : validité, unicité, plafond."""
    raw = [
        "reseau_couverture",
        "reseau_couverture",   # doublon
        "facturation_prix",
        "chose_inventee",      # hors taxonomie
        "app_bugs",
        "service_client",      # au-delà du plafond
    ]
    cleaned = _clean_aspects(raw)
    assert cleaned == ["reseau_couverture", "facturation_prix", "app_bugs"]
    assert len(cleaned) <= MAX_ASPECTS_PER_POLARITY


def test_clean_aspects_survives_garbage_shapes():
    """Le modèle renvoie parfois autre chose qu'une liste de chaînes."""
    assert _clean_aspects(None) == []
    assert _clean_aspects("reseau_couverture") == []  # chaîne nue, pas une liste
    assert _clean_aspects([None, 42, {"a": 1}]) == []


def test_batch_results_are_matched_by_declared_index():
    """L'appariement suit le numéro DÉCLARÉ, pas l'ordre d'arrivée.

    Si le modèle renvoie les avis dans le désordre — ce qu'il fait — se fier à
    la position attribuerait le verdict du deuxième avis au premier. Le défaut
    est invisible : chaque avis reçoit bien un sentiment, simplement pas le sien.
    """
    data = {
        "resultats": [
            {"i": 2, "sentiment": "positive"},
            {"i": 1, "sentiment": "negative"},
        ]
    }
    indexed = _index_results(data)
    assert indexed[1]["sentiment"] == "negative"
    assert indexed[2]["sentiment"] == "positive"


def test_batch_results_accept_a_bare_list():
    """Un petit modèle renvoie régulièrement la liste sans son enveloppe.

    Refuser cette forme ferait perdre le lot entier — vingt avis et un appel de
    quota — pour une différence d'emballage.
    """
    indexed = _index_results([{"sentiment": "neutral"}, {"sentiment": "negative"}])
    assert indexed[1]["sentiment"] == "neutral"
    assert indexed[2]["sentiment"] == "negative"


def test_batch_results_accept_an_unexpected_key():
    indexed = _index_results({"reviews": [{"i": 1, "sentiment": "positive"}]})
    assert indexed[1]["sentiment"] == "positive"


# ---------------------------------------------------------------------------
# Extraction JSON
# ---------------------------------------------------------------------------


def test_extract_json_handles_markdown_fences():
    """Plusieurs fournisseurs enrobent le JSON malgré la consigne."""
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_handles_surrounding_prose():
    assert extract_json('Voici le résultat : {"a": 1} — voilà.') == {"a": 1}


def test_extract_json_raises_on_unusable_output():
    with pytest.raises(LLMError):
        extract_json("je ne peux pas répondre à cette demande")


# ---------------------------------------------------------------------------
# Verdict d'un avis
# ---------------------------------------------------------------------------


class _StubClient:
    """Client minimal : ni réseau, ni configuration, ni base."""

    class cfg:  # noqa: N801
        max_review_chars = 700
        batch_size = 20

    available = True

    def __init__(self, answer=None):
        self.answer = answer or {}
        self.calls = []
        self.kwargs = []

    def complete_json(self, *, system, user, **kwargs):
        self.calls.append(user)
        self.kwargs.append(kwargs)
        return self.answer

    def unavailable_reason(self):
        return None


def _analyzer(answer=None):
    return SemanticAnalyzer(db=None, client=_StubClient(answer))


def test_invalid_sentiment_is_dropped_not_guessed():
    """Un label hors nomenclature devient NULL, jamais une valeur par défaut.

    NULL laisse la vue retomber sur le jugement du lexique (COALESCE). Choisir
    « neutral » à la place inventerait un verdict que personne n'a rendu, et le
    ferait gagner contre le lexique.
    """
    result = _analyzer()._to_result("r1", {"sentiment": "mitigé"})
    assert result.sentiment is None


def test_confidence_is_clamped_and_tolerates_junk():
    to_result = _analyzer()._to_result
    assert to_result("r", {"confiance": 1.4}).confidence == 1.0
    assert to_result("r", {"confiance": -3}).confidence == 0.0
    assert to_result("r", {"confiance": "haute"}).confidence is None
    assert to_result("r", {"confidence": 0.8}).confidence == 0.8


def test_missing_review_still_gets_a_result():
    """Un avis ignoré par le modèle est quand même estampillé comme traité.

    Sans cela, un avis que le modèle refuse systématiquement de classer serait
    resoumis à chaque exécution et consommerait du quota indéfiniment.
    """
    analyzer = _analyzer({"resultats": [{"i": 1, "sentiment": "negative"}]})
    results = analyzer.analyze_batch(
        [{"review_id": "a", "title": None, "text": "mauvais"},
         {"review_id": "b", "title": None, "text": "bof"}]
    )
    assert [r.review_id for r in results] == ["a", "b"]
    assert results[0].sentiment == "negative"
    assert results[1].sentiment is None       # ignoré par le modèle…
    assert results[1].neg_aspects == []       # …mais bien renvoyé


def test_output_ceiling_scales_with_the_batch():
    """Le plafond de sortie suit la taille du lot, il n'est jamais constant.

    RÉGRESSION MESURÉE : avec un plafond global de 1 600 jetons, un backfill de
    125 lots en a perdu 5 (4 %) sur « réponse illisible ». La sortie faisait
    1 012 jetons en moyenne et les lots les plus riches en aspects dépassaient le
    plafond ; une réponse tronquée n'est pas un JSON valide, et c'est vingt avis
    plus un appel de quota perdus à chaque fois.

    Ce test existe surtout pour le jour où quelqu'un montera LLM_BATCH_SIZE :
    avec une constante, la panne reviendrait sans le moindre signal.
    """
    from reviews.llm.semantic import _TOKENS_OVERHEAD, _TOKENS_PER_REVIEW

    analyzer = _analyzer()
    petit = [{"review_id": str(i), "title": None, "text": "x"} for i in range(5)]
    grand = [{"review_id": str(i), "title": None, "text": "x"} for i in range(50)]

    analyzer.analyze_batch(petit)
    analyzer.analyze_batch(grand)

    plafond_petit = analyzer.client.kwargs[0]["max_tokens"]
    plafond_grand = analyzer.client.kwargs[1]["max_tokens"]

    assert plafond_petit == _TOKENS_OVERHEAD + 5 * _TOKENS_PER_REVIEW
    assert plafond_grand == _TOKENS_OVERHEAD + 50 * _TOKENS_PER_REVIEW
    assert plafond_grand > plafond_petit

    # La marge doit couvrir le cas verbeux mesuré (~50 jetons/avis en moyenne),
    # pas seulement la moyenne.
    assert _TOKENS_PER_REVIEW >= 100


def test_prompt_carries_the_taxonomy_and_numbered_reviews():
    analyzer = _analyzer()
    analyzer.analyze_batch([{"review_id": "a", "title": "Titre", "text": "Texte"}])
    prompt = analyzer.client.calls[0]
    assert "reseau_couverture" in prompt
    assert "[1] Titre. Texte" in prompt


# ---------------------------------------------------------------------------
# Synthèses
# ---------------------------------------------------------------------------


def test_scope_to_uses_filter_identifiers_not_ranking_keys():
    """Un pays se désigne par son ISO alpha-2, comme partout dans les filtres.

    Utiliser le `country_id` des classements produirait un périmètre vide, donc
    une synthèse portant sur rien — sans qu'aucune erreur ne le signale.
    """
    base = StatsFilter(days=30)
    assert _scope_to(base, "country", "SN").countries == ("SN",)
    assert _scope_to(base, "subsidiary", "42").subsidiaries == (42,)
    assert _scope_to(base, "operator", "7").operators == (7,)
    assert _scope_to(base, "region", "Afrique de l'Ouest").regions == (
        "Afrique de l'Ouest",
    )


def test_scope_to_rejects_unsupported_levels():
    with pytest.raises(ValueError, match="source"):
        _scope_to(StatsFilter(), "source", "google_play")


def test_scope_to_preserves_the_rest_of_the_scope():
    """Resserrer sur une entité ne doit toucher à AUCUN autre axe.

    Une synthèse calculée sur une période différente de celle affichée
    commenterait d'autres chiffres que ceux à l'écran.
    """
    base = StatsFilter(days=7, regions=("Afrique de l'Est",), min_subsidiary_reviews=10)
    scoped = _scope_to(base, "subsidiary", "3")
    assert scoped.days == 7
    assert scoped.regions == ("Afrique de l'Est",)
    assert scoped.min_subsidiary_reviews == 10


class _FakeRepo:
    """Repository minimal : renvoie des mesures fixes, sans base."""

    def __init__(self, avis=200, avis_avant=1):
        self.avis, self.avis_avant = avis, avis_avant

    def overview(self, f):
        return {
            "current": {
                "avis_clients": self.avis,
                "part_negatifs": 23.3,
                "part_positifs": 73.2,
                "note_moyenne": 4.1,
            },
            "previous": {
                "avis_clients": self.avis_avant,
                "part_negatifs": 100.0,
                "part_positifs": 0.0,
            },
        }

    def themes(self, f, polarity, limit, dimension):
        return {"terms": []}

    def verbatims(self, f, polarity, limit):
        return {"reviews": []}

    def semantic_coverage(self, f):
        return {"total": 100, "analyses": 100, "part": 100.0}


class _FakePress:
    """Dépôt de presse minimal : rend ce qu'on lui a dit de rendre.

    Par défaut AUCUN article, pour que les tests d'origine mesurent le contexte
    chiffré sans que des faits externes viennent s'y mêler.
    """

    def __init__(self, articles=None, perimetre="cette filiale"):
        self.articles = articles or []
        self.perimetre = perimetre
        self.fenetres = []

    def evidence(self, *, window, level, value):
        self.fenetres.append(window)
        return {
            "articles": list(self.articles),
            "perimetre": self.perimetre,
            "elargi": False,
        }


def _service(repo, press=None):
    from reviews.llm.insights import InsightService

    svc = InsightService.__new__(InsightService)
    svc.repo, svc.db, svc.client = repo, None, None
    svc.press = press or _FakePress()
    svc._label = lambda level, value: f"entite-{value}"
    return svc


def test_comparison_context_omits_the_previous_window():
    """Une comparaison porte sur l'écart entre entités À LA MÊME DATE.

    RÉGRESSION MESURÉE : tant que la fenêtre précédente était transmise, le
    modèle refusait de comparer MTN Ghana (52,3 % de négatifs sur 262 avis) à
    Vodacom South Africa (23,3 % sur 257) au motif que « les volumes de la
    période précédente sont trop faibles ». Les deux volumes courants étaient
    pourtant excellents : c'est la donnée hors sujet qui l'égarait.
    """
    from reviews.llm.insights import COMPARISON

    svc = _service(_FakeRepo(avis=262, avis_avant=1))
    ctx = svc._build_context(COMPARISON, StatsFilter(days=90), "subsidiary", ["32", "72"])

    for entite in ctx["entites"]:
        assert "periode_precedente" not in entite
        assert "variation_negatifs_points" not in entite
        # Volume courant largement suffisant : aucun avertissement ne doit
        # décourager la comparaison.
        assert "avertissement" not in entite


def test_comparison_warns_on_a_thin_CURRENT_volume():
    """Sur une comparaison, la fragilité se juge sur le volume COURANT."""
    from reviews.llm.insights import COMPARISON

    svc = _service(_FakeRepo(avis=4, avis_avant=900))
    ctx = svc._build_context(COMPARISON, StatsFilter(days=90), "subsidiary", ["32"])
    assert "4 avis seulement" in ctx["entites"][0]["avertissement"]


def test_spike_context_keeps_the_previous_window_and_flags_thin_history():
    """Sur un pic, la fenêtre précédente est le sujet même — et son volume compte.

    Le cas réel : −76,7 points calculés contre UN avis. L'avertissement est
    produit en Python, pas espéré du modèle.
    """
    from reviews.llm.insights import SPIKE

    svc = _service(_FakeRepo(avis=257, avis_avant=1))
    entite = svc._build_context(SPIKE, StatsFilter(days=90), "subsidiary", ["72"])["entites"][0]

    assert entite["periode_precedente"]["avis_clients"] == 1
    assert entite["variation_negatifs_points"] == -76.7
    assert entite["variation_fiable"] is False
    assert "1 avis" in entite["avertissement"]


def test_spike_does_not_warn_when_both_windows_are_solid():
    from reviews.llm.insights import SPIKE

    svc = _service(_FakeRepo(avis=257, avis_avant=200))
    entite = svc._build_context(SPIKE, StatsFilter(days=90), "subsidiary", ["72"])["entites"][0]
    assert entite["variation_fiable"] is True
    assert "avertissement" not in entite


def test_delta_is_none_when_a_side_is_missing():
    """Pas de période antérieure = pas de variation, jamais un zéro.

    « 0 pt » se lit « rien n'a bougé » ; l'absence de mesure n'est pas cela.
    """
    assert _delta(None, 10) is None
    assert _delta(10, None) is None
    assert _delta(45.3, 51.6) == -6.3


# ---------------------------------------------------------------------------
# Empreinte de cache
# ---------------------------------------------------------------------------


class _StubService:
    """Expose la seule méthode testée ici, sans base ni client."""

    from reviews.llm.insights import InsightService

    _hash = InsightService._hash


def test_cache_key_ignores_entity_order():
    """Deux filiales sélectionnées dans l'autre sens posent la MÊME question.

    Sans ce tri, rouvrir le même écran après avoir cliqué les entités dans un
    ordre différent repaierait un appel pour obtenir la phrase déjà en cache.
    """
    svc, f = _StubService(), StatsFilter(days=30)
    assert svc._hash("comparison", f, "subsidiary", ["7", "42"]) == svc._hash(
        "comparison", f, "subsidiary", ["42", "7"]
    )


def test_cache_key_separates_different_scopes():
    """Deux périmètres différents ne partagent JAMAIS une synthèse.

    C'est la pire erreur possible ici : un texte juste, affiché sous des
    chiffres qui ne sont pas les siens.
    """
    svc = _StubService()
    base = svc._hash("comparison", StatsFilter(days=30), "subsidiary", ["7", "42"])

    assert base != svc._hash("comparison", StatsFilter(days=90), "subsidiary", ["7", "42"])
    assert base != svc._hash("comparison", StatsFilter(days=30), "operator", ["7", "42"])
    assert base != svc._hash("comparison", StatsFilter(days=30), "subsidiary", ["7", "43"])
    assert base != svc._hash("spike", StatsFilter(days=30), "subsidiary", ["7", "42"])
    assert base != svc._hash(
        "comparison", StatsFilter(days=30, countries=("SN",)), "subsidiary", ["7", "42"]
    )


def test_cache_key_is_a_sha256_hex_digest():
    """La colonne est un CHAR(64) : une empreinte plus longue serait tronquée,
    donc deux périmètres distincts pourraient collisionner en base."""
    key = _StubService()._hash("spike", StatsFilter(), "subsidiary", ["1"])
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_prompt_version_is_part_of_the_key():
    """Faire évoluer le prompt doit PÉRIMER le cache, pas réafficher l'ancien
    texte sous un nouveau format."""
    svc, f = _StubService(), StatsFilter(days=30)
    key = svc._hash("spike", f, "subsidiary", ["1"])
    import reviews.llm.insights as mod

    original = mod.PROMPT_VERSION
    try:
        mod.PROMPT_VERSION = original + 1
        assert svc._hash("spike", f, "subsidiary", ["1"]) != key
    finally:
        mod.PROMPT_VERSION = original


# ---------------------------------------------------------------------------
# Dimension des motifs — surface d'injection
# ---------------------------------------------------------------------------


def _repo():
    from reviews.storage.stats_repository import StatsRepository

    return StatsRepository(db=None)


def test_dimension_whitelist_accepts_only_known_names():
    """La dimension vient de l'URL et sert à composer un nom de VUE et de COLONNE.

    Ni l'un ni l'autre ne peut être un paramètre lié : l'interpolation est
    inévitable, donc la liste blanche est la SEULE protection. Un test qui la
    verrouille vaut mieux qu'un commentaire qui la recommande.
    """
    assert _repo()._resolve_dimension("terms")[0] == "v_review_terms"
    assert _repo()._resolve_dimension("aspects")[0] == "v_review_aspects"


@pytest.mark.parametrize(
    "malveillant",
    [
        "aspects; DROP TABLE reviews",
        "v_review_terms",          # nom de vue réel, mais pas une dimension
        "terms UNION SELECT 1",
        "ASPECTS",                 # la casse n'est pas tolérée
        "",
    ],
)
def test_dimension_whitelist_rejects_everything_else(malveillant):
    with pytest.raises(ValueError, match="inconnue"):
        _repo()._resolve_dimension(malveillant)


def test_dimension_error_lists_the_accepted_values():
    """Le message doit être exploitable : un 422 « valeur invalide » sans la
    liste des valeurs acceptées oblige à ouvrir le code."""
    with pytest.raises(ValueError) as exc:
        _repo()._resolve_dimension("motifs")
    assert "aspects" in str(exc.value) and "terms" in str(exc.value)


# ---------------------------------------------------------------------------
# Versionnement
# ---------------------------------------------------------------------------


def test_versions_are_positive_integers():
    """Persistés en SMALLINT et comparés avec `<` par le backfill : une version
    non entière ferait échouer la sélection des lignes à rejouer."""
    assert isinstance(ASPECT_VERSION, int) and ASPECT_VERSION > 0
    assert isinstance(PROMPT_VERSION, int) and PROMPT_VERSION > 0


# ---------------------------------------------------------------------------
# Faits externes : la preuve qui autorise une cause extérieure
# ---------------------------------------------------------------------------


def test_press_evidence_reaches_every_entity_of_the_context():
    """Sans ces clés dans le contexte, le prompt interdit toute cause externe.

    Le modèle n'a le droit d'invoquer l'extérieur qu'en citant un article de
    `faits_externes` ; `perimetre_presse` lui dit à quelle maille il a été
    trouvé. Les deux doivent donc accompagner CHAQUE entité, y compris celles
    qui n'ont aucun article — sinon leur absence se lit comme une recherche non
    faite plutôt que comme une recherche infructueuse.
    """
    from reviews.llm.insights import COMPARISON

    presse = _FakePress(
        articles=[{"date": "2026-07-31", "titre": "Hausse tarifaire", "media": "X"}],
        perimetre="ce pays, tous opérateurs confondus",
    )
    ctx = _service(_FakeRepo(avis=300), press=presse)._build_context(
        COMPARISON, StatsFilter(days=90), "subsidiary", ["32", "72"]
    )

    assert len(ctx["entites"]) == 2
    for entite in ctx["entites"]:
        assert entite["faits_externes"][0]["date"] == "2026-07-31"
        assert entite["perimetre_presse"] == "ce pays, tous opérateurs confondus"


def test_press_window_starts_before_the_analysed_period_but_never_ends_after():
    """Une cause précède son effet — et ne peut pas lui succéder.

    L'amorce vers l'amont rattrape le délai entre un événement et les avis
    qu'il provoque. La borne haute, elle, ne bouge pas : un article publié
    après la fin de la fenêtre observée ne peut pas expliquer des avis déjà
    écrits, et l'offrir au modèle serait lui tendre une coïncidence
    chronologiquement impossible.
    """
    from reviews.llm.insights import SPIKE, _AMORCE_PRESSE_JOURS

    presse = _FakePress()
    f = StatsFilter(days=90)
    _service(_FakeRepo(avis=300), press=presse)._build_context(
        SPIKE, f, "subsidiary", ["72"]
    )

    debut_analyse, fin_analyse = f.resolved_window()
    debut_presse, fin_presse = presse.fenetres[0]
    assert (debut_analyse - debut_presse).days == _AMORCE_PRESSE_JOURS
    assert fin_presse == fin_analyse
