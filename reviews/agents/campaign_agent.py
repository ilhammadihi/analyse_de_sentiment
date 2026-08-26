"""
Agent 2 — assistant de campagne : propose une campagne, et en mesure la suite.

CE QU'IL FAIT, DANS L'ORDRE OÙ IL LE FAIT
    mesures -> cible -> segment -> objectif -> leviers -> canal -> rédaction.

    Les six premières étapes sont calculées (`agents/campagne.py`) ; seule la
    dernière passe par le modèle. C'est la même architecture que l'Agent 1, pour
    la même raison : un assistant dont les recommandations changent d'un appel à
    l'autre sur des données identiques n'est plus consulté après la troisième
    fois. Ici, deux exécutions sur le même corpus proposent la même cible, le
    même segment et le même objectif — seule la formulation peut varier.

CE QU'IL NE FAIT PAS, ET QUI EST AUSSI IMPORTANT
    Il n'envoie RIEN à un client. Il propose, et un humain valide (`/valider`).
    Un texte commercial engage l'opérateur vis-à-vis de ses abonnés : c'est la
    seule décision de tout le projet qu'on ne pourrait pas défendre si elle
    revenait à un modèle de langage.

    Il n'invente aucun avantage. Toute promesse commerciale repérée dans le
    texte rédigé (`promesses_detectees`) fait rejeter la rédaction au profit du
    gabarit — « trois mois offerts » n'est pas une erreur d'analyse, c'est un
    engagement pris en votre nom envers des gens qui le tiendront pour vrai.

    Il ne mesure aucune performance marketing. La plateforme ne collecte que des
    avis publics : aucun envoi, aucune ouverture, aucun clic n'y figure. Le
    rapport mesure donc l'évolution de la SATISFACTION du segment visé, le dit
    explicitement, et donne l'évolution du pays comme repère — parce qu'une
    amélioration que tout le pays connaît en même temps ne s'attribue pas à une
    campagne.

SANS CLÉ DE MODÈLE, IL RESTE ENTIÈREMENT UTILISABLE
    La cible, le segment, l'objectif, les leviers et le rapport sont calculés ;
    le texte tombe sur un gabarit. Seule la description libre exige le modèle,
    puisqu'elle consiste précisément à comprendre une phrase.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from reviews.agents.campagne import (
    JOURS_DEFAUT,
    Brief,
    Cible,
    arbitrer_cibles,
    brief_vide,
    choisir_canal,
    choisir_objectif,
    choisir_segment,
    leviers,
    motif_du_segment,
    promesses_detectees,
    strategies_pour,
    valider_brief,
    valider_ton,
)
from reviews.agents.chiffres import chiffres_autorises, chiffres_inventes, nombre
from reviews.agents.contexte import (
    DIMENSIONS,
    CollecteurDeContexte,
    Contexte,
)
from reviews.agents.questions import Catalogue, QuestionRefusee
from reviews.alerting.notifiers import TelegramNotifier
from reviews.config import Settings
from reviews.domain.aspects import OTHER
from reviews.domain.aspects import label as aspect_label
from reviews.domain.marketing import (
    CANAUX,
    EMPLACEMENT_OFFRE,
    FORMATS,
    OBJECTIFS,
    SEGMENTS,
    STRATEGIES,
    TON_DEFAUT,
    TONS,
    Canal,
    Objectif,
    Segment,
    levier_label,
)
from reviews.llm.client import LLMClient, LLMError, LLMUnavailable
from reviews.storage.agent_repository import AgentRepository, should_report
from reviews.storage.campaign_repository import CampaignRepository
from reviews.storage.db import Database
from reviews.storage.market_repository import MarketRepository
from reviews.storage.filters import StatsFilter
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Nom sous lequel cet agent signe. Aligné sur la colonne `agent` de
#: `agent_reports`, même s'il écrit dans sa propre table : les deux vocabulaires
#: doivent rester interchangeables pour qu'on puisse un jour croiser les deux.
AGENT = "campaign"

#: Jours pendant lesquels on ne repropose pas de campagne pour la même entité.
#:
#: QUATORZE, CONTRE TROIS POUR LA VEILLE QUOTIDIENNE. Un briefing se lit et se
#: périme ; une campagne se prépare, se valide, puis se déroule. En reproposer
#: une tous les trois jours sur la même filiale, c'est demander à une équipe de
#: relire une décision qu'elle vient de prendre — et l'habituer à ignorer l'agent.
#: Deux semaines laissent le temps d'une validation et d'un premier effet.
COOLDOWN_JOURS = 14

#: Croissance du segment, en avis, qui rouvre le sujet malgré le refroidissement.
#:
#: La dissymétrie de `should_report` s'applique ici comme ailleurs : « déjà
#: proposé » fait taire, « déjà proposé mais le segment a nettement grossi » fait
#: reparler. Trente avis de plus sur un segment, c'est une aggravation qu'aucune
#: équipe ne veut découvrir dans deux semaines.
AGGRAVATION_AVIS = 30.0

#: Cibles examinées en détail à chaque passage.
#:
#: TROIS. Le classement grossier (sans motif) est calculé sur tout le périmètre
#: en une requête ; l'enrichissement par les motifs, lui, coûte une requête par
#: cible. Aller au-delà des trois premières reviendrait à payer pour départager
#: des candidates qui, de toute façon, ne seront pas retenues ce passage-ci.
MAX_CIBLES_EXAMINEES = 3

#: Lignes demandées au classement initial. Large : le classement est trié par
#: part de négatifs, or la cible se choisit sur la TAILLE du segment — une
#: filiale à fort volume et à taux moyen peut porter le plus gros segment du
#: parc tout en figurant au trentième rang.
MAX_LIGNES_CLASSEMENT = 60

#: Écart minimal, en points ou en avis, pour qu'un rapport conclue.
#:
#: DEUX POINTS. En deçà, l'écart est du bruit d'échantillonnage : un même
#: périmètre recollecté deux fois à un jour d'intervalle bouge déjà de cet
#: ordre. Conclure sous ce seuil ferait dire à un rapport qu'une campagne a
#: fonctionné alors que rien n'a changé — le seul résultat vraiment coûteux,
#: puisqu'il ferait reconduire ce qui n'a rien produit.
MARGE_VERDICT = 2.0

#: Avis exigés sur CHACUNE des deux fenêtres du rapport pour qu'il conclue.
#: Même raisonnement que le plancher de volume de l'arbitre de veille : une
#: variation calculée sur cinq avis n'est pas une variation.
VOLUME_RAPPORT = 10

#: Quel indicateur de marché éclaire quel motif de plainte.
#:
#: SANS CETTE TABLE, LE CONTEXTE DEVIENT UN DÉCOR. Accoler « couverture 4G
#: 88,9 % » à une plainte de facturation n'apporte rien, et le lecteur apprend
#: en trois campagnes à sauter la ligne de contexte — y compris le jour où elle
#: porte l'information décisive. Un motif sans indicateur pertinent n'en reçoit
#: donc aucun, ce qui est le comportement voulu.
#:
#: La valeur est un FRAGMENT du libellé produit par `contexte._MARCHE` : un
#: rapprochement par sous-chaîne, parce que la ligne porte aussi la valeur et sa
#: variation, qui ne sont pas connues ici.
_MOTIF_VERS_MARCHE: dict[str, str] = {
    "facturation_prix": "panier data mobile",
    "promotions_offres": "panier data mobile",
    "roaming_international": "panier data mobile",
    "forfaits_data": "consommation",
    "debit_lenteur": "trafic data",
    "reseau_couverture": "couverture 4G",
    "coupures_pannes": "couverture 4G",
}


# ---------------------------------------------------------------------------
# Ce que l'agent rend
# ---------------------------------------------------------------------------


@dataclass
class Campagne:
    """Une campagne proposée, telle qu'elle est rendue à la CLI et à Telegram."""

    cible: Optional[Cible] = None
    segment: Optional[Segment] = None
    objectif: Optional[Objectif] = None
    canal: Optional[Canal] = None
    actions: list[str] = field(default_factory=list)
    accroche: str = ""
    message: str = ""
    taille_segment: int = 0

    #: Nom commercial. Rédigé par le modèle quand il est là, composé depuis
    #: l'objectif et le motif sinon. Une campagne se cite par son nom, jamais
    #: par son numéro.
    nom: str = ""

    #: Le problème MESURÉ qui justifie la campagne. Interne à l'équipe : il
    #: porte les taux, que le message client ne doit jamais exposer.
    probleme: str = ""

    #: Marché, presse récente, veille de l'Agent 1, et ce qui manque.
    contexte: Optional[Contexte] = None

    #: Les trois angles proposés. Le premier est celui que les mesures
    #: désignent ; les deux autres sont des alternatives assumées.
    strategies: list = field(default_factory=list)

    #: Angle retenu, quand l'utilisateur en a choisi un.
    strategie: Optional[str] = None

    #: Registre d'écriture. Voir `domain/marketing.TONS`.
    ton: str = TON_DEFAUT

    #: Contenus par format, produits à la demande (`/contenus`).
    contenus: dict = field(default_factory=dict)

    #: Campagne dont celle-ci est une révision.
    parent_id: Optional[int] = None

    #: Objectif que les MESURES désignaient, quand l'utilisateur en a imposé un
    #: autre. Rendu visible : une campagne menée à contre-mesure peut être un
    #: choix parfaitement légitime, mais il doit être un choix conscient.
    objectif_mesure: Optional[str] = None

    brief: Optional[Brief] = None
    campaign_id: Optional[int] = None
    redige_par_modele: bool = False
    transmise: bool = False
    appels_llm: int = 0

    #: Renseigné quand aucune campagne n'a pu être proposée. C'est une réponse,
    #: pas une panne : « rien ne mérite une campagne cette semaine » est une
    #: information, et la taire ferait croire à un agent en échec.
    refus: Optional[str] = None

    #: Cibles écartées et pourquoi — lu en mode verbeux pour régler les seuils.
    ecartees: list[dict] = field(default_factory=list)

    #: Verdict de l'Agent 3 sur les données de la cible, quand il a pu être lu.
    #:
    #: PORTÉ PAR LA CAMPAGNE ET NON CALCULÉ À L'AFFICHAGE : une campagne est
    #: relue des semaines plus tard, depuis la fiche ou le dossier. Recalculer
    #: la qualité à ce moment-là afficherait celle d'aujourd'hui sous une
    #: décision prise hier — et une campagne validée sur des données correctes
    #: paraîtrait avoir été bâtie sur du sable, ou l'inverse.
    qualite_donnees: Optional[dict] = None

    def resume(self) -> str:
        if self.refus:
            return f"aucune campagne · {self.refus}"
        return (
            f"{self.cible.label if self.cible else '?'} · "
            f"{self.segment.label if self.segment else '?'} "
            f"({self.taille_segment} avis) · "
            f"{self.objectif.label if self.objectif else '?'} · "
            f"{'rédigé' if self.redige_par_modele else 'gabarit'}"
        )

    def as_dict(self) -> dict[str, Any]:
        """Trace journalisable : tout ce qui a fondé la proposition."""
        return {
            "nom": self.nom,
            "probleme": self.probleme,
            "cible": self.cible.as_dict() if self.cible else None,
            "segment": self.segment.cle if self.segment else None,
            "segment_label": self.segment.label if self.segment else None,
            "taille_segment": self.taille_segment,
            "objectif": self.objectif.cle if self.objectif else None,
            "objectif_mesure": self.objectif_mesure,
            "canal": self.canal.cle if self.canal else None,
            "actions": list(self.actions),
            "ton": self.ton,
            "strategie": self.strategie,
            "strategies": [
                {"cle": s.cle, "label": s.label, "angle": s.angle,
                 "objectif": s.objectif, "exige_une_offre": s.exige_une_offre}
                for s in self.strategies
            ],
            "parent_id": self.parent_id,
            "contexte": self.contexte.as_dict() if self.contexte else None,
            "brief": self.brief.as_dict() if self.brief else None,
            "redige_par_modele": self.redige_par_modele,
        }

    def avertissements_critiques(self) -> list[str]:
        """Réserves qui ne doivent JAMAIS rester silencieuses.

        PARTAGÉ ENTRE `.texte()` (le rapport complet) ET LE MESSAGE TELEGRAM
        COMPACT (17 août 2026), et c'est délibéré : dupliquer cette logique
        entre les deux garantirait qu'elles divergent le jour où l'une des
        deux oublie une mise à jour — exactement le défaut qu'un point unique
        de vérité est censé empêcher.

        Trois réserves, aucune décorative : la fiabilité des données de la
        cible (Agent 3), la mesurabilité de l'objectif choisi, et l'attribution
        de son KPI. Chacune peut faire dérailler une décision prise sans elle.
        """
        lignes: list[str] = []
        if self.qualite_donnees and self.qualite_donnees.get("mention"):
            lignes.append(self.qualite_donnees["mention"])
            if not self.qualite_donnees.get("fiable", True):
                lignes.append(
                    "Cette proposition ne doit pas être présentée comme fondée "
                    "sur des données représentatives."
                )
        if self.objectif and not self.objectif.mesurable:
            lignes.append(
                f"⚠️ L'atteinte de cet objectif ne sera pas mesurable : il "
                f"faudrait {self.objectif.donnee_manquante}."
            )
        elif self.objectif and self.objectif.maille == "pays":
            lignes.append(
                "⚠️ Le suivi de cet objectif n'existe qu'au niveau NATIONAL et "
                "annuel : il bougera aussi pour les concurrents, et ne "
                "s'attribuera pas à cette campagne."
            )
        return lignes

    def cible_lisible(self) -> str:
        """Le segment, en une phrase qui nomme le motif plutôt que le concept.

        `segment.critere` seul (« avis clients négatifs dont le motif dominant
        est identifié ») décrit la RÈGLE de ciblage, pas le grief réel. Pour un
        message lu en quelques secondes, « clients ayant signalé un problème de
        débit » est actionnable ; sa version générique ne l'est pas.
        """
        base = {
            "insatisfaits_motif": "clients ayant signalé un problème lié à",
            "detracteurs": "clients ayant déposé un avis négatif",
            "promoteurs": "clients ayant déposé un avis positif",
        }.get(self.segment.cle, self.segment.label.lower())
        motif = self.cible.motif if self.cible else None
        if self.segment.cle == "insatisfaits_motif" and motif:
            return f"{base} « {aspect_label(motif).lower()} »"
        return base

    def texte(self) -> str:
        """La proposition, en clair. Sert à Telegram, à la CLI et aux tests."""
        if self.refus:
            return self.refus
        lignes = [
            # SANS ÉMOJI : l'en-tête du canal en porte déjà un, et « 📣
            # Proposition de campagne / 📣 Nom » en alignait deux d'affilée. Le
            # texte doit rester composable avec l'en-tête de celui qui l'affiche.
            f"« {self.nom} »",
            "",
            f"Problème identifié : {self.probleme}",
            "",
            f"Cible : {self.cible.label}",
            f"Segment : {self.segment.label} — {self.taille_segment} avis "
            f"({self.segment.critere})",
            f"Objectif : {self.objectif.label} — {self.objectif.definition}",
            f"Suivi : {self.objectif.kpi_label}, attendue en {self.objectif.sens}",
            f"Canal : {self.canal.label}",
            "",
            f"Accroche : {self.accroche}",
            f"Message : {self.message}",
        ]
        # LA RÉSERVE DE QUALITÉ PASSE AVANT LES ACTIONS, contrairement au
        # briefing de l'Agent 1 où elle vient en dernier. Ce n'est pas une
        # incohérence : une campagne se lit pour être VALIDÉE, et la personne
        # qui valide doit connaître la réserve avant de lire ce qu'on lui
        # propose de faire. Un briefing, lui, se lit pour être informé — la
        # réserve y qualifie ce qu'on vient de lire.
        avertissements = self.avertissements_critiques()
        if avertissements:
            lignes.append("")
            lignes.extend(avertissements)

        if self.actions:
            lignes.append("")
            lignes.append("À faire, d'après ce que disent les avis :")
            lignes += [f"— {a}" for a in self.actions]
        if self.strategies:
            lignes.append("")
            lignes.append("Autres angles possibles :")
            for s in self.strategies[1:]:
                marque = " (offre à définir par vous)" if s.exige_une_offre else ""
                lignes.append(f"— Option {s.cle}, {s.label} : {s.angle}{marque}")
        # LE CONTEXTE VIENT APRÈS LA PROPOSITION, jamais avant. Il éclaire une
        # décision déjà lisible ; placé en tête, il ferait de la proposition une
        # note de synthèse que personne ne lirait jusqu'au message.
        if self.contexte and not self.contexte.vide:
            lignes.append("")
            lignes += self.contexte.lignes(self.cible.pays if self.cible else None)
        if self.contexte and self.contexte.indisponibles:
            lignes.append("")
            lignes += self.contexte.indisponibles
        if self.objectif_mesure and self.objectif_mesure != self.objectif.cle:
            lignes.append("")
            lignes.append(
                f"Note : les mesures désignaient plutôt l'objectif « "
                f"{OBJECTIFS[self.objectif_mesure].label} » ; c'est votre demande "
                "qui a été suivie."
            )
        return "\n".join(lignes)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEME_BRIEF = """\
Tu traduis une demande de campagne en PARAMÈTRES. Tu n'as accès à aucune donnée, \
tu ne rédiges rien et tu ne proposes aucune campagne : une autre partie du \
programme mesurera et décidera.

Tu réponds UNIQUEMENT par un objet JSON de cette forme :

{{
  "operateur": nom exact d'un opérateur de la liste, ou null,
  "pays":      nom exact d'un pays de la liste, ou null,
  "region":    nom exact d'une région de la liste, ou null,
  "objectif":  {objectifs} ou null,
  "canal":     {canaux} ou null,
  "jours":     entier (période à analyser) ou null,
  "segmentation": liste des critères de ciblage cités par la demande, choisis \
parmi {dimensions} — liste vide si la demande n'en cite aucun
}}

IMPORTANT SUR "segmentation" : relève TOUS les critères que la demande cite, \
même ceux dont tu ignores s'ils sont mesurables. « les jeunes de Casablanca qui \
consomment beaucoup » donne ["age", "ville", "consommation"]. C'est le \
programme qui dira lesquels sont disponibles ; ton rôle est de ne pas les \
laisser passer sous silence.

Ce que veut dire chaque objectif :
{gloses_objectifs}

Ce que veut dire chaque canal :
{gloses_canaux}

RÈGLES ABSOLUES
- N'invente JAMAIS un nom absent des listes ci-dessous.
- Une filiale est un opérateur ET un pays : « Orange Mali » se traduit par \
"operateur": "Orange", "pays": "Mali".
- Laisse null tout ce que la demande ne dit pas. N'essaie pas de deviner un \
objectif à partir du ton de la phrase : c'est la mesure qui le décide.

{vocabulaire}

EXEMPLES
Demande : « une campagne pour Orange au Mali, plutôt rassurante »
{{"operateur": "Orange", "pays": "Mali", "objectif": "reassurance", \
"canal": null, "jours": null, "segmentation": []}}

Demande : « les jeunes clients qui consomment beaucoup mais sont insatisfaits \
du prix »
{{"operateur": null, "pays": null, "region": null, "objectif": null, \
"canal": null, "jours": null, "segmentation": ["age", "consommation", "motif"]}}

Demande : « quelque chose par SMS sur les 90 derniers jours »
{{"operateur": null, "pays": null, "region": null, "objectif": null, \
"canal": "sms", "jours": 90, "segmentation": []}}
"""

_SYSTEME_REDACTION = """\
Tu rédiges le TEXTE d'une campagne, à partir d'un dossier déjà décidé par le \
programme : la cible, le segment, l'objectif et le canal ne sont pas discutables.

Tu réponds UNIQUEMENT par un objet JSON :
{{"nom": "...", "accroche": "...", "message": "..."}}

"nom" est le nom INTERNE de la campagne, celui dont l'équipe marketing parlera \
en réunion : deux ou trois mots, mémorisable, sans nom de marque ni chiffre \
(« Facture Claire », « Retour Réseau »). Ce n'est pas un slogan et il ne sera \
jamais montré à un client.

RÈGLES ABSOLUES
- N'ANNONCE AUCUN AVANTAGE COMMERCIAL. Ni remise, ni geste, ni offre, ni data \
offerte, ni compensation, ni gratuité — même vague, même au conditionnel. Tu ne \
sais pas ce que l'entreprise est prête à donner, et un message qui le suppose \
l'engage.
- N'écris aucun nombre qui ne figure pas dans les faits fournis.
- Ne promets pas de délai, de correctif ni de résultat : dis ce qui est fait ou \
ce qui est proposé, jamais ce qui est garanti.
- Le message s'adresse au CLIENT, à la deuxième personne. L'accroche fait au \
plus 60 caractères, le message respecte la limite du canal indiquée.
- Pas de nom de marque inventé, pas d'emoji, pas de majuscules criées.
- Français simple, phrases courtes. Le lecteur est sur un téléphone, souvent \
mécontent.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class CampaignAgent:
    """Propose des campagnes fondées sur la satisfaction mesurée, et en rend compte."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        client: Optional[LLMClient] = None,
        stats: Optional[StatsRepository] = None,
        campagnes: Optional[CampaignRepository] = None,
        notifier: Optional[TelegramNotifier] = None,
        contexte: Optional[CollecteurDeContexte] = None,
    ):
        self.db = db
        self.settings = settings
        self.stats = stats or StatsRepository(db)
        self.campagnes = campagnes or CampaignRepository(db)
        self.client = client
        self.notifier = notifier
        #: Facultatif : sans lui, la campagne reste complète mais perd son
        #: ancrage marché, presse et veille. Les avertissements sur les données
        #: manquantes, eux, sont rendus dans tous les cas — c'est la partie qui
        #: ne dépend d'aucune source.
        self.contexte = contexte
        #: Garde-fou de l'Agent 3. Dépendance Python directe et non appel à
        #: `/quality/trust` : les deux agents tournent dans le même processus,
        #: et l'API est un conteneur séparé du worker.
        from reviews.agents.quality.garde import construire_garde

        self.garde = construire_garde(db, settings)
        self._catalogue: Optional[Catalogue] = None

    # ----------------------------------------------------------------- Public

    def run(self, dry_run: bool = False) -> Campagne:
        """Passage automatique : l'agent choisit lui-même sa cible.

        C'est le mode du planificateur. Il n'a PAS de chemin de code à lui :
        c'est `proposer()` sans description. Deux chemins auraient divergé au
        premier réglage de seuil, et le mode automatique aurait fini par ne plus
        appliquer les mêmes règles que le mode interactif.
        """
        return self.proposer(dry_run=dry_run)

    def proposer(
        self,
        description: str = "",
        dry_run: bool = False,
        perimetre: Optional[tuple[StatsFilter, int]] = None,
    ) -> Campagne:
        """Propose une campagne. Ne lève jamais.

        Args:
            description: orientation libre de l'utilisateur. Vide = passage
                automatique sur tout le périmètre suivi.
            dry_run: mesure et décide, mais n'appelle pas le modèle pour la
                rédaction, n'enregistre rien et n'envoie rien. C'est le mode des
                réglages de seuils : le faire en conditions réelles consommerait
                du quota et remplirait la table de campagnes fictives.
            perimetre: périmètre IMPOSÉ par l'appelant, quand il le connaît sans
                avoir à le deviner. Couple `(filtre, jours)`.

                POURQUOI CE PARAMÈTRE EXISTE. La CLI et Telegram ne reçoivent
                qu'une phrase : il faut un modèle pour en tirer une filiale et
                une fenêtre. L'interface web, elle, a des SÉLECTEURS — l'entité
                et la période y sont choisies dans des listes déjà validées. Les
                faire re-deviner par un modèle risquerait un refus sur un
                périmètre pourtant certain, et pourrait rendre une AUTRE filiale
                que celle affichée à l'écran.

                IL NE REMPLACE QUE LE PÉRIMÈTRE. La description continue de
                passer par le modèle, qui en tire l'INTENTION — objectif visé,
                canal souhaité, et surtout les dimensions de segmentation que
                l'utilisateur croit disponibles (« les jeunes », « à
                Casablanca »), recueillies pour être RÉFUTÉES. Court-circuiter
                cette traduction ferait accepter en silence une demande dont la
                moitié est irréalisable.
        """
        campagne = Campagne()

        # 1. Le brief. Un périmètre mal compris est la faute à ne pas commettre :
        #    une description qu'on n'arrive pas à traduire fait refuser, jamais
        #    retomber sur « tout le périmètre » — ce qui produirait une campagne
        #    parfaitement présentable sur la mauvaise filiale.
        try:
            brief, appels = self._brief(description)
            if perimetre is not None:
                brief = _imposer_perimetre(brief, *perimetre)
        except QuestionRefusee as exc:
            return Campagne(refus=str(exc))
        except LLMUnavailable as exc:
            return Campagne(refus=str(exc))
        except LLMError:
            logger.warning("Traduction du brief en échec", exc_info=True)
            return Campagne(
                refus="Je n'arrive pas à interpréter votre description pour "
                "l'instant. Relancez sans description pour une proposition "
                "automatique, ou réessayez dans quelques minutes."
            )
        campagne.brief = brief
        campagne.appels_llm = appels

        # 2. Les mesures, puis l'arbitrage. Aucun modèle n'intervient ici.
        try:
            cibles = self._cibles(brief)
        except Exception:  # noqa: BLE001
            logger.exception("Mesures de campagne en échec")
            return Campagne(brief=brief, refus="La mesure a échoué de mon côté.")

        campagne.ecartees = [
            {"entite": c.label, "raison": c.ecartee_parce_que}
            for c in cibles
            if c.ecartee_parce_que
        ]

        cible = self._premiere_disponible(cibles, campagne)
        if cible is None:
            campagne.refus = campagne.refus or (
                "Aucun périmètre ne réunit aujourd'hui de quoi bâtir une "
                "campagne défendable : il faut un volume d'avis suffisant et un "
                "segment d'au moins quelques dizaines de personnes."
            )
            return campagne

        # 3. Segment, objectif, canal, leviers — décidés, jamais demandés.
        segment = choisir_segment(cible)
        mesure = choisir_objectif(cible, segment)
        objectif = OBJECTIFS.get(brief.objectif or "", mesure)
        canal = CANAUX[choisir_canal(cible.composition, brief.canal)]

        campagne.cible = cible
        campagne.segment = segment
        campagne.objectif = objectif
        campagne.objectif_mesure = mesure.cle
        campagne.canal = canal
        campagne.taille_segment = cible.taille_segment(segment)
        campagne.actions = leviers(cible, segment, objectif)

        # 4. Le contexte : marché, presse récente, veille de l'Agent 1, et ce
        #    qui manque. C'est ce qui rend la proposition RÉALISTE — sans lui,
        #    l'agent proposerait d'expliquer un prix sans savoir s'il est élevé.
        campagne.contexte = self._contexte(campagne)
        campagne.probleme = self._probleme(campagne)
        campagne.strategies = strategies_pour(mesure)

        # 5. La rédaction, seule étape confiée au modèle — et vérifiée après.
        accroche, message, redige, appels = self._rediger(campagne, dry_run=dry_run)
        campagne.accroche = accroche
        campagne.message = message
        campagne.redige_par_modele = redige
        campagne.appels_llm += appels
        campagne.nom = campagne.nom or self._nom_par_defaut(campagne)

        # QUALITÉ DES DONNÉES DE LA CIBLE, lue avant que la campagne ne parte.
        #
        # Elle ne bloque PAS la proposition, et c'est délibéré : une filiale
        # mal couverte est souvent celle dont personne ne s'occupe, donc
        # justement celle qui mériterait une action. Refuser de proposer la
        # rendrait définitivement invisible. On propose, et on dit ce que vaut
        # le socle — la décision reste à l'équipe.
        #
        # Renseignée AVANT `dry_run` : le mode à blanc sert à voir ce que
        # l'agent AURAIT proposé, réserve comprise.
        #
        # Ne s'applique qu'à la maille FILIALE : le score de qualité est calculé
        # par filiale, et le rapporter à un pays ou à un opérateur agrégerait
        # des situations trop différentes pour porter un seul verdict.
        if cible.level == "subsidiary":
            campagne.qualite_donnees = self.garde.verdict(cible.key).as_dict()

        if dry_run:
            return campagne

        campagne.campaign_id = self.campagnes.creer(
            name=campagne.nom,
            problem=campagne.probleme,
            entity_level=cible.level,
            entity_key=cible.key,
            entity_label=cible.label,
            segment=segment.cle,
            objective=objectif.cle,
            channel=canal.cle,
            segment_size=float(campagne.taille_segment),
            window_days=brief.jours,
            hook=accroche,
            message=message,
            brief=brief.texte or None,
            written_by_llm=redige,
            payload=campagne.as_dict(),
            tone=campagne.ton,
            strategies=campagne.as_dict()["strategies"],
        )
        campagne.transmise = self._transmettre(campagne)
        if campagne.campaign_id and campagne.transmise:
            # L'IDENTIFIANT DU MESSAGE EST GARDÉ ICI, pas plus tard : c'est la
            # seule fenêtre où il est disponible, et sans lui la proposition ne
            # pourra jamais être retirée du groupe (48 h, limite de l'API).
            self.campagnes.marquer_transmise(
                campagne.campaign_id,
                getattr(self.notifier, "last_message_id", None),
            )
        logger.info("Campagne proposée : %s", campagne.resume())
        return campagne

    # -------------------------------------------------------------- Le brief

    def catalogue(self) -> Catalogue:
        """Ce que la base connaît réellement — même source que le dashboard."""
        if self._catalogue is None:
            self._catalogue = Catalogue.depuis(self.stats.filter_options())
        return self._catalogue

    def _brief(self, description: str) -> tuple[Brief, int]:
        description = (description or "").strip()
        if not description:
            return brief_vide(JOURS_DEFAUT), 0

        if self.client is None or not self.client.available:
            raise LLMUnavailable(
                "Aucun modèle n'est configuré : je ne peux pas interpréter une "
                "description libre. Relancez sans description pour obtenir une "
                "proposition fondée sur les mesures seules."
            )

        systeme = _SYSTEME_BRIEF.format(
            objectifs=" | ".join(f'"{k}"' for k in OBJECTIFS),
            canaux=" | ".join(f'"{k}"' for k in CANAUX),
            dimensions=" | ".join(f'"{k}"' for k in DIMENSIONS),
            gloses_objectifs="\n".join(
                f"- {o.cle} : {o.definition}" for o in OBJECTIFS.values()
            ),
            gloses_canaux="\n".join(
                f"- {c.cle} : {c.label}, {c.note}" for c in CANAUX.values()
            ),
            vocabulaire=self.catalogue().vocabulaire(),
        )
        brut = self.client.complete_json(
            system=systeme,
            user=description,
            # Nulle : deux fois la même demande doit donner deux fois le même
            # périmètre. Une traduction qui varie ferait changer la cible sans
            # qu'aucune donnée ait bougé.
            temperature=0.0,
            max_tokens=250,
        )
        if not isinstance(brut, dict):
            raise LLMError(f"Brief inattendu : {type(brut).__name__}")
        logger.info("Brief traduit : %s", json.dumps(brut, ensure_ascii=False))
        return valider_brief(brut, self.catalogue(), description), 1

    # -------------------------------------------------------------- Les cibles

    def _cibles(self, brief: Brief) -> list[Cible]:
        """Mesure le périmètre et rend les cibles arbitrées, motifs compris.

        DEUX PASSES, ET C'EST UNE QUESTION DE COÛT. Le classement d'ensemble
        tient en une requête ; le motif dominant, lui, se mesure filiale par
        filiale. On arbitre donc d'abord sans les motifs — ce qui suffit à écarter
        l'immense majorité des périmètres —, puis on n'enrichit que les
        `MAX_CIBLES_EXAMINEES` premières, avant un second arbitrage qui, lui,
        connaît les motifs.

        LE NIVEAU EST TOUJOURS LA FILIALE, quel que soit le périmètre demandé.
        C'est le niveau où une campagne se décide et s'exécute : « une campagne
        pour l'Afrique de l'Ouest » n'a pas de destinataire, pas de budget et pas
        de responsable.
        """
        data = self.stats.ranking(
            f=brief.filtre,
            level="subsidiary",
            sort="volume",
            min_reviews=0,
            limit=MAX_LIGNES_CLASSEMENT,
        )
        cibles = [self._cible(r) for r in (data.get("rows") or [])]
        if not cibles:
            return []

        premieres = [
            c for c in arbitrer_cibles(cibles) if c.retenue
        ][:MAX_CIBLES_EXAMINEES]
        for c in premieres:
            self._enrichir_motif(c, brief)
        # Le second arbitrage a le dernier mot : c'est lui qui connaît les
        # motifs, donc lui qui mesure la vraie taille des segments.
        arbitrees = arbitrer_cibles(premieres)
        # Comparaison par IDENTITÉ et non par égalité : `Cible` est une
        # dataclass, donc deux filiales aux mesures identiques — courant sur les
        # petits volumes — sont « égales » et l'une masquerait l'autre.
        examinees = {id(c) for c in premieres}
        ecartees = [
            c for c in cibles if id(c) not in examinees and c.ecartee_parce_que
        ]
        return arbitrees + ecartees

    @staticmethod
    def _cible(ligne: dict) -> Cible:
        """Une ligne de classement -> une cible.

        Les conversions en `float` ne sont pas cosmétiques : PostgreSQL rend les
        pourcentages en `Decimal`, et `Decimal - float` lève. C'est le piège qui
        avait déjà fait tomber l'agent de veille.
        """
        def _f(cle: str) -> Optional[float]:
            valeur = ligne.get(cle)
            return None if valeur is None else float(valeur)

        return Cible(
            level="subsidiary",
            key=str(ligne.get("key")),
            label=ligne.get("label") or "?",
            pays=ligne.get("country"),
            iso2=ligne.get("iso2"),
            avis_clients=int(ligne.get("avis_clients") or 0),
            positifs=int(ligne.get("positifs") or 0),
            negatifs=int(ligne.get("negatifs") or 0),
            part_negatifs=_f("part_negatifs"),
            part_positifs=_f("part_positifs"),
            note_moyenne=_f("note_moyenne"),
            composition=dict(ligne.get("composition") or {}),
        )

    def _enrichir_motif(self, cible: Cible, brief: Brief) -> None:
        """Ajoute le motif dominant des avis négatifs, s'il y en a un.

        LES ASPECTS, JAMAIS LES TERMES DU LEXIQUE — et contrairement à l'agent de
        veille, sans repli sur ceux-ci. Un briefing peut se contenter de « les
        mots qui reviennent : can't, bad, useless » : le lecteur fait le tri. Une
        campagne, non. On ne bâtit pas un message client sur « can't » ; mieux
        vaut un segment « détracteurs » assumé, qui reste vrai, qu'un motif qui
        n'en est pas un.
        """
        f = self._filtre_cible(cible, brief.jours)
        try:
            data = self.stats.themes(
                f, polarity="negative", limit=1, dimension="aspects"
            )
        except Exception:  # noqa: BLE001
            logger.warning("Motifs indisponibles pour %s", cible.label, exc_info=True)
            return
        lignes = data.get("terms") or []
        if not lignes:
            return
        premier = lignes[0]
        if premier.get("term") == OTHER:
            return
        cible.motif = premier.get("term")
        cible.motif_avis = int(premier.get("avis") or 0)

    @staticmethod
    def _filtre_cible(cible: Cible, jours: int) -> StatsFilter:
        """Le périmètre d'UNE cible, dans le vocabulaire du contrat de filtre."""
        try:
            return StatsFilter(days=jours, subsidiaries=(int(cible.key),))
        except (TypeError, ValueError):
            return StatsFilter(days=jours)

    def _premiere_disponible(
        self, cibles: list[Cible], campagne: Campagne
    ) -> Optional[Cible]:
        """La meilleure cible qui n'a pas déjà eu sa campagne récemment.

        La RÈGLE de non-répétition est celle de l'agent de veille
        (`should_report`), appliquée à une autre table. La partager plutôt que la
        réécrire garantit qu'un réglage de la dissymétrie « déjà dit / mais
        pire » vaut pour les deux agents.
        """
        for cible in cibles:
            if not cible.retenue:
                continue
            segment = choisir_segment(cible)
            taille = cible.taille_segment(segment)
            derniere = self.campagnes.derniere_pour(cible.level, cible.key)
            proposer, pourquoi = should_report(
                derniere,
                float(taille),
                cooldown_days=COOLDOWN_JOURS,
                aggravation_points=AGGRAVATION_AVIS,
            )
            if proposer:
                cible.raisons.append(pourquoi)
                return cible
            campagne.ecartees.append({"entite": cible.label, "raison": pourquoi})
        return None

    # -------------------------------------------------- Contexte et problème

    def _contexte(self, campagne: Campagne) -> Contexte:
        """Marché, presse récente, veille — et ce qui manque."""
        cible = campagne.cible
        demandees = list(campagne.brief.dimensions) if campagne.brief else []
        if self.contexte is None:
            from reviews.agents.contexte import Contexte as _C
            from reviews.agents.contexte import declarer_indisponibles

            return _C(indisponibles=declarer_indisponibles(demandees))
        return self.contexte.pour(
            iso2=cible.iso2 if cible else None,
            subsidiary_id=cible.key if cible else None,
            pays=cible.pays if cible else None,
            dimensions_demandees=demandees,
        )

    @staticmethod
    def _probleme(campagne: Campagne) -> str:
        """Le problème mesuré qui justifie la campagne, en une ou deux phrases.

        ENTIÈREMENT CALCULÉ. C'est le champ que l'équipe relira dans six
        semaines pour juger si la campagne visait juste : le laisser rédiger
        produirait une formulation différente à chaque appel, donc incomparable
        d'une campagne à l'autre. Il ne contient que des nombres issus des
        agrégats.

        DEUX PHRASES AU PLUS, et la seconde n'apparaît que si le marché dit
        quelque chose que les avis ne disent pas. « Le panier data coûte 4,60 $
        par mois dans ce pays » transforme une plainte tarifaire en fait
        vérifiable — c'est exactement ce qui manquait pour qu'une campagne soit
        réaliste plutôt que plausible.
        """
        cible, segment = campagne.cible, campagne.segment
        if cible is None or segment is None:
            return ""

        motif = levier_label(motif_du_segment(cible, segment))
        if segment.polarite == "negative":
            phrase = (
                f"{nombre(cible.part_negatifs or 0.0)} % des avis clients sont "
                f"négatifs sur la période ({cible.avis_clients} avis)"
            )
            if motif:
                phrase += (
                    f", et {cible.motif_avis} de ces plaintes portent sur "
                    f"« {motif.lower()} » — soit "
                    f"{nombre(cible.part_motif, 0)} % des mécontents"
                )
        else:
            phrase = (
                f"{nombre(cible.part_positifs or 0.0)} % des avis clients sont "
                f"positifs sur la période ({cible.avis_clients} avis), une "
                "satisfaction assez élevée pour être montrée"
            )
        phrases = [phrase + "."]

        # LE MARCHÉ N'EST REPRIS QUE S'IL ÉCLAIRE LE MOTIF. Accoler la
        # couverture 4G à une plainte de facturation ferait du contexte un
        # décor : le lecteur apprendrait à le sauter, y compris quand il porte
        # l'information décisive.
        contexte = campagne.contexte
        if contexte and contexte.marche and motif:
            pertinents = [
                m for m in contexte.marche
                if _MOTIF_VERS_MARCHE.get(cible.motif or "", "") in m
            ]
            if pertinents:
                phrases.append(
                    f"Pour situer : {pertinents[0]} dans ce pays "
                    f"({contexte.annee_marche})."
                )
        return " ".join(phrases)

    @staticmethod
    def _nom_par_defaut(campagne: Campagne) -> str:
        """Un nom composé, quand le modèle n'en a pas produit.

        Descriptif plutôt qu'inventif — « Réassurance facturation · Orange
        Mali ». Un gabarit ne fabrique pas de « Data Boost » convaincant, et
        essayer produirait des noms interchangeables qui ne désigneraient plus
        rien dans une liste de vingt campagnes.
        """
        objectif = campagne.objectif.label if campagne.objectif else "Campagne"
        motif = levier_label(
            motif_du_segment(campagne.cible, campagne.segment)
            if campagne.cible and campagne.segment else None
        )
        entite = campagne.cible.label if campagne.cible else "?"
        return f"{objectif}{' ' + motif.lower() if motif else ''} · {entite}"

    # ------------------------------------------------------------- Rédaction

    def _rediger(
        self, campagne: Campagne, dry_run: bool, consigne: Optional[str] = None
    ) -> tuple[str, str, bool, int]:
        """Fait écrire l'accroche et le message. Rend toujours un texte utilisable.

        TROIS RAISONS DE RENDRE LE GABARIT, et dans les trois cas la proposition
        reste complète et défendable : pas de modèle, appel en échec, ou texte
        rejeté par les vérifications. C'est cette propriété qui autorise à
        brancher un modèle sur un contenu destiné à des clients.
        """
        accroche, message = self._gabarit(campagne)
        if dry_run or self.client is None or not self.client.available:
            return accroche, message, False, 0

        try:
            brut = self.client.complete_json(
                system=_SYSTEME_REDACTION,
                user=self._dossier(campagne, consigne),
                # Un peu de latitude sur la formulation : c'est un texte
                # commercial, un gabarit figé se reconnaît immédiatement. Le
                # fond, lui, est verrouillé par les vérifications qui suivent.
                temperature=0.4,
                max_tokens=400,
            )
        except (LLMUnavailable, LLMError) as exc:
            logger.info("Rédaction indisponible, repli sur le gabarit : %s", exc)
            return accroche, message, False, 0

        if not isinstance(brut, dict):
            return accroche, message, False, 1

        propose_accroche = str(brut.get("accroche") or "").strip()
        propose_message = str(brut.get("message") or "").strip()
        if not propose_accroche or not propose_message:
            return accroche, message, False, 1

        refus = self._refus_de_redaction(campagne, propose_accroche, propose_message)
        if refus:
            logger.warning("Rédaction de campagne écartée : %s", refus)
            return accroche, message, False, 1

        # LE NOM SUIT LE SORT DU TEXTE, jamais l'inverse. Retenir un nom produit
        # par un appel dont le message a été rejeté laisserait une campagne au
        # gabarit sous un nom inventé — un mélange que personne ne pourrait
        # relire. Il est donc affecté ICI, après les vérifications.
        propose_nom = str(brut.get("nom") or "").strip()
        if propose_nom and not promesses_detectees(propose_nom):
            campagne.nom = propose_nom[:60]
        return propose_accroche, propose_message, True, 1

    def _refus_de_redaction(
        self, campagne: Campagne, accroche: str, message: str
    ) -> Optional[str]:
        """Pourquoi le texte du modèle est refusé, ou None s'il est acceptable.

        TROIS VÉRIFICATIONS, DANS CET ORDRE DE GRAVITÉ :

        1. UNE PROMESSE COMMERCIALE. La plus grave, et la seule dont la victime
           serait un client : « trois mois offerts » sera lu comme un engagement.
        2. UN CHIFFRE INVENTÉ. Un pourcentage produit de tête est indiscernable
           d'un pourcentage mesuré une fois écrit dans une phrase.
        3. UN DÉPASSEMENT DE CANAL. Un SMS de 400 caractères n'est pas « un peu
           long » : il part en trois morceaux facturés trois fois, dont le
           dernier arrive tronqué.
        """
        texte = f"{accroche}\n{message}"

        promesses = promesses_detectees(texte)
        if promesses:
            return f"promesse commerciale non autorisée : {', '.join(promesses)}"

        inventes = chiffres_inventes(texte, self._chiffres_du_dossier(campagne))
        if inventes:
            return f"chiffres absents des mesures : {', '.join(inventes)}"

        if campagne.canal and len(message) > campagne.canal.max_caracteres:
            return (
                f"message de {len(message)} caractères pour un canal qui en "
                f"accepte {campagne.canal.max_caracteres}"
            )
        return None

    @staticmethod
    def _chiffres_du_dossier(campagne: Campagne) -> set[float]:
        """Les nombres qu'un texte de campagne a le droit d'employer.

        SANS LES RANGS, contrairement à une réponse de classement : un message
        client ne comporte pas de liste numérotée, et autoriser 1, 2, 3 ouvrirait
        la porte aux « 3 jours », « 2 fois plus » et autres engagements chiffrés
        que rien n'aurait mesurés.
        """
        cible = campagne.cible
        if cible is None:
            return set()
        return chiffres_autorises(
            [
                {
                    "avis_clients": cible.avis_clients,
                    "positifs": cible.positifs,
                    "negatifs": cible.negatifs,
                    "part_negatifs": cible.part_negatifs,
                    "part_positifs": cible.part_positifs,
                    "note_moyenne": cible.note_moyenne,
                    "taille_segment": campagne.taille_segment,
                    "motif_avis": cible.motif_avis,
                }
            ],
            (
                "avis_clients", "positifs", "negatifs", "part_negatifs",
                "part_positifs", "note_moyenne", "taille_segment", "motif_avis",
            ),
            avec_rangs=False,
        )

    def _dossier(self, campagne: Campagne, consigne: Optional[str] = None) -> str:
        """Les faits transmis au modèle. Rien d'autre ne l'atteint.

        Il n'y voit ni la base, ni le SQL, ni les seuils : seulement des mesures
        déjà faites et des décisions déjà prises. C'est ce qui rend impossible
        qu'il « choisisse » autre chose que la formulation.
        """
        cible, objectif, canal = campagne.cible, campagne.objectif, campagne.canal
        motif = levier_label(motif_du_segment(cible, campagne.segment))
        lignes = [
            f"Entité : {cible.label}" + (f" ({cible.pays})" if cible.pays else ""),
            f"Segment visé : {campagne.segment.label}, {campagne.taille_segment} "
            f"avis clients sur la période",
            f"Objectif : {objectif.label} — {objectif.definition}",
            f"Ce que l'objectif n'est pas : {objectif.exclusion}",
            f"Canal : {canal.label} — {canal.note}. "
            f"Le message doit tenir en {canal.max_caracteres} caractères.",
        ]
        if motif:
            lignes.append(f"Motif dominant des plaintes : {motif}")
        if campagne.actions:
            lignes.append(
                "Actions décidées par l'entreprise, que le message peut évoquer "
                "sans les chiffrer : " + " ; ".join(campagne.actions)
            )
        if cible.part_negatifs is not None:
            lignes.append(
                f"Mesures disponibles : {cible.avis_clients} avis clients, "
                f"{nombre(cible.part_negatifs)} % négatifs, "
                f"{nombre(cible.part_positifs or 0.0)} % positifs"
            )
        if campagne.brief and campagne.brief.texte:
            lignes.append(
                "Orientation demandée par l'utilisateur (à respecter pour le TON "
                "et l'angle, jamais pour les faits) : " + campagne.brief.texte
            )
        if campagne.ton and campagne.ton != TON_DEFAUT:
            lignes.append(f"Ton demandé : {TONS.get(campagne.ton, campagne.ton)}")
        if campagne.strategie and campagne.strategie in STRATEGIES:
            angle = STRATEGIES[campagne.strategie]
            lignes.append(f"Angle retenu : {angle.label} — {angle.angle}")
            if angle.exige_une_offre:
                # LE MODÈLE NE CHOISIT PAS L'OFFRE, il laisse la place. C'est la
                # seule façon d'écrire un message commercial sans engager
                # l'entreprise sur une contrepartie que personne n'a validée.
                lignes.append(
                    f"L'offre n'est PAS connue : écris « {EMPLACEMENT_OFFRE} » à "
                    "l'endroit où elle devra figurer, sans jamais l'inventer."
                )
        if consigne:
            lignes.append(
                "Demande de révision (registre et angle seulement, jamais les "
                "faits) : " + consigne
            )
        return "\n".join(lignes)

    @staticmethod
    def _gabarit(campagne: Campagne) -> tuple[str, str]:
        """Ce que l'agent sait écrire sans aucun modèle.

        Volontairement SOBRE : c'est un brouillon destiné à un humain qui
        validera, pas un texte publiable tel quel. Il dit ce qui est mesuré et
        ce qui est proposé, et il ne promet rien — donc il ne peut jamais être
        pire qu'inélégant.

        Il respecte la longueur du canal PAR CONSTRUCTION, et non par troncature
        de politesse : un gabarit qui déborderait rendrait le repli inutilisable
        exactement dans le cas où l'on en a besoin.
        """
        cible, objectif, canal = campagne.cible, campagne.objectif, campagne.canal
        if cible is None or objectif is None or canal is None:
            # UNE CAMPAGNE REFUSÉE N'A RIEN À RÉDIGER, et ce n'est pas une
            # anomalie : `proposer` rend un refus sans cible dès qu'aucun
            # périmètre ne franchit les seuils. Lever ici ferait d'une réponse
            # normale une trace d'exception, et ferait tomber tout appelant qui
            # traverse le résultat sans avoir regardé `refus` d'abord.
            return "", ""
        # MÊME RÈGLE QUE POUR LES LEVIERS : le motif est mesuré sur les avis
        # négatifs. « Merci pour vos retours sur la facturation » adressé à des
        # clients satisfaits leur rappellerait un problème qu'ils n'ont pas
        # signalé.
        motif = levier_label(motif_du_segment(cible, campagne.segment))
        # LES GUILLEMETS NE SONT PAS DÉCORATIFS : les libellés de la taxonomie
        # sont des étiquettes, pas des groupes nominaux (« Facturation & prix »).
        # Sans eux, le gabarit produisait « au sujet de facturation & prix »,
        # qui se lit comme une faute d'accord.
        sujet = f"vos retours sur « {motif.lower()} »" if motif else "vos retours"

        accroches = {
            "retention": f"{cible.label} : {sujet}, et ce que nous en faisons",
            "reassurance": f"{cible.label} : ce que vous payez, expliqué",
            "fidelisation": f"{cible.label} : merci pour {sujet}",
            "acquisition": f"{cible.label} : ce que nos clients en disent",
            "satisfaction": f"{cible.label} : ce que vous nous avez dit",
            "usage": f"{cible.label} : tirer le meilleur de votre connexion",
            "conversion": f"{cible.label} : une offre adaptée à votre usage",
            "upselling": f"{cible.label} : une offre adaptée à votre usage",
            "cross_selling": f"{cible.label} : un service qui peut vous servir",
        }
        messages = {
            "retention": (
                f"Nous avons lu {sujet}. Nous vous disons ici ce qui change, "
                "et où en suivre l'avancement."
            ),
            "reassurance": (
                "Plusieurs d'entre vous nous ont écrit au sujet de "
                + (f"« {motif.lower()} »" if motif else "votre offre")
                + ". Voici le détail de ce qui est décompté, et où le vérifier."
            ),
            "fidelisation": (
                "Merci pour vos retours : ils décident de ce que nous "
                "améliorons en premier. Voici la suite."
            ),
            "acquisition": (
                "Nos clients décrivent eux-mêmes ce qu'ils apprécient. "
                "Voici leurs mots, sans retouche."
            ),
            "satisfaction": (
                "Vos retours décident de ce que nous améliorons en premier. "
                "Voici où nous en sommes."
            ),
            "usage": (
                "Voici comment tirer le meilleur de votre connexion au "
                "quotidien, sans rien changer à votre forfait."
            ),
            # LES TROIS ANGLES COMMERCIAUX LAISSENT LA PLACE VIDE. Le gabarit ne
            # choisit pas plus d'offre que le modèle : il marque l'endroit où
            # elle doit être écrite par quelqu'un qui a le pouvoir de l'accorder.
            "conversion": (
                f"Au vu de votre usage, {EMPLACEMENT_OFFRE} pourrait vous "
                "convenir mieux que votre formule actuelle."
            ),
            "upselling": (
                f"Votre usage dépasse votre formule actuelle : "
                f"{EMPLACEMENT_OFFRE} y répondrait mieux."
            ),
            "cross_selling": (
                f"{EMPLACEMENT_OFFRE} complète ce que vous utilisez déjà. "
                "À découvrir quand vous voulez."
            ),
        }
        accroche = accroches.get(objectif.cle, f"{cible.label} : {sujet}")
        message = messages.get(objectif.cle, f"Nous avons lu {sujet}.")
        return _couper(accroche, 80), _couper(message, canal.max_caracteres)

    # ---------------------------------------------------------------- Envoi

    def _transmettre(self, campagne: Campagne) -> bool:
        """Pousse la proposition vers le canal de validation.

        VERS L'ÉQUIPE, JAMAIS VERS DES CLIENTS. Ce que cette méthode envoie est
        une demande de validation ; le message de campagne n'y figure qu'à titre
        de brouillon à relire.

        FORMAT COMPACT (17 août 2026), ET NON LE RAPPORT COMPLET. Le message
        Telegram donne de quoi décider en un regard — objectif, cible, message,
        canal — et rien de plus : le dossier entier (`.texte()`, `fiche()`)
        reste accessible par la CLI, le dashboard et `/campagne N` dans ce
        même canal, pour qui veut le détail avant de trancher.

        LES RÉSERVES CRITIQUES RESTENT AFFICHÉES, elles, quel que soit le
        format : `avertissements_critiques()` est le même point de vérité que
        `.texte()`, donc aucun risque qu'un garde-fou existe dans le rapport
        complet et disparaisse silencieusement du message qui déclenche
        `/valider`.
        """
        if self.notifier is None:
            logger.info("Assistant de campagne : aucun canal configuré.")
            return False

        e = TelegramNotifier._echapper
        lignes = [f"📣 <b>Campagne — {e(campagne.cible.label)}</b>", ""]

        # LA RÉSERVE PASSE EN PREMIER, avant même l'objectif — même principe
        # que `.texte()` : qui s'apprête à taper `/valider` doit la connaître
        # avant de lire ce qu'on lui propose de faire.
        avertissements = campagne.avertissements_critiques()
        if avertissements:
            lignes += [e(a) for a in avertissements]
            lignes.append("")

        lignes += [
            f"🎯 <b>Objectif :</b> {e(campagne.objectif.definition)}",
            "",
            f"👥 <b>Cible :</b> {e(campagne.cible_lisible())}",
            "",
            "💬 <b>Message :</b>",
            f"« {e(campagne.accroche)} »",
            "",
            f"📢 <b>Canal :</b> {e(campagne.canal.label)}",
        ]

        if campagne.campaign_id:
            lignes += [
                "",
                f"<i>Proposition n°{campagne.campaign_id} — à valider : "
                f"/valider {campagne.campaign_id} · à écarter : "
                f"/rejeter {campagne.campaign_id}</i>",
            ]
        lignes.append(
            "<i>Rien n'est envoyé à un client tant que la proposition n'est pas "
            "validée.</i>"
        )

        # BOUTONS EN PLUS DES COMMANDES TEXTE, JAMAIS À LEUR PLACE. Les deux
        # mènent à la même décision (voir `BoucleConversation._traiter_callback`
        # et `_texte_decision`, un seul point de vérité pour la confirmation) :
        # le bouton est plus rapide depuis un téléphone, la commande reste le
        # seul moyen sur un client Telegram qui ne rend pas les claviers en
        # ligne. `callback_data` porte le couple (commande, numéro) exactement
        # comme le texte, pour que le clic et la commande empruntent le même
        # chemin dans `BoucleConversation`.
        markup = (
            {
                "inline_keyboard": [[
                    {"text": "✅ Valider",
                     "callback_data": f"valider:{campagne.campaign_id}"},
                    {"text": "❌ Rejeter",
                     "callback_data": f"rejeter:{campagne.campaign_id}"},
                ]]
            }
            if campagne.campaign_id
            else None
        )
        return self.notifier.send_text("\n".join(lignes), reply_markup=markup)

    # ----------------------------------------------------- Fiche de campagne

    def fiche(self, campaign_id: int) -> dict:
        """Le dossier structuré d'une campagne — CAMPAIGN REPORT.

        ENTIÈREMENT COMPOSÉ DEPUIS CE QUI EST EN BASE, sans un seul appel de
        modèle. Ce document sert à décider et à archiver : deux exécutions
        doivent en rendre exactement le même, et il doit rester produisible le
        jour où le fournisseur de modèle est indisponible.

        LES KPI ATTENDUS SONT CEUX DE L'OBJECTIF, pas une liste passe-partout.
        « Taux de conversion, engagement, satisfaction, rétention » sous chaque
        campagne serait décoratif : personne ne les relèverait, et le rapport
        final ne pourrait s'appuyer sur aucun. Ici, un objectif porte un KPI et
        un seul — celui que `bilan()` ira mesurer.
        """
        campagne = self.campagnes.par_id(campaign_id)
        if campagne is None:
            return {"available": False, "raison": f"Campagne n°{campaign_id} inconnue."}

        objectif = OBJECTIFS.get(campagne["objective"])
        payload = campagne.get("payload") or {}
        contexte = payload.get("contexte") or {}
        canal = CANAUX.get(campagne["channel"])

        if objectif is None:
            kpi = "objectif inconnu"
        elif not objectif.mesurable:
            kpi = (
                f"{objectif.kpi_label} — NON MESURABLE ici : il faudrait "
                f"{objectif.donnee_manquante}"
            )
        else:
            kpi = (
                f"{objectif.kpi_label}, attendue en {objectif.sens}"
                + (" (maille nationale)" if objectif.maille == "pays" else "")
            )

        lignes = [
            "FICHE DE CAMPAGNE",
            "─" * 40,
            "",
            f"Campagne        : {campagne['name'] or '(sans nom)'}",
            f"Entité          : {campagne['entity_label']}",
            f"Statut          : {campagne['status']}"
            + (f" (par {campagne['decided_by']})" if campagne.get("decided_by") else ""),
            f"Proposée le     : {campagne['created_at'].strftime('%d/%m/%Y')}",
            "",
            f"Problème        : {campagne['problem'] or '(non renseigné)'}",
            "",
            f"Cible           : {payload.get('segment_label') or campagne['segment']}",
            f"Taille          : {int(campagne['segment_size'])} avis clients "
            f"sur {campagne['window_days']} jours",
            f"Objectif        : {objectif.label if objectif else campagne['objective']}",
            f"KPI de suivi    : {kpi}",
            f"Canal           : {canal.label if canal else campagne['channel']}",
            f"Ton             : {campagne.get('tone') or TON_DEFAUT}",
            "",
            f"Accroche        : {campagne['hook']}",
            f"Message         : {campagne['message']}",
        ]

        actions = payload.get("actions") or []
        if actions:
            lignes += ["", "Actions recommandées :"]
            lignes += [f"  {i}. {a}" for i, a in enumerate(actions, 1)]

        if contexte.get("marche"):
            lignes += ["", "Contexte marché :"]
            lignes += [f"  · {m}" for m in contexte["marche"]]
        if contexte.get("presse"):
            lignes += ["", "Presse récente :"]
            lignes += [f"  · {p}" for p in contexte["presse"]]
        if contexte.get("insight_veille"):
            lignes += ["", f"Veille (Agent 1) : {contexte['insight_veille']}"]
        if contexte.get("indisponibles"):
            lignes += ["", "Limites de la segmentation :"]
            lignes += [f"  {a}" for a in contexte["indisponibles"]]

        contenus = campagne.get("contents") or {}
        if contenus:
            lignes += ["", "CONTENUS", "─" * 40]
            lignes += _contenus_en_clair(contenus)

        lignes += [
            "",
            "─" * 40,
            "Aucune donnée d'envoi, d'ouverture ni de clic n'existe dans cette "
            "plateforme : le suivi porte sur la satisfaction du segment visé.",
        ]
        return {
            "available": True,
            "campaign_id": campaign_id,
            "texte": "\n".join(lignes),
        }

    # ------------------------------------------------- Contenus multi-formats

    def contenus(self, campaign_id: int) -> dict:
        """Décline le message dans les cinq formats. UN SEUL appel de modèle.

        POURQUOI À LA DEMANDE ET NON À CHAQUE PROPOSITION. Le budget de modèle
        est de 200 appels par jour, partagés avec l'analyse sémantique et le
        briefing du matin. Décliner systématiquement cinq formats pour une
        proposition dont neuf sur dix seront écartées consommerait ce budget
        pour des textes que personne ne lira. La proposition porte le message du
        canal recommandé ; le reste tient en une commande.
        """
        campagne = self.campagnes.par_id(campaign_id)
        if campagne is None:
            return {"available": False, "raison": f"Campagne n°{campaign_id} inconnue."}

        deja = campagne.get("contents") or {}
        if deja:
            # DÉJÀ PRODUITS = RENDUS TELS QUELS. Régénérer donnerait un autre
            # texte pour la même campagne, et l'équipe ne saurait plus lequel a
            # été validé. Une nouvelle version se demande par une révision.
            return {
                "available": True, "campaign_id": campaign_id, "regenere": False,
                "texte": "\n".join(_contenus_en_clair(deja)),
            }

        objectif = OBJECTIFS.get(campagne["objective"])
        if self.client is None or not self.client.available:
            produits = _contenus_gabarit(campagne)
            self.campagnes.enregistrer_contenus(campaign_id, produits)
            return {
                "available": True, "campaign_id": campaign_id, "regenere": True,
                "par_modele": False,
                "texte": "\n".join(_contenus_en_clair(produits)),
            }

        try:
            brut = self.client.complete_json(
                system=_SYSTEME_FORMATS.format(
                    formats="\n".join(
                        f"- {f.cle} : champs {', '.join(f.champs)} — "
                        f"{f.consigne} ({f.max_caracteres} caractères maximum)"
                        for f in FORMATS.values()
                    ),
                    ton=TONS.get(campagne.get("tone") or TON_DEFAUT, ""),
                    emplacement=EMPLACEMENT_OFFRE,
                ),
                user=_dossier_depuis_base(campagne, objectif),
                temperature=0.4,
                max_tokens=900,
            )
        except (LLMUnavailable, LLMError) as exc:
            logger.info("Déclinaison indisponible, repli sur les gabarits : %s", exc)
            produits = _contenus_gabarit(campagne)
        else:
            produits = self._retenir_contenus(brut, campagne)

        self.campagnes.enregistrer_contenus(campaign_id, produits)
        return {
            "available": True, "campaign_id": campaign_id, "regenere": True,
            "texte": "\n".join(_contenus_en_clair(produits)),
        }

    def _retenir_contenus(self, brut: Any, campagne: dict) -> dict:
        """Ne garde que les formats qui passent les vérifications.

        UNE PROMESSE OU UN CHIFFRE INVENTÉ FAIT TOUT REJETER, pas seulement le
        format fautif : un modèle qui promet une remise dans le SMS est en mode
        commercial, et rien ne dit que l'e-mail est plus sage — il est
        simplement plus long à relire. Un dépassement de longueur, lui, est
        mécanique et n'engage que son format : celui-là retombe seul sur son
        gabarit.
        """
        if not isinstance(brut, dict):
            return _contenus_gabarit(campagne)

        entier = " ".join(
            str(v) for f in brut.values() if isinstance(f, dict) for v in f.values()
        )
        promesses = promesses_detectees(entier)
        if promesses:
            logger.warning(
                "Déclinaison écartée, promesse commerciale : %s", promesses
            )
            return _contenus_gabarit(campagne)

        autorises = _chiffres_de_la_base(campagne)
        inventes = chiffres_inventes(entier, autorises)
        if inventes:
            logger.warning("Déclinaison écartée, chiffres inventés : %s", inventes)
            return _contenus_gabarit(campagne)

        gabarits = _contenus_gabarit(campagne)
        produits: dict[str, dict] = {}
        for cle, format_ in FORMATS.items():
            propose = brut.get(cle)
            if not isinstance(propose, dict):
                produits[cle] = gabarits[cle]
                continue
            retenu = {c: str(propose.get(c) or "").strip() for c in format_.champs}
            corps = " ".join(retenu.values()).strip()
            if not corps or len(corps) > format_.max_caracteres:
                logger.info(
                    "Format %s écarté (%s caractères pour %s autorisés)",
                    cle, len(corps), format_.max_caracteres,
                )
                produits[cle] = gabarits[cle]
                continue
            produits[cle] = retenu
        return produits

    # -------------------------------------------------------------- Révision

    def reviser(
        self, campaign_id: int, consigne: str, strategie: Optional[str] = None
    ) -> Campagne:
        """Rejoue une campagne sous un autre angle ou un autre ton.

        LES MESURES NE CHANGENT PAS, ET C'EST LA LIMITE À ÉNONCER. « Fais-moi
        une version plus agressive » agit sur le REGISTRE ; « cible plutôt les
        clients à risque de départ » demanderait un segment que cette
        plateforme ne sait pas construire, et la révision le dit au lieu de
        faire semblant. Ce qui est révisable : le ton, l'angle (A/B/C), et donc
        le texte. Ce qui ne l'est pas : la cible, la taille du segment, le
        problème mesuré.

        UNE RÉVISION EST UNE NOUVELLE LIGNE, jamais un écrasement — sans quoi
        l'on ne pourrait plus comparer les deux versions, c'est-à-dire exercer
        le seul jugement pour lequel on a demandé une révision.
        """
        origine = self.campagnes.par_id(campaign_id)
        if origine is None:
            return Campagne(refus=f"Campagne n°{campaign_id} inconnue.")

        campagne = self._depuis_base(origine)
        campagne.parent_id = campaign_id
        campagne.ton = valider_ton(_ton_devine(consigne))

        angle = (strategie or "").strip().upper()
        if angle and angle in STRATEGIES:
            campagne.strategie = angle
            choisi = STRATEGIES[angle]
            objectif = OBJECTIFS.get(choisi.objectif)
            if objectif is not None:
                campagne.objectif = objectif
                campagne.actions = leviers(
                    campagne.cible, campagne.segment, objectif
                )

        accroche, message, redige, appels = self._rediger(
            campagne, dry_run=False, consigne=consigne
        )
        campagne.accroche = accroche
        campagne.message = message
        campagne.redige_par_modele = redige
        campagne.appels_llm = appels
        campagne.nom = campagne.nom or self._nom_par_defaut(campagne)

        campagne.campaign_id = self.campagnes.creer(
            entity_level=campagne.cible.level,
            entity_key=campagne.cible.key,
            entity_label=campagne.cible.label,
            segment=campagne.segment.cle,
            objective=campagne.objectif.cle,
            channel=campagne.canal.cle,
            segment_size=float(campagne.taille_segment),
            window_days=int(origine["window_days"]),
            hook=accroche,
            message=message,
            name=campagne.nom,
            problem=campagne.probleme,
            brief=consigne or None,
            written_by_llm=redige,
            payload=campagne.as_dict(),
            tone=campagne.ton,
            strategy=campagne.strategie,
            parent_id=campaign_id,
        )
        logger.info(
            "Campagne n°%s révisée en n°%s (%s)",
            campaign_id, campagne.campaign_id, campagne.ton,
        )
        return campagne

    def _depuis_base(self, ligne: dict) -> Campagne:
        """Reconstruit une campagne depuis sa ligne, pour la réviser.

        LES MESURES SONT RELUES DU PAYLOAD, jamais recalculées. Une révision
        doit porter sur la MÊME campagne : si les chiffres du jour étaient
        remesurés, « fais-moi une version plus douce » pourrait changer de cible
        au passage, ce que personne n'a demandé.
        """
        payload = ligne.get("payload") or {}
        mesures = payload.get("cible") or {}
        cible = Cible(
            level=ligne["entity_level"],
            key=ligne["entity_key"],
            label=ligne["entity_label"],
            pays=mesures.get("pays"),
            iso2=mesures.get("iso2"),
            avis_clients=int(mesures.get("avis_clients") or 0),
            positifs=int(mesures.get("positifs") or 0),
            negatifs=int(mesures.get("negatifs") or 0),
            part_negatifs=mesures.get("part_negatifs"),
            part_positifs=mesures.get("part_positifs"),
            note_moyenne=mesures.get("note_moyenne"),
            motif=mesures.get("motif_dominant"),
            motif_avis=int(mesures.get("motif_avis") or 0),
        )
        return Campagne(
            cible=cible,
            segment=SEGMENTS.get(ligne["segment"]),
            objectif=OBJECTIFS.get(ligne["objective"]),
            canal=CANAUX.get(ligne["channel"]),
            actions=list(payload.get("actions") or []),
            taille_segment=int(ligne["segment_size"]),
            nom="",
            probleme=ligne.get("problem") or "",
            objectif_mesure=payload.get("objectif_mesure"),
        )

    # --------------------------------------------------------------- Rapport

    def rapport(self, campaign_id: int) -> dict:
        """Bilan d'une campagne : ce que la satisfaction du segment est devenue.

        CE QUE CE RAPPORT MESURE, ET CE QU'IL NE MESURE PAS — la première ligne
        de la réponse le dit, et ce n'est pas une précaution de style. Aucune
        donnée d'envoi, d'ouverture ou de clic n'existe dans cette plateforme :
        rendre un « taux d'ouverture » exigerait de l'inventer. Ce qui est
        mesurable, en revanche, l'est exactement : les avis du segment visé,
        avant et après, comptés par la même requête.

        LE REPÈRE PAYS EST CE QUI REND LE BILAN HONNÊTE. Une part de négatifs qui
        baisse de six points pendant que tout le pays baisse de cinq n'est pas un
        succès de campagne, c'est une marée. Ce n'est pas un contrefactuel — il
        n'y a pas de groupe témoin — mais c'est le meilleur garde-fou disponible
        contre l'attribution automatique.
        """
        campagne = self.campagnes.par_id(campaign_id)
        if campagne is None:
            return {"available": False, "raison": f"Campagne n°{campaign_id} inconnue."}

        # LES DEUX DATES DOIVENT VENIR DE LA MÊME HORLOGE. `date_de_reference`
        # rend un instant UTC ; `date.today()` rend la date LOCALE du serveur.
        # Les soustraire mélange deux référentiels, et pendant la tranche
        # horaire qui les sépare — une heure en UTC+1, davantage ailleurs — une
        # campagne créée à l'instant se lit comme vieille d'un jour. Le garde-fou
        # ci-dessous sautait alors, et le bilan comparait une journée entamée à
        # une journée pleine : exactement ce qu'il existe pour empêcher.
        depuis = self.campagnes.date_de_reference(campagne).date()
        jours = (datetime.now(timezone.utc).date() - depuis).days
        if jours < 1:
            # Rendre un rapport le jour même produirait une comparaison entre une
            # journée entamée et une journée pleine : l'écart mesurerait l'heure
            # qu'il est, pas la campagne.
            return {
                "available": False,
                "raison": "La campagne date d'aujourd'hui : il n'y a pas encore de "
                "période à comparer. Le premier bilan est possible demain.",
            }

        f_apres = self._filtre_rapport(campagne, depuis)
        mesures = self.stats.overview(f_apres)
        apres = mesures.get("current") or {}
        avant = mesures.get("previous") or {}

        objectif = OBJECTIFS.get(campagne["objective"])
        kpi = self._kpi(campagne, f_apres, apres, avant, objectif)
        repere = self._repere_pays(campagne, depuis, objectif)
        verdict = self._verdict(kpi, avant, apres, objectif, repere)

        rapport = {
            "available": True,
            "campaign_id": campaign_id,
            "entite": campagne["entity_label"],
            "segment": campagne["segment"],
            "objectif": campagne["objective"],
            "statut": campagne["status"],
            "depuis": depuis.isoformat(),
            "jours": jours,
            "kpi": kpi,
            "repere_pays": repere,
            "verdict": verdict,
            "avis_apres": int(apres.get("avis_clients") or 0),
            "avis_avant": int(avant.get("avis_clients") or 0),
            "avertissement": (
                "Ce rapport mesure la satisfaction du segment visé, pas la "
                "performance d'une campagne : la plateforme ne collecte aucun "
                "envoi, aucune ouverture et aucun clic."
            ),
        }
        rapport["texte"] = self._rapport_en_clair(rapport, campagne)
        self.campagnes.enregistrer_rapport(campaign_id, rapport)
        return rapport

    @staticmethod
    def _filtre_rapport(campagne: dict, depuis: date) -> StatsFilter:
        """Le périmètre de la campagne, rejoué à l'identique sur la période écoulée."""
        niveau, cle = campagne["entity_level"], campagne["entity_key"]
        commun = {"date_from": depuis}
        if niveau == "subsidiary":
            try:
                return StatsFilter(subsidiaries=(int(cle),), **commun)
            except (TypeError, ValueError):
                return StatsFilter(**commun)
        if niveau == "country":
            return StatsFilter(countries=(str(cle),), **commun)
        if niveau == "operator":
            try:
                return StatsFilter(operators=(int(cle),), **commun)
            except (TypeError, ValueError):
                return StatsFilter(**commun)
        if niveau == "region":
            return StatsFilter(regions=(str(cle),), **commun)
        return StatsFilter(**commun)

    def _kpi(
        self,
        campagne: dict,
        f_apres: StatsFilter,
        apres: dict,
        avant: dict,
        objectif: Optional[Objectif],
    ) -> dict:
        """La mesure suivie par l'objectif, avant et après.

        `part_motif` est calculée ici et non lue dans les agrégats : aucun
        indicateur général ne porte « part d'un motif donné dans les avis
        négatifs ». C'est pourtant le seul KPI qui dise si une campagne de
        réassurance a servi — la part globale de négatifs, elle, bougera surtout
        pour d'autres raisons.
        """
        if objectif is None:
            return {"cle": None, "avant": None, "apres": None, "delta": None}

        if objectif.kpi == "part_motif":
            motif = ((campagne.get("payload") or {}).get("cible") or {}).get(
                "motif_dominant"
            )
            valeur_apres = self._part_motif(f_apres, motif, apres)
            valeur_avant = self._part_motif(
                self._fenetre_precedente(f_apres), motif, avant
            )
        else:
            valeur_apres = _mesure(apres, objectif.kpi)
            valeur_avant = _mesure(avant, objectif.kpi)

        delta = (
            None
            if valeur_apres is None or valeur_avant is None
            else round(valeur_apres - valeur_avant, 1)
        )
        return {
            "cle": objectif.kpi,
            "label": objectif.kpi_label,
            "sens_attendu": objectif.sens,
            "avant": valeur_avant,
            "apres": valeur_apres,
            "delta": delta,
        }

    @staticmethod
    def _fenetre_precedente(f: StatsFilter) -> StatsFilter:
        """La fenêtre de comparaison, en dates explicites.

        `previous_window()` rend un intervalle semi-ouvert `[début, fin[` ; les
        filtres par dates sont, eux, inclusifs des deux côtés. D'où le jour
        retranché : sans lui, la fenêtre « avant » empiéterait d'une journée sur
        la fenêtre « après », et les deux mesures partageraient un avis.
        """
        debut, fin = f.previous_window()
        return StatsFilter(
            date_from=debut,
            date_to=fin - timedelta(days=1),
            countries=f.countries,
            regions=f.regions,
            operators=f.operators,
            subsidiaries=f.subsidiaries,
        )

    def _part_motif(
        self, f: StatsFilter, motif: Optional[str], mesures: dict
    ) -> Optional[float]:
        """Part d'un motif dans les avis négatifs d'une fenêtre, en pourcentage."""
        negatifs = int(mesures.get("negatifs") or 0)
        if not motif or negatifs <= 0:
            return None
        try:
            data = self.stats.themes(
                f, polarity="negative", limit=25, dimension="aspects"
            )
        except Exception:  # noqa: BLE001
            logger.warning("Motifs indisponibles pour le rapport", exc_info=True)
            return None
        for ligne in data.get("terms") or []:
            if ligne.get("term") == motif:
                return round(100.0 * int(ligne.get("avis") or 0) / negatifs, 1)
        return 0.0

    def _repere_pays(
        self, campagne: dict, depuis: date, objectif: Optional[Objectif]
    ) -> Optional[dict]:
        """Évolution du même KPI à l'échelle du pays, sur la même période.

        LE SEUL GARDE-FOU DISPONIBLE CONTRE L'ATTRIBUTION HÂTIVE. Il ne prouve
        rien à lui seul, et le rapport ne lui fait pas dire plus qu'il ne dit :
        il permet seulement de constater qu'un mouvement est propre à la cible,
        ou qu'il est partagé par tout le pays — auquel cas la campagne n'en est
        probablement pas la cause.

        `part_motif` en est exclu : le motif dominant d'une filiale n'a pas de
        raison d'être celui du pays, et comparer deux motifs différents produirait
        un repère qui n'en est pas un.
        """
        iso2 = ((campagne.get("payload") or {}).get("cible") or {}).get("iso2")
        if not iso2 or objectif is None or objectif.kpi == "part_motif":
            return None
        try:
            mesures = self.stats.overview(
                StatsFilter(date_from=depuis, countries=(str(iso2),))
            )
        except Exception:  # noqa: BLE001
            logger.warning("Repère pays indisponible", exc_info=True)
            return None
        apres = _mesure(mesures.get("current") or {}, objectif.kpi)
        avant = _mesure(mesures.get("previous") or {}, objectif.kpi)
        if apres is None or avant is None:
            return None
        return {"pays": iso2, "avant": avant, "apres": apres,
                "delta": round(apres - avant, 1)}

    @staticmethod
    def _verdict(
        kpi: dict,
        avant: dict,
        apres: dict,
        objectif: Optional[Objectif],
        repere: Optional[dict],
    ) -> dict:
        """Conclusion CALCULÉE, jamais rédigée par un modèle.

        C'est le point du projet où une phrase inventée coûterait le plus cher :
        le rapport est ce qui décide si une campagne est reconduite. Une
        conclusion produite par un modèle serait plausible, variable d'un appel à
        l'autre, et impossible à contester en réunion.
        """
        volume_min = min(
            int(avant.get("avis_clients") or 0), int(apres.get("avis_clients") or 0)
        )
        if volume_min < VOLUME_RAPPORT:
            return {
                "conclut": False,
                "texte": (
                    f"Trop peu d'avis pour conclure ({volume_min} sur la plus "
                    f"petite des deux périodes, minimum {VOLUME_RAPPORT})."
                ),
            }
        if objectif is None or kpi.get("delta") is None:
            return {"conclut": False, "texte": "Mesure indisponible sur cette période."}

        delta = float(kpi["delta"])
        progresse = (delta < 0) if objectif.sens == "baisse" else (delta > 0)

        if abs(delta) < MARGE_VERDICT:
            return {
                "conclut": True,
                "atteint": False,
                "texte": (
                    f"{objectif.kpi_label} stable ({delta:+.1f}) : aucun écart "
                    "significatif depuis le lancement."
                ),
            }

        texte = (
            f"{objectif.kpi_label} {'en baisse' if delta < 0 else 'en hausse'} de "
            f"{abs(delta):.1f} — "
            f"{'conforme' if progresse else 'contraire'} à l'objectif."
        )
        if repere and abs(repere["delta"]) >= MARGE_VERDICT and (
            (repere["delta"] < 0) == (delta < 0)
        ):
            texte += (
                f" Attention : le pays évolue dans le même sens "
                f"({repere['delta']:+.1f}), le mouvement n'est donc pas propre à "
                "cette entité."
            )
        return {"conclut": True, "atteint": bool(progresse), "texte": texte}

    @staticmethod
    def _rapport_en_clair(rapport: dict, campagne: dict) -> str:
        kpi = rapport["kpi"]
        lignes = [
            f"Campagne n°{rapport['campaign_id']} — {rapport['entite']}",
            f"Objectif : {campagne['objective']} · statut : {rapport['statut']}",
            f"Période mesurée : depuis le {rapport['depuis']} ({rapport['jours']} j), "
            f"comparée aux {rapport['jours']} jours précédents.",
            f"Avis clients : {rapport['avis_avant']} avant, "
            f"{rapport['avis_apres']} après.",
        ]
        if kpi.get("avant") is not None and kpi.get("apres") is not None:
            lignes.append(
                f"{kpi['label']} : {nombre(kpi['avant'])} → {nombre(kpi['apres'])} "
                f"({kpi['delta']:+.1f})"
            )
        if rapport.get("repere_pays"):
            r = rapport["repere_pays"]
            lignes.append(
                f"Repère {r['pays']} sur la même période : {nombre(r['avant'])} → "
                f"{nombre(r['apres'])} ({r['delta']:+.1f})"
            )
        lignes.append("")
        lignes.append(rapport["verdict"]["texte"])
        lignes.append("")
        lignes.append(rapport["avertissement"])
        return "\n".join(lignes)


_SYSTEME_FORMATS = """\
Tu décline UN MÊME message de campagne dans plusieurs formats, à partir d'un \
dossier déjà décidé par le programme.

Tu réponds UNIQUEMENT par un objet JSON dont les clés sont les formats :
{formats}

Chaque format est un objet portant exactement ses champs.

RÈGLES ABSOLUES — identiques pour tous les formats
- N'ANNONCE AUCUN AVANTAGE COMMERCIAL : ni remise, ni geste, ni offre, ni data \
offerte, ni gratuité, même au conditionnel. Si le dossier contient \
« {emplacement} », RECOPIE-LE tel quel : c'est un humain qui le remplira.
- N'écris aucun nombre qui ne figure pas dans le dossier.
- Ne promets ni délai, ni correctif, ni résultat.
- Respecte scrupuleusement les longueurs : elles sont des contraintes \
techniques, pas des indications.
- Pas d'emoji, pas de majuscules criées, pas de nom de marque inventé.

TON DEMANDÉ : {ton}
"""


def _imposer_perimetre(brief: Brief, filtre: StatsFilter, jours: int) -> Brief:
    """Remplace le PÉRIMÈTRE d'un brief, en gardant tout ce qui est INTENTION.

    La dissymétrie est le point : le périmètre vient des sélecteurs, qui le
    connaissent exactement ; l'objectif, le canal et les dimensions demandées
    viennent du texte libre, que seul un modèle sait lire. Écraser le brief
    entier ferait perdre les secondes — et une demande « pour les jeunes »
    passerait alors sans que rien ne signale que l'âge n'existe pas en base.
    """
    return replace(
        brief,
        jours=jours,
        filtre=filtre,
        portee=f"{_portee_du_filtre(filtre)} · {jours} derniers jours",
        cible_imposee=True,
    )


def _portee_du_filtre(filtre: StatsFilter) -> str:
    """Libellé du périmètre imposé, composé depuis ses identifiants."""
    for valeurs, prefixe in (
        (filtre.subsidiaries, "filiale"),
        (filtre.operators, "opérateur"),
        (filtre.countries, "pays"),
        (filtre.regions, "région"),
    ):
        if valeurs:
            return f"{prefixe} {valeurs[0]}"
    return "tout le périmètre suivi"


def _dossier_depuis_base(campagne: dict, objectif: Optional[Objectif]) -> str:
    """Le dossier transmis au modèle pour la déclinaison, lu depuis la base.

    RECOMPOSÉ DEPUIS LA LIGNE et non depuis un objet en mémoire : la
    déclinaison est demandée longtemps après la proposition, souvent depuis un
    autre processus. Repartir de la base garantit qu'on décline la campagne
    telle qu'elle a été validée, et non telle que les mesures du jour la
    produiraient.
    """
    payload = campagne.get("payload") or {}
    lignes = [
        f"Entité : {campagne['entity_label']}",
        f"Problème constaté : {campagne['problem']}",
        f"Objectif : {objectif.label if objectif else campagne['objective']}",
        f"Ce que l'objectif n'est pas : "
        f"{objectif.exclusion if objectif else 'aucune promesse'}",
        f"Message d'origine à décliner : {campagne['message']}",
        f"Accroche d'origine : {campagne['hook']}",
    ]
    actions = payload.get("actions") or []
    if actions:
        lignes.append(
            "Actions décidées, évocables sans les chiffrer : " + " ; ".join(actions)
        )
    return "\n".join(lignes)


def _chiffres_de_la_base(campagne: dict) -> set[float]:
    """Nombres qu'une déclinaison a le droit d'employer, lus depuis la ligne."""
    mesures = (campagne.get("payload") or {}).get("cible") or {}
    return chiffres_autorises(
        [{**mesures, "taille_segment": campagne.get("segment_size")}],
        (
            "avis_clients", "positifs", "negatifs", "part_negatifs",
            "part_positifs", "note_moyenne", "taille_segment", "motif_avis",
        ),
        avec_rangs=False,
    )


def _couper(texte: str, limite: int) -> str:
    """Tronque SUR UNE FRONTIÈRE DE MOT, avec des points de suspension.

    DÉFAUT CONSTATÉ LE 16 AOÛT 2026, et il ne se voit qu'à l'écran : le SMS de
    repli affichait « …directement dans votre applicati ». Une découpe au
    caractère près respecte la limite technique et produit un texte qu'aucune
    entreprise n'enverrait — or ce repli sert précisément dans les cas visibles,
    quand le modèle est absent ou vient d'être écarté.

    Le repli du repli est la coupe brutale : un « mot » plus long que la limite
    entière (une URL, un identifiant) n'a pas de frontière où couper, et rendre
    une chaîne vide serait pire que la couper.
    """
    texte = (texte or "").strip()
    if len(texte) <= limite:
        return texte
    tronque = texte[: limite - 1]
    espace = tronque.rfind(" ")
    # Le seuil des deux tiers évite de rendre trois mots quand la coupe tombe
    # juste après le premier espace d'une longue chaîne.
    if espace > limite * 2 // 3:
        tronque = tronque[:espace]
    return tronque.rstrip(" ,;:.") + "…"


def _contenus_gabarit(campagne: dict) -> dict[str, dict]:
    """Les cinq formats composés depuis l'accroche et le message enregistrés.

    LE REPLI DOIT TENIR DANS LES LONGUEURS PAR CONSTRUCTION. Un gabarit qui
    déborderait serait inutilisable exactement quand on en a besoin : quand le
    modèle est absent ou vient d'être écarté.
    """
    accroche = (campagne.get("hook") or "").strip()
    message = (campagne.get("message") or "").strip()
    intro = "Bonjour,"
    action_email = "Retrouvez le détail dans votre application."
    return {
        "sms": {"texte": _couper(message, 160)},
        "push": {"titre": _couper(accroche, 40), "texte": _couper(message, 75)},
        "email": {
            "objet": _couper(accroche, 60),
            "introduction": intro,
            # La longueur du format vaut pour l'ENSEMBLE des champs : le corps
            # doit donc laisser la place à l'objet, à l'introduction et à
            # l'appel à l'action, sans quoi la vérification de longueur
            # rejetterait notre propre gabarit.
            "corps": _couper(
                message,
                FORMATS["email"].max_caracteres
                - len(accroche[:60]) - len(intro) - len(action_email) - 4,
            ),
            "appel_action": action_email,
        },
        "reseaux": {"texte": _couper(message, 280)},
        "annonce": {
            "titre": _couper(accroche, 40),
            "description": _couper(message, 90),
            "appel_action": "En savoir plus",
        },
    }


def _contenus_en_clair(contenus: dict) -> list[str]:
    """Les formats mis en page pour Telegram et la CLI."""
    lignes: list[str] = []
    for cle, format_ in FORMATS.items():
        bloc = contenus.get(cle)
        if not isinstance(bloc, dict) or not any(bloc.values()):
            continue
        lignes.append("")
        lignes.append(f"── {format_.label} ──")
        for champ in format_.champs:
            valeur = (bloc.get(champ) or "").strip()
            if not valeur:
                continue
            # Un format à champ unique n'a pas besoin d'être étiqueté : « texte :
            # … » sous « SMS » n'apprend rien et ajoute une ligne à un écran de
            # téléphone.
            if len(format_.champs) == 1:
                lignes.append(valeur)
            else:
                lignes.append(f"{champ.replace('_', ' ').capitalize()} : {valeur}")
    return lignes


#: Mots qui désignent un registre, et le ton qu'ils commandent.
#:
#: RECONNAISSANCE PAR MOTS-CLÉS, SANS MODÈLE, et ce n'est pas une facilité : une
#: révision de ton doit coûter UN appel (la réécriture), pas deux. Faire traduire
#: « plus agressif » par le modèle avant de lui demander de réécrire doublerait
#: le coût de la fonctionnalité la plus utilisée du dispositif.
#:
#: Les préfixes sont volontairement courts pour attraper les variantes :
#: « agressif », « agressive », « agressivement ».
_MOTS_DE_TON: tuple[tuple[str, str], ...] = (
    ("agressi", "commercial"),
    ("vendeur", "commercial"),
    ("commercial", "commercial"),
    ("incitat", "commercial"),
    ("punch", "commercial"),
    ("percutant", "commercial"),
    ("empath", "empathique"),
    ("chaleureux", "empathique"),
    ("humain", "empathique"),
    ("doux", "empathique"),
    ("excuse", "empathique"),
    ("institutionnel", "institutionnel"),
    ("officiel", "institutionnel"),
    ("formel", "institutionnel"),
    ("sobre", "factuel"),
    ("factuel", "factuel"),
    ("neutre", "factuel"),
)


def _ton_devine(consigne: str) -> str:
    """Le registre demandé par une consigne libre, ou le ton par défaut."""
    texte = (consigne or "").lower()
    for motif, ton in _MOTS_DE_TON:
        if motif in texte:
            return ton
    return TON_DEFAUT


def _mesure(bloc: dict, cle: str) -> Optional[float]:
    """Une mesure d'`overview`, convertie. `None` si absente.

    NULL ET ZÉRO NE SE CONFONDENT PAS : `part_negatifs` vaut NULL sur un
    périmètre sans aucun avis client, et le rendre à 0 ferait conclure « 0 % de
    négatifs, objectif atteint » sur une absence totale de données.
    """
    valeur = bloc.get(cle)
    if valeur is None:
        return None
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fabrique
# ---------------------------------------------------------------------------


def build_campaign_agent(db: Database, settings: Settings) -> CampaignAgent:
    """Assemble l'agent avec ses dépendances réelles.

    LES DEUX DÉPENDANCES EXTERNES SONT OPTIONNELLES, et leur absence n'a pas les
    mêmes conséquences — d'où deux messages distincts. Sans clé de modèle,
    l'agent propose des campagnes complètes au gabarit et ne perd que la
    description libre. Sans Telegram, il enregistre ses propositions sans les
    pousser : elles restent lisibles par la CLI, mais personne n'est prévenu.
    """
    client = None
    if settings.llm.enabled and settings.llm.api_key:
        from reviews.llm.client import get_client

        client = get_client(db)
    else:
        logger.info(
            "Assistant de campagne : aucune clé de modèle, les textes seront "
            "des gabarits et la description libre sera refusée."
        )

    notifier = None
    cfg = settings.alerting
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notifier = TelegramNotifier(cfg)
    else:
        logger.info("Assistant de campagne : Telegram non configuré, aucun envoi.")

    stats = StatsRepository(db)
    contexte = CollecteurDeContexte(
        stats=stats,
        marche=MarketRepository(db),
        veille=AgentRepository(db),
    )
    return CampaignAgent(
        db, settings, client=client, stats=stats, notifier=notifier,
        contexte=contexte,
    )
