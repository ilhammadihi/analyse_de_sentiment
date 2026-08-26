"""
Le contexte d'une campagne : ce que l'on sait d'ailleurs, et ce que l'on ignore.

CE MODULE RÉPOND À DEUX EXIGENCES QUI N'EN FONT QU'UNE
    « Que la campagne soit réaliste » et « n'invente rien » sont le même
    problème pris par ses deux bouts. Une campagne bâtie sur les seuls avis dit
    de quoi les clients se plaignent, mais ignore le marché où ils vivent : elle
    proposera d'expliquer un prix sans savoir si ce prix est élevé, et de
    promouvoir la data dans un pays où la couverture ne suit pas. À l'inverse,
    une campagne qui comble ces trous en devinant est pire qu'inutile.

    La sortie de ce module est donc double, et les deux moitiés sont montrées :
    ce qui est MESURÉ (marché, presse, veille), et ce qui est INDISPONIBLE, dit
    en toutes lettres plutôt que passé sous silence.

POURQUOI DÉCLARER L'INDISPONIBLE PLUTÔT QUE DE L'OMETTRE
    Une segmentation par âge est le premier réflexe d'un marketeur. Si l'agent
    rend un segment sans jamais parler d'âge, le lecteur suppose que l'âge a été
    pris en compte — c'est le comportement normal devant un outil qui affiche un
    résultat. Le silence sur une dimension absente est donc lu comme une
    affirmation. Une ligne « âge indisponible » coûte deux secondes de lecture
    et supprime le malentendu.

LES TROIS SOURCES DE RÉALISME, ET CE QU'ELLES APPORTENT CHACUNE
    marché   Les indicateurs pays (UIT/Banque Mondiale) : prix du panier data,
             consommation par abonné, trafic, abonnés, couverture. C'est ce qui
             dit si « le prix est trop élevé » relève du ressenti ou du fait.
    presse   Les articles RÉCENTS citant la filiale, déjà collectés. C'est la
             seule source qui sache qu'une hausse tarifaire a été annoncée la
             semaine dernière — un avis client ne le dit jamais aussi tôt.
    veille   Le dernier signalement de l'Agent 1 sur cette entité. C'est ce qui
             empêche les deux agents de raconter deux histoires différentes de
             la même filiale le même jour.

    Les trois sont FACULTATIVES : un pays sans indicateurs, une filiale sans
    presse ou une veille muette ne bloquent rien. Le contexte est alors plus
    pauvre, et il le dit.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from reviews.agents.chiffres import nombre
from reviews.storage.agent_repository import AgentRepository
from reviews.storage.filters import PRESS, StatsFilter
from reviews.storage.market_repository import MarketRepository
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Nom sous lequel l'Agent 1 signe dans `agent_reports`.
#: Repris ici plutôt qu'importé de `insight_agent` : cet import tirerait tout
#: l'agent de veille — son arbitrage, son briefing, son notifieur — pour lire
#: une chaîne de sept caractères.
AGENT_VEILLE = "insight"

#: Indicateurs de marché retenus pour une campagne, dans cet ordre.
#:
#: DIFFÉRENTS DE CEUX DU BRIEFING QUOTIDIEN, et c'est délibéré. L'Agent 1
#: répond à « le réseau explique-t-il ce recul ? » et prend donc la couverture.
#: Une campagne répond à « qu'est-ce qui rendrait ce message crédible ? » et
#: prend d'abord le PRIX : c'est le premier motif de plainte du corpus, et le
#: seul indicateur qui permette de dire si une insatisfaction tarifaire est
#: fondée ou si elle relève de la perception.
#:
#: La clé est « indicateur|unité » : l'unité fait partie de l'identité de la
#: mesure — `PRI_DO_MOB` existe en USD, en parité de pouvoir d'achat et en part
#: du revenu national, trois nombres différents pour le même nom.
_MARCHE = (
    ("PRI_DO_MOB|USD", "panier data mobile", " $/mois"),
    ("IT_BB_MOB_PSB|GB_SB", "consommation", " Go/abonné"),
    ("ACT_MOB_SB|SB", "abonnés mobiles actifs", ""),
    ("IT_BB_MOB_TRF|XB_Y", "trafic data", " Go/mois"),
    ("MOB_COV_4G|PT_POP", "couverture 4G", " %"),
)

#: Articles de presse repris dans le contexte.
#:
#: TROIS. Le corpus en compte 7 779 : les aligner ferait du dossier une revue de
#: presse, que ni un lecteur ni un modèle ne traite. Trois titres récents
#: suffisent à savoir s'il s'est passé quelque chose.
MAX_ARTICLES = 3

#: Fenêtre de la presse, en jours. PLUS COURTE que celle des avis : un article
#: de trois mois ne rend pas une campagne réaliste, il la date.
JOURS_PRESSE = 30

#: Ancienneté maximale d'un indicateur de marché, en années.
#:
#: DÉFAUT CONSTATÉ SUR DONNÉES RÉELLES LE 13 AOÛT 2026. Le contexte du Mali
#: s'affichait « (2017–2025) » : le prix du panier data datait de 2025, mais le
#: trafic data remontait à 2017 — et valait 0,0 Go/mois, une valeur qui n'a plus
#: aucun sens aujourd'hui. Les deux figuraient dans la même phrase, sous le même
#: libellé « contexte », comme s'ils décrivaient le même moment.
#:
#: `latest()` rend la DERNIÈRE valeur connue, ce qui est le bon comportement pour
#: un dashboard — mieux vaut une valeur ancienne que rien. Pour une campagne
#: c'est l'inverse : un chiffre de 2017 ne rend pas une proposition réaliste, il
#: la rend fausse, et il est d'autant plus dangereux qu'il paraît précis.
#:
#: TROIS ANS et non un : l'UIT publie avec un à deux ans de retard, et les
#: dernières valeurs disponibles au 13 août 2026 datent de 2024 pour la plupart
#: des indicateurs. Exiger moins de deux ans viderait la ligne de contexte pour
#: tout le monde.
ANCIENNETE_MAX_ANNEES = 3


# ---------------------------------------------------------------------------
# Ce que la plateforme ne sait pas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """Une dimension de segmentation, disponible ou non."""

    cle: str
    label: str
    disponible: bool

    #: Pourquoi elle manque, en français, montré à l'utilisateur. Écrit pour
    #: quelqu'un qui vient de demander cette dimension et doit comprendre en une
    #: phrase que ce n'est pas un réglage à changer.
    pourquoi: str = ""

    #: Ce qu'il faudrait pour l'obtenir. Ce n'est pas une excuse mais une
    #: FEUILLE DE ROUTE : la question suivante est toujours « et si on la
    #: voulait ? », et y répondre dans la même phrase évite un aller-retour.
    pour_l_obtenir: str = ""


#: Les dimensions qu'un marketeur demande spontanément, et leur état réel.
#:
#: MESURÉ SUR LA BASE LE 13 AOÛT 2026, jamais supposé. `place_id` est renseigné
#: sur 0 avis des 38 243 ; l'âge n'existe dans aucune des huit sources ; l'ARPU
#: n'est publié par aucune source gratuite au niveau opérateur.
DIMENSIONS: dict[str, Dimension] = {
    "motif": Dimension(
        "motif", "motif d'insatisfaction", True,
    ),
    "satisfaction": Dimension(
        "satisfaction", "niveau de satisfaction", True,
    ),
    "pays": Dimension("pays", "pays", True),
    "operateur": Dimension("operateur", "opérateur", True),
    "source": Dimension("source", "plateforme d'expression", True),
    "age": Dimension(
        "age", "âge", False,
        pourquoi="les avis publics sont anonymes : aucune des huit sources "
        "collectées ne rend l'âge de leur auteur",
        pour_l_obtenir="un rapprochement avec le fichier abonnés de l'opérateur",
    ),
    "sexe": Dimension(
        "sexe", "sexe", False,
        pourquoi="même raison que l'âge : les avis publics sont anonymes",
        pour_l_obtenir="un rapprochement avec le fichier abonnés de l'opérateur",
    ),
    "ville": Dimension(
        "ville", "ville ou zone", False,
        pourquoi="856 agences sont identifiées par le collecteur Google Maps, "
        "mais la ville interrogée n'est pas conservée sur l'avis",
        pour_l_obtenir="stocker la ville de recherche dans le collecteur "
        "Google Maps — la donnée est produite, elle est jetée",
    ),
    "consommation": Dimension(
        "consommation", "consommation individuelle", False,
        pourquoi="la consommation n'est connue qu'en MOYENNE NATIONALE "
        "(Go par abonné, UIT), jamais par client",
        pour_l_obtenir="les compteurs de consommation de l'opérateur",
    ),
    "offre": Dimension(
        "offre", "forfait souscrit", False,
        pourquoi="rien dans un avis public ne dit quel forfait son auteur a "
        "souscrit",
        pour_l_obtenir="le référentiel d'offres et le parc abonnés",
    ),
    "arpu": Dimension(
        "arpu", "revenu par client", False,
        pourquoi="aucune source gratuite ne publie l'ARPU par opérateur ; les "
        "indicateurs UIT s'arrêtent au pays",
        pour_l_obtenir="les rapports annuels des groupes, ou les publications "
        "trimestrielles des régulateurs nationaux",
    ),
    "churn": Dimension(
        "churn", "risque de départ", False,
        pourquoi="le départ d'un client ne se voit pas dans un avis public",
        pour_l_obtenir="le parc abonnés, ou à défaut un aspect « intention de "
        "départ » ajouté à l'analyse sémantique",
    ),
}


def declarer_indisponibles(demandees: list[str]) -> list[str]:
    """Phrases d'avertissement pour les dimensions demandées mais absentes.

    Rend une liste vide quand tout ce qui est demandé existe — c'est le cas
    normal, et il ne doit produire aucun bruit. Une dimension inconnue du
    catalogue est ignorée en silence : le modèle qui traduit la demande peut
    rendre un mot hors liste, ce qui n'est pas une raison pour alarmer
    l'utilisateur sur une dimension qui n'a peut-être jamais été demandée.
    """
    phrases: list[str] = []
    for cle in demandees or ():
        dimension = DIMENSIONS.get(str(cle).strip().lower())
        if dimension is None or dimension.disponible:
            continue
        phrases.append(
            f"⚠️ Donnée {dimension.label} indisponible — {dimension.pourquoi}. "
            f"Pour l'obtenir : {dimension.pour_l_obtenir}."
        )
    return phrases


# ---------------------------------------------------------------------------
# Le contexte rassemblé
# ---------------------------------------------------------------------------


@dataclass
class Contexte:
    """Tout ce que l'agent sait d'ailleurs, prêt à être affiché ou transmis."""

    marche: list[str] = field(default_factory=list)
    annee_marche: Optional[str] = None
    presse: list[str] = field(default_factory=list)
    insight_veille: Optional[str] = None
    indisponibles: list[str] = field(default_factory=list)

    @property
    def vide(self) -> bool:
        return not (self.marche or self.presse or self.insight_veille)

    def lignes(self, pays: Optional[str] = None) -> list[str]:
        """Le contexte MESURÉ, en clair. Les indisponibilités sont rendues à part.

        Les deux ne se mélangent pas : le contexte éclaire la proposition, les
        avertissements la limitent. Fondus dans un même bloc, les seconds se
        liraient comme une note de bas de page — or ce sont eux qui empêchent le
        lecteur de croire qu'une dimension a été prise en compte.
        """
        out: list[str] = []
        if self.marche:
            lieu = pays or "le pays"
            out.append(
                f"Contexte {lieu} ({self.annee_marche}) : " + " ; ".join(self.marche)
            )
        if self.insight_veille:
            out.append(f"Veille (Agent 1) : {self.insight_veille}")
        if self.presse:
            out.append("Presse récente : " + " · ".join(self.presse))
        return out

    def as_dict(self) -> dict:
        return {
            "marche": list(self.marche),
            "annee_marche": self.annee_marche,
            "presse": list(self.presse),
            "insight_veille": self.insight_veille,
            "indisponibles": list(self.indisponibles),
        }


class CollecteurDeContexte:
    """Rassemble le contexte d'une cible. Ne lève jamais, ne calcule aucun taux.

    CHAQUE SOURCE EST ISOLÉE DANS SON PROPRE `try`. Une table d'indicateurs
    absente, une filiale sans presse ou un journal de veille illisible ne
    doivent pas priver la campagne des deux autres sources : le contexte est un
    enrichissement, jamais une condition.
    """

    def __init__(
        self,
        stats: StatsRepository,
        marche: Optional[MarketRepository] = None,
        veille: Optional[AgentRepository] = None,
    ):
        self.stats = stats
        self.marche = marche
        self.veille = veille

    def pour(
        self,
        *,
        iso2: Optional[str],
        subsidiary_id: Optional[str],
        pays: Optional[str] = None,
        dimensions_demandees: Optional[list[str]] = None,
    ) -> Contexte:
        contexte = Contexte(
            indisponibles=declarer_indisponibles(dimensions_demandees or [])
        )
        lignes, annee = self._marche(iso2)
        contexte.marche = lignes
        contexte.annee_marche = annee
        contexte.presse = self._presse(subsidiary_id)
        contexte.insight_veille = self._veille(subsidiary_id)
        return contexte

    # ------------------------------------------------------------------ Marché

    def _marche(self, iso2: Optional[str]) -> tuple[list[str], Optional[str]]:
        """Les indicateurs pays, en phrases courtes.

        LES CHIFFRES SONT CALCULÉS EN SQL, la phrase est un gabarit — règle du
        projet. Et l'année affichée est celle des indicateurs RETENUS, jamais le
        maximum du pays : les prix vont jusqu'en 2025 quand la couverture
        s'arrête à 2024, et dater une couverture de 2024 comme « 2025 » la
        rendrait invérifiable.
        """
        if not iso2 or self.marche is None:
            return [], None
        try:
            derniers = self.marche.latest(iso2)
        except Exception:  # noqa: BLE001
            logger.warning("Contexte marché illisible pour %s", iso2, exc_info=True)
            return [], None
        if not derniers:
            return [], None

        plancher = date.today().year - ANCIENNETE_MAX_ANNEES
        morceaux: list[str] = []
        annees: list[int] = []
        for cle, libelle, suffixe in _MARCHE:
            mesure = derniers.get(cle)
            if mesure is None:
                continue
            if int(mesure["year"]) < plancher:
                # Écarté SANS BRUIT : ce n'est pas une anomalie mais le régime
                # normal d'une source qui ne couvre pas tous les pays à la même
                # profondeur. Le signaler ferait un avertissement par pays mal
                # couvert, que personne ne lirait après le troisième.
                logger.debug(
                    "Indicateur %s écarté pour %s : %s est trop ancien",
                    cle, iso2, mesure["year"],
                )
                continue
            # LES GRANDS COMPTES N'ONT PAS DE DÉCIMALE. « 37 442 000,0 abonnés »
            # affiche une précision au dixième d'abonné sur une estimation
            # nationale — une fausse exactitude qui décrédibilise toute la ligne.
            decimales = 0 if abs(mesure["value"]) >= 10_000 else 1
            texte = f"{libelle} {nombre(mesure['value'], decimales)}{suffixe}"
            variation = mesure.get("variation_pct")
            if variation is not None:
                # Virgule décimale ici aussi : « 146,6 Go (+3.2 %) » mélangeait
                # les deux conventions dans la même parenthèse.
                signe = "+" if variation >= 0 else "−"
                texte += f" ({signe}{nombre(abs(variation))} %)"
            morceaux.append(texte)
            annees.append(int(mesure["year"]))

        if not morceaux:
            return [], None
        plus_ancienne, plus_recente = min(annees), max(annees)
        periode = (
            str(plus_recente)
            if plus_ancienne == plus_recente
            else f"{plus_ancienne}–{plus_recente}"
        )
        return morceaux, periode

    # ------------------------------------------------------------------ Presse

    def _presse(self, subsidiary_id: Optional[str]) -> list[str]:
        """Titres d'articles récents citant la filiale.

        `feed` ET NON `verbatims` : le fil est CHRONOLOGIQUE. Trier la presse par
        intensité de sentiment produirait un florilège d'articles alarmants sans
        rapport avec l'actualité de la semaine — or c'est précisément
        l'actualité qui rend une campagne réaliste ou datée.
        """
        if not subsidiary_id:
            return []
        try:
            f = StatsFilter(
                days=JOURS_PRESSE,
                subsidiaries=(int(subsidiary_id),),
                source_kind=PRESS,
            )
        except (TypeError, ValueError):
            return []
        try:
            data = self.stats.feed(f, limit=MAX_ARTICLES)
        except Exception:  # noqa: BLE001
            logger.warning("Presse récente illisible", exc_info=True)
            return []

        titres: list[str] = []
        for article in data.get("presse") or []:
            titre = (article.get("title") or "").strip()
            if not titre:
                continue
            quand = article.get("occurred_at")
            date_courte = quand.strftime("%d/%m") if quand else "?"
            titres.append(f"{titre[:120]} ({date_courte})")
        return titres

    # ------------------------------------------------------------------ Veille

    def _veille(self, subsidiary_id: Optional[str]) -> Optional[str]:
        """Le dernier signalement de l'Agent 1 sur cette entité, s'il existe.

        C'EST LE POINT DE JONCTION DES DEUX AGENTS, et il est volontairement en
        LECTURE SEULE et sans repli. L'Agent 2 ne recalcule pas la veille et ne
        la déclenche pas : il lit ce qu'elle a écrit, ou se passe d'elle. Un
        couplage plus fort ferait dépendre les campagnes de la disponibilité
        d'un modèle et d'un canal Telegram, alors qu'elles n'en ont pas besoin.

        Conséquence assumée : tant que l'Agent 1 n'a rien signalé sur une
        entité, cette ligne reste absente. C'est le comportement voulu — inventer
        un insight pour remplir la case serait exactement ce que les deux agents
        existent pour éviter.
        """
        if not subsidiary_id or self.veille is None:
            return None
        try:
            dernier = self.veille.last_report(
                AGENT_VEILLE, "subsidiary", str(subsidiary_id)
            )
        except Exception:  # noqa: BLE001
            logger.warning("Journal de veille illisible", exc_info=True)
            return None
        if not dernier:
            return None
        texte = (dernier.get("text") or "").strip()
        if not texte:
            return None
        # La première ligne SEULEMENT : un briefing en compte jusqu'à cinq, dont
        # le contexte marché que l'on vient déjà de reprendre au-dessus. La
        # première porte le fait ; les suivantes le commentent.
        premiere = texte.splitlines()[0]
        quand = dernier.get("created_at")
        if quand is not None:
            return f"{premiere} (signalé le {quand.strftime('%d/%m')})"
        return premiere
