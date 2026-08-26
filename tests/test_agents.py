"""
Tests de l'agent de veille : arbitrage et mémoire.

Aucune base, aucun réseau, aucun modèle. Les deux mécanismes testés ici sont
précisément ceux qui séparent un agent d'un endpoint, et ce sont les deux
seuls dont une régression est INVISIBLE en exploitation : un arbitrage trop
laxiste noie le lecteur, une mémoire défaillante le lasse. Ni l'un ni l'autre
ne lève d'exception ni ne remplit un journal d'erreurs.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reviews.agents.arbitrage import (
    AMPLEUR_PLANCHER,
    VOLUME_PLANCHER,
    Candidat,
    arbitrer,
    retenus,
)
from reviews.storage.agent_repository import should_report


def _candidat(**kw):
    base = dict(
        level="subsidiary", key="1", label="Filiale", pays="Zambie",
        delta_negatifs=20.0, part_negatifs=50.0,
        avis_clients=100, avis_clients_avant=100,
    )
    base.update(kw)
    return Candidat(**base)


# ---------------------------------------------------------------------------
# Arbitrage : ce qui est écarté
# ---------------------------------------------------------------------------


def test_un_volume_trop_faible_ecarte_meme_une_variation_spectaculaire():
    """LE garde-fou principal, et il doit primer sur l'ampleur.

    RÉGRESSION MESURÉE SUR LES VRAIES DONNÉES : Vodacom South Africa affichait
    −75,2 points parce que sa fenêtre antérieure contenait UN avis. La
    soustraction est exacte et l'affirmation qu'elle suggère est fausse.
    Remonter ce genre de chiffre dans un briefing quotidien décrédibilise
    l'agent en une fois.

    Les volumes du cas sont exprimés RELATIVEMENT au plancher, et non en dur :
    ce test doit continuer de mesurer la règle le jour où la fenêtre — donc le
    plancher — change.
    """
    sous_le_seuil = VOLUME_PLANCHER - 2
    c = _candidat(
        delta_negatifs=39.9,
        avis_clients=VOLUME_PLANCHER * 3,
        avis_clients_avant=sous_le_seuil,
    )
    (resultat,) = arbitrer([c])

    assert resultat.retenu is False
    assert resultat.score == 0.0
    assert "volume insuffisant" in resultat.ecarte_parce_que
    # Le message doit porter les DEUX volumes : « insuffisant » sans les
    # chiffres n'apprend pas quelle fenêtre est en cause.
    assert str(sous_le_seuil) in resultat.ecarte_parce_que
    assert str(VOLUME_PLANCHER * 3) in resultat.ecarte_parce_que


def test_le_volume_se_juge_sur_la_fenetre_la_plus_pauvre():
    """Une fenêtre bien dotée ne rachète pas l'autre : le taux d'avant porte
    autant la variation que celui d'après."""
    maigre_avant = arbitrer([_candidat(avis_clients=500, avis_clients_avant=5)])[0]
    maigre_apres = arbitrer([_candidat(avis_clients=5, avis_clients_avant=500)])[0]
    assert maigre_avant.retenu is False and maigre_apres.retenu is False


def test_une_variation_sous_le_plancher_est_ecartee():
    """Le plancher reprend celui de l'alerting : un agent quotidien ne doit pas
    remonter des mouvements qu'aucune alerte n'a jugés dignes."""
    c = arbitrer([_candidat(delta_negatifs=AMPLEUR_PLANCHER - 0.1)])[0]
    assert c.retenu is False
    assert "trop faible" in c.ecarte_parce_que


def test_une_amelioration_n_est_jamais_signalee_comme_degradation():
    """Un delta négatif est une amélioration. La confondre avec une dégradation
    ferait alerter sur une bonne nouvelle."""
    c = arbitrer([_candidat(delta_negatifs=-30.0)])[0]
    assert c.retenu is False


def test_le_plancher_de_volume_se_juge_par_rapport_a_la_fenetre():
    """Un seuil de volume n'a de sens que rapporté à sa fenêtre.

    Le plancher de l'agent valait autrefois 30, aligné sur celui des synthèses
    du dashboard — mais celles-ci portent sur quatre-vingt-dix jours. Depuis
    que l'agent compare deux semaines à deux semaines, exiger 30 avis de chaque
    côté reviendrait à demander 60 avis par mois : mesuré, une seule filiale du
    parc y parvient, et l'agent se tairait presque toujours.

    Ce test verrouille l'encadrement plutôt qu'une valeur : plus exigeant que
    l'alerting rapporté à sa propre fenêtre, moins que les synthèses.
    """
    from reviews.agents.insight_agent import FENETRE_JOURS
    from reviews.llm.insights import _MIN_VOLUME_FOR_DELTA

    # L'alerting accepte 10 avis sur 7 jours ; ramené à la fenêtre de l'agent,
    # cela vaut environ 10 × 14/7 = 20. Le plancher doit rester dans un ordre
    # de grandeur comparable, sans jamais dépasser celui des synthèses.
    equivalent_alerting = 10 * FENETRE_JOURS / 7
    assert VOLUME_PLANCHER <= _MIN_VOLUME_FOR_DELTA
    assert VOLUME_PLANCHER >= equivalent_alerting / 2


# ---------------------------------------------------------------------------
# Arbitrage : ce qui fait remonter
# ---------------------------------------------------------------------------


def test_la_persistance_fait_passer_un_mouvement_modere_devant_un_pic_isole():
    """C'est la raison d'être du critère.

    Trois alertes critiques en cinq jours décrivent une dégradation installée ;
    un pic isolé plus ample est souvent un accident. Sans ce poids, l'agent ne
    signalerait jamais que des accidents.
    """
    installe = _candidat(label="Installée", delta_negatifs=22.0, alertes_recentes=3)
    isole = _candidat(label="Isolée", delta_negatifs=30.0, alertes_recentes=0)

    classement = arbitrer([isole, installe])
    assert classement[0].label == "Installée"
    assert any("dégradation installée" in r for r in classement[0].raisons)


def test_une_seule_alerte_ne_vaut_pas_persistance():
    """Une alerte, c'est le mouvement lui-même vu autrement — pas une
    répétition. La compter doublerait le même signal."""
    c = arbitrer([_candidat(alertes_recentes=1)])[0]
    assert c.score == c.delta_negatifs


def test_l_etendue_signale_un_possible_fait_national():
    """Trois filiales du même pays qui décrochent ensemble ne sont pas trois
    incidents : coupure, décision du régulateur, hausse tarifaire."""
    seule = _candidat(label="Seule", voisins_degrades=0)
    accompagnee = _candidat(label="Accompagnée", voisins_degrades=2)

    classement = arbitrer([seule, accompagnee])
    assert classement[0].label == "Accompagnée"
    assert any("fait national" in r for r in classement[0].raisons)


def test_l_etendue_ne_compte_pas_sans_pays_connu():
    """Sans pays, « d'autres filiales du même pays » ne veut rien dire — et le
    bonus s'appliquerait sur un regroupement inexistant."""
    c = arbitrer([_candidat(pays=None, voisins_degrades=3)])[0]
    assert c.score == c.delta_negatifs


def test_les_bonus_sont_plafonnes():
    """Une filiale à huit alertes ne doit pas rafler la première place devant un
    décrochage de quarante points."""
    enorme = _candidat(label="Énorme", delta_negatifs=45.0)
    bavarde = _candidat(label="Bavarde", delta_negatifs=12.0,
                        alertes_recentes=8, voisins_degrades=8)
    classement = arbitrer([bavarde, enorme])
    assert classement[0].label == "Énorme"


def test_l_arbitrage_est_deterministe():
    """Même entrée, même sortie — c'est ce qu'un LLM ne garantirait pas, et la
    raison pour laquelle ce tri lui est retiré."""
    lot = [_candidat(label=f"F{i}", delta_negatifs=10.0 + i) for i in range(6)]
    assert [c.label for c in arbitrer(list(lot))] == [c.label for c in arbitrer(list(lot))]


def test_retenus_coupe_a_la_limite_et_ignore_les_ecartes():
    """Un briefing de dix sujets n'est pas lu ; et un candidat écarté ne doit
    jamais occuper une des places disponibles."""
    lot = [_candidat(label=f"F{i}", delta_negatifs=40.0 - i) for i in range(5)]
    lot.append(_candidat(label="Écartée", avis_clients_avant=2))

    top = retenus(arbitrer(lot), 3)
    assert len(top) == 3
    assert "Écartée" not in [c.label for c in top]


# ---------------------------------------------------------------------------
# Mémoire : ne pas se répéter, mais ne pas se taire non plus
# ---------------------------------------------------------------------------


def _dernier(score, il_y_a_jours):
    return {
        "score": score,
        "created_at": datetime.now(timezone.utc) - timedelta(days=il_y_a_jours),
    }


def test_un_sujet_jamais_signale_est_toujours_dit():
    parler, raison = should_report(None, 30.0, cooldown_days=3, aggravation_points=10)
    assert parler is True and "jamais" in raison


def test_on_ne_se_repete_pas_dans_la_periode_de_refroidissement():
    """C'est ce qui empêche l'agent de devenir un bruit qu'on filtre."""
    parler, raison = should_report(
        _dernier(30.0, 1), 31.0, cooldown_days=3, aggravation_points=10
    )
    assert parler is False
    # La raison doit porter les deux notes : « déjà signalé » sans les chiffres
    # ne permet pas de vérifier que le silence est justifié.
    assert "30.0" in raison and "31.0" in raison


def test_une_aggravation_fait_reparler_malgre_le_refroidissement():
    """LA dissymétrie voulue. Sans elle, l'agent se tairait exactement quand une
    dégradation s'installe — c'est-à-dire quand il sert à quelque chose."""
    parler, raison = should_report(
        _dernier(20.0, 1), 35.0, cooldown_days=3, aggravation_points=10
    )
    assert parler is True and "aggravation" in raison


def test_une_amelioration_ne_fait_pas_reparler():
    """Reparler d'un sujet qui va MIEUX pour dire qu'il va mieux userait la même
    attention que celle réservée aux dégradations."""
    parler, _ = should_report(
        _dernier(40.0, 1), 15.0, cooldown_days=3, aggravation_points=10
    )
    assert parler is False


def test_apres_le_refroidissement_on_reparle_sans_aggravation():
    parler, raison = should_report(
        _dernier(30.0, 5), 30.0, cooldown_days=3, aggravation_points=10
    )
    assert parler is True and "5 j" in raison


def test_les_pourcentages_venus_de_postgres_sont_convertis_en_flottants():
    """RÉGRESSION VÉCUE : PostgreSQL rend les pourcentages en `Decimal`, et le
    briefing calcule « la part d'avant » par soustraction. `Decimal - float`
    lève, et l'agent entier tombait — un passage quotidien perdu pour un type.
    """
    from decimal import Decimal

    from reviews.agents.insight_agent import InsightAgent

    class _Stats:
        def movers(self, f, level, limit, min_reviews):
            return {
                "available": True,
                "degraded": [{
                    "key": 1, "label": "X", "country": "Y", "iso2": "YY",
                    "delta_negatifs": 20.0,
                    "part_negatifs": Decimal("69.6"),   # tel que rend la base
                    "avis_clients": 80, "avis_clients_avant": 40,
                }],
            }

    class _Alerts:
        def list_recent(self, **kw):
            return []

    agent = InsightAgent.__new__(InsightAgent)
    agent.stats, agent.alerts = _Stats(), _Alerts()
    candidat = agent._candidats()[0]

    assert isinstance(candidat.part_negatifs, float)
    # L'opération qui levait doit passer sans convertir de nouveau.
    assert candidat.part_negatifs - candidat.delta_negatifs == pytest.approx(49.6)


def test_l_annee_du_contexte_marche_est_celle_des_indicateurs_affiches():
    """RÉGRESSION VÉCUE : depuis l'ajout des prix — renseignés jusqu'en 2025
    quand la couverture s'arrête à 2024 — l'agent écrivait « Contexte Zambie
    (2025) : couverture 4G 91,2 % » alors que ce 91,2 % datait de 2024.

    Un chiffre juste sous une année fausse est pire qu'un chiffre absent : il
    est invérifiable, et il décrédibilise tous les autres.
    """
    from reviews.agents.arbitrage import Candidat
    from reviews.agents.insight_agent import InsightAgent

    class _Marche:
        def latest(self, iso2):
            return {
                "MOB_COV_4G|PT_POP": {"year": 2024, "value": 91.2, "variation_pct": 0.0},
                "IT_CEL_SETS|SB_10P2_HB": {"year": 2024, "value": 108.7,
                                           "variation_pct": 6.5},
                # Un prix bien plus récent, JAMAIS affiché par l'agent : c'est
                # lui qui faisait dériver l'année.
                "PRI_DO_MOB|USD": {"year": 2025, "value": 4.9, "variation_pct": 1.0},
            }

    agent = InsightAgent.__new__(InsightAgent)
    agent.market = _Marche()
    ligne = agent._contexte_marche(
        Candidat(level="subsidiary", key="1", label="Zamtel", pays="Zambie", iso2="ZM")
    )

    assert "(2024)" in ligne, f"année erronée : {ligne}"
    assert "2025" not in ligne
    assert "couverture 4G 91.2 %" in ligne


def test_l_annee_du_contexte_devient_un_intervalle_si_les_mesures_different():
    """Élire une année au hasard parmi des mesures d'années différentes
    reviendrait à en dater faussement au moins une."""
    from reviews.agents.arbitrage import Candidat
    from reviews.agents.insight_agent import InsightAgent

    class _Marche:
        def latest(self, iso2):
            return {
                "MOB_COV_4G|PT_POP": {"year": 2022, "value": 80.0, "variation_pct": None},
                "IT_BB_MOB_TRF|XB_Y": {"year": 2024, "value": 5.5, "variation_pct": None},
            }

    agent = InsightAgent.__new__(InsightAgent)
    agent.market = _Marche()
    ligne = agent._contexte_marche(
        Candidat(level="subsidiary", key="1", label="X", pays="Y", iso2="YY")
    )
    assert "(2022–2024)" in ligne


def test_un_signalement_sans_date_ne_fait_pas_taire_l_agent():
    """Donnée abîmée : le défaut sûr est de parler. Se taire sur une entité à
    cause d'une ligne de journal corrompue est une panne invisible."""
    parler, _ = should_report(
        {"score": 50.0}, 10.0, cooldown_days=3, aggravation_points=10
    )
    assert parler is True


def test_une_date_naive_est_traitee_comme_utc():
    """PostgreSQL rend des dates aware, mais un test ou un import peut produire
    une date naïve ; la soustraire à une date aware lève, et l'agent
    n'annoncerait plus rien sans que l'erreur soit visible."""
    parler, _ = should_report(
        {"score": 30.0, "created_at": datetime.now() - timedelta(days=1)},
        31.0, cooldown_days=3, aggravation_points=10,
    )
    assert parler is False
