"""
Tests de l'Agent 2 — assistant conversationnel.

Aucune base, aucun réseau, aucun modèle : le modèle est remplacé par un double
qui rend ce qu'on lui dicte, ce qui permet de tester ce qui compte vraiment —
non pas « le modèle comprend-il ? », mais « que fait le programme de ce que le
modèle a rendu ? ».

C'est la distinction qui structure ce fichier. Les régressions redoutées ici ne
lèvent aucune exception et ne remplissent aucun journal d'erreurs : un
classement calculé sur trois avis, un chiffre inventé dans une phrase, un
opérateur voisin retenu à la place de celui demandé. Toutes produisent une
réponse parfaitement lisible, et fausse.
"""

import pytest

from reviews.agents.chat_agent import (
    ChatAgent,
    _chiffres_autorises,
    _chiffres_inventes,
)
from reviews.agents.questions import (
    JOURS_DEFAUT,
    JOURS_MAX,
    LIMITE_MAX,
    TRIS,
    Catalogue,
    QuestionRefusee,
    valider,
)
from reviews.agents.telegram_chat import (
    BoucleConversation,
    MessageEntrant,
    analyser,
)
from reviews.llm.client import LLMResponse, LLMUnavailable

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

#: Extrait du catalogue RÉEL, choisi pour ses pièges (voir les tests de
#: résolution) : « Airtel » préfixe « AirtelTigo », « Guinée » préfixe deux
#: autres pays, « Congo » n'est le nom exact d'aucun des deux Congo.
_OPTIONS = {
    "operators": [
        {"id": 7, "label": "Orange"},
        {"id": 4, "label": "MTN"},
        {"id": 2, "label": "Airtel"},
        {"id": 21, "label": "AirtelTigo"},
        {"id": 10, "label": "Vodacom"},
        {"id": 11, "label": "Vodafone"},
    ],
    "countries": [
        {"iso2": "ML", "label": "Mali"},
        {"iso2": "EG", "label": "Égypte"},
        {"iso2": "GN", "label": "Guinée"},
        {"iso2": "GW", "label": "Guinée-Bissau"},
        {"iso2": "GQ", "label": "Guinée équatoriale"},
        {"iso2": "CG", "label": "Congo-Brazzaville"},
        {"iso2": "CD", "label": "RD Congo"},
    ],
    "regions": ["Afrique de l'Ouest", "Afrique du Nord"],
}

_CATALOGUE = Catalogue.depuis(_OPTIONS)


class _Stats:
    """Repository factice : rend les lignes qu'on lui donne, note ses appels."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"label": "Orange Mali", "avis_clients": 219,
             "part_negatifs": 92.2, "note_moyenne": 1.2},
        ]
        self.appels = []

    def filter_options(self):
        return _OPTIONS

    def ranking(self, **kw):
        self.appels.append(kw)
        return {"rows": self.rows}


class _Modele:
    """Client LLM factice. `reponses` est consommée dans l'ordre des appels."""

    def __init__(self, *reponses, available=True):
        self.reponses = list(reponses)
        self.available = available
        self.appels = []

    def _suivante(self):
        if not self.reponses:
            raise AssertionError("appel au modèle non prévu par le test")
        return self.reponses.pop(0)

    def complete_json(self, *, system, user, **kw):
        self.appels.append(("json", system, user))
        valeur = self._suivante()
        if isinstance(valeur, Exception):
            raise valeur
        return valeur

    def complete(self, *, system, user, **kw):
        self.appels.append(("texte", system, user))
        valeur = self._suivante()
        if isinstance(valeur, Exception):
            raise valeur
        return LLMResponse(text=valeur, model="factice")


def _agent(modele=None, stats=None):
    agent = ChatAgent.__new__(ChatAgent)
    agent.db = None
    agent.settings = None
    agent.stats = stats or _Stats()
    agent.client = modele
    agent._catalogue = None
    agent._catalogue_charge_a = 0.0
    return agent


# ---------------------------------------------------------------------------
# Résolution des noms — les pièges du catalogue réel
# ---------------------------------------------------------------------------


def test_l_egalite_exacte_prime_sur_l_inclusion():
    """LE piège du catalogue réel : « Airtel » est un préfixe de « AirtelTigo ».

    Avec une règle par inclusion appliquée en premier, « Airtel » — quatrième
    opérateur du périmètre par le volume — deviendrait ambigu et serait refusé.
    Une question parfaitement claire recevrait alors « précisez lequel ».
    """
    assert _CATALOGUE.operateur("Airtel") == (2, "Airtel")
    assert _CATALOGUE.operateur("AirtelTigo") == (21, "AirtelTigo")


def test_l_egalite_exacte_prime_aussi_sur_les_pays_emboites():
    """Même piège côté pays : « Guinée » préfixe deux autres noms du périmètre."""
    assert _CATALOGUE.pays_("Guinée") == ("GN", "Guinée")
    assert _CATALOGUE.pays_("Guinée-Bissau") == ("GW", "Guinée-Bissau")


def test_les_accents_ne_sont_pas_exiges():
    """Personne ne tape « Égypte » accentué dans Telegram.

    Sans normalisation, le robot répondrait qu'il ne connaît pas un pays qui
    porte pourtant 909 avis en base — l'incohérence la plus décourageante qu'il
    puisse produire.
    """
    assert _CATALOGUE.pays_("egypte") == ("EG", "Égypte")
    assert _CATALOGUE.pays_("EGYPTE") == ("EG", "Égypte")


def test_un_nom_reellement_ambigu_est_refuse_en_disant_lesquels():
    """« Congo » désigne deux pays du périmètre. Trancher au hasard produirait
    une réponse juste sur le mauvais pays, que rien ne signalerait."""
    with pytest.raises(QuestionRefusee) as erreur:
        _CATALOGUE.pays_("Congo")
    assert "Congo-Brazzaville" in str(erreur.value)
    assert "RD Congo" in str(erreur.value)


def test_une_inclusion_sans_ambiguite_est_acceptee():
    """« vodacom sa » doit trouver Vodacom : le second recours sert à cela."""
    assert _CATALOGUE.operateur("vodacom sa") == (10, "Vodacom")


def test_un_nom_inconnu_est_refuse_et_non_rapproche():
    """Le rapprochement au plus proche est la faute à ne jamais commettre :
    « Bouygues » deviendrait un opérateur africain quelconque."""
    with pytest.raises(QuestionRefusee) as erreur:
        _CATALOGUE.operateur("Bouygues")
    assert "Bouygues" in str(erreur.value)


def test_les_filiales_ne_sont_pas_dans_le_vocabulaire():
    """Une filiale est un opérateur ET un pays. Lister les 135 noms de filiales
    doublerait le prompt pour une information que le modèle a déjà."""
    vocabulaire = _CATALOGUE.vocabulaire()
    assert "Orange" in vocabulaire and "Mali" in vocabulaire
    assert "Orange Mali" not in vocabulaire


# ---------------------------------------------------------------------------
# Validation : les seuils sont en Python, jamais dans le prompt
# ---------------------------------------------------------------------------


def test_une_question_sans_periode_est_bornee_a_la_fenetre_par_defaut():
    """« Ces jours-ci » porte sur le présent. Répondre sur tout l'historique
    répondrait à une autre question, avec des chiffres justes — et rien dans la
    réponse ne signalerait le malentendu."""
    d = valider({"intention": "classement", "operateur": "Orange"}, _CATALOGUE)
    assert d.jours == JOURS_DEFAUT
    assert d.filtre.days == JOURS_DEFAUT


def test_les_valeurs_hors_bornes_sont_ramenees_sans_lever():
    """Le modèle rend ce qu'il veut : null, « trente », 9999, -1. Lever sur
    chacun transformerait une question compréhensible en refus."""
    d = valider(
        {"intention": "classement", "jours": 99999, "limite": 500}, _CATALOGUE
    )
    assert d.jours == JOURS_MAX
    assert d.limite == LIMITE_MAX

    d = valider({"intention": "classement", "jours": "trente"}, _CATALOGUE)
    assert d.jours == JOURS_DEFAUT


def test_un_tri_par_taux_impose_un_plancher_de_volume():
    """LA règle qui empêche « 100 % de négatifs » sur deux avis.

    Mesuré le 12 août 2026 : sans plancher, les trois pays « les plus
    mécontents » sur 30 jours sont Madagascar (2 avis), Mali (222) et Niger
    (4). Deux réponses sur trois sans aucun sens statistique.
    """
    from reviews.storage.stats_repository import RELIABILITY_MIN_REVIEWS

    par_taux = valider({"intention": "classement", "tri": "negatifs"}, _CATALOGUE)
    assert par_taux.min_avis == RELIABILITY_MIN_REVIEWS


def test_un_tri_par_volume_n_impose_pas_le_plancher_des_taux():
    """Un classement par nombre d'avis ne peut pas être faussé par un petit
    effectif — mais il ne doit pas non plus se remplir d'entités à zéro avis
    client, qui répondent « aucun » à une question qui demande « le plus »."""
    par_volume = valider({"intention": "classement", "tri": "volume"}, _CATALOGUE)
    assert par_volume.min_avis == 1


def test_une_intention_inconnue_est_refusee_avec_l_explication_du_modele():
    """La limite annoncée d'avance. Se rabattre sur le classement le plus proche
    donnerait une réponse chiffrée à une question sans données."""
    with pytest.raises(QuestionRefusee) as erreur:
        valider(
            {"intention": None, "pourquoi": "aucune donnée d'abonnés"}, _CATALOGUE
        )
    assert "abonnés" in str(erreur.value)


def test_un_niveau_ou_un_tri_hors_liste_blanche_ne_retombe_pas_sur_le_defaut():
    """Un repli silencieux serait pire qu'un refus : le robot répondrait à une
    autre question que celle posée, avec l'assurance d'un chiffre."""
    with pytest.raises(QuestionRefusee):
        valider({"intention": "classement", "niveau": "source"}, _CATALOGUE)
    with pytest.raises(QuestionRefusee):
        valider({"intention": "classement", "tri": "abonnes"}, _CATALOGUE)


def test_les_tris_offerts_existent_tous_dans_le_repository():
    """VERROU DE COHÉRENCE. Un tri retiré de `_SORTS` et laissé ici ferait
    retomber `ranking` sur son tri par défaut : le robot annoncerait un
    classement par note et rendrait un classement par part de négatifs."""
    from reviews.storage.stats_repository import _SORTS

    assert set(TRIS) <= set(_SORTS)


def test_le_perimetre_resolu_arrive_intact_dans_le_filtre():
    """Le point de jonction avec le contrat de filtre existant : ce sont des
    identifiants résolus qui partent en base, jamais le texte de la question."""
    d = valider(
        {"intention": "classement", "operateur": "orange", "pays": "mali"},
        _CATALOGUE,
    )
    assert d.filtre.operators == (7,)
    assert d.filtre.countries == ("ML",)
    assert "Orange" in d.portee and "Mali" in d.portee


# ---------------------------------------------------------------------------
# Chiffres : le modèle ne calcule jamais, et c'est vérifié
# ---------------------------------------------------------------------------


def test_un_chiffre_absent_des_mesures_est_detecte():
    """LE garde-fou de la règle centrale du projet. Un pourcentage calculé de
    tête par un modèle est indiscernable d'un pourcentage mesuré, une fois
    écrit dans une phrase."""
    autorises = _chiffres_autorises(
        [{"avis_clients": 219, "part_negatifs": 92.2}]
    )
    assert _chiffres_inventes("219 avis, 92,2 % de négatifs", autorises) == []
    assert _chiffres_inventes("soit 27 avis négatifs de plus", autorises) == ["27"]


def test_l_arrondi_d_un_pourcentage_reste_autorise():
    """« 92 % » pour une mesure de 92,2 est ce qu'un humain écrirait, et le
    prompt l'autorise explicitement. Le rejeter ferait tomber la rédaction au
    gabarit à presque chaque réponse."""
    autorises = _chiffres_autorises([{"part_negatifs": 92.2}])
    assert _chiffres_inventes("environ 92 % des avis", autorises) == []
    assert _chiffres_inventes("environ 85 % des avis", autorises) == ["85"]


def test_les_rangs_d_une_liste_sont_autorises():
    """« en deuxième position » est une mise en forme, pas une mesure."""
    autorises = _chiffres_autorises([{"avis_clients": 219}, {"avis_clients": 187}])
    assert _chiffres_inventes("1. Orange Mali\n2. Orange Égypte", autorises) == []


def test_une_redaction_qui_invente_un_chiffre_est_ecartee_au_profit_du_factuel():
    """Le comportement complet, de bout en bout : la réponse reste juste, et le
    journal garde la trace que le modèle a été écarté."""
    modele = _Modele(
        {"intention": "classement", "operateur": "Orange", "tri": "volume"},
        "Orange Mali domine avec 219 avis, soit 40 % de plus que l'an dernier.",
    )
    reponse = _agent(modele).repondre("quelle filiale d'Orange revient le plus ?")

    assert reponse.redige_par_modele is False
    assert "40 %" not in reponse.texte
    assert "219 avis" in reponse.texte          # le gabarit a pris le relais
    assert "92,2 % négatifs" in reponse.texte


def test_une_redaction_fidele_est_conservee():
    modele = _Modele(
        {"intention": "classement", "operateur": "Orange", "tri": "volume"},
        "Orange Mali arrive en tête avec 219 avis, dont 92,2 % de négatifs.",
    )
    reponse = _agent(modele).repondre("quelle filiale d'Orange revient le plus ?")

    assert reponse.redige_par_modele is True
    assert "Orange Mali arrive en tête" in reponse.texte


def test_le_perimetre_est_toujours_ajoute_par_le_programme():
    """Un taux lu sans son périmètre est un taux mal interprété. Cette ligne ne
    doit dépendre d'aucune rédaction — et ses dates, qui sont des nombres, ne
    doivent pas non plus ouvrir une brèche dans la vérification des chiffres."""
    modele = _Modele(
        {"intention": "classement", "operateur": "Orange", "tri": "volume"},
        "Orange Mali arrive en tête avec 219 avis.",
    )
    reponse = _agent(modele).repondre("quelle filiale d'Orange revient le plus ?")
    assert "Périmètre : Orange · 30 derniers jours" in reponse.texte


# ---------------------------------------------------------------------------
# Exécution : ce qui part réellement en base
# ---------------------------------------------------------------------------


def test_les_parametres_valides_sont_passes_tels_quels_au_repository():
    """Aucun SQL n'est composé par l'agent : il appelle la méthode que le
    dashboard appelle déjà, avec le filtre validé."""
    stats = _Stats()
    modele = _Modele(
        {"intention": "classement", "niveau": "country", "tri": "negatifs",
         "jours": 90, "limite": 3},
        "Le Mali arrive en tête.",
    )
    _agent(modele, stats).repondre("quels pays sont les plus mécontents ?")

    (appel,) = stats.appels
    assert appel["level"] == "country"
    assert appel["sort"] == "negatifs"
    assert appel["limit"] == 3
    assert appel["f"].days == 90
    assert appel["min_reviews"] > 1     # plancher de fiabilité appliqué


def test_un_perimetre_sans_donnees_le_dit_au_lieu_de_faire_rediger_un_vide():
    """Une absence de données ne se met pas en phrase, elle se dit — et faire
    rédiger un vide par le modèle coûterait un appel pour une périphrase."""
    modele = _Modele({"intention": "classement", "operateur": "Orange"})
    reponse = _agent(modele, _Stats(rows=[])).repondre("et Orange ?")

    assert "Aucun avis client" in reponse.texte
    assert reponse.appels_llm == 1      # traduction seule, pas de rédaction


def test_un_modele_absent_donne_une_reponse_et_non_une_panne():
    """Comprendre une question libre est ce qu'on demande à cet agent : sans
    modèle il ne peut pas, et doit le dire plutôt que de lever."""
    agent = _agent(modele=None)
    reponse = agent.repondre("quelle filiale d'Orange revient le plus ?")
    assert reponse.refus == "modèle indisponible"
    assert "question" in reponse.texte.lower()


def test_un_modele_en_panne_ne_fait_pas_taire_le_robot():
    modele = _Modele(LLMUnavailable("Budget quotidien atteint (200 appels)."))
    reponse = _agent(modele).repondre("quelle filiale d'Orange revient le plus ?")
    assert "Budget quotidien" in reponse.texte


# ---------------------------------------------------------------------------
# Telegram : le tri gratuit, avant tout appel au modèle
# ---------------------------------------------------------------------------


def test_le_suffixe_du_robot_est_retire_de_la_commande():
    """Dans un groupe, l'autocomplétion envoie « /q@Digiwise_alert_bot … ».
    Sans nettoyage, le nom du robot deviendrait un mot de la question."""
    c = analyser("/q@Digiwise_alert_bot quelle filiale d'Orange ?", prive=False)
    assert c.question == "quelle filiale d'Orange ?"


def test_le_bavardage_de_groupe_n_atteint_jamais_le_modele():
    """À 200 appels par jour, laisser un « ok merci » atteindre le traducteur
    consomme la veille du lendemain."""
    assert analyser("ok merci", prive=False).ignorer
    assert analyser("", prive=False).ignorer


def test_en_prive_une_phrase_suffit():
    """Tout message d'une conversation privée est adressé au robot : exiger
    « /q » d'un encadrant qui teste depuis son téléphone n'apporterait rien."""
    assert analyser("quelle filiale d'Orange ?", prive=True).question


def test_une_commande_inconnue_reste_muette_en_groupe():
    """Plusieurs robots cohabitent dans un groupe. Répondre « commande
    inconnue » à la commande d'un autre robot ferait de celui-ci une nuisance."""
    assert analyser("/stats", prive=False).ignorer
    assert analyser("/stats", prive=True).reponse_immediate


def test_q_sans_question_explique_au_lieu_d_interroger_le_modele():
    c = analyser("/q", prive=True)
    assert c.question is None
    assert "/q" in c.reponse_immediate


# ---------------------------------------------------------------------------
# Telegram : la boucle
# ---------------------------------------------------------------------------


class _Canal:
    def __init__(self, *salves):
        self.salves = list(salves)
        self.envois = []
        self.utilisable = True

    def mises_a_jour(self, offset):
        if not self.salves:
            return [], offset
        updates = self.salves.pop(0)
        suivant = max(u["update_id"] for u in updates) + 1 if updates else offset
        return updates, suivant

    def envoyer(self, chat_id, texte):
        self.envois.append((chat_id, texte))
        return True


class _Journal:
    def __init__(self, deja=0):
        self.deja = deja
        self.ecrits = []

    def count_since(self, agent, depuis, *, utilisateur=None):
        return self.deja

    def record(self, **kw):
        self.ecrits.append(kw)
        return 1


def _update(uid, texte, chat_id=-100, user_id=42, prive=False):
    return {
        "update_id": uid,
        "message": {
            "text": texte,
            "chat": {"id": chat_id, "type": "private" if prive else "supergroup"},
            "from": {"id": user_id, "username": "encadrant"},
        },
    }


class _AgentFactice:
    def __init__(self):
        self.questions = []

    def repondre(self, question):
        from reviews.agents.chat_agent import Reponse

        self.questions.append(question)
        return Reponse(texte="réponse", lignes=1)


def _boucle(canal, journal=None, autorises=None, plafond=20):
    from reviews.config import ChatConfig

    return BoucleConversation(
        agent=_AgentFactice(),
        canal=canal,
        cfg=ChatConfig(daily_questions_per_user=plafond),
        journal=journal,
        chats_autorises=autorises if autorises is not None else {-100},
    )


def test_un_tour_repond_et_avance_l_offset():
    """L'offset ACQUITTE les mises à jour côté Telegram. Sans progression, un
    redémarrage rejouerait les questions déjà traitées — donc renverrait les
    mêmes réponses, non sollicitées, et paierait deux fois le modèle."""
    canal = _Canal([_update(11, "/q quelle filiale d'Orange ?")])
    boucle = _boucle(canal)

    assert boucle.tour() == 1
    assert boucle.offset == 12
    assert canal.envois[0][0] == -100


def test_une_conversation_non_autorisee_reste_sans_reponse():
    """L'identifiant du robot est public : n'importe qui peut lui écrire.
    Répondre « vous n'êtes pas autorisé » confirmerait à un inconnu que le
    robot est vivant et l'inviterait à insister."""
    canal = _Canal([_update(1, "/q secrets ?", chat_id=999)])
    boucle = _boucle(canal, autorises={-100})

    assert boucle.tour() == 0
    assert canal.envois == []
    assert boucle.agent.questions == []      # le modèle n'a pas été appelé


def test_le_plafond_par_personne_arrete_les_questions_sans_arreter_le_robot():
    """Le plafond protège le budget du LENDEMAIN : le quota de modèle est
    quotidien et partagé avec l'analyse sémantique et le briefing du matin."""
    canal = _Canal([_update(1, "/q encore une ?")])
    boucle = _boucle(canal, journal=_Journal(deja=20), plafond=20)

    assert boucle.tour() == 1
    assert "plafond" in canal.envois[0][1]
    assert boucle.agent.questions == []      # aucun appel au modèle


def test_un_journal_indisponible_laisse_passer_plutot_que_de_bloquer():
    """Un plafond qui se ferme à cause d'une panne de base serait une panne plus
    grave que celle qu'il prévient : le robot cesserait de répondre sans dire
    pourquoi."""
    boucle = _boucle(_Canal([_update(1, "/q une question ?")]), journal=None)
    assert boucle.tour() == 1
    assert boucle.agent.questions == ["une question ?"]


def test_la_question_et_les_parametres_compris_sont_journalises():
    """« Le robot s'est trompé » n'est débogable que si l'on peut relire les
    paramètres retenus — que le modèle ne redonnera pas deux fois à
    l'identique."""
    journal = _Journal()
    boucle = _boucle(_Canal([_update(1, "/q quelle filiale ?")]), journal=journal)
    boucle.tour()

    (ecrit,) = journal.ecrits
    assert ecrit["agent"] == "chat"
    assert ecrit["payload"]["question"] == "/q quelle filiale ?"
    assert ecrit["payload"]["utilisateur"] == "42"


def test_ce_qui_n_est_pas_un_message_texte_est_ignore_sans_bruit():
    """Un groupe produit des mises à jour qui ne sont pas des questions :
    membres qui rejoignent, réactions, messages édités. Ce n'est pas une
    anomalie, c'est le fonctionnement normal."""
    assert MessageEntrant.depuis({"update_id": 1, "my_chat_member": {}}) is None
    assert MessageEntrant.depuis(
        {"update_id": 2, "message": {"photo": [], "chat": {"id": 1}}}
    ) is None


def test_un_message_reel_est_reduit_a_ce_qui_sert():
    m = MessageEntrant.depuis(_update(7, "/q test", chat_id=-100, prive=False))
    assert (m.update_id, m.chat_id, m.user_id, m.prive) == (7, -100, 42, False)
    assert m.auteur == "encadrant"
