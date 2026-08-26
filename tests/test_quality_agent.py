"""
Tests de l'Agent 3 — diagnostic, score, corroboration, validation.

AUCUNE BASE, AUCUN RÉSEAU, AUCUN MODÈLE. Tout ce qui est testé ici est du
raisonnement pur : c'est délibéré, et c'est ce qui rend ces tests utiles.

CE QU'ILS PROTÈGENT, ET POURQUOI ÇA COMPTE
    Une régression de l'Agent 3 est INVISIBLE en exploitation. Il ne lève pas
    d'exception, ne remplit aucun journal d'erreur : il rend simplement un
    diagnostic faux, et l'action qui en découle — relancer un collecteur sain,
    chercher une source pour une donnée déjà collectée, ou taire une filiale
    réellement en panne — est prise sans que rien ne signale l'erreur.

    Le cas le plus coûteux est déjà survenu pendant l'écriture du module : lire
    `collection_jobs.status` au lieu de `last_success_at` faisait passer une
    source interrogée avec succès pour une source jamais tentée. Deux
    diagnostics opposés sur les mêmes lignes. Le test qui l'attrape est
    `test_une_unite_deja_reussie_mais_repassee_en_attente_reste_une_tentative`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reviews.agents.quality.claims import (
    CORROBORATED,
    PLAUSIBLE,
    UNCONFIRMED,
    evaluer_corroboration,
)
from reviews.agents.quality.couverture import CouvertureFiliale, EtatSource
from reviews.agents.quality.diagnostic import Cas, diagnostiquer
from reviews.agents.quality.score import (
    calculer_score,
    poids_normalises,
    statut_confiance,
)
from reviews.llm.quality_validator import Verdict, _bool, _indexer

POIDS = {
    "coverage": 0.30, "freshness": 0.20, "completeness": 0.15,
    "consistency": 0.15, "diversity": 0.10, "reliability": 0.10,
}


def _couverture(**kw) -> CouvertureFiliale:
    base = dict(
        subsidiary_id=1, subsidiary="Orange Mali", operator="Orange",
        country="Mali", iso2="ML",
    )
    base.update(kw)
    return CouvertureFiliale(**base)


def _source(code="google_maps", **kw) -> EtatSource:
    etat = EtatSource(code=code)
    for k, v in kw.items():
        setattr(etat, k, v)
    return etat


# ===========================================================================
# 1. Couverture — la notion de « source attendue »
# ===========================================================================


def test_une_source_non_declaree_ne_compte_pas_comme_un_manque():
    """LE GARDE-FOU CENTRAL, et il coûte cher à ne pas avoir.

    86 filiales sur 135 déclarent une application App Store. Compter les 49
    autres comme « non couvertes App Store » produirait 49 fausses anomalies
    permanentes — la faute Trustpilot (43 alertes/semaine pour une absence de
    cible) reproduite à l'échelle du périmètre.
    """
    c = _couverture(
        avis_clients=50,
        sources={
            "google_maps": _source(attendue=True, avis=50, unites_deja_reussies=1),
            # Présente en base mais JAMAIS déclarée : ne doit peser sur rien.
            "app_store": _source("app_store", attendue=False, avis=0),
        },
    )
    assert c.sources_attendues == ["google_maps"]
    assert c.taux_couverture_sources == 1.0
    assert c.sources_muettes == []


def test_sans_aucune_source_declaree_la_couverture_est_indefinie_pas_nulle():
    """`None` et non 0,0 : le défaut est dans NOTRE configuration, pas dans la
    collecte, et les deux appellent un travail différent. Rendre 0,0 ferait
    remonter la filiale en tête des « pires », où elle masquerait de vraies
    pannes."""
    assert _couverture().taux_couverture_sources is None


def test_une_unite_deja_reussie_mais_repassee_en_attente_reste_une_tentative():
    """LA RÉGRESSION QUI A ÉTÉ RÉELLEMENT COMMISE, et le seul test qui l'attrape.

    `reschedule_due` repasse en `pending` toute unité dont la cadence est
    écoulée. Les six unités Google Maps de Comores Telecom sont donc `pending`
    avec `last_success_at` renseigné et `items_inserted = 0`.

    Lues sur le STATUT : « jamais tentée » -> l'agent dit d'attendre.
    Lues sur le DERNIER SUCCÈS : « interrogée et vide » -> l'agent dit de
    chercher ailleurs.

    Deux actions incompatibles sur les mêmes lignes. Le fait durable est le
    dernier succès.
    """
    etat = _source(attendue=True, unites_succes=0, unites_deja_reussies=6,
                   unites_attente=6, items_inserted=0)
    assert etat.tentee is True

    c = _couverture(sources={"google_maps": etat})
    assert c.sources_jamais_tentees == []
    assert c.sources_muettes == ["google_maps"]


def test_un_echec_isole_apres_un_succes_n_est_pas_une_panne():
    """Leçon de `collection_jobs` : seules les unités qui n'ont JAMAIS abouti
    sont inquiétantes. Une unité qui échoue aujourd'hui après avoir réussi hier
    signale une page lente, pas un collecteur mort."""
    c = _couverture(
        sources={
            "google_maps": _source(
                attendue=True, unites_deja_reussies=4, unites_jamais_reussies=1
            )
        }
    )
    assert c.sources_en_erreur == []


# ===========================================================================
# 2. Diagnostic — les cas A, B, C, D et leur ordre
# ===========================================================================


def test_filiale_sans_avis_dont_le_collecteur_echoue_est_un_cas_technique():
    """CAS A. Prioritaire sur tout le reste : tant qu'un collecteur échoue, on
    ignore ce que la source contient. Chercher ailleurs masquerait la panne."""
    c = _couverture(
        sources={
            "google_maps": _source(
                attendue=True, unites_jamais_reussies=3, unites_deja_reussies=0,
                derniere_erreur="timeout",
            )
        }
    )
    d = diagnostiquer(c)

    assert d.cas is Cas.COLLECTEUR_EN_ECHEC
    assert d.bloquant is True
    # LE POINT CRUCIAL : on n'a PAS le droit de chercher une source externe.
    assert d.enrichissable is False
    assert any(p["type"] == "erreur_collecte" for p in d.preuves)


def test_collecteur_fonctionnel_mais_vide_autorise_la_recherche_de_sources():
    """CAS B/D — le scénario de démonstration, sur les chiffres réels de
    Comores Telecom : Google Maps interrogé avec succès, zéro avis inséré, et
    42 articles de presse qui prouvent que l'entité est reconnue."""
    c = _couverture(
        subsidiary="Comores Telecom", articles_presse=42,
        sources={
            "google_maps": _source(
                attendue=True, unites_deja_reussies=6, items_inserted=0, avis=0
            )
        },
    )
    d = diagnostiquer(c)

    assert d.cas is Cas.AUCUNE_SOURCE_EXPLOITABLE
    assert d.enrichissable is True
    # La recommandation doit dire explicitement de NE PAS relancer : c'est
    # l'exigence du §5, et le réflexe naturel qu'elle corrige.
    assert "ne pas relancer" in d.recommandation.lower()
    # La presse sert de preuve d'EXISTENCE, jamais d'avis client.
    assert any(p["type"] == "preuve_existence" for p in d.preuves)
    assert "42" in d.raison


def test_un_indice_de_mapping_prime_sur_la_recherche_de_sources():
    """CAS C avant CAS D. Si la donnée est déjà collectée mais mal rangée,
    aller la chercher ailleurs ne corrige rien et ajoute une source à
    maintenir."""
    c = _couverture(
        sources={"google_maps": _source(attendue=True, unites_deja_reussies=2)}
    )
    d = diagnostiquer(
        c, indices_mapping=[{"type": "indice_mapping", "kind": "alias_manquant"}]
    )

    assert d.cas is Cas.MAPPING_SUSPECT
    assert d.enrichissable is False
    assert d.bloquant is True


def test_une_source_jamais_executee_interdit_de_conclure():
    """Le cinquième cas, absent de l'énoncé et imposé par la file de collecte.

    Mesuré : 863 unités Google Maps en attente. Conclure « aucune donnée
    n'existe » avant qu'elles aient tourné serait un diagnostic posé sur une
    collecte incomplète."""
    c = _couverture(
        sources={
            "google_maps": _source(
                attendue=True, unites_deja_reussies=0, unites_attente=5
            )
        }
    )
    d = diagnostiquer(c)

    assert d.cas is Cas.JAMAIS_TENTE
    assert d.enrichissable is False


def test_rien_de_declare_designe_notre_configuration_et_non_la_collecte():
    d = diagnostiquer(_couverture())
    assert d.cas is Cas.RIEN_DE_DECLARE
    assert "operators.json" in d.recommandation or "configuration" in d.recommandation


def test_une_panne_partielle_ne_bloque_pas_une_filiale_par_ailleurs_couverte():
    """La contrepartie du test précédent, et elle est nécessaire : si UNE source
    en panne suffisait à bloquer, une filiale correctement alimentée par ailleurs
    serait plafonnée à 30 % pour un détail. La panne partielle est déjà
    pénalisée par la composante de fiabilité."""
    c = _couverture(
        avis_clients=400,
        sources={
            "google_play": _source("google_play", attendue=True, avis=400),
            "google_maps": _source(
                attendue=True, avis=0, unites_jamais_reussies=2,
                unites_deja_reussies=0,
            ),
        },
    )
    d = diagnostiquer(c, min_reviews=10, min_sources=2)

    assert d.cas is not Cas.COLLECTEUR_EN_ECHEC
    assert d.bloquant is False
    # Mais la fiabilité, elle, doit bien enregistrer la panne.
    score = calculer_score(c, d, poids=POIDS, min_sources=2)
    assert score.valeur("reliability") < 1.0


def test_filiale_bien_pourvue_sur_plusieurs_sources_est_declaree_couverte():
    c = _couverture(
        avis_clients=400,
        sources={
            "google_maps": _source(attendue=True, avis=250, unites_deja_reussies=3),
            "google_play": _source("google_play", attendue=True, avis=150),
        },
    )
    d = diagnostiquer(c, min_reviews=10, min_sources=2)

    assert d.cas is Cas.COUVERT
    assert d.enrichissable is False


def test_volume_suffisant_sur_une_seule_source_reste_une_fragilite():
    """130 filiales sur 135 ne sont couvertes que par Google Maps. Ce n'est pas
    une anomalie — c'est une dépendance, et elle doit se voir sans devenir une
    alerte."""
    c = _couverture(
        avis_clients=300,
        sources={"google_maps": _source(attendue=True, avis=300, unites_deja_reussies=1)},
    )
    d = diagnostiquer(c, min_sources=2)

    assert d.cas is Cas.SOUS_COUVERT
    assert d.bloquant is False


# ===========================================================================
# 3. Score — explicabilité et renormalisation
# ===========================================================================


def test_une_composante_non_mesurable_est_retiree_et_les_poids_renormalises():
    """LA DÉCISION LA PLUS IMPORTANTE DU SCORE. Sans renormalisation, une
    filiale à trois composantes sur six plafonnerait mécaniquement à 55 % même
    parfaite, et son statut de confiance serait faux."""
    poids = poids_normalises(POIDS, {"coverage", "diversity"})

    assert set(poids) == {"coverage", "diversity"}
    assert sum(poids.values()) == pytest.approx(1.0)
    # Le RAPPORT entre les poids conservés est préservé : 0,30 contre 0,10.
    assert poids["coverage"] == pytest.approx(0.75)


def test_le_score_reste_borne_et_chaque_composante_porte_son_explication():
    c = _couverture(
        avis_clients=200,
        sources={
            "google_maps": _source(
                attendue=True, avis=200, unites_deja_reussies=2,
                derniere_collecte=datetime.now(timezone.utc),
            ),
            "google_play": _source("google_play", attendue=True, avis=80,
                                   derniere_collecte=datetime.now(timezone.utc)),
        },
    )
    score = calculer_score(
        c, poids=POIDS, min_reviews=10, min_sources=2,
        stats_completude={"total": 200, "avec_texte": 180, "avec_date": 200},
        cadences_minutes={"google_maps": 1440, "google_play": 360},
    )

    assert 0.0 <= score.global_score <= 1.0
    # L'énoncé exige un score EXPLICABLE : toute composante retenue doit porter
    # une phrase, sans quoi « pourquoi 42 ? » reste sans réponse.
    for nom, composante in score.composantes.items():
        if composante.valeur is not None:
            assert composante.explication, f"{nom} sans explication"
    assert sum(score.poids_appliques.values()) == pytest.approx(1.0)


def test_un_diagnostic_bloquant_plafonne_le_score_quoi_qu_il_arrive():
    """Sans ce plafond, une filiale dont le collecteur est mort depuis une
    semaine affiche un score honorable : ses avis anciens restent nombreux,
    complets et cohérents. Le score décrirait fidèlement un corpus FIGÉ, et les
    Agents 1 et 2 continueraient de raisonner dessus comme s'il vivait."""
    c = _couverture(
        avis_clients=500,
        sources={
            # Les avis datent d'avant la panne : le corpus est nombreux ET figé.
            # C'est exactement la configuration qui trompe un score naïf.
            "google_maps": _source(
                attendue=True, avis=500, unites_jamais_reussies=3,
                unites_deja_reussies=0,
                derniere_collecte=datetime.now(timezone.utc) - timedelta(days=9),
            )
        },
    )
    diag = diagnostiquer(c)
    assert diag.cas is Cas.COLLECTEUR_EN_ECHEC
    assert diag.bloquant is True

    score = calculer_score(
        c, diag, poids=POIDS, degraded_at=0.30,
        stats_completude={"total": 500, "avec_texte": 500, "avec_date": 500},
        cadences_minutes={"google_maps": 1440},
    )
    assert score.global_score <= 0.30
    assert score.statut in ("DEGRADED", "UNTRUSTED")


def test_les_seuils_de_confiance_sont_ordonnes():
    args = dict(trusted_at=0.75, acceptable_at=0.55, degraded_at=0.30)
    assert statut_confiance(0.90, **args) == "TRUSTED"
    assert statut_confiance(0.60, **args) == "ACCEPTABLE"
    assert statut_confiance(0.40, **args) == "DEGRADED"
    assert statut_confiance(0.10, **args) == "UNTRUSTED"


def test_une_configuration_de_poids_non_normalisee_est_acceptee():
    """L'énoncé demande une configuration MODIFIABLE. Un exploitant qui double
    le poids de la couverture ne doit pas avoir à recalculer les cinq autres."""
    poids = poids_normalises(
        {"coverage": 3.0, "freshness": 1.0}, {"coverage", "freshness"}
    )
    assert sum(poids.values()) == pytest.approx(1.0)
    assert poids["coverage"] == pytest.approx(0.75)


# ===========================================================================
# 4. Corroboration — les espèces, pas le volume
# ===========================================================================


def test_quarante_avis_concordants_ne_corroborent_rien_a_eux_seuls():
    """LE CŒUR DU MODULE 5. Quarante avis restent UNE espèce de preuve : ils
    peuvent décrire la même rumeur, le même fil viral, ou une panne de quartier
    prise pour une panne nationale."""
    statut, confiance = evaluer_corroboration(
        [{"source": "customer_reviews", "count": 42}]
    )
    assert statut == PLAUSIBLE
    assert confiance < 0.75


def test_deux_especes_independantes_corroborent():
    statut, confiance = evaluer_corroboration(
        [
            {"source": "customer_reviews", "count": 42},
            {"source": "news", "url": "https://x", "date": "2026-08-10"},
        ]
    )
    assert statut == CORROBORATED
    assert confiance >= 0.75


def test_une_source_officielle_confirme_a_elle_seule():
    """Une source officielle ne rapporte pas un ressenti : elle constate."""
    statut, confiance = evaluer_corroboration(
        [{"source": "official", "url": "https://arptc.gouv.cd"}]
    )
    assert statut == "CONFIRMED"
    assert confiance >= 0.9


def test_un_signal_trop_faible_reste_non_confirme():
    statut, _ = evaluer_corroboration([{"source": "customer_reviews", "count": 2}])
    assert statut == UNCONFIRMED


def test_sans_preuve_aucune_affirmation_n_est_exploitable():
    statut, confiance = evaluer_corroboration([])
    assert statut == UNCONFIRMED
    assert confiance == 0.0


# ===========================================================================
# 5. Validation LLM — le repli est un statut, jamais un rejet
# ===========================================================================


def test_une_reponse_illisible_devient_a_revoir_et_jamais_un_rejet():
    """LE GARDE-FOU LE PLUS IMPORTANT DE LA COUCHE MODÈLE. Rejeter sur une
    réponse qu'on n'a pas su lire serait la suppression silencieuse que le §22
    interdit — obtenue par accident plutôt que par décision."""
    verdict = Verdict(flag_id=1, review_id="r1")  # rien de renseigné
    assert verdict.valide is False
    assert verdict.statut() == "REVIEW_REQUIRED"


def test_une_confiance_basse_ne_suffit_pas_a_rejeter():
    verdict = Verdict(
        flag_id=1, review_id="r1", relevant=False, spam=True, confidence=0.3
    )
    assert verdict.statut(seuil_confiance=0.6) == "REVIEW_REQUIRED"


def test_un_verdict_net_et_confiant_rejette():
    verdict = Verdict(
        flag_id=1, review_id="r1", relevant=False, spam=True, confidence=0.95
    )
    assert verdict.statut() == "REJECTED"


def test_un_avis_pertinent_est_accepte():
    verdict = Verdict(
        flag_id=1, review_id="r1", relevant=True, operator_match=True,
        spam=False, duplicate=False, confidence=0.9,
    )
    assert verdict.statut() == "ACCEPTED"


def test_les_booleens_textuels_des_petits_modeles_sont_tolerés():
    """Les petits modèles rendent « true »/« oui » en chaîne malgré la consigne.
    Refuser ces formes perdrait le verdict — donc l'appel — pour une différence
    d'emballage."""
    assert _bool("true") is True
    assert _bool("Oui") is True
    assert _bool("false") is False
    assert _bool(1) is True
    assert _bool("peut-être") is None


def test_une_liste_nue_est_acceptee_comme_l_objet_demande():
    """Même défense que `semantic._index_results`, pour la même raison :
    refuser la liste nue perdrait le lot entier."""
    attendu = {1: {"relevant": True}}
    assert _indexer({"resultats": [{"relevant": True}]}) == attendu
    assert _indexer([{"relevant": True}]) == attendu


def test_le_modele_peut_renvoyer_les_avis_dans_le_desordre():
    """La numérotation explicite prime sur la position : un modèle qui rend
    l'avis 2 avant l'avis 1 ne doit pas faire attribuer les verdicts croisés."""
    indexe = _indexer([{"i": 2, "spam": True}, {"i": 1, "spam": False}])
    assert indexe[2]["spam"] is True
    assert indexe[1]["spam"] is False
