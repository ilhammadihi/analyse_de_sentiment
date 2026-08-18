"""
Vocabulaire fermé des campagnes : segments, objectifs, canaux, leviers.

POURQUOI CES LISTES SONT FERMÉES — C'EST LA LEÇON DE `aspects.py`
    Laisser un modèle nommer librement le segment (« clients frustrés »,
    « utilisateurs mécontents », « abonnés à risque ») produirait autant de
    formulations que d'appels : rien ne serait comparable d'une campagne à
    l'autre, et le rapport ne pourrait pas dire si le même segment a bougé.
    Une liste fermée force le choix, et c'est ce qui rend les campagnes
    comptables dans le temps.

    Même raisonnement pour les objectifs, avec une conséquence en plus : chaque
    objectif porte ICI le CRITÈRE qui dira s'il est atteint. Un objectif dont on
    ne sait pas mesurer l'atteinte n'est pas un objectif, c'est une intention —
    et un rapport de campagne assis sur une intention se rédige aussi bien avant
    qu'après.

CE QU'UN SEGMENT EST, ET CE QU'IL N'EST PAS — À LIRE AVANT D'AJOUTER UNE ENTRÉE
    La plateforme ne connaît AUCUN abonné. Elle ne collecte que des avis
    publics : elle sait qu'un client s'est plaint de sa facture sur le Play
    Store, elle ne connaît ni son numéro, ni son forfait, ni son ancienneté.

    Un « segment » est donc un ensemble d'AVIS, pas de clients. La différence
    n'est pas une nuance de vocabulaire : elle commande le canal. On ne peut pas
    envoyer un SMS à quelqu'un dont on n'a pas le numéro, mais on peut répondre
    publiquement là où il a parlé — et cette réponse est lue par tous les
    suivants. C'est pourquoi les libellés disent « clients ayant déposé un
    avis » et jamais « clients », et pourquoi `CANAL_PAR_SOURCE` existe.

LES LEVIERS SONT UN CATALOGUE, PAS UNE GÉNÉRATION
    « Proposition de solutions fondées sur la satisfaction client » : le
    FONDEMENT est le motif mesuré (l'aspect), la SOLUTION est écrite ici, à la
    main, une fois. Demander les actions au modèle donnerait des conseils
    plausibles, différents à chaque appel, que personne n'aurait relus. Ici,
    c'est le rattachement motif -> action qui est automatique ; l'action, elle,
    est relue et stable.
"""

from dataclasses import dataclass
from typing import Optional

from reviews.domain.aspects import ASPECTS, OTHER

# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """Un segment adressable, et comment on le compte."""

    cle: str
    label: str

    #: Ce qui définit l'appartenance, en français, tel qu'il sera affiché sous
    #: la campagne. Un segment dont le critère n'est pas montré est un segment
    #: qu'on ne peut pas contester — donc qu'on ne peut pas corriger.
    critere: str

    #: Polarité des avis qui composent le segment. Commande la mesure de sa
    #: taille et le choix des verbatims du rapport.
    polarite: str


#: Les trois segments servis, du plus précis au plus large.
#:
#: TROIS, ET AUCUN SEGMENT « SILENCIEUX ». La tentation serait d'ajouter les
#: clients qui n'ont rien écrit — le gros du parc. Ils n'existent dans aucune
#: table : les compter reviendrait à inventer un effectif, et à bâtir une
#: campagne sur un chiffre que rien ne soutient.
SEGMENTS: dict[str, Segment] = {
    "insatisfaits_motif": Segment(
        cle="insatisfaits_motif",
        label="Clients ayant déposé un avis négatif sur un motif précis",
        critere="avis clients négatifs dont le motif dominant est identifié",
        polarite="negative",
    ),
    "detracteurs": Segment(
        cle="detracteurs",
        label="Clients ayant déposé un avis négatif",
        critere="avis clients négatifs, tous motifs confondus",
        polarite="negative",
    ),
    "promoteurs": Segment(
        cle="promoteurs",
        label="Clients ayant déposé un avis positif",
        critere="avis clients positifs, tous motifs confondus",
        polarite="positive",
    ),
}


# ---------------------------------------------------------------------------
# Objectifs marketing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Objectif:
    """Un objectif, et le chiffre qui dira s'il est atteint."""

    cle: str
    label: str
    definition: str

    #: Mesure suivie par le rapport. Doit être l'une des clés produites par
    #: `StatsRepository.overview`, ou `part_motif` — calculé à part, à partir
    #: des motifs, parce qu'aucun agrégat général ne le porte.
    kpi: str

    #: Sens attendu : "baisse" ou "hausse". Sans lui, le rapport ne pourrait pas
    #: dire si un mouvement est un succès ou un échec — il n'y a pas de sens
    #: universel : une hausse du nombre d'avis est bonne pour l'acquisition et
    #: neutre pour la rétention.
    sens: str

    kpi_label: str

    #: Ce que cet objectif N'EST PAS. Écrit pour le prompt de rédaction : sans
    #: cette frontière, le modèle transforme n'importe quel objectif en
    #: promotion, qui est le registre marketing par défaut.
    exclusion: str

    #: L'atteinte de cet objectif est-elle mesurable avec ce que collecte la
    #: plateforme ?
    #:
    #: TOUS NE LE SONT PAS, ET C'EST À DIRE PLUTÔT QU'À CACHER. Conversion,
    #: montée en gamme et vente croisée sont des objectifs marketing
    #: parfaitement légitimes ; ils exigent de savoir quelle offre chaque client
    #: a souscrite, ce qu'aucun avis public ne dit. Les proposer sans le
    #: signaler produirait un rapport de campagne qui se rédige aussi bien avant
    #: qu'après — le défaut exact que le KPI existe pour empêcher.
    #:
    #: Un objectif non mesurable n'est JAMAIS choisi par l'arbitrage : il ne
    #: peut qu'être imposé explicitement par l'utilisateur, qui est alors
    #: prévenu.
    mesurable: bool = True

    #: Ce qu'il faudrait collecter pour mesurer l'atteinte. Vide si mesurable.
    donnee_manquante: str = ""

    #: Maille à laquelle le KPI existe : `entite` ou `pays`.
    #:
    #: LA DISTINCTION N'EST PAS THÉORIQUE. La consommation data n'est publiée
    #: que par pays (UIT) : une campagne d'une filiale peut la faire bouger, mais
    #: le chiffre bougera aussi pour ses concurrents. Un rapport qui présenterait
    #: une hausse nationale comme le résultat d'une campagne serait faux, et
    #: personne ne pourrait le contredire sans aller lire la source.
    maille: str = "entite"


#: Quatre objectifs, chacun avec un KPI distinct et mesurable sur les avis.
#:
#: L'ABSENCE D'UN OBJECTIF « ACQUISITION DE NOUVEAUX CLIENTS » AU SENS STRICT
#: est délibérée : la plateforme ne voit que des gens qui parlent déjà de
#: l'opérateur. `acquisition` désigne donc ce qui est réellement mesurable —
#: faire de la satisfaction constatée un argument public, et vérifier que le
#: nombre d'avis positifs augmente. Promettre de mesurer des abonnements gagnés
#: serait promettre une donnée qui n'entrera jamais dans cette base.
OBJECTIFS: dict[str, Objectif] = {
    "retention": Objectif(
        cle="retention",
        label="Rétention",
        definition=(
            "retenir des clients qui viennent d'exprimer une insatisfaction, en "
            "montrant que leur motif est traité"
        ),
        kpi="part_negatifs",
        sens="baisse",
        kpi_label="part d'avis négatifs",
        exclusion=(
            "ce n'est pas une promotion : un client qui part parce que le réseau "
            "tombe ne reste pas pour une remise"
        ),
    ),
    "reassurance": Objectif(
        cle="reassurance",
        label="Réassurance",
        definition=(
            "lever un malentendu sur ce que le client paie ou consomme, en "
            "expliquant plutôt qu'en compensant"
        ),
        kpi="part_motif",
        sens="baisse",
        kpi_label="part du motif dominant dans les avis négatifs",
        exclusion=(
            "ce n'est ni un geste commercial ni un remboursement : l'objectif est "
            "que le client COMPRENNE, pas qu'il soit dédommagé"
        ),
    ),
    "fidelisation": Objectif(
        cle="fidelisation",
        label="Fidélisation",
        definition=(
            "renforcer l'attachement des clients déjà satisfaits, et leur donner "
            "une raison de le redire"
        ),
        kpi="part_positifs",
        sens="hausse",
        kpi_label="part d'avis positifs",
        exclusion=(
            "ce n'est pas une campagne de reconquête : ces clients ne sont pas "
            "mécontents, leur rappeler un problème serait contre-productif"
        ),
    ),
    "acquisition": Objectif(
        cle="acquisition",
        label="Acquisition par la preuve",
        definition=(
            "faire de la satisfaction constatée un argument public, auprès de "
            "clients qui hésitent encore"
        ),
        kpi="positifs",
        sens="hausse",
        kpi_label="nombre d'avis positifs",
        exclusion=(
            "aucun chiffre d'abonnés, de parts de marché ou de conquête ne peut "
            "être avancé : cette base ne contient que des avis"
        ),
    ),
    "satisfaction": Objectif(
        cle="satisfaction",
        label="Satisfaction",
        definition=(
            "faire remonter le ressenti général, au-delà du seul motif qui "
            "domine les plaintes"
        ),
        # LA NOTE ET NON LA PART DE NÉGATIFS, pour ne pas doubler la rétention.
        # Les deux mesures bougent ensemble mais pas au même rythme : on peut
        # faire taire les plaintes extrêmes sans que le ressenti moyen monte,
        # et c'est précisément la différence entre calmer et satisfaire.
        kpi="note_moyenne",
        sens="hausse",
        kpi_label="note moyenne",
        exclusion=(
            "ce n'est pas une campagne de réparation ciblée : elle s'adresse à "
            "tous, pas aux seuls plaignants"
        ),
    ),
    "usage": Objectif(
        cle="usage",
        label="Usage",
        definition="augmenter la consommation de données des clients",
        kpi="consommation_pays",
        sens="hausse",
        kpi_label="consommation data par abonné (national)",
        exclusion=(
            "aucun volume de données ne peut être promis : les forfaits ne sont "
            "pas connus de cette plateforme"
        ),
        # MESURABLE, MAIS PAS ATTRIBUABLE. L'UIT publie les Go par abonné par
        # PAYS et par AN : le chiffre bougera aussi pour les concurrents, et il
        # arrivera avec un an de retard. On le dit plutôt que de laisser croire
        # à un suivi de campagne.
        maille="pays",
    ),
    "conversion": Objectif(
        cle="conversion",
        label="Conversion",
        definition="faire souscrire une offre supérieure à des clients existants",
        kpi="",
        sens="hausse",
        kpi_label="taux de souscription",
        exclusion=(
            "l'offre elle-même doit venir de vous : l'agent ne connaît aucun "
            "catalogue et n'en inventera pas"
        ),
        mesurable=False,
        donnee_manquante=(
            "l'offre souscrite par client, avant et après — elle n'existe que "
            "dans le système de facturation de l'opérateur"
        ),
    ),
    "upselling": Objectif(
        cle="upselling",
        label="Montée en gamme",
        definition="faire passer les clients vers une offre plus rentable",
        kpi="",
        sens="hausse",
        kpi_label="revenu moyen par client",
        exclusion=(
            "aucun prix ni aucun avantage ne peut être annoncé sans vous"
        ),
        mesurable=False,
        donnee_manquante=(
            "l'ARPU par client, ou à défaut par filiale : aucune source gratuite "
            "ne le publie sous le niveau du pays"
        ),
    ),
    "cross_selling": Objectif(
        cle="cross_selling",
        label="Vente croisée",
        definition="promouvoir un service que le client n'utilise pas encore",
        kpi="",
        sens="hausse",
        kpi_label="taux d'adoption du service",
        exclusion=(
            "l'agent ignore quels services le client utilise déjà : il ne peut "
            "pas affirmer qu'un service est nouveau pour lui"
        ),
        mesurable=False,
        donnee_manquante=(
            "le parc de services par client — même source que la conversion"
        ),
    ),
}

#: Objectifs que l'arbitrage a le droit de CHOISIR.
#:
#: Les autres restent proposables, mais seulement sur demande explicite : un
#: agent qui choisirait de lui-même un objectif dont il ne saura jamais dire
#: s'il est atteint fabriquerait des campagnes invérifiables en série.
OBJECTIFS_MESURABLES = tuple(o.cle for o in OBJECTIFS.values() if o.mesurable)


# ---------------------------------------------------------------------------
# Canaux
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Canal:
    cle: str
    label: str

    #: Longueur maximale du message, en caractères. CONTRAINTE DURE, vérifiée
    #: après rédaction : un SMS de 400 caractères n'est pas « un peu long », il
    #: part en trois morceaux facturés trois fois, et le troisième arrive coupé.
    max_caracteres: int

    #: Ce que le canal permet, et ce qu'il coûte. Transmis au modèle : un même
    #: message ne s'écrit pas de la même façon selon qu'il est lu par une
    #: personne ou par tous les visiteurs d'une fiche.
    note: str


CANAUX: dict[str, Canal] = {
    "reponse_avis": Canal(
        cle="reponse_avis",
        label="Réponse publique aux avis",
        max_caracteres=350,
        note=(
            "le message répond publiquement là où le client a écrit ; il est lu "
            "par tous les visiteurs suivants, donc il s'adresse autant à eux qu'à "
            "l'auteur de l'avis"
        ),
    ),
    "push_app": Canal(
        cle="push_app",
        label="Notification dans l'application",
        max_caracteres=120,
        note=(
            "le message est vu sans avoir été demandé et interrompt : une seule "
            "idée, une seule action"
        ),
    ),
    "sms": Canal(
        cle="sms",
        label="SMS",
        max_caracteres=160,
        note="au-delà de 160 caractères le message est découpé et refacturé",
    ),
    "email": Canal(
        cle="email",
        label="E-mail",
        max_caracteres=600,
        note="le seul canal qui supporte une explication en plusieurs phrases",
    ),
    "reseaux_sociaux": Canal(
        cle="reseaux_sociaux",
        label="Réseaux sociaux",
        max_caracteres=280,
        note=(
            "message public et repartageable : il sera cité hors contexte, donc "
            "il doit rester exact isolément"
        ),
    ),
}


#: Où le client a parlé -> où on peut lui répondre.
#:
#: C'EST LE SEUL CIBLAGE HONNÊTE DONT ON DISPOSE. Sans fichier client, le canal
#: ne se choisit pas dans l'absolu : il se déduit de la source où le segment
#: s'exprime réellement. Un segment composé à 80 % d'avis App Store ne se traite
#: pas par SMS — on n'a pas les numéros — mais par la réponse aux avis, qui est
#: gratuite, immédiate, et lue par les futurs installateurs.
#:
#: Les sources de presse n'y figurent pas : `composition` ne compte que les avis
#: clients, un article n'a pas d'auteur à qui répondre.
CANAL_PAR_SOURCE: dict[str, str] = {
    "google_play": "reponse_avis",
    "app_store": "reponse_avis",
    "google_maps": "reponse_avis",
    "hellopeter": "reponse_avis",
    "trustpilot": "reponse_avis",
    "reddit": "reseaux_sociaux",
}

#: Canal retenu quand la composition des sources est inconnue ou vide.
#: La réponse aux avis est le seul canal disponible sans aucune donnée de
#: contact : c'est le repli qui ne suppose rien.
CANAL_DEFAUT = "reponse_avis"


# ---------------------------------------------------------------------------
# Formats de contenu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Format:
    """Un format de contenu, et les parties qui le composent."""

    cle: str
    label: str

    #: Champs attendus du modèle, dans l'ordre d'affichage. Un e-mail sans objet
    #: n'est pas un e-mail, et une annonce sans appel à l'action non plus : le
    #: format impose sa structure, elle n'est pas laissée au modèle.
    champs: tuple[str, ...]

    #: Longueur maximale du corps, en caractères.
    max_caracteres: int

    consigne: str


#: Les formats produits. TOUS EN UN SEUL APPEL DE MODÈLE.
#:
#: Le coût d'un appel est dominé par le contexte envoyé — le dossier de campagne,
#: identique pour les cinq formats. Les demander séparément multiplierait ce
#: contexte par cinq pour un gain nul, et sur un quota gratuit de 200 appels
#: quotidiens partagés avec l'analyse sémantique, cela se voit.
FORMATS: dict[str, Format] = {
    "sms": Format(
        cle="sms", label="SMS", champs=("texte",), max_caracteres=160,
        consigne="une seule phrase utile ; au-delà de 160 caractères le message "
        "est découpé et refacturé",
    ),
    "push": Format(
        cle="push", label="Notification mobile", champs=("titre", "texte"),
        max_caracteres=120,
        consigne="interrompt sans avoir été demandée : une idée, une action",
    ),
    "email": Format(
        cle="email", label="E-mail",
        champs=("objet", "introduction", "corps", "appel_action"),
        max_caracteres=900,
        consigne="le seul format qui supporte une explication ; l'objet fait au "
        "plus 60 caractères et ne promet rien",
    ),
    "reseaux": Format(
        cle="reseaux", label="Réseaux sociaux", champs=("texte",),
        max_caracteres=280,
        consigne="public et repartageable : il sera cité hors contexte, donc il "
        "doit rester exact isolément. Aucun mot-dièse inventé.",
    ),
    "annonce": Format(
        cle="annonce", label="Annonce publicitaire",
        champs=("titre", "description", "appel_action"),
        max_caracteres=200,
        consigne="titre de 40 caractères au plus, description de 90, appel à "
        "l'action de 3 mots",
    ),
}


# ---------------------------------------------------------------------------
# Leviers — les solutions, rattachées aux motifs mesurés
# ---------------------------------------------------------------------------

#: Motif d'insatisfaction -> actions concrètes.
#:
#: LES CLÉS SONT CELLES DE LA TAXONOMIE `aspects.ASPECTS`, et un test le
#: verrouille : un aspect ajouté là-bas et oublié ici ferait retomber la
#: campagne sur des leviers génériques, sans que rien ne le signale — c'est-à-
#: dire perdre exactement l'information pour laquelle l'analyse sémantique a été
#: construite.
#:
#: CHAQUE ACTION DOIT ÊTRE VÉRIFIABLE PAR CELUI QUI LA REÇOIT. « Améliorer la
#: satisfaction client » n'est pas un levier ; « publier le délai réel
#: d'activation, par pays » en est un : on peut constater le lendemain s'il a
#: été fait.
LEVIERS: dict[str, tuple[str, ...]] = {
    "reseau_couverture": (
        "Publier la carte de couverture réelle et le calendrier des sites en "
        "cours de déploiement dans les zones citées par les avis",
        "Réserver le geste commercial aux zones effectivement mal couvertes, "
        "plutôt que de l'étendre à tout le parc",
    ),
    "debit_lenteur": (
        "Communiquer les débits réellement observés par zone et par tranche "
        "horaire, plutôt que les débits théoriques du forfait",
        "Proposer un diagnostic guidé dans l'application, avec bascule vers un "
        "forfait adapté à l'usage constaté",
    ),
    "coupures_pannes": (
        "Annoncer l'incident avant que le client ne le découvre : page d'état "
        "publique et notification, dès la détection",
        "Compenser automatiquement les heures d'indisponibilité, sans obliger "
        "le client à déposer une réclamation",
    ),
    "facturation_prix": (
        "Détailler la facture ligne à ligne dans l'application, en nommant ce "
        "qui a été décompté et quand",
        "Prévenir avant le dépassement plutôt que de le facturer après",
        "Traiter les contestations de prélèvement en priorité, avec un délai de "
        "réponse annoncé et tenu",
    ),
    "forfaits_data": (
        "Rendre la consommation visible en temps réel et alerter à 80 % du "
        "forfait",
        "Proposer un forfait recalculé sur la consommation réelle des trois "
        "derniers mois",
    ),
    "recharge_paiement": (
        "Confirmer chaque recharge par un message immédiat portant un numéro de "
        "suivi utilisable en cas d'échec",
        "Annoncer un délai de rétablissement pour les recharges non créditées, "
        "et le tenir",
    ),
    "service_client": (
        "Répondre à chaque avis négatif sous 48 h, avec un interlocuteur nommé "
        "et une suite annoncée",
        "Rappeler les réclamations restées sans réponse au lieu d'attendre une "
        "relance du client",
    ),
    "agence_boutique": (
        "Afficher les temps d'attente par agence et ouvrir la prise de "
        "rendez-vous",
        "Basculer en ligne les démarches qui n'exigent pas un déplacement, et le "
        "faire savoir en agence",
    ),
    "app_bugs": (
        "Publier une note de version qui nomme les bugs corrigés, en reprenant "
        "les mots employés dans les avis",
        "Répondre aux avis concernés dès la mise en ligne de la version qui "
        "corrige, en citant son numéro",
    ),
    "app_connexion": (
        "Ouvrir un parcours de secours pour la connexion : code par SMS, "
        "réinitialisation assistée",
        "Signaler dans l'application quand l'envoi des codes est perturbé, au "
        "lieu de laisser l'utilisateur réessayer",
    ),
    "app_ergonomie": (
        "Rétablir un accès direct aux trois fonctions les plus utilisées et "
        "l'annoncer explicitement",
        "Faire tester la prochaine version par des utilisateurs ayant critiqué "
        "la refonte",
    ),
    "sim_identification": (
        "Publier la liste des pièces exigées et le délai réel d'activation, pays "
        "par pays",
        "Permettre de suivre l'avancement d'une identification en cours",
    ),
    "roaming_international": (
        "Annoncer le coût dès l'arrivée à l'étranger, avant le premier usage",
        "Proposer un forfait par destination plutôt qu'une facturation à l'unité",
    ),
    "promotions_offres": (
        "Écrire les conditions d'une offre dans le même bloc de texte que "
        "l'offre elle-même",
        "Cesser de promouvoir les offres dont les avis établissent qu'elles ne "
        "s'appliquent pas",
    ),
    "fibre_domicile": (
        "Donner un délai de raccordement engageant, et prévenir dès qu'il glisse",
        "Proposer une solution d'attente aux raccordements retardés",
    ),
    "fraude_securite": (
        "Publier les arnaques en cours et la marche à suivre, dans l'application "
        "et par SMS",
        "Confirmer toute souscription par un second canal, et rendre la "
        "résiliation aussi simple que la souscription",
    ),
    OTHER: (
        "Faire préciser le motif dans la réponse à l'avis : sans motif nommé, "
        "aucune action ne peut être décidée",
    ),
}


#: Leviers retenus quand AUCUN motif ne domine — cas fréquent et normal : sur un
#: périmètre où les plaintes sont diffuses, désigner un motif au hasard serait
#: pire que de reconnaître qu'il n'y en a pas.
LEVIERS_PAR_OBJECTIF: dict[str, tuple[str, ...]] = {
    "retention": (
        "Reprendre contact avec les auteurs d'avis négatifs en citant leur motif, "
        "plutôt qu'avec un message unique",
        "Publier ce qui a changé depuis leur avis, même partiellement",
    ),
    "reassurance": (
        "Reprendre les questions les plus fréquentes des avis et y répondre au "
        "même endroit",
        "Expliquer le fonctionnement contesté avec un exemple chiffré tiré d'une "
        "facture réelle",
    ),
    "fidelisation": (
        "Remercier nominativement les auteurs d'avis positifs, sans rien leur "
        "vendre dans le même message",
        "Leur proposer d'essayer en avant-première ce qui change",
    ),
    "acquisition": (
        "Mettre en avant les motifs de satisfaction les plus cités, avec leur "
        "formulation d'origine",
        "Répondre aux avis positifs publiquement : la réponse est lue par les "
        "visiteurs suivants",
    ),
}

#: Leviers proposés au maximum par campagne.
#:
#: DEUX. Une campagne qui liste six actions n'en fait exécuter aucune : elle
#: devient un audit, dont chacun retient la partie qui le concerne le moins.
#: C'est la même retenue que les trois sujets du briefing quotidien.
MAX_LEVIERS = 2


# ---------------------------------------------------------------------------
# Stratégies : plusieurs angles pour la même cible
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategie:
    """Un angle possible sur la même cible et le même segment."""

    cle: str
    label: str

    #: Ce que la campagne raconterait sous cet angle. Affiché à l'utilisateur
    #: pour qu'il choisisse en connaissance de cause, et transmis au modèle
    #: comme consigne de registre.
    angle: str

    #: Objectif que cet angle sert. Doit exister dans `OBJECTIFS`.
    objectif: str

    #: Vrai si l'angle suppose une CONTREPARTIE COMMERCIALE que l'agent ne
    #: connaît pas.
    #:
    #: C'EST LA MENTION LA PLUS IMPORTANTE DE CE MODULE. « Offrir un bonus
    #: data » est une stratégie parfaitement valable — mais le bonus, son
    #: volume, sa durée et son coût ne sont écrits nulle part dans cette
    #: plateforme. L'agent laisse donc un emplacement à remplir plutôt que
    #: d'inventer une offre, et il le dit. Un texte où le modèle aurait choisi
    #: « 2 Go pendant un mois » serait indiscernable d'une offre validée.
    exige_une_offre: bool = False


#: Marque l'emplacement d'une contrepartie que seul un humain peut décider.
#: Volontairement voyant : un gabarit dont le trou passe inaperçu finit envoyé
#: tel quel.
EMPLACEMENT_OFFRE = "[offre à préciser]"


#: Les trois angles proposés pour toute cible. Le choix revient à l'utilisateur.
#:
#: TROIS, ET TOUJOURS LES MÊMES TROIS. Ils ne sont pas générés : ce sont les
#: trois réponses possibles à une insatisfaction — la réparer, la compenser
#: commercialement, ou récompenser la fidélité malgré elle. Les faire produire
#: par un modèle donnerait trois angles différents à chaque appel, donc
#: incomparables d'une campagne à l'autre, et souvent trois variantes du même.
STRATEGIES: dict[str, Strategie] = {
    "A": Strategie(
        cle="A",
        label="Satisfaction",
        angle=(
            "communiquer sur le traitement du problème lui-même : ce qui a été "
            "constaté, ce qui change, et où le vérifier"
        ),
        objectif="retention",
    ),
    "B": Strategie(
        cle="B",
        label="Commerciale",
        angle=(
            "proposer une offre adaptée au problème constaté, plutôt que "
            "d'expliquer le problème"
        ),
        objectif="conversion",
        exige_une_offre=True,
    ),
    "C": Strategie(
        cle="C",
        label="Fidélisation",
        angle=(
            "reconnaître la gêne subie par un geste, sans prétendre que le "
            "problème est réglé"
        ),
        objectif="fidelisation",
        exige_une_offre=True,
    ),
}


# ---------------------------------------------------------------------------
# Tons
# ---------------------------------------------------------------------------

#: Registres d'écriture demandables lors d'une révision.
#:
#: UN TON N'ÉLARGIT JAMAIS CE QU'ON A LE DROIT DE DIRE. « Plus agressif
#: commercialement » change le rythme, la longueur des phrases et la place de
#: l'appel à l'action — pas l'autorisation d'annoncer une remise. Les
#: vérifications de promesse s'appliquent à l'identique quel que soit le ton, et
#: c'est ce qui rend cette liste sûre : elle ne peut pas servir à contourner le
#: garde-fou en le demandant poliment.
TONS: dict[str, str] = {
    "factuel": (
        "sobre et informatif, phrases courtes, aucun superlatif — le registre "
        "par défaut"
    ),
    "empathique": (
        "reconnaît la gêne avant de parler de la suite, s'adresse à la personne "
        "et non au parc"
    ),
    "commercial": (
        "direct et incitatif, une seule idée, appel à l'action en fin de "
        "message. N'ANNONCE TOUJOURS AUCUN AVANTAGE CHIFFRÉ."
    ),
    "institutionnel": (
        "posé, à la première personne du pluriel, sans familiarité — le registre "
        "d'une communication officielle"
    ),
}

TON_DEFAUT = "factuel"


def levier_label(aspect: Optional[str]) -> str:
    """Libellé affichable d'un motif ; vide si aucun motif n'a été retenu."""
    if not aspect:
        return ""
    entry = ASPECTS.get(aspect)
    return entry[0] if entry else aspect


def leviers_pour(aspect: Optional[str], objectif: str) -> list[str]:
    """Actions proposées pour un motif, ou à défaut pour l'objectif.

    L'ORDRE DE PRÉFÉRENCE EST LE FOND DE LA MÉTHODE. Un levier rattaché à un
    motif MESURÉ est une solution fondée sur la satisfaction constatée ; un
    levier rattaché à l'objectif seul n'est qu'une bonne pratique. On préfère
    donc toujours le premier, et on ne se rabat sur le second que lorsque
    l'analyse sémantique n'a rien fait remonter — cas où prétendre le contraire
    serait une invention.
    """
    if aspect and aspect != OTHER:
        actions = LEVIERS.get(aspect)
        if actions:
            return list(actions[:MAX_LEVIERS])
    return list(LEVIERS_PAR_OBJECTIF.get(objectif, ())[:MAX_LEVIERS])
