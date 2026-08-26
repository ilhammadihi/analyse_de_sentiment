"""
Tests de l'Agent 2 — assistant de campagne.

Aucune base, aucun réseau, aucun modèle : les repositories et le client LLM sont
remplacés par des doubles. Ce n'est pas une commodité, c'est ce qui permet de
tester la seule chose qui compte ici — non pas « le modèle écrit-il bien ? »,
mais « que fait le programme de ce que le modèle a rendu, et que décide-t-il
avant de l'appeler ? ».

LES RÉGRESSIONS REDOUTÉES ICI NE LÈVENT AUCUNE EXCEPTION. Une campagne bâtie sur
six avis, un objectif de réassurance sur une panne réseau, une promesse de
gratuité glissée dans un message client, un rapport qui conclut à un succès
pendant que tout le pays s'améliore : toutes produisent une proposition
parfaitement présentable, et fausse.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reviews.agents.campagne import (
    CONCENTRATION_MOTIF,
    SEGMENT_PLANCHER,
    Cible,
    arbitrer_cibles,
    choisir_canal,
    choisir_objectif,
    choisir_segment,
    leviers,
    motif_du_segment,
    promesses_detectees,
    valider_brief,
)
from reviews.agents.campaign_agent import CampaignAgent, Campagne
from reviews.agents.questions import Catalogue, QuestionRefusee
from reviews.domain.aspects import ASPECTS
from reviews.domain.marketing import CANAUX, LEVIERS, OBJECTIFS, SEGMENTS
from reviews.llm.client import LLMResponse

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

_OPTIONS = {
    "operators": [{"id": 7, "label": "Orange"}, {"id": 4, "label": "MTN"}],
    "countries": [{"iso2": "ML", "label": "Mali"}, {"iso2": "GH", "label": "Ghana"}],
    "regions": ["Afrique de l'Ouest"],
}

_CATALOGUE = Catalogue.depuis(_OPTIONS)


def _ligne(**kw):
    """Une ligne de classement telle que `ranking` la rend."""
    base = {
        "key": 12, "label": "Orange Mali", "country": "Mali", "iso2": "ML",
        "avis_clients": 220, "positifs": 40, "negatifs": 150,
        "part_negatifs": 68.2, "part_positifs": 18.2, "note_moyenne": 1.9,
        "composition": {"google_play": 180, "app_store": 40},
    }
    base.update(kw)
    return base


class _Stats:
    """Repository factice : rend ce qu'on lui donne, note ses appels."""

    def __init__(self, rows=None, motifs=None, apercus=None):
        self.rows = [_ligne()] if rows is None else rows
        #: motifs par appel successif à `themes`
        self.motifs = motifs if motifs is not None else [
            [{"term": "facturation_prix", "avis": 90}]
        ]
        self.apercus = apercus or []
        self.appels = []

    def filter_options(self):
        return _OPTIONS

    def ranking(self, **kw):
        self.appels.append(("ranking", kw))
        return {"rows": self.rows}

    def themes(self, f, **kw):
        self.appels.append(("themes", kw))
        lignes = self.motifs.pop(0) if self.motifs else []
        return {"terms": lignes}

    def overview(self, f=None):
        self.appels.append(("overview", f))
        return self.apercus.pop(0) if self.apercus else {"current": {}, "previous": {}}


class _Depot:
    """Repository de campagnes factice."""

    def __init__(self, derniere=None, campagne=None):
        self._derniere = derniere
        self._campagne = campagne
        self.creees = []
        self.rapports = []
        self.decisions = []
        self.contenus_ecrits = None

    def derniere_pour(self, level, key):
        return self._derniere

    def decider(self, campaign_id, statut, par):
        self.decisions.append((campaign_id, statut, par))
        return True

    def lister(self, statut=None, limit=10):
        return []

    def creer(self, **kw):
        self.creees.append(kw)
        return 42

    def marquer_transmise(self, campaign_id, message_id=None):
        self.transmises = getattr(self, "transmises", [])
        self.transmises.append((campaign_id, message_id))

    def par_id(self, campaign_id):
        return self._campagne

    def enregistrer_rapport(self, campaign_id, rapport):
        self.rapports.append((campaign_id, rapport))

    def enregistrer_contenus(self, campaign_id, contenus):
        self.contenus_ecrits = contenus

    @staticmethod
    def date_de_reference(campagne):
        return campagne["decided_at"]


class _Modele:
    """Client LLM factice. `reponses` est consommée dans l'ordre des appels."""

    def __init__(self, *reponses, available=True):
        self.reponses = list(reponses)
        self.available = available
        self.appels = []

    def _suivante(self):
        if not self.reponses:
            raise AssertionError("appel au modèle non prévu par le test")
        valeur = self.reponses.pop(0)
        if isinstance(valeur, Exception):
            raise valeur
        return valeur

    def complete_json(self, *, system, user, **kw):
        self.appels.append(("json", system, user))
        return self._suivante()

    def complete(self, *, system, user, **kw):
        self.appels.append(("texte", system, user))
        return LLMResponse(text=self._suivante(), model="factice")


def _agent(modele=None, stats=None, depot=None, contexte=None):
    agent = CampaignAgent.__new__(CampaignAgent)
    agent.db = None
    agent.settings = None
    agent.stats = stats or _Stats()
    agent.campagnes = depot or _Depot()
    agent.client = modele
    agent.notifier = None
    # `None` par défaut : le contexte est facultatif, et la plupart des tests
    # portent sur des décisions qui ne doivent pas en dépendre. Les
    # avertissements sur les données manquantes, eux, restent produits.
    agent.contexte = contexte
    # Garde-fou de l'Agent 3, DÉSACTIVÉ ici. Ces tests portent sur les
    # décisions de campagne, pas sur la qualité des données : un garde actif
    # ajouterait une réserve dans `texte()` et ferait échouer des assertions
    # qui n'ont rien à voir. Désactivé, il rend INDETERMINE et laisse passer —
    # exactement le comportement d'avant l'intégration.
    from reviews.agents.quality.garde import GardeQualite

    agent.garde = GardeQualite(None, enabled=False)
    agent._catalogue = None
    return agent


def _cible(**kw) -> Cible:
    base = {
        "level": "subsidiary", "key": "12", "label": "Orange Mali",
        "pays": "Mali", "iso2": "ML", "avis_clients": 220, "positifs": 40,
        "negatifs": 150, "part_negatifs": 68.2, "part_positifs": 18.2,
        "note_moyenne": 1.9,
    }
    base.update(kw)
    return Cible(**base)


# ---------------------------------------------------------------------------
# Le segment est un ensemble d'avis, et il se compte
# ---------------------------------------------------------------------------


def test_un_motif_concentre_donne_le_segment_le_plus_precis():
    """« Les clients qui se plaignent de leur facture » se traite ; « les
    mécontents » ne se traite pas. C'est toute la valeur de l'analyse
    sémantique, et elle se perd si le segment reste générique."""
    cible = _cible(motif="facturation_prix", motif_avis=90)
    segment = choisir_segment(cible)
    assert segment.cle == "insatisfaits_motif"
    assert cible.taille_segment(segment) == 90


def test_un_motif_diffus_ne_fabrique_pas_un_segment_qui_n_existe_pas():
    """Sur un périmètre où les plaintes sont diffuses, nommer un motif
    reviendrait à affirmer quelque chose de faux AUX CLIENTS visés — pire qu'une
    campagne générique."""
    cible = _cible(motif="app_bugs", motif_avis=15)   # 10 % des 150 négatifs
    assert cible.part_motif < CONCENTRATION_MOTIF
    assert choisir_segment(cible).cle == "detracteurs"


def test_un_motif_bien_identifie_mais_minuscule_ne_fait_pas_perdre_la_cible():
    """LE PIÈGE D'INTERACTION ENTRE LES DEUX SEUILS. Sur 15 avis négatifs dont
    40 % portent le même motif, le segment « motif » n'en compte que 6 : sous le
    plancher. Sans la seconde condition, un motif BIEN identifié faisait écarter
    une cible dont le segment « détracteurs » était parfaitement viable."""
    cible = _cible(negatifs=15, motif="app_bugs", motif_avis=6, avis_clients=40)
    assert cible.part_motif >= CONCENTRATION_MOTIF     # le motif domine bien
    assert cible.motif_avis < SEGMENT_PLANCHER         # mais il est minuscule
    segment = choisir_segment(cible)
    assert segment.cle == "detracteurs"
    assert cible.taille_segment(segment) == 15


def test_une_entite_majoritairement_satisfaite_donne_un_segment_de_promoteurs():
    cible = _cible(part_negatifs=12.0, part_positifs=71.0, positifs=160, negatifs=27)
    segment = choisir_segment(cible)
    assert segment.cle == "promoteurs"
    assert cible.taille_segment(segment) == 160


# ---------------------------------------------------------------------------
# L'objectif se déduit des mesures, il ne se choisit pas
# ---------------------------------------------------------------------------


def test_un_motif_informationnel_appelle_la_reassurance():
    """Un client qui conteste un décompte peut avoir raison OU s'être mépris :
    une explication règle une partie des cas sans qu'un euro change de main."""
    cible = _cible(motif="facturation_prix", motif_avis=90)
    objectif = choisir_objectif(cible, choisir_segment(cible))
    assert objectif.cle == "reassurance"


def test_une_panne_reseau_n_appelle_jamais_la_reassurance():
    """LA FAUTE À NE PAS COMMETTRE. Un client dont le réseau tombe tous les soirs
    n'a pas un problème de compréhension : lui « expliquer » sa situation prouve
    qu'on a vu la plainte sans rien changer, et aggrave le mécontentement."""
    cible = _cible(motif="coupures_pannes", motif_avis=90)
    objectif = choisir_objectif(cible, choisir_segment(cible))
    assert objectif.cle == "retention"


def test_la_satisfaction_ne_devient_un_argument_public_qu_au_dela_du_seuil():
    """En dessous, mettre en avant sa satisfaction expose à ce qu'un lecteur
    ouvre la fiche et y trouve immédiatement le contraire."""
    fort = _cible(part_negatifs=10.0, part_positifs=72.0, positifs=160)
    faible = _cible(part_negatifs=20.0, part_positifs=48.0, positifs=110)
    assert choisir_objectif(fort, SEGMENTS["promoteurs"]).cle == "acquisition"
    assert choisir_objectif(faible, SEGMENTS["promoteurs"]).cle == "fidelisation"


def test_chaque_objectif_porte_soit_un_kpi_soit_la_donnee_qui_lui_manque():
    """VERROU DE CONCEPTION. Un objectif dont on ne sait pas mesurer l'atteinte
    n'est pas interdit — conversion et montée en gamme sont parfaitement
    légitimes — mais il doit DIRE ce qui lui manque. Sans cela, son rapport de
    campagne se rédigerait aussi bien avant qu'après."""
    for objectif in OBJECTIFS.values():
        assert objectif.kpi_label
        assert objectif.sens in ("hausse", "baisse")
        if objectif.mesurable:
            assert objectif.kpi, objectif.cle
        else:
            assert objectif.donnee_manquante, objectif.cle


def test_l_arbitrage_ne_choisit_jamais_un_objectif_non_mesurable():
    """Un agent qui choisirait de lui-même un objectif dont il ne saura jamais
    dire s'il est atteint fabriquerait des campagnes invérifiables en série. Les
    non mesurables ne peuvent qu'être IMPOSÉS, et l'utilisateur est prévenu."""
    from reviews.domain.marketing import OBJECTIFS_MESURABLES

    cas = [
        _cible(motif="facturation_prix", motif_avis=90),
        _cible(motif="coupures_pannes", motif_avis=90),
        _cible(part_negatifs=10.0, part_positifs=75.0, positifs=160, negatifs=20),
        _cible(part_negatifs=20.0, part_positifs=45.0, positifs=110, negatifs=48),
    ]
    for cible in cas:
        objectif = choisir_objectif(cible, choisir_segment(cible))
        assert objectif.cle in OBJECTIFS_MESURABLES
        assert objectif.mesurable is True


# ---------------------------------------------------------------------------
# L'arbitrage : ce qui ne mérite pas de campagne
# ---------------------------------------------------------------------------


def test_un_volume_trop_faible_ecarte_la_cible_en_disant_pourquoi():
    """Une campagne bâtie sur un taux non publiable vise une impression."""
    (cible,) = arbitrer_cibles([_cible(avis_clients=12, negatifs=9)])
    assert not cible.retenue
    assert "avis clients" in cible.ecartee_parce_que


def test_un_segment_minuscule_ecarte_la_cible():
    """Le rapport comparerait ensuite deux poignées d'avis, et n'importe quel
    mouvement y paraîtrait spectaculaire."""
    (cible,) = arbitrer_cibles(
        [_cible(avis_clients=200, negatifs=4, positifs=6, part_negatifs=2.0,
                part_positifs=3.0)]
    )
    assert not cible.retenue
    assert "segment" in cible.ecartee_parce_que


def test_la_taille_du_segment_prime_et_l_urgence_departage():
    """Une campagne coûte le même prix qu'elle touche vingt personnes ou deux
    cents : son rendement suit l'audience. À taille comparable, une dégradation
    en cours passe devant une insatisfaction stable."""
    gros = _cible(label="Gros", negatifs=300, avis_clients=600)
    petit_urgent = _cible(label="Urgent", negatifs=80, avis_clients=200,
                          delta_negatifs=25.0)
    stable = _cible(label="Stable", negatifs=80, avis_clients=200)

    classement = [c.label for c in arbitrer_cibles([stable, petit_urgent, gros])]
    assert classement == ["Gros", "Urgent", "Stable"]


def test_le_bonus_d_urgence_ne_peut_pas_fabriquer_un_sujet_majeur():
    """Un bonus doit AMPLIFIER un signal réel, jamais s'y substituer : plafonné à
    la taille du segment, il peut au mieux la doubler."""
    (petit,) = arbitrer_cibles(
        [_cible(negatifs=20, avis_clients=100, delta_negatifs=60.0)]
    )
    assert petit.score <= 40.0


def test_le_classement_est_reproductible():
    """Deux exécutions sur les mêmes données doivent proposer la même cible. Un
    assistant dont les recommandations bougent sans que les chiffres bougent
    n'est plus consulté après la troisième fois."""
    cibles = [_cible(label=f"F{i}", negatifs=50 + i, avis_clients=200)
              for i in range(5)]
    premier = [c.label for c in arbitrer_cibles(list(cibles))]
    second = [c.label for c in arbitrer_cibles(list(reversed(cibles)))]
    assert premier == second


# ---------------------------------------------------------------------------
# Le canal : le seul ciblage honnête sans fichier client
# ---------------------------------------------------------------------------


def test_le_canal_se_deduit_de_la_source_ou_le_segment_a_parle():
    """Un segment composé d'avis App Store ne se joint pas par SMS : on n'a aucun
    numéro. La réponse publique, elle, atteint son auteur ET les visiteurs
    suivants de la fiche."""
    assert choisir_canal({"app_store": 120, "reddit": 10}) == "reponse_avis"
    assert choisir_canal({"reddit": 90, "app_store": 10}) == "reseaux_sociaux"


def test_la_demande_explicite_prime_sur_la_deduction():
    """Celui qui écrit le brief sait de quels canaux il dispose réellement — ce
    que cette base ignore complètement."""
    assert choisir_canal({"app_store": 200}, demande="sms") == "sms"


def test_un_canal_inconnu_ne_fait_pas_tomber_la_deduction():
    assert choisir_canal({"app_store": 200}, demande="pigeon") == "reponse_avis"


# ---------------------------------------------------------------------------
# Les leviers : des solutions fondées sur un motif MESURÉ
# ---------------------------------------------------------------------------


def test_les_leviers_suivent_le_motif_quand_il_domine():
    cible = _cible(motif="facturation_prix", motif_avis=90)
    actions = leviers(cible, SEGMENTS["insatisfaits_motif"], OBJECTIFS["reassurance"])
    assert actions
    assert actions[0] in LEVIERS["facturation_prix"]


def test_les_leviers_retombent_sur_l_objectif_quand_aucun_motif_ne_domine():
    """Prétendre le contraire serait une invention : sans motif dominant, le
    levier n'est plus « fondé sur la satisfaction », c'est une bonne pratique —
    et il faut que le code le sache."""
    cible = _cible(motif="app_bugs", motif_avis=5)
    actions = leviers(cible, SEGMENTS["detracteurs"], OBJECTIFS["retention"])
    assert actions
    assert actions[0] not in LEVIERS["app_bugs"]


def test_une_campagne_vers_des_clients_satisfaits_ne_leur_parle_pas_des_plaintes():
    """DÉFAUT CONSTATÉ SUR DONNÉES RÉELLES le 13 août 2026 : sur Vodacom South
    Africa, le segment retenu était celui des PROMOTEURS et les actions
    proposées disaient « répondre à chaque avis négatif sous 48 h ».

    Le motif dominant est mesuré sur les avis NÉGATIFS. L'appliquer à une
    campagne de fidélisation revient à répondre à des clients satisfaits en leur
    rappelant un problème — exactement ce que l'exclusion de cet objectif
    interdit en toutes lettres.
    """
    cible = _cible(
        part_negatifs=12.0, part_positifs=71.0, positifs=160, negatifs=27,
        motif="service_client", motif_avis=20,
    )
    segment = choisir_segment(cible)
    assert segment.cle == "promoteurs"
    assert motif_du_segment(cible, segment) is None

    actions = leviers(cible, segment, OBJECTIFS["fidelisation"])
    assert actions
    assert all(a not in LEVIERS["service_client"] for a in actions)
    assert all("négatif" not in a for a in actions)


def test_chaque_motif_de_la_taxonomie_a_ses_leviers():
    """VERROU DE COHÉRENCE. Un aspect ajouté à la taxonomie et oublié ici ferait
    retomber la campagne sur des leviers génériques sans que rien ne le
    signale — c'est-à-dire perdre exactement l'information pour laquelle
    l'analyse sémantique a été construite."""
    assert set(ASPECTS) == set(LEVIERS)


# ---------------------------------------------------------------------------
# Le brief libre
# ---------------------------------------------------------------------------


def test_un_perimetre_resolu_arrive_intact_dans_le_filtre():
    brief = valider_brief(
        {"operateur": "orange", "pays": "mali", "canal": "sms"},
        _CATALOGUE, "pour Orange au Mali par SMS",
    )
    assert brief.filtre.operators == (7,)
    assert brief.filtre.countries == ("ML",)
    assert brief.canal == "sms"
    assert brief.cible_imposee is True


def test_une_entite_inconnue_fait_refuser_et_non_approcher():
    """Bâtir la campagne sur un périmètre approchant produirait des chiffres
    justes sur la mauvaise filiale, et rien ne le signalerait."""
    with pytest.raises(QuestionRefusee):
        valider_brief({"operateur": "Bouygues"}, _CATALOGUE, "pour Bouygues")


def test_un_objectif_hors_liste_est_ignore_et_non_refuse():
    """Un brief est une orientation, pas une requête. « Quelque chose d'un peu
    vendeur pour les jeunes » ne se range dans aucune liste fermée, et refuser
    la demande pour autant serait absurde : les mesures décident alors."""
    brief = valider_brief(
        {"objectif": "parrainage", "canal": "pigeon"}, _CATALOGUE, "du parrainage"
    )
    assert brief.objectif is None and brief.canal is None


def test_le_texte_du_brief_est_conserve_tel_quel():
    """Il est retransmis au modèle pour le TON. Il ne traverse jamais la couche
    de mesure : aucune requête ne dépend d'un mot que l'utilisateur a écrit."""
    brief = valider_brief({}, _CATALOGUE, "quelque chose de chaleureux")
    assert brief.texte == "quelque chose de chaleureux"


# ---------------------------------------------------------------------------
# La rédaction : ce que le modèle n'a pas le droit d'écrire
# ---------------------------------------------------------------------------


def test_une_promesse_commerciale_est_detectee():
    """« Trois mois offerts » n'est pas une erreur d'analyse : c'est un
    engagement pris au nom de l'entreprise envers des gens qui le tiendront pour
    vrai."""
    assert promesses_detectees("Profitez de 3 mois offerts !") == ["offert"]
    assert promesses_detectees("Une remise vous attend") == ["remise"]
    assert promesses_detectees("Nous avons lu vos retours.") == []


def test_les_faux_positifs_courants_ne_declenchent_pas_le_garde_fou():
    """Un garde-fou qui rejette une phrase sur deux finit par être désactivé,
    donc par ne plus protéger de rien."""
    assert promesses_detectees("sans avoir à nous écrire") == []
    assert promesses_detectees("la double authentification") == []


def test_une_redaction_qui_promet_est_ecartee_au_profit_du_gabarit():
    """Le comportement complet : la proposition reste défendable, et la trace du
    rejet part dans les journaux."""
    modele = _Modele(
        {"accroche": "Orange Mali vous récompense",
         "message": "Pour nous faire pardonner, 2 Go offerts ce mois-ci."}
    )
    agent = _agent(modele)
    campagne = agent.proposer()

    assert campagne.redige_par_modele is False
    assert "offert" not in campagne.message.lower()
    assert campagne.message                      # le gabarit a pris le relais


def test_une_redaction_qui_invente_un_chiffre_est_ecartee():
    """Un pourcentage produit de tête est indiscernable d'un pourcentage mesuré
    une fois écrit dans une phrase."""
    modele = _Modele(
        {"accroche": "Nous vous écoutons",
         "message": "83 % de nos clients constatent déjà une amélioration."}
    )
    campagne = _agent(modele).proposer()
    assert campagne.redige_par_modele is False


def test_un_message_trop_long_pour_son_canal_est_ecarte():
    """Un SMS de 400 caractères n'est pas « un peu long » : il part en trois
    morceaux facturés trois fois, dont le dernier arrive tronqué."""
    campagne = Campagne(
        cible=_cible(), segment=SEGMENTS["detracteurs"],
        objectif=OBJECTIFS["retention"], canal=CANAUX["sms"], taille_segment=150,
    )
    refus = CampaignAgent._refus_de_redaction(
        _agent(), campagne, "Accroche", "x" * 300
    )
    assert "caractères" in refus


def test_une_redaction_fidele_est_conservee():
    modele = _Modele(
        {"accroche": "Vos retours sur la facturation",
         "message": "Nous avons lu ce que vous nous avez écrit sur vos factures. "
                    "Voici le détail de ce qui est décompté, et où le vérifier."}
    )
    campagne = _agent(modele).proposer()
    assert campagne.redige_par_modele is True
    assert "Vos retours sur la facturation" == campagne.accroche


def test_le_gabarit_respecte_la_longueur_du_canal_par_construction():
    """Un repli qui déborderait serait inutilisable exactement dans le cas où
    l'on en a besoin."""
    for cle, canal in CANAUX.items():
        campagne = Campagne(
            cible=_cible(motif="coupures_pannes", motif_avis=90),
            segment=SEGMENTS["detracteurs"], objectif=OBJECTIFS["retention"],
            canal=canal, taille_segment=150,
        )
        _, message = CampaignAgent._gabarit(campagne)
        assert len(message) <= canal.max_caracteres, cle


# ---------------------------------------------------------------------------
# Le passage complet
# ---------------------------------------------------------------------------


def test_sans_modele_la_campagne_reste_complete():
    """La cible, le segment, l'objectif et les leviers sont calculés. Seule la
    description libre exige un modèle — le reste doit fonctionner sans clé."""
    campagne = _agent(modele=None).proposer()

    assert campagne.refus is None
    assert campagne.cible.label == "Orange Mali"
    assert campagne.segment.cle == "insatisfaits_motif"
    assert campagne.objectif.cle == "reassurance"
    assert campagne.actions
    assert campagne.accroche and campagne.message
    assert campagne.redige_par_modele is False


def test_une_description_libre_sans_modele_est_refusee_et_non_ignoree():
    """Retomber sur « tout le périmètre » produirait une campagne parfaitement
    présentable sur la mauvaise filiale."""
    campagne = _agent(modele=None).proposer("pour Orange au Mali")
    assert campagne.refus
    assert "description" in campagne.refus.lower()


def test_la_campagne_enregistree_porte_ses_mesures():
    """Sans elles, le rapport comparerait l'état d'aujourd'hui à un souvenir."""
    depot = _Depot()
    _agent(modele=None, depot=depot).proposer()

    (ecrit,) = depot.creees
    assert ecrit["entity_level"] == "subsidiary"
    assert ecrit["segment"] == "insatisfaits_motif"
    assert ecrit["segment_size"] == 90
    assert ecrit["payload"]["cible"]["iso2"] == "ML"
    assert ecrit["payload"]["actions"]


def test_une_cible_deja_traitee_recemment_n_est_pas_reproposee():
    """En reproposer une tous les trois jours sur la même filiale, c'est demander
    à une équipe de relire une décision qu'elle vient de prendre."""
    depot = _Depot(derniere={
        "created_at": datetime.now(timezone.utc) - timedelta(days=2),
        "score": 88.0,
    })
    campagne = _agent(modele=None, depot=depot).proposer()

    assert campagne.refus
    assert depot.creees == []


def test_un_segment_qui_a_nettement_grossi_rouvre_le_sujet():
    """La dissymétrie de la règle de non-répétition : « déjà proposé » fait
    taire, « déjà proposé mais le segment a nettement grossi » fait reparler."""
    depot = _Depot(derniere={
        "created_at": datetime.now(timezone.utc) - timedelta(days=2),
        "score": 40.0,      # le segment en compte 90 aujourd'hui
    })
    campagne = _agent(modele=None, depot=depot).proposer()
    assert campagne.refus is None


def test_un_objectif_impose_par_l_utilisateur_est_suivi_mais_signale():
    """Mener une campagne à contre-mesure peut être un choix légitime, mais il
    doit être un choix conscient."""
    modele = _Modele(
        {"operateur": "Orange", "objectif": "fidelisation"},
        {"accroche": "Merci pour vos retours",
         "message": "Vos retours décident de ce que nous améliorons en premier."},
    )
    campagne = _agent(modele).proposer("fidéliser chez Orange")

    assert campagne.objectif.cle == "fidelisation"
    assert campagne.objectif_mesure == "reassurance"
    assert "reassurance" in campagne.as_dict()["objectif_mesure"]
    assert "mesures désignaient" in campagne.texte()


def test_un_perimetre_sans_cible_defendable_le_dit():
    """« Rien ne mérite une campagne cette semaine » est une information. La
    taire ferait croire à un agent en échec."""
    stats = _Stats(rows=[_ligne(avis_clients=8, negatifs=5)], motifs=[[]])
    campagne = _agent(modele=None, stats=stats).proposer()
    assert campagne.refus
    assert campagne.ecartees


def test_le_motif_autre_n_est_jamais_retenu_comme_motif():
    """« Autre » est le repli de la taxonomie : bâtir une campagne dessus
    reviendrait à écrire aux clients au sujet de rien."""
    stats = _Stats(motifs=[[{"term": "autre", "avis": 120}]])
    campagne = _agent(modele=None, stats=stats).proposer()
    assert campagne.cible.motif is None
    assert campagne.segment.cle == "detracteurs"


# ---------------------------------------------------------------------------
# Le rapport
# ---------------------------------------------------------------------------


def _campagne_en_base(objective="retention", jours=20):
    return {
        "campaign_id": 42,
        "entity_level": "subsidiary", "entity_key": "12",
        "entity_label": "Orange Mali",
        "segment": "detracteurs", "objective": objective, "status": "approved",
        "decided_at": datetime.now(timezone.utc) - timedelta(days=jours),
        "created_at": datetime.now(timezone.utc) - timedelta(days=jours + 2),
        "payload": {"cible": {"iso2": "ML", "motif_dominant": "facturation_prix"}},
    }


def _apercu(avis, part_negatifs, avant_avis, avant_part):
    return {
        "current": {"avis_clients": avis, "part_negatifs": part_negatifs,
                    "negatifs": int(avis * part_negatifs / 100)},
        "previous": {"avis_clients": avant_avis, "part_negatifs": avant_part,
                     "negatifs": int(avant_avis * avant_part / 100)},
    }


def test_le_rapport_mesure_la_satisfaction_et_le_dit():
    """Aucune donnée d'envoi, d'ouverture ou de clic n'existe : rendre un « taux
    d'ouverture » exigerait de l'inventer."""
    stats = _Stats(apercus=[_apercu(120, 55.0, 100, 68.0), _apercu(400, 40.0, 380, 39.0)])
    agent = _agent(modele=None, stats=stats, depot=_Depot(campagne=_campagne_en_base()))

    rapport = agent.rapport(42)
    assert rapport["available"] is True
    assert "aucun envoi" in rapport["avertissement"]
    assert rapport["kpi"]["delta"] == -13.0
    assert rapport["verdict"]["atteint"] is True


def test_un_mouvement_partage_par_tout_le_pays_est_signale():
    """Une part de négatifs qui baisse de treize points pendant que le pays
    baisse de douze n'est pas un succès de campagne, c'est une marée."""
    stats = _Stats(apercus=[_apercu(120, 55.0, 100, 68.0), _apercu(900, 41.0, 880, 53.0)])
    agent = _agent(modele=None, stats=stats, depot=_Depot(campagne=_campagne_en_base()))

    rapport = agent.rapport(42)
    assert "pays évolue dans le même sens" in rapport["verdict"]["texte"]


def test_un_rapport_sans_volume_ne_conclut_pas():
    """Une variation calculée sur cinq avis n'est pas une variation."""
    stats = _Stats(apercus=[_apercu(6, 50.0, 4, 75.0)])
    agent = _agent(modele=None, stats=stats, depot=_Depot(campagne=_campagne_en_base()))

    rapport = agent.rapport(42)
    assert rapport["verdict"]["conclut"] is False
    assert "Trop peu d'avis" in rapport["verdict"]["texte"]


def test_un_ecart_negligeable_ne_se_lit_pas_comme_un_succes():
    """Conclure sous la marge ferait reconduire une campagne qui n'a rien
    produit — le seul résultat vraiment coûteux."""
    stats = _Stats(apercus=[_apercu(120, 67.0, 100, 68.0), _apercu(400, 40.0, 380, 40.0)])
    agent = _agent(modele=None, stats=stats, depot=_Depot(campagne=_campagne_en_base()))

    rapport = agent.rapport(42)
    assert rapport["verdict"]["atteint"] is False
    assert "stable" in rapport["verdict"]["texte"]


def test_le_rapport_d_une_campagne_du_jour_ne_compare_rien():
    """Comparer une journée entamée à une journée pleine mesurerait l'heure
    qu'il est, pas la campagne."""
    agent = _agent(
        modele=None, depot=_Depot(campagne=_campagne_en_base(jours=0))
    )
    rapport = agent.rapport(42)
    assert rapport["available"] is False


def test_un_objectif_de_reassurance_suit_le_motif_et_non_le_taux_global():
    """La part globale de négatifs bougera surtout pour d'autres raisons : seule
    la part du motif dit si la réassurance a servi."""
    stats = _Stats(
        apercus=[_apercu(200, 60.0, 180, 62.0)],
        motifs=[[{"term": "facturation_prix", "avis": 30}],
                [{"term": "facturation_prix", "avis": 80}]],
    )
    agent = _agent(
        modele=None, stats=stats,
        depot=_Depot(campagne=_campagne_en_base(objective="reassurance")),
    )
    rapport = agent.rapport(42)

    assert rapport["kpi"]["cle"] == "part_motif"
    assert rapport["kpi"]["apres"] == 25.0     # 30 avis sur 120 négatifs
    assert rapport["kpi"]["avant"] == pytest.approx(72.1, abs=0.2)  # 80 sur 111
    assert rapport["verdict"]["atteint"] is True


# ---------------------------------------------------------------------------
# Les commandes Telegram
# ---------------------------------------------------------------------------


class _CampagneFactice:
    """Assistant de campagne factice, avec son dépôt."""

    def __init__(self):
        self.campagnes = _Depot()
        self.demandes = []

    def proposer(self, description=""):
        self.demandes.append(description)
        return Campagne(
            cible=_cible(), segment=SEGMENTS["detracteurs"],
            objectif=OBJECTIFS["retention"], canal=CANAUX["reponse_avis"],
            taille_segment=150, accroche="A", message="M", campaign_id=42,
        )

    def rapport(self, numero):
        return {"available": True, "texte": f"bilan {numero}"}


def _update(uid, texte, chat_id=-100, user_id=42, prive=False):
    return {
        "update_id": uid,
        "message": {
            "text": texte,
            "chat": {"id": chat_id, "type": "private" if prive else "supergroup"},
            "from": {"id": user_id, "username": "encadrant"},
        },
    }


def _update_callback(uid, data, chat_id=-100, user_id=42, message_id=55):
    return {
        "update_id": uid,
        "callback_query": {
            "id": f"cb{uid}",
            "data": data,
            "from": {"id": user_id, "username": "encadrant"},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "supergroup"},
            },
        },
    }


class _Canal:
    def __init__(self, *salves):
        self.salves = list(salves)
        self.envois = []
        self.reponses_callback = []
        self.claviers_retires = []
        self.utilisable = True

    def mises_a_jour(self, offset):
        if not self.salves:
            return [], offset
        updates = self.salves.pop(0)
        return updates, max(u["update_id"] for u in updates) + 1

    def envoyer(self, chat_id, texte):
        self.envois.append((chat_id, texte))
        return True

    def repondre_callback(self, callback_query_id, texte="", alerte=False):
        self.reponses_callback.append((callback_query_id, texte, alerte))
        return True

    def retirer_clavier(self, chat_id, message_id):
        self.claviers_retires.append((chat_id, message_id))
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


def _boucle(canal, campagne=None, journal=None, plafond=20):
    from reviews.agents.telegram_chat import BoucleConversation
    from reviews.config import ChatConfig

    class _Muet:
        def repondre(self, question):
            raise AssertionError("la question ne devait pas atteindre le chat")

    return BoucleConversation(
        agent=_Muet(),
        canal=canal,
        cfg=ChatConfig(daily_questions_per_user=plafond),
        journal=journal,
        chats_autorises={-100},
        campagne=campagne,
    )


def test_les_commandes_de_campagne_ne_tombent_pas_dans_l_assistant_de_questions():
    """RÉGRESSION SILENCIEUSE PAR EXCELLENCE : une commande non reconnue serait
    traitée comme une question libre. Elle consommerait deux appels de modèle
    pour répondre à côté, sans qu'aucune erreur n'apparaisse nulle part."""
    from reviews.agents.telegram_chat import analyser

    for texte, attendu in (
        ("/campagne", "campagne"),
        ("/campagne pour Orange au Mali", "campagne"),
        ("/campagnes", "campagnes"),
        ("/valider 12", "valider"),
        ("/rejeter 12", "rejeter"),
        ("/rapport 12", "rapport"),
    ):
        consigne = analyser(texte, prive=False)
        assert consigne.commande == attendu, texte
        assert consigne.question is None, texte


def test_campagnes_au_pluriel_ne_se_confond_pas_avec_campagne():
    """Les deux commandes ne diffèrent que d'un « s » : la première liste, la
    seconde consomme du modèle."""
    from reviews.agents.telegram_chat import analyser

    assert analyser("/campagnes", prive=True).commande == "campagnes"
    assert analyser("/campagne", prive=True).commande == "campagne"


def test_une_commande_de_decision_sans_numero_explique_au_lieu_d_agir():
    from reviews.agents.telegram_chat import analyser

    consigne = analyser("/valider", prive=True)
    assert consigne.commande is None
    assert "12" in consigne.reponse_immediate


def test_seules_les_commandes_qui_appellent_le_modele_coutent_du_quota():
    """Soumettre /valider au plafond interdirait de décider d'une campagne à
    quelqu'un qui a simplement beaucoup interrogé le robot dans la journée."""
    from reviews.agents.telegram_chat import analyser

    assert analyser("/q une question ?", prive=True).coute_un_appel is True
    assert analyser("/campagne libre", prive=True).coute_un_appel is True
    assert analyser("/valider 12", prive=True).coute_un_appel is False
    assert analyser("/campagnes", prive=True).coute_un_appel is False


def test_une_validation_passe_malgre_un_quota_epuise():
    campagne = _CampagneFactice()
    canal = _Canal([_update(1, "/valider 42")])
    boucle = _boucle(canal, campagne=campagne, journal=_Journal(deja=99))

    assert boucle.tour() == 1
    assert "validée" in canal.envois[0][1]


def test_une_proposition_est_refusee_quand_le_quota_est_epuise():
    campagne = _CampagneFactice()
    canal = _Canal([_update(1, "/campagne pour Orange")])
    boucle = _boucle(canal, campagne=campagne, journal=_Journal(deja=99))

    boucle.tour()
    assert "plafond" in canal.envois[0][1]
    assert campagne.demandes == []          # le modèle n'a pas été appelé


def test_une_decision_deja_prise_le_dit_au_lieu_de_l_ecraser():
    """Deux personnes cliquant à quelques secondes d'intervalle écraseraient
    mutuellement leur décision, et le journal attribuerait la campagne à la
    dernière."""
    campagne = _CampagneFactice()
    campagne.campagnes.decider = lambda *a, **kw: False
    canal = _Canal([_update(1, "/valider 42")])
    _boucle(canal, campagne=campagne).tour()

    assert "déjà été décidée" in canal.envois[0][1]


def test_sans_assistant_de_campagne_les_commandes_le_disent():
    """Son absence ne doit ni faire planter la boucle ni laisser la commande
    sans réponse : un silence serait attribué au réseau."""
    canal = _Canal([_update(1, "/campagne")])
    _boucle(canal, campagne=None).tour()
    assert "pas configuré" in canal.envois[0][1]


def test_une_commande_en_echec_devient_une_phrase_et_non_un_silence():
    campagne = _CampagneFactice()
    campagne.rapport = lambda numero: 1 / 0
    canal = _Canal([_update(1, "/rapport 42")])
    _boucle(canal, campagne=campagne).tour()

    assert "échoué" in canal.envois[0][1]


def test_le_numero_tolere_ce_qu_on_recopie_depuis_la_liste():
    """La liste préfixe les identifiants d'un dièse : exiger l'entier nu ferait
    échouer la commande la plus évidente à taper."""
    from reviews.agents.telegram_chat import _numero

    assert _numero("#12") == 12
    assert _numero("12.") == 12
    assert _numero("douze") is None


# ---------------------------------------------------------------------------
# Boutons Valider/Rejeter — même décision que la commande texte, autre porte
# ---------------------------------------------------------------------------


def test_un_clic_valider_decide_accuse_retire_le_clavier_et_confirme():
    campagne = _CampagneFactice()
    canal = _Canal([_update_callback(1, "valider:42")])
    journal = _Journal()
    boucle = _boucle(canal, campagne=campagne, journal=journal)

    assert boucle.tour() == 1
    assert campagne.campagnes.decisions == [(42, "approved", "encadrant")]
    assert canal.claviers_retires == [(-100, 55)]
    assert "validée" in canal.envois[0][1]
    assert canal.reponses_callback[0][0] == "cb1"
    assert canal.reponses_callback[0][2] is False   # pas d'alerte sur un succès
    (ecrit,) = journal.ecrits
    assert ecrit["payload"]["via"] == "bouton"
    assert ecrit["payload"]["commande"] == "valider"


def test_un_clic_rejeter_decide_rejected_et_non_approved():
    campagne = _CampagneFactice()
    canal = _Canal([_update_callback(1, "rejeter:42")])
    boucle = _boucle(canal, campagne=campagne)

    boucle.tour()
    assert campagne.campagnes.decisions == [(42, "rejected", "encadrant")]
    assert "écartée" in canal.envois[0][1]


def test_le_texte_de_confirmation_est_identique_bouton_ou_commande():
    """UN SEUL POINT DE VÉRITÉ (`_texte_decision`) : la phrase qui confirme une
    décision ne doit jamais dépendre du moyen utilisé pour décider."""
    campagne_texte = _CampagneFactice()
    canal_texte = _Canal([_update(1, "/valider 42")])
    _boucle(canal_texte, campagne=campagne_texte).tour()

    campagne_bouton = _CampagneFactice()
    canal_bouton = _Canal([_update_callback(1, "valider:42")])
    _boucle(canal_bouton, campagne=campagne_bouton).tour()

    assert canal_texte.envois[0][1] == canal_bouton.envois[0][1]


def test_un_clic_sur_une_decision_deja_prise_alerte_sans_rien_ecraser():
    campagne = _CampagneFactice()
    campagne.campagnes.decider = lambda *a, **kw: False
    canal = _Canal([_update_callback(1, "valider:42")])
    boucle = _boucle(canal, campagne=campagne)

    boucle.tour()
    assert canal.envois == []                # aucune confirmation envoyée
    assert canal.claviers_retires == []       # le clavier reste en place
    _, texte, alerte = canal.reponses_callback[0]
    assert "déjà décidée" in texte
    assert alerte is True


def test_un_clic_hors_conversation_autorisee_reste_silencieux_mais_accuse():
    """Même principe que pour un message non autorisé : pas de décision, pas
    de réponse informative — seulement l'accusé technique qu'exige Telegram."""
    campagne = _CampagneFactice()
    canal = _Canal([_update_callback(1, "valider:42", chat_id=999)])
    boucle = _boucle(canal, campagne=campagne)

    boucle.tour()
    assert campagne.campagnes.decisions == []
    assert canal.envois == []
    assert canal.reponses_callback == [("cb1", "", False)]


def test_un_clic_sans_assistant_de_campagne_alerte_au_lieu_de_planter():
    canal = _Canal([_update_callback(1, "valider:42")])
    boucle = _boucle(canal, campagne=None)

    boucle.tour()
    _, texte, alerte = canal.reponses_callback[0]
    assert "non configuré" in texte
    assert alerte is True


def test_un_bouton_illisible_alerte_sans_lever():
    campagne = _CampagneFactice()
    canal = _Canal([_update_callback(1, "autrechose:abc")])
    boucle = _boucle(canal, campagne=campagne)

    boucle.tour()
    assert campagne.campagnes.decisions == []
    _, texte, alerte = canal.reponses_callback[0]
    assert alerte is True


def test_la_campagne_proposee_porte_un_clavier_valider_rejeter():
    """Les boutons accompagnent la proposition envoyée au groupe, en plus des
    commandes texte — jamais à leur place (voir `_transmettre`)."""
    from reviews.config import get_settings
    from reviews.storage.db import get_database

    class _NotifierFactice:
        def __init__(self):
            self.envois = []

        def send_text(self, corps_html, reply_markup=None):
            self.envois.append((corps_html, reply_markup))
            return True

    agent = CampaignAgent(
        db=object(), settings=get_settings(), notifier=_NotifierFactice(),
    )
    campagne = Campagne(
        cible=_cible(), segment=SEGMENTS["detracteurs"],
        objectif=OBJECTIFS["retention"], canal=CANAUX["reponse_avis"],
        taille_segment=150, accroche="A", message="M", campaign_id=42,
    )
    agent._transmettre(campagne)

    (corps, markup) = agent.notifier.envois[0]
    assert "/valider 42" in corps            # repli texte conservé
    boutons = markup["inline_keyboard"][0]
    assert boutons[0]["callback_data"] == "valider:42"
    assert boutons[1]["callback_data"] == "rejeter:42"


# ---------------------------------------------------------------------------
# Le contexte : ce qui rend la campagne réaliste, et ce qu'on ne sait pas
# ---------------------------------------------------------------------------


def test_une_dimension_demandee_mais_absente_est_declaree_et_non_tue():
    """LE SILENCE SUR UNE DIMENSION ABSENTE EST LU COMME UNE AFFIRMATION. Un
    lecteur qui demande « les jeunes » et reçoit un segment sans un mot sur
    l'âge suppose que l'âge a été pris en compte — c'est le comportement normal
    devant un outil qui affiche un résultat."""
    from reviews.agents.contexte import declarer_indisponibles

    phrases = declarer_indisponibles(["age", "motif", "ville"])
    assert len(phrases) == 2                      # motif est disponible
    assert any("âge" in p for p in phrases)
    assert any("ville" in p for p in phrases)
    assert all("Pour l'obtenir" in p for p in phrases)


def test_une_dimension_disponible_ne_produit_aucun_bruit():
    """Le cas normal ne doit rien afficher : un avertissement systématique
    s'apprend à sauter, y compris le jour où il compte."""
    from reviews.agents.contexte import declarer_indisponibles

    assert declarer_indisponibles(["motif", "pays", "satisfaction"]) == []
    assert declarer_indisponibles([]) == []


def test_une_dimension_inconnue_du_catalogue_est_ignoree_sans_bruit():
    """Le modèle qui traduit la demande peut rendre un mot hors liste : ce n'est
    pas une raison d'alarmer sur une dimension jamais demandée."""
    from reviews.agents.contexte import declarer_indisponibles

    assert declarer_indisponibles(["licorne"]) == []


def test_les_avertissements_survivent_a_l_absence_de_contexte():
    """Le contexte marché est facultatif ; la déclaration de ce qui manque ne
    l'est pas. Elle ne dépend d'aucune source, seulement de ce qui a été
    demandé."""
    modele = _Modele(
        {"operateur": "Orange", "segmentation": ["age", "ville"]},
        {"nom": "Facture Claire", "accroche": "Votre facture",
         "message": "Nous détaillons ce qui est décompté."},
    )
    campagne = _agent(modele, contexte=None).proposer("les jeunes de Casablanca")

    assert len(campagne.contexte.indisponibles) == 2
    assert "âge" in campagne.texte()
    assert "ville" in campagne.texte()


def test_le_probleme_identifie_est_calcule_et_porte_les_mesures():
    """C'est le champ que l'équipe relira dans six semaines pour juger si la
    campagne visait juste : rédigé librement, il changerait de formulation à
    chaque appel et deviendrait incomparable."""
    campagne = _agent(modele=None).proposer()

    assert "%" in campagne.probleme
    assert "220 avis" in campagne.probleme
    assert "facturation" in campagne.probleme.lower()
    assert "90 de ces plaintes" in campagne.probleme   # les avis portant le motif


def test_la_campagne_porte_un_nom_meme_sans_modele():
    """Une campagne se cite par son nom en réunion, jamais par son numéro."""
    campagne = _agent(modele=None).proposer()
    assert campagne.nom
    assert "Orange Mali" in campagne.nom


def test_le_nom_du_modele_est_retenu_quand_le_texte_passe_les_verifications():
    modele = _Modele(
        {"nom": "Facture Claire", "accroche": "Votre facture, expliquée",
         "message": "Nous détaillons ce qui est décompté, et où le vérifier."}
    )
    campagne = _agent(modele).proposer()
    assert campagne.nom == "Facture Claire"


def test_un_nom_produit_avec_un_texte_rejete_n_est_pas_retenu():
    """Retenir le nom d'un appel dont le message a été rejeté laisserait une
    campagne au gabarit sous un nom inventé — un mélange illisible."""
    modele = _Modele(
        {"nom": "Cadeau Data", "accroche": "Offre",
         "message": "Profitez de 2 Go offerts."}
    )
    campagne = _agent(modele).proposer()

    assert campagne.redige_par_modele is False
    assert campagne.nom != "Cadeau Data"


def test_le_contexte_marche_n_est_repris_que_s_il_eclaire_le_motif():
    """Accoler la couverture 4G à une plainte de facturation ferait du contexte
    un décor : le lecteur apprend à sauter la ligne, y compris quand elle porte
    l'information décisive."""
    from reviews.agents.contexte import Contexte

    pertinent = Contexte(
        marche=["panier data mobile 4,6 $/mois (-3,2 %)", "couverture 4G 88,9 %"],
        annee_marche="2025",
    )
    hors_sujet = Contexte(marche=["couverture 4G 88,9 %"], annee_marche="2024")

    cible = _cible(motif="facturation_prix", motif_avis=90)
    avec = Campagne(cible=cible, segment=SEGMENTS["insatisfaits_motif"],
                    objectif=OBJECTIFS["reassurance"], contexte=pertinent)
    sans = Campagne(cible=cible, segment=SEGMENTS["insatisfaits_motif"],
                    objectif=OBJECTIFS["reassurance"], contexte=hors_sujet)

    assert "panier data" in CampaignAgent._probleme(avec)
    assert "couverture" not in CampaignAgent._probleme(sans)


# ---------------------------------------------------------------------------
# Stratégies, révision, contenus
# ---------------------------------------------------------------------------


def test_l_option_A_porte_toujours_l_objectif_mesure():
    """Les trois angles ne sont pas équivalents : A est celui que les chiffres
    désignent, B et C sont des alternatives assumées. Les présenter à égalité
    ferait choisir au goût entre trois propositions dont une seule est fondée."""
    from reviews.agents.campagne import strategies_pour

    options = strategies_pour(OBJECTIFS["reassurance"])
    assert options[0].cle == "A"
    assert options[0].objectif == "reassurance"
    assert [o.cle for o in options] == ["A", "B", "C"]


def test_un_angle_en_double_est_ecarte():
    """Sur un segment déjà satisfait, « fidélisation » serait proposé deux fois :
    une fois comme mesure, une fois comme alternative. Deux options identiques
    dans une liste de trois donnent l'impression d'un choix inexistant."""
    from reviews.agents.campagne import strategies_pour

    options = strategies_pour(OBJECTIFS["fidelisation"])
    objectifs = [o.objectif for o in options]
    assert len(objectifs) == len(set(objectifs))


def test_les_angles_commerciaux_exigent_une_offre_que_l_agent_n_invente_pas():
    """« Offrir un bonus data » est une stratégie valable — mais le bonus, son
    volume et son coût ne sont écrits nulle part dans cette plateforme."""
    from reviews.domain.marketing import EMPLACEMENT_OFFRE, STRATEGIES

    assert STRATEGIES["B"].exige_une_offre is True
    assert STRATEGIES["C"].exige_une_offre is True
    assert STRATEGIES["A"].exige_une_offre is False

    campagne = Campagne(
        cible=_cible(), segment=SEGMENTS["detracteurs"],
        objectif=OBJECTIFS["conversion"], canal=CANAUX["sms"], taille_segment=150,
    )
    _, message = CampaignAgent._gabarit(campagne)
    assert EMPLACEMENT_OFFRE in message


def test_un_objectif_non_mesurable_est_signale_dans_la_proposition():
    """Le proposer sans le dire produirait un rapport de campagne qui se rédige
    aussi bien avant qu'après."""
    campagne = Campagne(
        cible=_cible(), segment=SEGMENTS["detracteurs"],
        objectif=OBJECTIFS["upselling"], canal=CANAUX["sms"], taille_segment=150,
        nom="Test", probleme="…",
    )
    texte = campagne.texte()
    assert "ne sera pas mesurable" in texte
    assert "ARPU" in texte


def test_un_kpi_national_est_signale_comme_non_attribuable():
    """La consommation data ne se publie que par pays et par an : elle bougera
    aussi pour les concurrents."""
    campagne = Campagne(
        cible=_cible(), segment=SEGMENTS["detracteurs"],
        objectif=OBJECTIFS["usage"], canal=CANAUX["sms"], taille_segment=150,
        nom="Test", probleme="…",
    )
    assert "NATIONAL" in campagne.texte()


def test_le_ton_se_devine_sans_appeler_le_modele():
    """Une révision de ton doit coûter UN appel — la réécriture — et non deux."""
    from reviews.agents.campaign_agent import _ton_devine

    assert _ton_devine("fais une version plus agressive commercialement") == "commercial"
    assert _ton_devine("plus empathique s'il te plaît") == "empathique"
    assert _ton_devine("quelque chose de plus officiel") == "institutionnel"
    assert _ton_devine("change juste la fin") == "factuel"


def test_un_ton_hors_liste_ne_fait_pas_echouer_la_revision():
    """« Plus punchy », « comme Orange » sont des demandes légitimes qui ne se
    rangent dans aucune case. Refuser la révision serait absurde."""
    from reviews.agents.campagne import valider_ton

    assert valider_ton("comme Orange") == "factuel"
    assert valider_ton("commercial") == "commercial"


def test_une_declinaison_qui_promet_est_entierement_ecartee():
    """Un modèle qui promet une remise dans le SMS est en mode commercial : rien
    ne dit que l'e-mail est plus sage, il est seulement plus long à relire."""
    agent = _agent()
    ligne = {
        "hook": "Votre facture", "message": "Le détail est dans l'application.",
        "segment_size": 54, "payload": {"cible": {"avis_clients": 156}},
    }
    produits = agent._retenir_contenus(
        {"sms": {"texte": "2 Go offerts ce mois-ci !"},
         "reseaux": {"texte": "Nous expliquons votre facture."}},
        ligne,
    )
    assert "offert" not in produits["sms"]["texte"].lower()
    # LE LOT ENTIER retombe sur les gabarits, y compris le format irréprochable :
    # c'est la confiance dans l'appel qui est en cause, pas ce format-là.
    assert produits["reseaux"]["texte"] == "Le détail est dans l'application."


def test_un_format_trop_long_retombe_seul_sur_son_gabarit():
    """Un dépassement de longueur est mécanique et n'engage que son format,
    contrairement à une promesse qui met en doute tout le lot."""
    agent = _agent()
    ligne = {
        "hook": "Votre facture", "message": "Le détail est dans l'application.",
        "segment_size": 54, "payload": {"cible": {"avis_clients": 156}},
    }
    produits = agent._retenir_contenus(
        {"sms": {"texte": "x" * 300},
         "reseaux": {"texte": "Nous expliquons votre facture."}},
        ligne,
    )
    assert produits["sms"]["texte"] == "Le détail est dans l'application."
    assert produits["reseaux"]["texte"] == "Nous expliquons votre facture."


def test_les_gabarits_de_formats_respectent_toutes_les_longueurs():
    """Un repli qui déborderait serait inutilisable exactement quand on en a
    besoin : quand le modèle est absent ou vient d'être écarté."""
    from reviews.agents.campaign_agent import _contenus_gabarit
    from reviews.domain.marketing import FORMATS

    produits = _contenus_gabarit({
        "hook": "A" * 200, "message": "B" * 2000,
    })
    for cle, format_ in FORMATS.items():
        corps = " ".join(produits[cle].values())
        assert len(corps) <= format_.max_caracteres, cle


def test_un_indicateur_trop_ancien_est_ecarte_du_contexte():
    """DÉFAUT RÉEL DU 13 AOÛT 2026 : le contexte du Mali affichait
    « (2017–2025) », mêlant un prix de 2025 et un trafic data de 2017 valant
    0,0 Go/mois. Un chiffre de neuf ans ne rend pas une campagne réaliste, il la
    rend fausse — et il est d'autant plus dangereux qu'il paraît précis."""
    from datetime import date

    from reviews.agents.contexte import ANCIENNETE_MAX_ANNEES, CollecteurDeContexte

    annee = date.today().year

    class _Marche:
        @staticmethod
        def latest(iso2):
            return {
                "PRI_DO_MOB|USD": {"value": 8.0, "year": annee - 1,
                                   "variation_pct": 22.1},
                "IT_BB_MOB_TRF|XB_Y": {"value": 0.0,
                                       "year": annee - ANCIENNETE_MAX_ANNEES - 5,
                                       "variation_pct": None},
            }

    lignes, periode = CollecteurDeContexte(
        stats=_Stats(), marche=_Marche()
    )._marche("ML")

    assert len(lignes) == 1
    assert "panier data" in lignes[0]
    assert periode == str(annee - 1)


def test_la_declinaison_va_jusqu_au_bout_avec_un_modele():
    """RÉGRESSION VÉCUE : le gabarit de prompt portait un « {emplacement} » que
    l'appelant ne fournissait pas. Un `KeyError` qui ne se déclenche qu'au
    moment d'appeler le modèle — donc jamais dans un test qui s'arrête aux
    décisions, et toujours devant l'utilisateur."""
    depot = _Depot(campagne={
        "campaign_id": 7, "hook": "Votre facture",
        "message": "Le détail est dans l'application.",
        "objective": "reassurance", "segment_size": 54, "tone": "commercial",
        "entity_label": "Orange Mali", "problem": "…",
        "payload": {"cible": {"avis_clients": 156}, "actions": ["Détailler"]},
        "contents": {},
    })
    modele = _Modele({
        "sms": {"texte": "Votre facture, expliquée dans l'application."},
        "push": {"titre": "Votre facture", "texte": "Le détail est en ligne."},
        "email": {"objet": "Votre facture", "introduction": "Bonjour,",
                  "corps": "Le détail est dans l'application.",
                  "appel_action": "Consultez votre espace."},
        "reseaux": {"texte": "Nous détaillons votre facture."},
        "annonce": {"titre": "Facture claire", "description": "Le détail en ligne.",
                    "appel_action": "En savoir plus"},
    })
    resultat = _agent(modele, depot=depot).contenus(7)

    assert resultat["available"] is True
    assert "SMS" in resultat["texte"]
    assert "E-mail" in resultat["texte"]
    assert depot.contenus_ecrits            # les formats sont persistés


def test_des_contenus_deja_produits_ne_sont_pas_regeneres():
    """Régénérer donnerait un autre texte pour la même campagne, et l'équipe ne
    saurait plus lequel a été validé."""
    depot = _Depot(campagne={
        "campaign_id": 7, "hook": "h", "message": "m", "objective": "reassurance",
        "segment_size": 54, "entity_label": "Orange Mali", "problem": "…",
        "payload": {}, "contents": {"sms": {"texte": "déjà écrit"}},
    })
    resultat = _agent(_Modele(), depot=depot).contenus(7)

    assert "déjà écrit" in resultat["texte"]
    assert resultat["regenere"] is False


def test_le_repli_ne_coupe_jamais_au_milieu_d_un_mot():
    """DÉFAUT VU À L'ÉCRAN LE 16 AOÛT 2026 : le SMS de repli affichait
    « …directement dans votre applicati ». La limite technique était respectée
    et le texte inenvoyable — or ce repli sert justement dans les cas visibles,
    quand le modèle vient d'être écarté."""
    from reviews.agents.campaign_agent import _couper

    coupe = _couper("directement dans votre application mobile", 30)
    assert coupe.endswith("…")
    assert "applicati…" not in coupe
    assert len(coupe) <= 30

    # Rien à couper : le texte revient intact, sans points de suspension.
    assert _couper("court", 30) == "court"

    # Un « mot » plus long que la limite entière n'a aucune frontière : la coupe
    # brutale vaut mieux qu'une chaîne vide.
    assert len(_couper("A" * 100, 20)) <= 20


def test_une_campagne_refusee_traverse_l_affichage_sans_lever():
    """RÉGRESSION VÉCUE : `proposer` rend un refus SANS cible ni segment dès
    qu'aucun périmètre ne franchit les seuils — un refroidissement suffit. Tout
    appelant qui traverse le résultat sans avoir regardé `refus` d'abord tombait
    alors sur un AttributeError, transformant une réponse normale en trace
    d'exception."""
    vide = Campagne(refus="toutes les cibles sont en refroidissement")

    assert vide.texte() == "toutes les cibles sont en refroidissement"
    assert motif_du_segment(None, None) is None
    assert CampaignAgent._gabarit(vide) == ("", "")
    assert CampaignAgent._probleme(vide) == ""
    assert "aucune campagne" in vide.resume()
