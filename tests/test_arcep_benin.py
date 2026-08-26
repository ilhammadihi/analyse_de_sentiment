"""
Tests du collecteur ARCEP Bénin.

Aucun réseau : on rejoue en mémoire la STRUCTURE RÉELLE que rend
`pdfplumber.extract_tables()` sur le tableau de bord annuel — colonnes
fusionnées comptées comme cellules `None`/`''` supplémentaires, opérateur
entré tardivement (CELTIIS) avec moins de valeurs que d'années — vérifiée le
23 août 2026 en téléchargeant le PDF publié par l'ARCEP.
"""

from reviews.collectors.arcep_benin import ArcepBeninCollector, OPERATEURS


def _table_reelle() -> list[list]:
    """Extrait de `Tableau 1 : Abonnements mobiles actifs`, tel que rendu par
    `pdfplumber.extract_tables()` — cellules fusionnées incluses."""
    return [
        ["DESIGNATIONS", "", "2020", "", "", "2021", "", "2022", "2023", "", "2024"],
        ["SPACETEL BENIN", "", "6 459 220", None, "7 599 404", None,
         "8 470 047", None, "8 840 581", None, "8 860 782"],
        ["MOOV AFRICA BENIN", "", "4 681 671", None, "5 132 378", None,
         "5 480 213", None, "5 746 786", None, "5 705 177"],
        ["CELTIIS", "", "", "", "", "", "599 727", None, "1 786 519", None, "3 648 522"],
        ["Total Abonnements mobiles", "", "11 140 891", None, "12 731 782", None,
         "14 549 987", None, "16 373 886", None, "18 214 481"],
        ["actifs", None, None, None, None, None, None, None, None, None, None],
    ]


class _Collecteur(ArcepBeninCollector):
    def __init__(self, table):
        super().__init__()
        self._table = table

    def _fetch(self):
        return object()  # jamais utilisé, _trouver_table est aussi remplacé

    def _trouver_table(self, pdf):
        return self._table


# ---------------------------------------------------------------------------
# Nettoyage des cellules fusionnées
# ---------------------------------------------------------------------------


def test_les_cellules_fusionnees_sont_filtrees_avant_tout_alignement():
    lignes, erreurs = _Collecteur(_table_reelle()).collect()
    assert erreurs == []
    codes = {l["operator_code"] for l in lignes}
    assert codes == set(OPERATEURS.values())


def test_les_lignes_total_et_continuation_sont_ignorees():
    """« Total Abonnements mobiles » et sa continuation « actifs » (l'étiquette
    est coupée sur deux lignes du tableau source) ne doivent pas être prises
    pour un quatrième opérateur."""
    lignes, _ = _Collecteur(_table_reelle()).collect()
    noms = {l["operator_code"] for l in lignes}
    assert "total" not in noms and len(noms) == 3


# ---------------------------------------------------------------------------
# Alignement depuis la droite pour un opérateur entré tardivement
# ---------------------------------------------------------------------------


def test_un_operateur_tardif_est_aligne_sur_les_dernieres_annees_pas_les_premieres():
    """RÉGRESSION ÉVITÉE À LA SOURCE : CELTIIS ne rend que 3 valeurs après
    filtrage des cellules vides, pour 5 années dans l'en-tête. Un zip naïf
    (`zip(annees, valeurs)`) attribuerait son parc 2022 à l'année 2020."""
    lignes, _ = _Collecteur(_table_reelle()).collect()
    celtiis = {l["period"]: l["value"] for l in lignes if l["operator_code"] == "celtiis"}
    assert celtiis == {
        "2022-12-31": 599_727.0,
        "2023-12-31": 1_786_519.0,
        "2024-12-31": 3_648_522.0,
    }
    assert "2020-12-31" not in celtiis and "2021-12-31" not in celtiis


def test_un_operateur_present_toutes_les_annees_couvre_toute_la_serie():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    mtn = {l["period"] for l in lignes if l["operator_code"] == "mtn"}
    assert mtn == {f"{a}-12-31" for a in range(2020, 2025)}


# ---------------------------------------------------------------------------
# Valeurs et métadonnées
# ---------------------------------------------------------------------------


def test_les_espaces_insecables_sont_retires_du_nombre():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    mtn_2024 = next(l for l in lignes if l["operator_code"] == "mtn" and l["period"] == "2024-12-31")
    assert mtn_2024["value"] == 8_860_782.0


def test_metadonnees_de_chaque_ligne():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    l = lignes[0]
    assert l["iso2"] == "BJ"
    assert l["metric"] == "abonnes_mobiles_actifs"
    assert l["frequency"] == "annual"
    assert l["source"] == "arcep_benin"


# ---------------------------------------------------------------------------
# Robustesse
# ---------------------------------------------------------------------------


def test_absence_de_la_table_est_signalee_sans_lever():
    lignes, erreurs = _Collecteur(None).collect()
    assert lignes == []
    assert len(erreurs) == 1
    assert "introuvable" in erreurs[0]
