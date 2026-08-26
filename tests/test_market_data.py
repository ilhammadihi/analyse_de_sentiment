"""
Tests du collecteur d'indicateurs de marché.

Aucun réseau, aucune base : on éprouve la NORMALISATION, c'est-à-dire l'endroit
où les deux incidents rencontrés en collecte réelle se sont produits. Les deux
sont invisibles à l'exécution — l'un affiche un chiffre faux, l'autre fait
échouer un lot entier — et aucun ne serait rattrapé par un test d'intégration
qui se contenterait de compter les lignes.
"""

import pytest

from reviews.collectors.market_data import (
    ANNEE_PLANCHER,
    INDICATEURS,
    MarketDataCollector,
    libelle,
)


def _obs(**kw):
    """Observation brute, telle que la rend la source, tous axes au total."""
    base = {
        "INDICATOR": "IT_CEL_SETS",
        "REF_AREA": "MAR",
        "TIME_PERIOD": "2024",
        "OBS_VALUE": "58286168",
        "UNIT_MEASURE": "SB",
        "SEX": "_T",
        "AGE": "_T",
        "URBANISATION": "_T",
        "COMP_BREAKDOWN_1": "_Z",
        "COMP_BREAKDOWN_2": "_Z",
        "COMP_BREAKDOWN_3": "_Z",
    }
    base.update(kw)
    return base


def _norm(obs, country_id=1, plancher=ANNEE_PLANCHER):
    return MarketDataCollector._normaliser(obs, country_id, plancher)


# ---------------------------------------------------------------------------
# Ventilations : le piège du chiffre faux
# ---------------------------------------------------------------------------


def test_seuls_les_totaux_sont_retenus():
    """MESURÉ SUR LA SOURCE, et c'est le piège le plus grave.

    `IT_NET_USER` rend pour le Maroc 2012 HUIT observations sous la même unité
    et la même année : le total (55,4 %), les femmes (45,8 %), les hommes
    (65,4 %), puis des tranches d'âge. Sans ce filtre, l'écran afficherait
    « 65,4 % d'utilisateurs d'Internet au Maroc » — le chiffre des hommes,
    présenté comme celui du pays, selon le seul ordre d'arrivée.
    """
    assert _norm(_obs()) is not None
    assert _norm(_obs(SEX="F")) is None
    assert _norm(_obs(SEX="M")) is None
    assert _norm(_obs(AGE="Y15T24")) is None
    assert _norm(_obs(URBANISATION="U")) is None
    assert _norm(_obs(COMP_BREAKDOWN_1="PREPAID")) is None


def test_un_axe_absent_de_la_reponse_ne_disqualifie_pas_l_observation():
    """La source n'envoie pas toujours tous les axes. Traiter une clé manquante
    comme une ventilation viderait la collecte sans erreur visible."""
    partiel = _obs()
    del partiel["COMP_BREAKDOWN_3"]
    del partiel["URBANISATION"]
    assert _norm(partiel) is not None


# ---------------------------------------------------------------------------
# Unités : deux chiffres différents sous le même indicateur
# ---------------------------------------------------------------------------


def test_l_unite_est_conservee_car_elle_distingue_deux_mesures():
    """`IT_CEL_SETS` rend pour le Maroc 2024 à la fois 58 286 168 abonnements
    (`SB`) et 153,06 pour 100 habitants (`SB_10P2_HB`). Perdre l'unité, c'est
    afficher « 153 abonnés au Maroc »."""
    absolu = _norm(_obs(UNIT_MEASURE="SB", OBS_VALUE="58286168"))
    taux = _norm(_obs(UNIT_MEASURE="SB_10P2_HB", OBS_VALUE="153.05726"))

    assert absolu["unit"] == "SB" and absolu["value"] == 58286168.0
    assert taux["unit"] == "SB_10P2_HB" and taux["value"] == pytest.approx(153.06, abs=0.01)
    # Même pays, même indicateur, même année : seule l'unité les sépare.
    assert (absolu["country_id"], absolu["indicator"], absolu["year"]) == (
        taux["country_id"], taux["indicator"], taux["year"]
    )


def test_une_unite_absente_recoit_un_marqueur_plutot_que_none():
    """La colonne est NOT NULL et fait partie de la clé primaire : une unité
    nulle ferait échouer l'insertion de la ligne entière."""
    sans = _obs()
    del sans["UNIT_MEASURE"]
    assert _norm(sans)["unit"] == "_Z"


# ---------------------------------------------------------------------------
# Robustesse de la normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "champ,valeur",
    [("OBS_VALUE", None), ("OBS_VALUE", ""), ("TIME_PERIOD", None),
     ("OBS_VALUE", "n/a"), ("TIME_PERIOD", "inconnu")],
)
def test_une_observation_inexploitable_est_ecartee_sans_lever(champ, valeur):
    """Une valeur illisible ne doit pas interrompre la collecte des 485 autres
    appels : elle est ignorée, pas propagée."""
    assert _norm(_obs(**{champ: valeur})) is None


def test_les_annees_anterieures_au_plancher_sont_ecartees():
    """Le dashboard raisonne sur des tendances récentes ; charger 2000 alourdit
    la base sans servir un écran."""
    assert _norm(_obs(TIME_PERIOD=str(ANNEE_PLANCHER - 1))) is None
    assert _norm(_obs(TIME_PERIOD=str(ANNEE_PLANCHER))) is not None


def test_le_libelle_retombe_sur_le_code_si_l_indicateur_est_inconnu():
    """Un indicateur ajouté à la source mais pas au catalogue doit rester
    affichable : afficher son code vaut mieux qu'afficher « None »."""
    assert libelle("IT_CEL_SETS") == INDICATEURS["IT_CEL_SETS"]
    assert libelle("INDICATEUR_INEDIT") == "INDICATEUR_INEDIT"


def test_toutes_les_unites_presentes_en_base_sont_traduites():
    """Sans traduction, « Abonnements mobiles : 153,1 SB_10P2_HB » se lit comme
    un nombre d'abonnés alors que c'est un taux pour 100 habitants — les deux
    lignes de `IT_CEL_SETS` ne se distinguent QUE par l'unité.

    La liste est celle réellement observée après collecte des 54 pays : si la
    source en introduit une nouvelle, ce test ne la verra pas, mais
    `unite_libelle` la rendra telle quelle plutôt que de mentir.
    """
    from reviews.collectors.market_data import UNITES, unite_libelle

    observees = {"SB", "SB_10P2_HB", "PT_POP", "GB_SB", "XB_Y"}
    assert observees <= set(UNITES), "unité vue en base mais non traduite"
    assert unite_libelle("UNITE_INEDITE") == "UNITE_INEDITE"


# ---------------------------------------------------------------------------
# Dédoublonnage avant écriture
# ---------------------------------------------------------------------------


def test_le_depot_dedoublonne_avant_d_envoyer_a_postgres():
    """RÉGRESSION VÉCUE À LA PREMIÈRE COLLECTE RÉELLE.

    PostgreSQL refuse qu'un `ON CONFLICT DO UPDATE` touche deux fois la même
    ligne dans une seule commande. Deux lignes de même clé dans le lot ont fait
    échouer LE LOT ENTIER — 177 mesures perdues à cause d'une seule. Le
    dédoublonnage en Python est donc une condition de survie du lot, pas une
    optimisation.
    """
    from reviews.storage.market_repository import MarketRepository

    envoyes = {}

    class _Curseur:
        def execute(self, sql, params=None):
            pass

    class _Db:
        def cursor(self, dict_rows=False):
            from contextlib import contextmanager

            @contextmanager
            def _o():
                yield _Curseur()

            return _o()

    def _capture(cur, sql, valeurs):
        envoyes["valeurs"] = valeurs

    import reviews.storage.market_repository as mod

    original = mod.execute_values
    mod.execute_values = _capture
    try:
        lot = [
            {"country_id": 1, "indicator": "X", "unit": "SB", "year": 2024, "value": 1.0},
            {"country_id": 1, "indicator": "X", "unit": "SB", "year": 2024, "value": 2.0},
            {"country_id": 1, "indicator": "X", "unit": "SB_10P2_HB", "year": 2024, "value": 3.0},
        ]
        n = MarketRepository(_Db()).upsert(lot)
    finally:
        mod.execute_values = original

    assert n == 2, "les deux lignes de même clé doivent être fusionnées"
    valeurs = {v[4] for v in envoyes["valeurs"]}
    # Le dernier arrivé fait foi : c'est la valeur la plus récemment publiée.
    assert 2.0 in valeurs and 1.0 not in valeurs
    assert 3.0 in valeurs, "une unité différente reste une ligne distincte"
