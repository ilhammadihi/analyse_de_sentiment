"""Tests du moteur de sentiment (lexique FR/EN)."""

import pytest

from reviews.domain.sentiment import analyze_sentiment
from reviews.domain.models import SentimentEnum, SourceEnum


@pytest.mark.parametrize("text,expected", [
    ("Service excellent, très rapide et efficace !", SentimentEnum.POSITIVE),
    ("Je recommande vivement, parfait", SentimentEnum.POSITIVE),
    ("Arnaque totale, réseau en panne, injoignable", SentimentEnum.NEGATIVE),
    ("Horrible, pire opérateur, service client nul", SentimentEnum.NEGATIVE),
])
def test_polarity(text, expected):
    assert analyze_sentiment(text).sentiment == expected


def test_negation_inverts():
    # "pas bien" ne doit pas être classé positif
    assert analyze_sentiment("ce n'est pas bien du tout").sentiment != SentimentEnum.POSITIVE


def test_empty_text_is_neutral():
    assert analyze_sentiment("").sentiment == SentimentEnum.NEUTRAL
    assert analyze_sentiment(None).sentiment == SentimentEnum.NEUTRAL


def test_score_bounds():
    s = analyze_sentiment("excellent parfait génial super")
    assert -1.0 <= s.score <= 1.0


# ---------------------------------------------------------------------------
# Termes déclenchés (migration 004 — alimente l'onglet « Motifs »)
# ---------------------------------------------------------------------------


def test_triggered_terms_are_reported():
    """Le moteur doit dire QUELS mots ont fait pencher le score.

    C'est ce qui permet au dashboard de répondre « pourquoi » et non seulement
    « combien » — et c'est l'entrée dont un agent de campagne aura besoin.
    """
    s = analyze_sentiment("Reseau nul, coupures constantes, service injoignable")
    assert set(s.negative_terms) >= {"nul", "coupures", "injoignable"}
    assert s.positive_terms == []


def test_negated_term_is_classed_by_the_polarity_it_took():
    """« pas rapide » doit compter comme un motif NÉGATIF, et le dire.

    Deux exigences en une :
      - la polarité retenue est celle que le mot a PRISE, sinon l'onglet Motifs
        afficherait « rapide » parmi les points forts d'une filiale dont les
        clients disent précisément qu'elle ne l'est pas ;
      - le terme stocké porte la négation, faute de quoi le motif s'affiche
        « rapide » dans la liste des insatisfactions et passe pour une erreur.
    """
    s = analyze_sentiment("Ce n'est pas rapide")
    assert "pas rapide" in s.negative_terms
    assert "rapide" not in s.negative_terms
    assert s.positive_terms == []


def test_negated_and_plain_terms_are_distinct_motifs():
    """« lent » et « pas lent » ne doivent jamais se regrouper.

    Les confondre inverserait le sens de l'un des deux dans les agrégats.
    """
    plain = analyze_sentiment("Le reseau est lent")
    negated = analyze_sentiment("Le reseau n'est pas lent")
    assert plain.negative_terms == ["lent"]
    assert "lent" not in negated.negative_terms
    assert "pas lent" in negated.positive_terms


def test_terms_are_deduplicated_and_in_reading_order():
    """Ordre stable et sans doublon : ces valeurs sont persistées en base.

    Un ensemble non ordonné renverrait les termes dans un ordre dépendant du
    hachage, donc variable d'un processus à l'autre.
    """
    s = analyze_sentiment("nul, vraiment nul, et lent")
    assert s.negative_terms == ["nul", "lent"]


def test_terms_are_accent_folded():
    """Les termes sont stockés sans accent, forme comparable entre sources.

    Les avis de sources différentes n'accentuent pas de la même façon ;
    regrouper « décevant » et « decevant » exige une clé unique.
    """
    s = analyze_sentiment("Service décevant et médiocre")
    assert "decevant" in s.negative_terms
    assert "mediocre" in s.negative_terms


def test_unknown_vocabulary_yields_no_terms():
    """Un texte sans mot du lexique ne produit aucun motif — pas un motif vide.

    Attendu pour la presse, qui décrit sans juger : c'est pourquoi l'onglet
    Motifs ne se calcule que sur les avis clients.
    """
    s = analyze_sentiment("Le conseil d'administration se reunit mardi")
    assert s.negative_terms == []
    assert s.positive_terms == []


# ---------------------------------------------------------------------------
# Cloisonnement par domaine (lexique v5)
# ---------------------------------------------------------------------------


def test_lexique_appris_reserve_aux_avis_clients():
    """Les poids appris ne doivent PAS s'appliquer à la presse.

    Ils viennent exclusivement d'avis d'applications mobiles. Hors de ce
    domaine ils produisent des absurdités vérifiées : « La fibre optique ARRIVE
    dans certains quartiers » était classé négatif à cause du mot « arrive »,
    discriminant chez les mécontents d'une app (« je n'arrive pas à… ») et sans
    aucun sens dans un article de journal.
    """
    from reviews.domain.sentiment import CUSTOMER_REVIEW, PRESS

    titre = "La fibre optique arrive dans certains quartiers a Casa et Rabat"
    assert analyze_sentiment(titre, domain=PRESS).sentiment.value == "neutral"
    # Le domaine par défaut reste celui des avis clients.
    assert analyze_sentiment(titre).sentiment.value == \
        analyze_sentiment(titre, domain=CUSTOMER_REVIEW).sentiment.value


def test_vocabulaire_general_actif_dans_les_deux_domaines():
    """Le lexique écrit à la main vaut partout : c'est du vocabulaire général.

    Une panne reste une mauvaise nouvelle, dans un avis comme dans un article.
    """
    from reviews.domain.sentiment import CUSTOMER_REVIEW, PRESS

    for domaine in (CUSTOMER_REVIEW, PRESS):
        s = analyze_sentiment("Panne generale, service catastrophe", domain=domaine)
        assert s.sentiment.value == "negative", domaine


def test_domaine_deduit_de_la_source():
    """Une seule source produit de la presse ; toutes les autres, des avis."""
    from reviews.domain.sentiment import CUSTOMER_REVIEW, PRESS, domain_for_source

    assert domain_for_source(SourceEnum.RSS_FEED) == PRESS
    assert domain_for_source("rss_feed") == PRESS
    for source in (SourceEnum.APP_STORE, SourceEnum.GOOGLE_PLAY,
                   SourceEnum.GOOGLE_MAPS, SourceEnum.TRUSTPILOT):
        assert domain_for_source(source) == CUSTOMER_REVIEW


def test_les_deux_lexiques_sont_distincts():
    """Garde-fou : le lexique de presse doit rester le sous-ensemble curé.

    Si un jour les poids appris fuitaient dans CURATED_WEIGHTS, le
    cloisonnement deviendrait décoratif sans qu'aucun test ne le signale.
    """
    from reviews.domain.sentiment import (
        CURATED_WEIGHTS, LEARNED_WEIGHTS, SENTIMENT_WEIGHTS,
    )

    assert len(CURATED_WEIGHTS) < len(SENTIMENT_WEIGHTS)
    fuites = set(LEARNED_WEIGHTS) & set(CURATED_WEIGHTS) - set(CURATED_WEIGHTS)
    assert not fuites
    # Un terme typiquement appris ne doit pas figurer dans le lexique général.
    assert "crashing" not in CURATED_WEIGHTS
