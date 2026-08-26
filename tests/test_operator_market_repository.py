"""
Tests du dépôt des indicateurs de marché par opérateur.

Aucune vraie base : on rejoue les DEUX pièges déjà rencontrés sur
`MarketRepository` (voir sa documentation et `test_market_data.py`), plus
celui propre à cette table — résoudre (operator_code, iso2) en subsidiary_id
sans faire échouer tout le lot si une filiale est inconnue.
"""

from contextlib import contextmanager

from reviews.storage.operator_market_repository import OperatorMarketRepository


class _Curseur:
    """Simule un curseur : `execute` capture la requête, `fetchall` rend le
    catalogue (operator_code, iso2) -> subsidiary_id fourni au test."""

    def __init__(self, catalogue):
        self._catalogue = catalogue
        self._derniere = None

    def execute(self, sql, params=None):
        self._derniere = sql

    def fetchall(self):
        if "dim_subsidiary" in self._derniere and "operator_market_indicators" not in self._derniere:
            return [(code, iso2, sid) for (code, iso2), sid in self._catalogue.items()]
        return []


class _Db:
    def __init__(self, catalogue):
        self._catalogue = catalogue

    def cursor(self, dict_rows=False):
        @contextmanager
        def _o():
            yield _Curseur(self._catalogue)
        return _o()


_CATALOGUE = {("mtn", "NG"): 42, ("airtel", "NG"): 43}


def _capturer_execute_values(monkeypatch):
    envoyes = {}

    def _capture(cur, sql, valeurs):
        envoyes["valeurs"] = valeurs

    import reviews.storage.operator_market_repository as mod
    monkeypatch.setattr(mod, "execute_values", _capture)
    return envoyes


def test_une_filiale_inconnue_est_ecartee_sans_faire_echouer_le_lot(monkeypatch):
    envoyes = _capturer_execute_values(monkeypatch)
    lignes = [
        {"operator_code": "mtn", "iso2": "NG", "metric": "abonnes_gsm",
         "period": "2026-05-01", "frequency": "monthly", "value": 1.0,
         "source": "ncc_nigeria", "source_url": None},
        {"operator_code": "operateur_inconnu", "iso2": "NG", "metric": "abonnes_gsm",
         "period": "2026-05-01", "frequency": "monthly", "value": 2.0,
         "source": "ncc_nigeria", "source_url": None},
    ]
    n = OperatorMarketRepository(_Db(_CATALOGUE)).upsert(lignes)

    assert n == 1
    assert len(envoyes["valeurs"]) == 1
    assert envoyes["valeurs"][0][0] == 42  # subsidiary_id de MTN Nigeria


def test_le_depot_dedoublonne_avant_d_envoyer_a_postgres(monkeypatch):
    """RÉGRESSION ÉVITÉE À LA SOURCE : même piège que `MarketRepository`
    (`ON CONFLICT DO UPDATE` refuse de toucher deux fois la même ligne dans
    une seule commande) — voir `test_market_data.py`."""
    envoyes = _capturer_execute_values(monkeypatch)
    lignes = [
        {"operator_code": "mtn", "iso2": "NG", "metric": "abonnes_gsm",
         "period": "2026-05-01", "frequency": "monthly", "value": 1.0,
         "source": "ncc_nigeria", "source_url": None},
        {"operator_code": "mtn", "iso2": "NG", "metric": "abonnes_gsm",
         "period": "2026-05-01", "frequency": "monthly", "value": 2.0,
         "source": "ncc_nigeria", "source_url": None},
    ]
    n = OperatorMarketRepository(_Db(_CATALOGUE)).upsert(lignes)

    assert n == 1, "les deux lignes de même clé doivent être fusionnées"
    assert envoyes["valeurs"][0][4] == 2.0, "le dernier arrivé fait foi"


def test_lot_vide_ne_touche_pas_la_base(monkeypatch):
    envoyes = _capturer_execute_values(monkeypatch)
    n = OperatorMarketRepository(_Db(_CATALOGUE)).upsert([])
    assert n == 0
    assert "valeurs" not in envoyes


# ---------------------------------------------------------------------------
# latest_by_subsidiary : filtre "année en cours" — demandé le 24 août 2026
# ---------------------------------------------------------------------------


class _CurseurLecture:
    """Simule le curseur de `latest_by_subsidiary` : rend directement les
    lignes fournies au test, en dict — `dict(ligne)` fonctionne déjà sur un
    dict, pas besoin de simuler des tuples nommés psycopg2."""

    def __init__(self, lignes):
        self._lignes = lignes

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._lignes


class _DbLecture:
    def __init__(self, lignes):
        self._lignes = lignes

    def cursor(self, dict_rows=False):
        @contextmanager
        def _o():
            yield _CurseurLecture(self._lignes)
        return _o()


def _ligne(subsidiary_id, annee, mois=6, jour=15, **overrides):
    from datetime import date as _date

    base = {
        "subsidiary_id": subsidiary_id, "subsidiary": f"Filiale {subsidiary_id}",
        "operator_id": 1, "operator": "Op", "iso2": "NG", "country": "Nigeria",
        "metric": "abonnes_gsm", "period": _date(annee, mois, jour),
        "frequency": "monthly", "value": 1.0, "source": "src", "source_url": None,
        "valeur_precedente": None, "periode_precedente": None,
    }
    base.update(overrides)
    return base


def test_recent_only_ecarte_les_filiales_dont_la_derniere_mesure_est_ancienne():
    """RÉGRESSION À NE PAS PERDRE : demandé explicitement — le dashboard ne
    doit plus montrer un opérateur dont la dernière donnée n'est pas de
    l'année en cours, même mêlée à des opérateurs à jour."""
    from datetime import date as _date

    annee_courante = _date.today().year
    lignes = [
        _ligne(1, annee_courante),        # à jour : conservée
        _ligne(2, annee_courante - 1),    # année dernière : écartée
        _ligne(3, annee_courante - 2),    # deux ans : écartée
    ]
    resultat = OperatorMarketRepository(_DbLecture(lignes)).latest_by_subsidiary()

    ids = {l["subsidiary_id"] for l in resultat}
    assert ids == {1}


def test_recent_only_false_rend_tout_sans_filtrer():
    from datetime import date as _date

    lignes = [_ligne(1, _date.today().year), _ligne(2, 2020)]
    resultat = OperatorMarketRepository(_DbLecture(lignes)).latest_by_subsidiary(
        recent_only=False
    )

    ids = {l["subsidiary_id"] for l in resultat}
    assert ids == {1, 2}


def test_recent_only_ne_casse_pas_le_calcul_de_variation():
    """Le filtre s'applique APRÈS le calcul de variation, pas avant — une
    filiale à jour garde sa variation même si la ligne précédente, elle,
    aurait été écartée si elle avait dû être évaluée seule."""
    from datetime import date as _date

    annee_courante = _date.today().year
    lignes = [_ligne(1, annee_courante, valeur_precedente=50.0)]
    lignes[0]["value"] = 100.0
    resultat = OperatorMarketRepository(_DbLecture(lignes)).latest_by_subsidiary()

    assert resultat[0]["variation_pct"] == 100.0
