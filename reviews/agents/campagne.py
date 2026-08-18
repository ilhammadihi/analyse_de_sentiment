"""
Arbitrage de campagne : qui viser, quel segment, quel objectif, quel canal.

TOUT CE QUI EST DÉCIDÉ ICI EST DÉCIDÉ SANS MODÈLE, ET C'EST LE CŒUR DU SUJET
    Un assistant de campagne est la fonctionnalité où la tentation de tout
    confier au modèle est la plus forte : « voici les chiffres, propose-moi une
    campagne » produit immédiatement quelque chose de présentable. C'est aussi
    la fonctionnalité où cette facilité coûte le plus cher, pour trois raisons
    mesurables :

      1. Le segment ne serait pas comptable. « Clients frustrés par le réseau »
         n'est pas une requête : personne ne peut dire combien ils sont, ni
         vérifier six semaines plus tard si leur nombre a baissé.
      2. Le classement changerait à chaque appel. Deux exécutions sur les mêmes
         données proposeraient deux cibles différentes — et un assistant dont
         les recommandations bougent sans que les chiffres bougent n'est plus
         consulté après la troisième fois.
      3. L'objectif serait décoratif. Un modèle propose volontiers « améliorer
         l'image de marque », qu'aucun chiffre de cette base ne mesure.

    Ici, la cible est classée par une note reproductible, le segment est un
    ensemble d'avis qu'une requête recompte à l'identique, et l'objectif porte
    le KPI qui dira s'il est atteint. Le modèle n'entre qu'après, pour RÉDIGER
    (`campaign_agent._rediger`).

L'ORDRE, ET POURQUOI IL N'EST PAS INVERSIBLE
    mesures -> cible -> segment -> objectif -> leviers -> canal -> rédaction.

    Chaque étape se déduit de la précédente. Choisir l'objectif avant le
    segment, par exemple, obligerait à trouver ensuite des clients à qui il
    s'applique — c'est-à-dire à partir de l'intention et à chercher les chiffres
    qui la justifient, exactement à l'envers de ce que fait le reste du projet.

CE QUE CE MODULE NE TOUCHE PAS
    Ni base, ni réseau, ni horloge. Il ne reçoit que des mesures déjà faites et
    ne rend que des décisions. C'est ce qui permet de le tester entièrement, et
    donc de discuter les seuils sur des cas réels plutôt que sur des intuitions.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from reviews.agents.questions import (
    JOURS_MAX,
    JOURS_MIN,
    Catalogue,
    QuestionRefusee,
    borner_entier,
)
from reviews.domain.marketing import (
    CANAL_DEFAUT,
    CANAL_PAR_SOURCE,
    CANAUX,
    OBJECTIFS,
    SEGMENTS,
    STRATEGIES,
    TON_DEFAUT,
    TONS,
    Objectif,
    Segment,
    Strategie,
    leviers_pour,
)
from reviews.storage.filters import StatsFilter

# ---------------------------------------------------------------------------
# Seuils — EN PYTHON, jamais dans un prompt
# ---------------------------------------------------------------------------

#: Fenêtre d'observation par défaut, en jours.
#:
#: TRENTE, ET NON QUATORZE COMME LA VEILLE QUOTIDIENNE. Les deux agents ne
#: répondent pas à la même question. La veille cherche ce qui vient de bouger,
#: donc regarde court. Une campagne cherche un motif INSTALLÉ, sur lequel on va
#: engager du temps et un message : à quatorze jours, le motif dominant d'une
#: filiale moyenne repose sur une poignée d'avis et change d'une semaine à
#: l'autre. Bâtir une campagne là-dessus, c'est communiquer sur du bruit.
JOURS_DEFAUT = 30

def _plancher_volume() -> int:
    """Avis clients exigés sur la fenêtre pour qu'une entité soit une cible.

    C'est `RELIABILITY_MIN_REVIEWS`, le plancher de publiabilité d'un taux déjà
    appliqué par le dashboard et par l'assistant conversationnel. Une troisième
    valeur, même bien choisie, finirait par diverger : l'écran dirait « taux non
    fiable » là où l'agent aurait bâti une campagne dessus.
    """
    from reviews.storage.stats_repository import RELIABILITY_MIN_REVIEWS

    return RELIABILITY_MIN_REVIEWS


#: Taille minimale d'un segment, en avis.
#:
#: DIX. En dessous, la campagne ne vise personne de mesurable : son rapport
#: comparerait ensuite deux poignées d'avis, et n'importe quel mouvement y
#: paraîtrait spectaculaire. C'est le seuil que l'alerting applique déjà avant
#: de calculer un taux (`min_reviews_for_ratio`).
SEGMENT_PLANCHER = 10

#: Part des avis négatifs qu'un motif doit porter pour être dit « dominant ».
#:
#: VINGT-CINQ POUR CENT. La taxonomie compte seize motifs : à répartition
#: uniforme, chacun en porterait six. Un motif à un quart des plaintes pèse donc
#: quatre fois la normale — c'est un phénomène, pas une fluctuation. Plus bas,
#: on nommerait un motif sur chaque périmètre, y compris ceux où les plaintes
#: sont réellement diffuses ; et une campagne qui traite un motif inexistant est
#: pire qu'une campagne générique, parce qu'elle affirme quelque chose de faux
#: aux clients qu'elle vise.
CONCENTRATION_MOTIF = 25.0

#: Part d'avis positifs à partir de laquelle la satisfaction devient un
#: ARGUMENT public et non un simple constat interne.
#:
#: Soixante pour cent : deux avis sur trois. En dessous, mettre en avant sa
#: satisfaction client expose à ce qu'un lecteur ouvre la fiche et y trouve
#: immédiatement le contraire — le seul mode d'échec vraiment coûteux d'une
#: campagne d'acquisition par la preuve.
SEUIL_ACQUISITION = 60.0

#: Hausse de la part de négatifs, en points, à partir de laquelle une cible est
#: dite URGENTE. Aligné sur `alerting.spike_delta_points` et sur le plancher
#: d'ampleur de l'arbitre de veille : trois seuils différents pour « ça a
#: nettement empiré » rendraient les trois agents incomparables.
URGENCE_POINTS = 10.0

#: Poids de l'urgence dans la note de la cible.
#:
#: LA TAILLE DU SEGMENT EST LA BASE — une campagne coûte le même prix qu'elle
#: touche vingt personnes ou deux cents, son rendement suit donc l'audience. Mais
#: à taille comparable, une dégradation en cours mérite d'être traitée avant une
#: insatisfaction stable : la seconde attendra une semaine de plus sans empirer.
#: Le bonus est PLAFONNÉ à la taille elle-même, pour la raison déjà établie par
#: l'arbitre de veille — un bonus doit amplifier un signal, jamais s'y
#: substituer, sinon on fabrique un sujet majeur à partir d'un petit segment qui
#: bouge.
POIDS_URGENCE = 3.0

#: Motifs qu'un MESSAGE peut réellement résoudre.
#:
#: LA DISTINCTION QUI ÉVITE LA CAMPAGNE INDÉCENTE. Un client dont le réseau
#: tombe tous les soirs n'a pas un problème de compréhension : lui « expliquer »
#: sa situation par un message aggrave le mécontentement, parce que le message
#: prouve qu'on a vu la plainte sans rien changer. En revanche, un client qui
#: conteste un décompte, un forfait ou une promotion peut avoir raison OU s'être
#: mépris — et là, une explication claire règle une partie des cas sans qu'aucun
#: euro ne change de main.
#:
#: C'est ce qui sépare `reassurance` de `retention` : la première promet de faire
#: comprendre, la seconde promet de faire traiter. Se tromper de registre est la
#: faute la plus visible d'une campagne.
MOTIFS_INFORMATIONNELS = frozenset({
    "facturation_prix",
    "forfaits_data",
    "promotions_offres",
    "roaming_international",
})


# ---------------------------------------------------------------------------
# La cible
# ---------------------------------------------------------------------------


@dataclass
class Cible:
    """Une entité mesurée, candidate à une campagne.

    Les champs sont ceux que rendent `StatsRepository.ranking` et
    `StatsRepository.themes` : cette classe ne calcule rien, elle rassemble.
    """

    level: str
    key: str
    label: str
    pays: Optional[str] = None
    iso2: Optional[str] = None

    avis_clients: int = 0
    positifs: int = 0
    negatifs: int = 0
    part_negatifs: Optional[float] = None
    part_positifs: Optional[float] = None
    note_moyenne: Optional[float] = None

    #: Variation de la part de négatifs contre la période précédente, en points.
    #: `None` quand elle n'a pas été mesurée — et c'est alors traité comme
    #: « pas d'urgence », jamais comme « zéro ».
    delta_negatifs: Optional[float] = None

    #: Motif dominant des avis négatifs, s'il en existe un : (clé d'aspect,
    #: nombre d'avis). Vient de l'analyse sémantique, jamais du lexique — un
    #: motif nommé « can't » ne fonde aucune campagne.
    motif: Optional[str] = None
    motif_avis: int = 0

    #: Répartition des avis clients par source, telle que `_COMPOSITION` la
    #: rend. Sert à choisir le canal : c'est le seul ciblage possible sans
    #: fichier client.
    composition: dict[str, int] = field(default_factory=dict)

    #: Renseignés par `arbitrer_cibles`.
    score: float = 0.0
    retenue: bool = False
    raisons: list[str] = field(default_factory=list)
    ecartee_parce_que: Optional[str] = None

    # -------------------------------------------------------------- Dérivés

    @property
    def part_motif(self) -> float:
        """Part du motif dominant dans les avis négatifs, en pourcentage."""
        if not self.negatifs or not self.motif:
            return 0.0
        return 100.0 * self.motif_avis / self.negatifs

    @property
    def urgente(self) -> bool:
        return (self.delta_negatifs or 0.0) >= URGENCE_POINTS

    def taille_segment(self, segment: Segment) -> int:
        """Nombre d'avis composant le segment. LA mesure d'un segment.

        Elle n'est pas approchée : c'est un décompte d'avis réellement présents
        en base, que la même requête retrouvera à l'identique dans six semaines.
        C'est ce qui rend le rapport de campagne possible.
        """
        if segment.cle == "insatisfaits_motif":
            return self.motif_avis
        if segment.cle == "detracteurs":
            return self.negatifs
        return self.positifs

    def as_dict(self) -> dict[str, Any]:
        return {
            "niveau": self.level,
            "entite": self.label,
            "pays": self.pays,
            # LE CODE ISO EST CONSERVÉ EN PLUS DU NOM, et ce n'est pas une
            # redondance : le repère pays du rapport de campagne se lit par code,
            # jamais par nom — « Côte d'Ivoire » et « Cote d'Ivoire » ne
            # joindraient pas. Même leçon que le contexte marché de l'Agent 1.
            "iso2": self.iso2,
            "avis_clients": self.avis_clients,
            "positifs": self.positifs,
            "negatifs": self.negatifs,
            "part_negatifs": self.part_negatifs,
            "part_positifs": self.part_positifs,
            "note_moyenne": self.note_moyenne,
            "variation_negatifs_points": self.delta_negatifs,
            "motif_dominant": self.motif,
            "motif_avis": self.motif_avis,
            "part_motif": round(self.part_motif, 1),
            "composition": dict(self.composition),
            "score": round(self.score, 1),
            "raisons_de_la_selection": list(self.raisons),
        }


def arbitrer_cibles(cibles: list[Cible]) -> list[Cible]:
    """Note et trie les cibles. Ne coupe rien : l'appelant décide combien il en prend.

    Même contrat que `arbitrage.arbitrer` pour l'agent de veille, et pour la
    même raison : chaque cible repart annotée d'un `score` reproductible et d'un
    texte disant pourquoi elle a été retenue ou écartée. Un agent qui ne propose
    rien doit pouvoir expliquer son silence, sinon il est indébogable.
    """
    for c in cibles:
        c.score = 0.0
        c.raisons = []
        c.ecartee_parce_que = None
        c.retenue = False

        if c.avis_clients < _plancher_volume():
            c.ecartee_parce_que = (
                f"{c.avis_clients} avis clients sur la période (minimum "
                f"{_plancher_volume()}) — les taux ne sont pas publiables, une "
                "campagne bâtie dessus viserait une impression"
            )
            continue

        segment = choisir_segment(c)
        taille = c.taille_segment(segment)
        if taille < SEGMENT_PLANCHER:
            c.ecartee_parce_que = (
                f"segment « {segment.label} » réduit à {taille} avis (minimum "
                f"{SEGMENT_PLANCHER}) — rien de mesurable à viser"
            )
            continue

        c.raisons.append(f"{taille} avis composent le segment retenu")

        bonus = 0.0
        if c.urgente:
            bonus = min(float(taille), (c.delta_negatifs or 0.0) * POIDS_URGENCE)
            c.raisons.append(
                f"part d'avis négatifs en hausse de {c.delta_negatifs:.1f} points "
                "sur la période — dégradation en cours"
            )
        if c.motif and c.part_motif >= CONCENTRATION_MOTIF:
            c.raisons.append(
                f"{c.part_motif:.0f} % des avis négatifs portent le même motif — "
                "un message peut viser juste"
            )

        c.score = float(taille) + bonus
        c.retenue = True

    return sorted(cibles, key=lambda c: (-c.score, c.label))


# ---------------------------------------------------------------------------
# Segment, objectif, canal
# ---------------------------------------------------------------------------


def choisir_segment(cible: Cible) -> Segment:
    """Le segment que les mesures désignent. Décision pure et reproductible.

    L'ORDRE DES TESTS EST LA RÈGLE : du plus précis au plus large. Un motif
    concentré vaut toujours mieux qu'un segment générique — « les clients qui se
    plaignent de leur facture » se traite, « les mécontents » ne se traite pas —
    mais on ne le nomme que s'il domine réellement (`CONCENTRATION_MOTIF`).

    LA SECONDE CONDITION SUR LA TAILLE N'EST PAS REDONDANTE, et son absence
    produisait un comportement absurde : sur une filiale à 15 avis négatifs dont
    40 % portent le même motif, le segment « motif » n'en compte que 6, passe
    sous le plancher, et la cible entière était écartée — alors que le segment
    « détracteurs », lui, était parfaitement viable. Un motif bien identifié
    faisait donc perdre une campagne.
    """
    negatifs_dominent = (cible.part_negatifs or 0.0) >= (cible.part_positifs or 0.0)
    if negatifs_dominent:
        if (
            cible.motif
            and cible.part_motif >= CONCENTRATION_MOTIF
            and cible.motif_avis >= SEGMENT_PLANCHER
        ):
            return SEGMENTS["insatisfaits_motif"]
        return SEGMENTS["detracteurs"]
    return SEGMENTS["promoteurs"]


def choisir_objectif(cible: Cible, segment: Segment) -> Objectif:
    """L'objectif marketing que le segment et les mesures imposent.

    CE N'EST PAS UN CHOIX DE GOÛT. Chaque branche répond à une question de fait :
    ces clients sont-ils mécontents ? leur motif se règle-t-il par une
    explication ? leur satisfaction est-elle assez forte pour être montrée ?
    Trois questions auxquelles les chiffres répondent, et dont on peut discuter
    les seuils — ce qui ne serait pas le cas d'une préférence de modèle.
    """
    if segment.cle == "promoteurs":
        if (cible.part_positifs or 0.0) >= SEUIL_ACQUISITION:
            return OBJECTIFS["acquisition"]
        return OBJECTIFS["fidelisation"]

    if segment.cle == "insatisfaits_motif" and cible.motif in MOTIFS_INFORMATIONNELS:
        return OBJECTIFS["reassurance"]

    return OBJECTIFS["retention"]


def choisir_canal(composition: dict[str, int], demande: Optional[str] = None) -> str:
    """Où adresser le message : là où le segment a effectivement parlé.

    LA DEMANDE EXPLICITE PRIME, sans discussion : celui qui écrit le brief sait
    de quels canaux il dispose réellement, ce que cette base ignore
    complètement. Elle ne connaît ni les numéros, ni les adresses, ni le budget.

    À défaut, le canal se DÉDUIT de la source dominante du segment. C'est le
    seul ciblage honnête possible ici : un segment composé d'avis App Store ne
    se joint pas par SMS — on n'a aucun numéro —, mais la réponse publique à ces
    avis atteint son auteur ET tous les visiteurs suivants de la fiche.
    """
    if demande and demande in CANAUX:
        return demande
    if composition:
        source_dominante = max(composition.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return CANAL_PAR_SOURCE.get(source_dominante, CANAL_DEFAUT)
    return CANAL_DEFAUT


# ---------------------------------------------------------------------------
# Le brief libre
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Brief:
    """Une description libre, traduite en paramètres et validée.

    Le TEXTE d'origine est conservé à côté des paramètres : il est retransmis
    au modèle au moment de la rédaction, pour qu'il en respecte le ton et
    l'angle. Mais il ne traverse jamais la couche de mesure — aucune requête ne
    dépend d'un mot que l'utilisateur a écrit.
    """

    texte: str
    jours: int
    filtre: StatsFilter
    portee: str

    #: Objectif et canal IMPOSÉS par l'utilisateur, quand il en a nommé un.
    #: `None` = les mesures décident.
    objectif: Optional[str] = None
    canal: Optional[str] = None

    #: Vrai si un périmètre a été explicitement demandé. Commande le mode :
    #: sans périmètre, l'agent choisit lui-même sa cible.
    cible_imposee: bool = False

    #: Dimensions de segmentation que l'utilisateur a nommées (« les jeunes »,
    #: « à Casablanca », « les gros consommateurs »).
    #:
    #: RECUEILLIES POUR ÊTRE RÉFUTÉES, pas pour être utilisées. La plupart
    #: n'existent pas dans cette base ; les collecter permet de le DIRE
    #: (`contexte.declarer_indisponibles`) au lieu de rendre un segment qui
    #: ignore silencieusement la moitié de la demande. Sans cette liste, un
    #: utilisateur qui demande « les jeunes de Casablanca » recevrait un segment
    #: sans âge ni ville et croirait les deux prises en compte.
    dimensions: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "texte": self.texte,
            "jours": self.jours,
            "objectif_demande": self.objectif,
            "canal_demande": self.canal,
            "portee": self.portee,
            "cible_imposee": self.cible_imposee,
            "dimensions_demandees": list(self.dimensions),
            "filtre": self.filtre.describe(),
        }


def brief_vide(jours: int = JOURS_DEFAUT) -> Brief:
    """Le brief d'un passage automatique : aucune contrainte, tout le périmètre.

    Existe pour que `proposer()` n'ait qu'UN chemin, avec ou sans description.
    Deux chemins — l'un piloté par un brief, l'autre non — auraient divergé au
    premier réglage, et le mode automatique aurait fini par ne plus appliquer
    les mêmes seuils que le mode interactif.
    """
    return Brief(
        texte="",
        jours=jours,
        filtre=StatsFilter(days=jours),
        portee=f"tout le périmètre suivi · {jours} derniers jours",
    )


def valider_brief(brut: dict, catalogue: Catalogue, texte: str) -> Brief:
    """Traduit la sortie du modèle en brief exécutable, ou refuse.

    LA DISSYMÉTRIE ENTRE PÉRIMÈTRE ET INTENTION EST VOULUE, et c'est la règle
    la plus importante de cette fonction :

      - un NOM D'ENTITÉ inconnu fait REFUSER. Bâtir la campagne sur un périmètre
        approchant produirait des chiffres justes sur la mauvaise filiale, et
        rien dans la proposition ne le signalerait.
      - un objectif ou un canal non reconnu est IGNORÉ, pas refusé. Un brief est
        une orientation, pas une requête : « quelque chose d'un peu vendeur pour
        les jeunes » ne se range dans aucune liste fermée, et refuser la demande
        pour autant serait absurde. Les mesures décident alors, et la campagne
        dit ce qu'elle a retenu.

    Raises:
        QuestionRefusee: message en français, montré tel quel à l'utilisateur.
    """
    if not isinstance(brut, dict):
        raise QuestionRefusee("Je n'ai pas compris la description.")

    operateurs: tuple[int, ...] = ()
    pays: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    morceaux: list[str] = []

    if brut.get("operateur"):
        op_id, op_label = catalogue.operateur(str(brut["operateur"]))
        operateurs = (op_id,)
        morceaux.append(op_label)

    if brut.get("pays"):
        iso2, pays_label = catalogue.pays_(str(brut["pays"]))
        pays = (iso2,)
        morceaux.append(pays_label)

    if brut.get("region"):
        region, region_label = catalogue.region(str(brut["region"]))
        regions = (region,)
        morceaux.append(region_label)

    jours = borner_entier(brut.get("jours"), JOURS_DEFAUT, JOURS_MIN, JOURS_MAX)

    objectif = str(brut.get("objectif") or "").strip() or None
    if objectif not in OBJECTIFS:
        objectif = None

    canal = str(brut.get("canal") or "").strip() or None
    if canal not in CANAUX:
        canal = None

    # Les dimensions sont prises TELLES QUELLES, sans filtrage sur une liste
    # blanche : c'est `declarer_indisponibles` qui décide quoi en faire, et une
    # dimension inconnue y est ignorée sans bruit. Filtrer ici obligerait à
    # tenir la liste à deux endroits.
    dimensions = tuple(
        str(d).strip().lower()
        for d in (brut.get("segmentation") or [])
        if str(d).strip()
    )

    cible_imposee = bool(operateurs or pays or regions)
    morceaux.append(f"{jours} derniers jours")

    return Brief(
        texte=texte,
        jours=jours,
        objectif=objectif,
        canal=canal,
        cible_imposee=cible_imposee,
        dimensions=dimensions,
        portee=" · ".join(morceaux) if cible_imposee else
        f"tout le périmètre suivi · {jours} derniers jours",
        filtre=StatsFilter(
            days=jours, countries=pays, regions=regions, operators=operateurs
        ),
    )


# ---------------------------------------------------------------------------
# Ce qu'un texte de campagne n'a pas le droit de dire
# ---------------------------------------------------------------------------

#: Termes qui transforment un message en ENGAGEMENT COMMERCIAL.
#:
#: POURQUOI CE GARDE-FOU EXISTE, ET POURQUOI IL EST PLUS STRICT QUE LE RESTE
#:     La vérification des chiffres inventés protège l'exactitude d'une réponse.
#:     Celle-ci protège autre chose : un texte de campagne s'adresse à des
#:     CLIENTS. « Profitez de trois mois offerts » écrit par un modèle n'est pas
#:     une erreur d'analyse, c'est une promesse faite en votre nom à des gens qui
#:     la tiendront pour vraie — et que personne, dans cette base, n'a le pouvoir
#:     d'honorer.
#:
#:     Le coût d'un rejet à tort est une accroche moins accrocheuse. Le coût d'un
#:     faux positif de l'autre côté est un litige. L'asymétrie justifie d'être
#:     large, y compris au prix de rejets abusifs : le repli est un gabarit
#:     déterministe, jamais un silence.
#:
#: Les remises chiffrées sont couvertes deux fois — ici par le mot, et par la
#: vérification des nombres, qui rejette « -20 % » faute de mesure
#: correspondante. Deux verrous indépendants sur la promesse la plus courante.
#: « avoir » et « double » ont été RETIRÉS de cette liste après essai : « sans
#: avoir à nous écrire » et « double authentification » les déclenchaient, et un
#: garde-fou qui rejette une phrase sur deux finit par être désactivé — donc par
#: ne plus protéger de rien. Un garde-fou n'a de valeur que s'il reste supportable.
PROMESSES_INTERDITES: tuple[str, ...] = (
    "offert", "offerte", "gratuit", "gratuite", "cadeau", "bonus",
    "remise", "réduction", "reduction", "promo", "promotion", "soldes",
    "remboursé", "rembourse", "remboursement", "dédommag", "dedommag",
    "compensation", "indemnis", "illimité", "illimite", "doublé",
)

#: Repère une promesse en début de mot : la frontière `\b` évite « promo » dans
#: un mot quelconque, tandis que l'absence de frontière FINALE laisse « offert »
#: attraper « offerts » et « dédommag » attraper « dédommagement ».
_PROMESSE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in PROMESSES_INTERDITES) + r")",
    re.IGNORECASE,
)


def promesses_detectees(texte: str) -> list[str]:
    """Engagements commerciaux repérés dans un texte rédigé. Vide = acceptable."""
    return sorted({m.group(0).lower() for m in _PROMESSE_RE.finditer(texte or "")})


def motif_du_segment(
    cible: Optional[Cible], segment: Optional[Segment]
) -> Optional[str]:
    """Le motif dominant, mais seulement s'il concerne le segment visé.

    DÉFAUT CONSTATÉ SUR DONNÉES RÉELLES, le 13 août 2026 : sur Vodacom South
    Africa, le segment retenu était celui des PROMOTEURS (449 avis positifs) et
    les actions proposées parlaient de « répondre à chaque avis négatif sous
    48 h ». Le motif dominant est mesuré sur les avis NÉGATIFS : l'appliquer à
    une campagne de fidélisation revient à répondre à des clients satisfaits en
    leur rappelant un problème — précisément ce que l'exclusion de l'objectif
    `fidelisation` interdit en toutes lettres.

    Le motif n'est donc lisible que pour un segment négatif. Pour un segment
    positif, on préfère assumer l'absence de motif : les motifs de satisfaction
    ne sont pas mesurés ici, et en fabriquer un serait une invention.

    TOLÈRE `None` DES DEUX CÔTÉS. Une campagne refusée — cible en
    refroidissement, périmètre trop maigre — n'a ni cible ni segment, et le code
    d'affichage la traverse quand même. Lever ici transformerait un refus, qui
    est une réponse normale, en trace d'exception.
    """
    if cible is None or segment is None or segment.polarite != "negative":
        return None
    if not cible.motif or cible.part_motif < CONCENTRATION_MOTIF:
        return None
    return cible.motif


def leviers(cible: Cible, segment: Segment, objectif: Objectif) -> list[str]:
    """Les solutions proposées, fondées sur le motif MESURÉ quand il s'applique."""
    return leviers_pour(motif_du_segment(cible, segment), objectif.cle)


def strategies_pour(objectif_mesure: Objectif) -> list[Strategie]:
    """Les trois angles proposés, l'option A portant l'objectif mesuré.

    L'OPTION A N'EST PAS UN ANGLE PARMI TROIS : c'est celui que les chiffres
    désignent, et les deux autres sont des alternatives assumées. L'ordre le dit,
    et l'affichage le répète — sans quoi l'utilisateur choisirait au goût entre
    trois propositions présentées comme équivalentes, alors qu'une seule est
    fondée sur une mesure.

    Les doublons d'objectif sont écartés : sur un segment déjà satisfait,
    l'angle « fidélisation » serait proposé deux fois, une fois comme mesure et
    une fois comme alternative. Deux options identiques dans une liste de trois
    donnent l'impression d'un choix là où il n'y en a que deux.
    """
    principale = replace(STRATEGIES["A"], objectif=objectif_mesure.cle)
    retenues = [principale]
    for cle in ("B", "C"):
        candidate = STRATEGIES[cle]
        if candidate.objectif in {s.objectif for s in retenues}:
            continue
        retenues.append(candidate)
    return retenues


def valider_ton(demande: Optional[str]) -> str:
    """Ramène un ton demandé à la liste fermée, ou rend le ton par défaut.

    NE LÈVE PAS. « Plus punchy », « moins formel », « comme Orange » sont des
    demandes légitimes qui ne se rangent dans aucune case : refuser la révision
    pour autant serait absurde. Le ton retenu est de toute façon affiché, donc
    l'utilisateur voit ce qui a été compris et peut reformuler.
    """
    cle = (demande or "").strip().lower()
    return cle if cle in TONS else TON_DEFAUT
