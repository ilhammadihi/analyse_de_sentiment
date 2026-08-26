"""
Tests du collecteur NCA Ghana.

Aucun réseau : on rejoue en mémoire la STRUCTURE RÉELLE que rend
`pdfplumber.extract_tables()` sur l'Annexe A du bulletin — l'opérateur
n'apparaît QUE sur la ligne « Subscriptions », `None` sur la ligne
« Market Share (%) » qui suit — vérifiée le 24 août 2026 en téléchargeant
le bulletin publié par le NCA.
"""

from reviews.collectors.nca_ghana import NcaGhanaCollector, OPERATEURS


def _table_reelle() -> list[list]:
    """Extrait de `Table 2 : Mobile Voice Subscriptions and Market Share per
    Operator`, tel que rendu par `pdfplumber.extract_tables()`."""
    return [
        ["Mobile Network Operator", None, "Q2 2024", "Q3 2024", "Q4 2024", "Q1 2025", "Q2 2025"],
        ["MTN", "Subscriptions", "28,381,349", "28,885,932", "28,408,649", "29,521,441", "30,264,594"],
        [None, "Market Share (%)", "75.45%", "75.09%", "73.96%", "73.86%", "73.67%"],
        ["Telecel", "Subscriptions", "6,373,676", "6,678,979", "6,968,464", "7,296,855", "7,628,251"],
        [None, "Market Share (%)", "16.94%", "17.36%", "18.14%", "18.26%", "18.57%"],
        ["AT", "Subscriptions", "2,861,582", "2,904,357", "3,031,599", "3,152,005", "3,186,038"],
        [None, "Market Share (%)", "7.61%", "7.55%", "7.89%", "7.89%", "7.76%"],
        ["Total Industry Subscription", None, "37,616,607", "38,469,268", "38,408,712", "39,970,301", "41,078,883"],
    ]


class _Collecteur(NcaGhanaCollector):
    def __init__(self, table):
        super().__init__()
        self._table = table

    def _fetch(self):
        return object()  # jamais utilisé, _trouver_table est aussi remplacé

    def _trouver_table(self, pdf):
        return self._table


# ---------------------------------------------------------------------------
# L'opérateur courant se propage depuis la ligne « Subscriptions »
# ---------------------------------------------------------------------------


def test_le_nom_d_operateur_se_propage_a_la_ligne_market_share_qui_suit():
    """La cellule opérateur n'apparaît QUE sur la ligne « Subscriptions » —
    la ligne « Market Share (%) » suivante hérite `None` en colonne 0. Un
    parseur qui perdrait l'opérateur courant à cette ligne attribuerait la
    valeur suivante au mauvais opérateur, ou la perdrait."""
    lignes, erreurs = _Collecteur(_table_reelle()).collect()
    assert erreurs == []
    codes = {l["operator_code"] for l in lignes}
    assert codes == set(OPERATEURS.values())


def test_seule_la_ligne_subscriptions_est_retenue_jamais_market_share():
    """« Market Share (%) » est un pourcentage, pas un décompte d'abonnés —
    le mélanger produirait des valeurs à deux chiffres dans la même colonne
    que des dizaines de millions."""
    lignes, _ = _Collecteur(_table_reelle()).collect()
    mtn_q2_2025 = next(l for l in lignes if l["operator_code"] == "mtn" and l["period"] == "2025-06-30")
    assert mtn_q2_2025["value"] == 30_264_594.0


def test_la_ligne_total_industrie_n_est_pas_prise_pour_un_operateur():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    assert all(l["operator_code"] != "total_industry_subscription" for l in lignes)
    assert len(lignes) == 15  # 3 opérateurs x 5 trimestres


# ---------------------------------------------------------------------------
# Conversion des trimestres et des nombres
# ---------------------------------------------------------------------------


def test_les_trimestres_anglophones_sont_convertis_en_dernier_jour_du_trimestre():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    periodes = {l["period"] for l in lignes}
    assert periodes == {"2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"}


def test_les_virgules_milliers_anglophones_sont_retirees():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    at_q2_2024 = next(l for l in lignes if l["operator_code"] == "airteltigo" and l["period"] == "2024-06-30")
    assert at_q2_2024["value"] == 2_861_582.0


def test_metadonnees_de_chaque_ligne():
    lignes, _ = _Collecteur(_table_reelle()).collect()
    l = lignes[0]
    assert l["iso2"] == "GH"
    assert l["metric"] == "abonnes_voix_mobile"
    assert l["frequency"] == "quarterly"
    assert l["source"] == "nca_ghana"


# ---------------------------------------------------------------------------
# Robustesse
# ---------------------------------------------------------------------------


def test_absence_de_la_table_est_signalee_sans_lever():
    lignes, erreurs = _Collecteur(None).collect()
    assert lignes == []
    assert len(erreurs) == 1
    assert "introuvable" in erreurs[0]


# ---------------------------------------------------------------------------
# Alignement des trimestres — même piège que CELTIIS pour ARCEP Bénin
# ---------------------------------------------------------------------------


def test_un_operateur_avec_une_cellule_vide_en_tete_est_aligne_sur_les_derniers_trimestres():
    """RÉGRESSION ÉVITÉE À LA SOURCE : un opérateur sans donnée publiée sur
    les trimestres les plus anciens de la fenêtre glissante (ex. un nouvel
    entrant) rend moins de valeurs que de trimestres d'en-tête. Zipper
    naïvement `zip(trimestres, valeurs)` attribuerait alors sa première
    valeur RÉELLE (Q4 2024) au premier trimestre de l'en-tête (Q2 2024) — même
    piège que CELTIIS pour ARCEP Bénin, corrigé ici par le même alignement
    depuis la droite."""
    table = [
        ["Mobile Network Operator", None, "Q2 2024", "Q3 2024", "Q4 2024", "Q1 2025", "Q2 2025"],
        ["MTN", "Subscriptions", "1,000,000", "1,100,000", "1,200,000", "1,300,000", "1,400,000"],
        [None, "Market Share (%)", "50%", "50%", "48%", "48%", "47%"],
        # AT n'a de donnée publiée que sur les TROIS derniers trimestres.
        ["AT", "Subscriptions", "500,000", "550,000", "600,000"],
        [None, "Market Share (%)", "25%", "25%", "24%"],
    ]
    lignes, _ = _Collecteur(table).collect()
    at = {l["period"]: l["value"] for l in lignes if l["operator_code"] == "airteltigo"}
    assert at == {
        "2024-12-31": 500_000.0,
        "2025-03-31": 550_000.0,
        "2025-06-30": 600_000.0,
    }
