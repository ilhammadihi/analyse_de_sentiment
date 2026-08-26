"""
Tests du collecteur NCC Nigeria et du dépôt associé.

Aucun réseau : on rejoue un extrait DU JSON RÉELLEMENT OBSERVÉ le 23 août 2026
en récupérant la page (voir la documentation de `ncc_nigeria.py`), avec
plusieurs blocs concurrents pour éprouver l'isolation par comptage
d'accolades — c'est l'endroit où une regex naïve se tromperait de bloc.
"""

import pytest

from reviews.collectors.ncc_nigeria import NccNigeriaCollector, OPERATEURS


def _page(bloc_cible: str, autre_bloc_avant: bool = True, autre_bloc_apres: bool = True) -> str:
    """Simule la page HTML : plusieurs widgets JSON à la suite, comme sur la
    vraie page (INTGSM, PORTINC… avant et après SUBSCRIBER BY OPERATOR)."""
    avant = (
        '"OTHER WIDGET":{"title":"X","series":[{"category":"INTGSM","name":"Airtel","data":[1,2]}]},'
        if autre_bloc_avant else ""
    )
    apres = (
        ',"YET ANOTHER":{"title":"Y","series":[{"category":"PORTINC","name":"MTN","data":[3]}]}'
        if autre_bloc_apres else ""
    )
    return "{" + avant + bloc_cible + apres + "}"


_BLOC_REEL = (
    '"SUBSCRIBER BY OPERATOR (GSM)":{"title":"GSM","chartType":"line",'
    '"categories":["May\\u002726","Apr\\u002726","Mar\\u002726"],'
    '"series":['
    '{"category":"SUBSGSM","name":"Airtel","data":[65450286,64670018,63629101]},'
    '{"category":"SUBSGSM","name":"T2","data":[3538021,3538021,""]},'
    '{"category":"SUBSGSM","name":"Globacom","data":[23468730,23178597,22639893]},'
    '{"category":"SUBSGSM","name":"MTN","data":[96977835,96391419,95759210]},'
    '{"category":"SUBSGSM","name":"VITEL","data":["","",""]},'
    '{"category":"SUBSGSM","name":"TOTAL","data":[189434872,187778055,185506748]}'
    '],"tableData":[]}'
)


class _Collecteur(NccNigeriaCollector):
    """Court-circuite la requête HTTP : `_fetch` rend une page synthétique."""

    def __init__(self, html: str):
        super().__init__()
        self._html = html

    def _fetch(self) -> str:
        return self._html


# ---------------------------------------------------------------------------
# Isolation du bon bloc JSON, au milieu d'autres structurellement identiques
# ---------------------------------------------------------------------------


def test_le_bloc_est_isole_par_comptage_d_accolades_pas_par_regex_globale():
    """MESURÉ SUR LA VRAIE PAGE : une quinzaine de blocs voisins partagent la
    même forme (INTGSM, PORTINC…). Une regex non-gourmande sur tout le
    document risquerait de s'arrêter à la fermeture d'un AUTRE bloc."""
    html = _page(_BLOC_REEL)
    bloc = NccNigeriaCollector._extraire_bloc(html)
    assert bloc is not None
    assert '"SUBSGSM"' in bloc
    assert '"INTGSM"' not in bloc  # le bloc voisin AVANT n'a pas fuité
    assert '"PORTINC"' not in bloc  # ni celui d'APRÈS


def test_absence_du_widget_ne_leve_pas():
    html = _page("").replace(_BLOC_REEL, "")
    assert NccNigeriaCollector._extraire_bloc(html) is None


# ---------------------------------------------------------------------------
# Conversion des mois
# ---------------------------------------------------------------------------


def test_les_libelles_de_mois_anglais_sont_convertis_en_dates():
    bloc = NccNigeriaCollector._extraire_bloc(_page(_BLOC_REEL))
    mois = NccNigeriaCollector._extraire_mois(bloc)
    assert mois == ["2026-05-01", "2026-04-01", "2026-03-01"]


# ---------------------------------------------------------------------------
# Séries par opérateur : nombres et cellules vides
# ---------------------------------------------------------------------------


def test_une_cellule_vide_est_conservee_dans_la_serie_brute():
    """La cellule vide (mois sans donnée publiée pour T2) doit rester alignée
    avec les autres mois — c'est `_nombre` qui l'écarte ensuite, pas ici."""
    bloc = NccNigeriaCollector._extraire_bloc(_page(_BLOC_REEL))
    serie = NccNigeriaCollector._extraire_serie(bloc, "T2")
    assert serie == ["3538021", "3538021", ""]


def test_un_operateur_absent_de_la_serie_rend_none():
    bloc = NccNigeriaCollector._extraire_bloc(_page(_BLOC_REEL))
    assert NccNigeriaCollector._extraire_serie(bloc, "Inconnu") is None


@pytest.mark.parametrize("brute,attendu", [
    ("65450286", 65450286.0), ("", None), ("n/a", None), (None, None),
])
def test_nombre_ecarte_les_cellules_illisibles_sans_lever(brute, attendu):
    assert NccNigeriaCollector._nombre(brute) == attendu


# ---------------------------------------------------------------------------
# Collecte bout en bout, sur les quatre opérateurs suivis
# ---------------------------------------------------------------------------


def test_collect_rend_une_ligne_par_operateur_et_par_mois_avec_donnee():
    lignes, erreurs = _Collecteur(_page(_BLOC_REEL)).collect()
    assert erreurs == []
    # 4 opérateurs x 3 mois, MOINS la cellule vide de T2 en mars = 11
    assert len(lignes) == 11

    airtel_mai = next(
        l for l in lignes if l["operator_code"] == "airtel" and l["period"] == "2026-05-01"
    )
    assert airtel_mai["value"] == 65450286.0
    assert airtel_mai["iso2"] == "NG"
    assert airtel_mai["frequency"] == "monthly"
    assert airtel_mai["source"] == "ncc_nigeria"

    # TOTAL et VITEL ne sont pas des opérateurs suivis par `dim_operator`.
    codes = {l["operator_code"] for l in lignes}
    assert codes == set(OPERATEURS.values())


def test_collect_signale_une_erreur_lisible_si_le_widget_disparait():
    lignes, erreurs = _Collecteur("{}").collect()
    assert lignes == []
    assert len(erreurs) == 1
    assert "introuvable" in erreurs[0]
