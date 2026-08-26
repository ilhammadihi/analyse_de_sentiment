"""
Tests du collecteur ANRT Maroc.

Aucun réseau : on rejoue en mémoire la STRUCTURE RÉELLE du classeur XLSX
(légende lettrée A→N sur les 14 premières lignes, ligne d'en-tête « Période (à
fin) », données trimestrielles), vérifiée le 23 août 2026 en téléchargeant le
jeu de données publié par l'ANRT.
"""

import openpyxl
import pytest

from reviews.collectors.anrt_maroc import AnrtMarocCollector, OPERATEURS, colonnes_lettre


def _classeur() -> openpyxl.Workbook:
    """Construit un classeur miniature dans la forme réelle : légende A..F,
    une ligne vide, l'en-tête, puis des données — colonnes A/B non « par
    opérateur » incluses pour éprouver le filtre sur la légende."""
    wb = openpyxl.Workbook()
    ws = wb.active
    legende = [
        ("A", "Parc téléphonie mobile postpayé_ITISSALAT AL-MAGHRIB (en milliers)"),
        ("B", "Parc téléphonie mobile postpayé global (en milliers)"),
        ("C", "Parc téléphonie mobile_ITISSALAT AL-MAGHRIB (en milliers)"),
        ("D", "Parc téléphonie mobile_MEDI TELECOM (en milliers)"),
        ("E", "Parc téléphonie mobile_WANA CORPORATE (en milliers)"),
        ("F", "Parc téléphonie mobile global (en milliers)"),
    ]
    for row in legende:
        ws.append(row)
    ws.append([])  # ligne vide, comme sur la vraie feuille

    lettres = [l for l, _ in legende]
    ws.append(["Période (à fin)"] + lettres)
    # T4-2005 : WANA CORPORATE n'existe pas encore -> "NA"
    donnees = {
        "T4-2005": {"A": 300, "B": 600, "C": 8000, "D": 4000, "E": "NA", "F": 12000},
        "T2-2026": {"A": 2800, "B": 8600, "C": 18430, "D": 20296, "E": 20095, "F": 58821},
    }
    for periode, valeurs in donnees.items():
        ws.append([periode] + [valeurs.get(l) for l in lettres])
    return wb


class _Collecteur(AnrtMarocCollector):
    def __init__(self, wb: openpyxl.Workbook):
        super().__init__()
        self._wb = wb

    def _fetch(self):
        return self._wb


# ---------------------------------------------------------------------------
# Lecture de la légende : seules les colonnes « par opérateur » sont retenues
# ---------------------------------------------------------------------------


def test_seules_les_colonnes_parc_total_par_operateur_sont_retenues():
    """Les colonnes postpayé/prépayé séparées et le total global ne doivent
    PAS être confondues avec un parc par opérateur — voir la documentation du
    module sur ce piège."""
    lignes, erreurs = _Collecteur(_classeur()).collect()
    assert erreurs == []
    codes = {l["operator_code"] for l in lignes}
    assert codes == set(OPERATEURS.values())


def test_colonnes_lettre_retrouve_la_bonne_colonne():
    legende = {"C": "Parc téléphonie mobile_ITISSALAT AL-MAGHRIB (en milliers)",
               "A": "Parc téléphonie mobile postpayé_ITISSALAT AL-MAGHRIB (en milliers)"}
    assert colonnes_lettre(legende, "ITISSALAT AL-MAGHRIB") == "C"
    assert colonnes_lettre(legende, "OPERATEUR INCONNU") is None


# ---------------------------------------------------------------------------
# Conversion des périodes et des valeurs
# ---------------------------------------------------------------------------


def test_les_trimestres_sont_convertis_en_dernier_jour_du_trimestre():
    """La source mesure « à fin » de trimestre (en-tête « Période (à fin) »,
    vérifié dans le classeur) — le premier jour ferait paraître la donnée
    jusqu'à deux mois plus ancienne qu'elle ne l'est."""
    lignes, _ = _Collecteur(_classeur()).collect()
    periodes = {l["period"] for l in lignes}
    assert "2026-06-30" in periodes  # T2-2026
    assert "2005-12-31" in periodes  # T4-2005, pour les opérateurs déjà actifs


def test_une_cellule_na_est_ecartee_sans_fausser_les_autres_operateurs():
    """T4-2005 : WANA CORPORATE n'existait pas encore ('NA' dans la source).
    Les deux autres opérateurs doivent rester présents pour cette période."""
    lignes, _ = _Collecteur(_classeur()).collect()
    t4_2005 = [l for l in lignes if l["period"] == "2005-12-31"]
    codes = {l["operator_code"] for l in t4_2005}
    assert codes == {"maroc_telecom", "orange"}
    assert "inwi" not in codes


def test_les_valeurs_sont_multipliees_par_mille():
    """La source exprime tout « en milliers », littéralement dans le libellé
    de chaque colonne — un chiffre non converti sous-évaluerait le marché
    marocain d'un facteur 1000."""
    lignes, _ = _Collecteur(_classeur()).collect()
    iam_2026 = next(
        l for l in lignes if l["operator_code"] == "maroc_telecom" and l["period"] == "2026-06-30"
    )
    assert iam_2026["value"] == 18_430_000.0


def test_metadonnees_de_chaque_ligne():
    lignes, _ = _Collecteur(_classeur()).collect()
    l = lignes[0]
    assert l["iso2"] == "MA"
    assert l["metric"] == "abonnes_mobile"
    assert l["frequency"] == "quarterly"
    assert l["source"] == "anrt_maroc"


# ---------------------------------------------------------------------------
# Robustesse : structure inattendue
# ---------------------------------------------------------------------------


def test_absence_de_colonne_par_operateur_est_signalee_sans_lever():
    wb = _classeur()
    wb.active.delete_rows(1, 6)  # retire toute la légende
    lignes, erreurs = _Collecteur(wb).collect()
    assert lignes == []
    assert len(erreurs) == 1
