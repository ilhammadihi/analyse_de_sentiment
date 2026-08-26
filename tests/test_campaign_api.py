"""
Agent 2 exposé au web : périmètre, dossier en treize sections, routes.

CE QUE CES TESTS PROTÈGENT
    L'agent lui-même est déjà couvert par `test_campaign_agent.py` (80 tests).
    Ce module ne teste QUE la couche ajoutée pour l'application web, et
    exclusivement les fautes qui ne lèveraient aucune erreur :

      - un périmètre de sélecteur mal traduit : la campagne serait bâtie sur une
        AUTRE filiale que celle affichée à l'écran, avec des chiffres justes ;
      - un périmètre imposé qui écrase l'intention : « pour les jeunes » ne
        serait plus réfuté, et l'utilisateur croirait l'âge pris en compte ;
      - un dossier qui invente un impact prévu : un nombre faux au milieu d'un
        document exact, donc indétectable.

AUCUN APPEL DE MODÈLE, AUCUNE BASE. Les doubles rendent ce qu'on leur donne.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reviews.agents.campagne import JOURS_DEFAUT, Brief, brief_vide
from reviews.agents.campaign_agent import _imposer_perimetre
from reviews.agents.dossier import DossierDeCampagne, TITRES
from reviews.api.routes.campaigns import _perimetre_des_selecteurs
from reviews.storage.filters import StatsFilter


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _campagne(**kw):
    """Une campagne telle que `CampaignRepository.par_id` la rend."""
    base = {
        "campaign_id": 7,
        "entity_level": "subsidiary",
        "entity_key": "12",
        "entity_label": "Orange Mali",
        "segment": "insatisfaits_motif",
        "objective": "reassurance",
        "channel": "reponse_avis",
        "segment_size": 54.0,
        "window_days": 30,
        "status": "proposed",
        "name": "Réassurance facturation",
        "problem": "90,4 % des avis clients sont négatifs sur la période.",
        "hook": "Ce que vous payez, expliqué",
        "message": "Voici le détail de ce qui est décompté.",
        "brief": "campagne pour Orange au Mali",
        "written_by_llm": True,
        "tone": "factuel",
        "strategy": None,
        "strategies": [],
        "contents": {},
        "created_at": datetime(2026, 8, 13, 9, 41, tzinfo=timezone.utc),
        "payload": {
            "cible": {
                "pays": "Mali",
                "avis_clients": 156,
                "negatifs": 141,
                "part_negatifs": 90.4,
                "motif_dominant": "facturation_prix",
                "motif_avis": 54,
                "part_motif": 38.3,
                "composition": {"app_store": 141, "google_play": 9},
            },
            "actions": ["Détailler la facture ligne à ligne"],
            "contexte": {"marche": ["panier data 8,0 $/mois"], "indisponibles": []},
        },
    }
    base.update(kw)
    return base


class _Depot:
    def __init__(self, campagne=None):
        self._campagne = campagne

    def par_id(self, campaign_id):
        return self._campagne


class _Stats:
    """Rend des identifiants d'avis, et note le filtre reçu."""

    def __init__(self, reviews=None, leve=False):
        self._reviews = reviews if reviews is not None else [{"review_id": "abc123"}]
        self._leve = leve
        self.filtre = None

    def verbatims(self, f, polarity="negative", limit=20):
        if self._leve:
            raise RuntimeError("agrégats indisponibles")
        self.filtre = f
        return {"reviews": self._reviews[:limit]}


def _sections(dossier):
    return {s["cle"]: s for s in dossier["sections"]}


# ---------------------------------------------------------------------------
# Le périmètre vient des sélecteurs
# ---------------------------------------------------------------------------


def test_une_filiale_choisie_devient_un_axe_du_contrat_de_filtre():
    """Le sélecteur désigne la cible ; rien ne doit la redeviner.

    Si la traduction se trompait d'axe, la campagne serait bâtie sur un autre
    périmètre que celui affiché — avec des chiffres parfaitement exacts, donc
    sans rien pour alerter le lecteur.
    """
    filtre, jours = _perimetre_des_selecteurs({"subsidiary": 52, "days": 30})
    assert filtre.subsidiaries == (52,)
    assert filtre.operators == ()
    assert jours == 30


@pytest.mark.parametrize(
    "corps,attendu",
    [
        ({"operator": 7}, ("operators", (7,))),
        ({"country": "ML"}, ("countries", ("ML",))),
        ({"region": "Afrique de l'Ouest"}, ("regions", ("Afrique de l'Ouest",))),
    ],
)
def test_chaque_niveau_se_range_dans_son_axe(corps, attendu):
    filtre, _ = _perimetre_des_selecteurs(corps)
    champ, valeur = attendu
    assert getattr(filtre, champ) == valeur


def test_un_identifiant_illisible_est_refuse_et_non_ignore():
    """Ignorer l'identifiant produirait une campagne sur TOUT le périmètre.

    Elle serait présentable, chiffrée, et sans rapport avec ce que
    l'utilisateur a demandé. Mieux vaut un message d'erreur.
    """
    with pytest.raises(ValueError, match="illisible"):
        _perimetre_des_selecteurs({"subsidiary": "Orange Mali"})


def test_une_periode_hors_bornes_est_refusee():
    with pytest.raises(ValueError, match="entre 1 et 3650"):
        _perimetre_des_selecteurs({"subsidiary": 52, "days": 99999})


def test_sans_selecteur_ni_periode_l_agent_garde_la_main():
    """Aucun périmètre imposé : l'agent choisit sa cible comme en CLI.

    C'est ce qui garde UN seul jeu de règles pour les trois surfaces.
    """
    assert _perimetre_des_selecteurs({}) is None
    assert _perimetre_des_selecteurs({"days": JOURS_DEFAUT}) is None


def test_une_periode_seule_est_tout_de_meme_imposee():
    """Choisir 90 jours sans choisir d'entité doit rester respecté."""
    perimetre = _perimetre_des_selecteurs({"days": 90})
    assert perimetre is not None
    filtre, jours = perimetre
    assert jours == 90
    assert filtre.subsidiaries == ()


# ---------------------------------------------------------------------------
# Le périmètre remplace le périmètre, jamais l'intention
# ---------------------------------------------------------------------------


def test_imposer_un_perimetre_conserve_l_intention_du_texte():
    """LA règle de cette couche.

    Les dimensions demandées (« les jeunes », « à Casablanca ») sont recueillies
    par le modèle POUR ÊTRE RÉFUTÉES. Les perdre en imposant le périmètre ferait
    accepter en silence une demande dont la moitié est irréalisable — et
    l'utilisateur croirait l'âge pris en compte.
    """
    traduit = Brief(
        texte="pour les jeunes gros consommateurs",
        jours=30,
        filtre=StatsFilter(days=30),
        portee="tout le périmètre",
        objectif="retention",
        canal="sms",
        dimensions=("age", "consommation"),
    )
    impose = _imposer_perimetre(traduit, StatsFilter(days=90, subsidiaries=(52,)), 90)

    assert impose.dimensions == ("age", "consommation")
    assert impose.objectif == "retention"
    assert impose.canal == "sms"
    assert impose.texte == "pour les jeunes gros consommateurs"

    assert impose.filtre.subsidiaries == (52,)
    assert impose.jours == 90
    assert impose.cible_imposee is True


def test_le_libelle_du_perimetre_nomme_le_niveau_impose():
    impose = _imposer_perimetre(brief_vide(), StatsFilter(days=30, countries=("ML",)), 30)
    assert "pays ML" in impose.portee


# ---------------------------------------------------------------------------
# Dossier — les treize sections
# ---------------------------------------------------------------------------


def test_le_dossier_rend_les_treize_sections_dans_l_ordre():
    """La numérotation est celle du livrable et ne bouge pas.

    Un lecteur qui cherche « 11. Expected Impact » doit la trouver, y compris
    quand la campagne n'a rien à y mettre.
    """
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    assert d["available"] is True
    assert [s["numero"] for s in d["sections"]] == list(range(1, 14))
    assert [s["cle"] for s in d["sections"]] == [cle for cle, _ in TITRES]


def test_une_section_sans_matiere_est_dite_et_non_supprimee():
    nue = _campagne(payload={}, strategies=[], strategy=None, contents={})
    d = DossierDeCampagne(_Depot(nue), _Stats()).composer(7)
    for section in d["sections"]:
        assert section["champs"] or section["lignes"] or section["texte"]


def test_le_dossier_d_une_campagne_inconnue_est_une_reponse_pas_une_panne():
    d = DossierDeCampagne(_Depot(None), _Stats()).composer(999)
    assert d["available"] is False
    assert "999" in d["raison"]


def test_l_impact_attendu_ne_promet_aucun_chiffre():
    """JAMAIS de projection.

    Un « -8 points attendus » serait un nombre inventé au milieu d'un document
    exact, donc indétectable et d'autant plus dommageable.
    """
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    impact = _sections(d)["expected_impact"]
    assert "ne prédit pas" in " ".join(impact["lignes"])
    # La seule valeur chiffrée admise est le POINT DE DÉPART, qui est mesuré.
    for champ in impact["champs"]:
        assert champ["label"] == "Valeur de départ"


def test_un_objectif_non_mesurable_ne_produit_aucun_impact_chiffre():
    d = DossierDeCampagne(_Depot(_campagne(objective="conversion")), _Stats()).composer(7)
    impact = _sections(d)["expected_impact"]
    assert "pas mesurable" in impact["texte"]
    assert impact["champs"] == []


def test_le_segment_rappelle_qu_il_designe_des_avis_et_non_des_abonnes():
    """Sans ce rappel, « segment » se lit comme un fichier adressable."""
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    assert "pas d'abonnés" in _sections(d)["target_segment"]["texte"]


def test_le_segment_est_justifie_par_la_mesure_qui_l_a_produit():
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    texte = _sections(d)["target_segment"]["texte"]
    assert "38.3 %" in texte
    assert "Facturation & prix" in texte


def test_les_limites_declarent_l_absence_de_donnees_d_envoi():
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    lignes = " ".join(_sections(d)["limitations"]["lignes"])
    assert "aucun clic" in lignes
    assert "avis PUBLICS" in lignes


def test_un_texte_de_gabarit_est_signale_dans_les_limites():
    """Une campagne non rédigée par le modèle doit le dire : le lecteur juge
    autrement un texte composé mécaniquement."""
    d = DossierDeCampagne(_Depot(_campagne(written_by_llm=False)), _Stats()).composer(7)
    assert any("gabarit" in x for x in _sections(d)["limitations"]["lignes"])


def test_aucun_kpi_d_ouverture_n_est_propose():
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    assert "aucune donnée d'envoi" in " ".join(_sections(d)["kpis"]["lignes"])


# ---------------------------------------------------------------------------
# Dossier — la traçabilité
# ---------------------------------------------------------------------------


def test_les_preuves_citent_des_identifiants_d_avis():
    """Pouvoir remonter aux avis sources est l'exigence de traçabilité."""
    stats = _Stats(reviews=[{"review_id": "r1"}, {"review_id": "r2"}])
    d = DossierDeCampagne(_Depot(_campagne()), stats).composer(7)
    lignes = " ".join(_sections(d)["data_evidence"]["lignes"])
    assert "r1" in lignes and "r2" in lignes


def test_les_preuves_reprennent_le_perimetre_exact_de_la_campagne():
    """Une requête de preuve sur un autre périmètre ramènerait des avis voisins
    présentés comme la matière de la campagne."""
    stats = _Stats()
    DossierDeCampagne(_Depot(_campagne()), stats).composer(7)
    assert stats.filtre.subsidiaries == (12,)
    assert stats.filtre.days == 30


def test_une_lecture_impossible_se_distingue_d_une_absence_d_avis():
    """Les deux appellent une phrase différente : « on n'a pas pu lire » n'est
    pas « il n'y a rien »."""
    muet = DossierDeCampagne(_Depot(_campagne()), _Stats(leve=True)).composer(7)
    assert "ne peuvent pas être rendus" in " ".join(
        _sections(muet)["data_evidence"]["lignes"]
    )

    vide = DossierDeCampagne(_Depot(_campagne()), _Stats(reviews=[])).composer(7)
    assert "Aucun avis ne remonte" in " ".join(
        _sections(vide)["data_evidence"]["lignes"]
    )


def test_sans_acces_aux_agregats_le_dossier_reste_produit():
    """La traçabilité est un plus, jamais une condition : le dossier doit rester
    lisible quand les agrégats sont indisponibles."""
    d = DossierDeCampagne(_Depot(_campagne()), stats=None).composer(7)
    assert d["available"] is True
    assert len(d["sections"]) == 13


# ---------------------------------------------------------------------------
# Dossier — le rendu
# ---------------------------------------------------------------------------


def test_le_markdown_porte_le_titre_et_les_treize_titres_de_section():
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    md = d["markdown"]
    assert md.startswith("# Campaign Report — Réassurance facturation")
    for numero, (_, titre) in enumerate(TITRES, start=1):
        assert f"## {numero}. {titre}" in md


def test_le_markdown_et_les_sections_viennent_de_la_meme_composition():
    """Un rapport exporté ne peut pas différer de celui qui est à l'écran."""
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    assert _sections(d)["main_problems"]["texte"] in d["markdown"]


def test_la_periode_analysee_est_celle_qui_a_servi_a_mesurer():
    """Afficher une autre fenêtre que celle des chiffres rendrait le dossier
    indéfendable — c'est la première question qu'on lui posera."""
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    champs = {c["label"]: c["valeur"] for c in _sections(d)["context"]["champs"]}
    assert "30 jours" in champs["Période analysée"]
    assert "2026-07-14" in champs["Période analysée"]


def test_la_demande_de_l_utilisateur_est_conservee_dans_le_contexte():
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    champs = {c["label"]: c["valeur"] for c in _sections(d)["context"]["champs"]}
    assert champs["Demande de l'utilisateur"] == "campagne pour Orange au Mali"


def test_un_objectif_impose_contre_les_mesures_est_rendu_visible():
    """Mener une campagne à contre-mesure peut être légitime, mais ce doit être
    un choix conscient."""
    campagne = _campagne()
    campagne["payload"]["objectif_mesure"] = "retention"
    d = DossierDeCampagne(_Depot(campagne), _Stats()).composer(7)
    lignes = " ".join(_sections(d)["marketing_objective"]["lignes"])
    assert "Rétention" in lignes
    assert "demande de l'utilisateur" in lignes


def test_le_canal_est_justifie_par_la_source_du_segment():
    """Sans fichier client, c'est le seul ciblage honnête : le dossier doit le
    dire, sinon le canal paraît choisi arbitrairement."""
    d = DossierDeCampagne(_Depot(_campagne()), _Stats()).composer(7)
    lignes = " ".join(_sections(d)["recommended_channels"]["lignes"])
    assert "app_store" in lignes
    assert "seul ciblage honnête" in lignes


def test_les_contenus_absents_sont_signales_plutot_que_tus():
    d = DossierDeCampagne(_Depot(_campagne(contents={})), _Stats()).composer(7)
    assert any(
        "pas encore été produites" in x
        for x in _sections(d)["campaign_content"]["lignes"]
    )


def test_les_contenus_produits_sont_repris_format_par_format():
    campagne = _campagne(contents={"sms": {"texte": "Votre recharge est suivie."}})
    d = DossierDeCampagne(_Depot(campagne), _Stats()).composer(7)
    lignes = " ".join(_sections(d)["campaign_content"]["lignes"])
    assert "[sms]" in lignes
    assert "Votre recharge est suivie." in lignes


def test_un_payload_corrompu_ne_fait_pas_tomber_le_dossier():
    """L'Agent 2 ne doit jamais faire tomber l'application."""
    d = DossierDeCampagne(_Depot(_campagne(payload=None)), _Stats()).composer(7)
    assert d["available"] is True


def test_une_campagne_recente_reste_datable():
    """Garde-fou de non-régression sur le mélange UTC / heure locale : la
    fenêtre affichée se calcule depuis `created_at`, pas depuis l'horloge du
    serveur."""
    hier = datetime.now(timezone.utc) - timedelta(days=1)
    d = DossierDeCampagne(_Depot(_campagne(created_at=hier)), _Stats()).composer(7)
    champs = {c["label"]: c["valeur"] for c in _sections(d)["context"]["champs"]}
    assert hier.date().isoformat() in champs["Période analysée"]
