"""Tests du collecteur GDELT. Aucun appel réseau.

Porte sur le piège vérifié le 19 août 2026 : GDELT refuse une recherche par
PHRASE (entre guillemets) trop courte (« The specified phrase is too short. »)
pour les opérateurs à sigle court (MTN, Glo, e&, WE, BTC...), alors que la
même requête SANS guillemets est acceptée et rend de vrais articles.
"""

from reviews.collectors.gdelt import GDELTScraper


def _sans_init() -> GDELTScraper:
    """Contourne __init__ (settings, session HTTP) : `_build_query` ne s'en sert pas."""
    return object.__new__(GDELTScraper)


class TestConstructionRequete:
    def test_sigle_court_part_sans_guillemets(self):
        query = _sans_init()._build_query({"term": "MTN", "iso2": "NG"})
        assert query == 'MTN sourcecountry:NI'

    def test_operateur_ordinaire_reste_entre_guillemets(self):
        query = _sans_init()._build_query({"term": "Orange", "iso2": "SN"})
        assert query == '"Orange" sourcecountry:SG'

    def test_seuil_a_quatre_lettres(self):
        court = _sans_init()._build_query({"term": "BTC", "iso2": "BW"})
        long_ = _sans_init()._build_query({"term": "Uber", "iso2": "BW"})
        assert not court.startswith('"')
        assert long_.startswith('"')
