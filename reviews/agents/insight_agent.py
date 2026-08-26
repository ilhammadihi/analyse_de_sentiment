"""
Agent 1 — veille de satisfaction : décide ce qui mérite d'être signalé, et le dit.

CE QU'IL AJOUTE À CE QUI EXISTAIT DÉJÀ
    `/insights/diagnose`, `/stats/movers` et l'alerting répondent quand on les
    interroge. Cet agent ne répond pas : il PARLE, une fois par jour, sans
    qu'un humain ouvre un écran. Trois briques rendent cela supportable :

      1. l'ARBITRAGE (`arbitrage.py`) — sur 14 pics critiques, lesquels valent
         une notification ? Calculé, jamais demandé au modèle.
      2. la MÉMOIRE (`agent_reports`) — ne pas redire trois jours de suite ce
         qui n'a pas bougé, mais toujours redire ce qui a empiré.
      3. la RETENUE — trois sujets au maximum. Un briefing de dix sujets n'est
         pas lu, et un briefing qu'on ne lit pas est pire qu'aucun briefing :
         il donne le sentiment d'être couvert.

L'ORDRE DES ÉTAPES EST LA GARANTIE
    candidats → arbitrage → mémoire → rédaction. Le modèle n'entre qu'en
    quatrième position, sur des sujets déjà choisis et des chiffres déjà
    mesurés. Inverser rédaction et arbitrage — demander au LLM de choisir puis
    de rédiger — produirait un classement qui change à chaque appel pour les
    mêmes données. C'est la même règle que celle qui régit `llm/insights.py`.

CE QU'IL NE FAIT PAS
    Il ne collecte pas, ne recalcule aucun agrégat, n'écrit dans aucune table
    d'avis. Il lit des mesures déjà produites, les trie, et consigne ce qu'il a
    dit. Une panne de cet agent ne peut donc pas abîmer les données.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from reviews.agents.arbitrage import Candidat, arbitrer, retenus
from reviews.agents.chiffres import nombre
from reviews.alerting.notifiers import TelegramNotifier
from reviews.config import Settings
from reviews.domain.aspects import label as aspect_label
from reviews.llm.briefing import BriefingService
from reviews.storage.agent_repository import AgentRepository, should_report
from reviews.storage.db import Database
from reviews.storage.filters import StatsFilter
from reviews.storage.market_repository import MarketRepository
from reviews.storage.repository import AlertRepository
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Nom sous lequel l'agent signe ses signalements dans le journal.
AGENT = "insight"

#: Sujets remontés au maximum par passage. Voir la RETENUE ci-dessus.
MAX_SUJETS = 3

#: Jours pendant lesquels on ne redit pas la même chose sur la même entité.
COOLDOWN_JOURS = 3

#: Hausse de note à partir de laquelle on reparle malgré le refroidissement.
#:
#: Exprimée dans l'échelle de l'arbitre, où l'ampleur pèse 1 point par point de
#: part de négatifs : dix ici valent donc « dix points de négatifs de plus
#: qu'au dernier signalement », un écart qu'un responsable veut connaître même
#: s'il a déjà été prévenu avant-hier.
AGGRAVATION_POINTS = 10.0

#: Fenêtre d'analyse du passage : DEUX SEMAINES, comparées aux deux semaines
#: précédentes.
#:
#: RAMENÉE DE QUATRE-VINGT-DIX JOURS À QUATORZE, et ce n'est pas un réglage
#: cosmétique. Un briefing quotidien qui annonce « contre trois mois plus tôt »
#: parle d'un mouvement dont l'essentiel est déjà ancien : le lecteur ne peut
#: plus agir dessus, et la moitié du corpus comparé date d'avant les décisions
#: qu'il a lui-même prises depuis. Deux semaines, c'est l'horizon sur lequel
#: quelque chose reste réparable.
#:
#: Le prix est mesuré : sur 14 jours, seules 3 filiales franchissent les
#: seuils, contre 6 sur 90 jours. Un agent qui parle moins souvent mais de
#: choses actionnables vaut mieux que l'inverse.
FENETRE_JOURS = 14

#: Fenêtre de comptage des alertes critiques, pour le critère de persistance.
#: Alignée sur la borne de fraîcheur du fil d'alertes du dashboard.
FENETRE_ALERTES_JOURS = 7

#: Variation à partir de laquelle une filiale compte comme « voisine dégradée »
#: pour le critère d'étendue. Plus bas que le plancher d'ampleur : on cherche
#: ici un faisceau, pas un second incident majeur.
VOISIN_SEUIL_POINTS = 5.0

#: Indicateurs de marché repris dans le briefing, dans cet ordre.
#:
#: TROIS, PAS NEUF. Le pays en porte neuf, mais un briefing est lu sur un
#: téléphone : au-delà de trois chiffres de contexte, la ligne devient un
#: tableau et le lecteur saute le paragraphe entier — y compris la partie
#: satisfaction qui, elle, appelait une action.
#:
#: Ceux-ci répondent à la question que pose un recul de satisfaction : le
#: réseau suit-il, et les gens consomment-ils plus ? La clé est
#: « indicateur|unité » car l'unité fait partie de l'identité de la mesure.
#: L'unité est portée par le SUFFIXE et non par la table `UNITES` du
#: collecteur : « couverture 4G 91,2 % de la population » serait exact mais
#: illisible dans une ligne qui en aligne trois. Le suffixe court dit la même
#: chose — et sans lui, « trafic data 5,9 » ne veut rien dire du tout.
_MARCHE_AFFICHE = (
    ("MOB_COV_4G|PT_POP", "couverture 4G", " %"),
    ("IT_BB_MOB_TRF|XB_Y", "trafic data", " Go/mois"),
    ("IT_CEL_SETS|SB_10P2_HB", "abonnements pour 100 hab.", ""),
)


@dataclass
class Redaction:
    """Un signalement décomposé en trois blocs, plutôt qu'un texte plat.

    ADAPTATION MÉTIER (17 août 2026). Le format Telegram demandé sépare
    visuellement CE QUI SE PASSE (mesuré, jamais rédigé), POURQUOI (la synthèse
    du modèle, ou son repli déterministe) et QUOI FAIRE (une recommandation, ou
    rien). `_rediger` calcule cette décomposition UNE SEULE FOIS ; `.texte()`
    l'aplatit pour le journal et la CLI, `_envoyer` la met en forme pour
    Telegram — sans jamais rappeler le modèle une seconde fois pour la même
    filiale.
    """

    #: Le fait mesuré, jamais soumis au modèle. Peut tenir sur plusieurs
    #: lignes (le taux, puis le motif dominant).
    signal: str

    #: La synthèse — cause probable rédigée par le modèle, ou à défaut les
    #: signaux d'étendue déterministes (alertes, voisinage, marché). None
    #: seulement si aucun des deux n'a produit de matière.
    insight: Optional[str] = None

    #: UNE recommandation, jamais deux : ce canal se lit d'un regard. Absente
    #: si le modèle n'a rien recommandé, ou si la filiale est jugée non fiable
    #: (voir le garde-fou de l'Agent 3 dans `_rediger`).
    action: Optional[str] = None

    #: Réserve de qualité des données (`GardeQualite`), quand applicable.
    reserve: Optional[str] = None

    def texte(self) -> str:
        """Forme plate : journal (`agent_reports.text`), CLI, `passage.signales`."""
        morceaux = [self.signal]
        if self.insight:
            morceaux.append(self.insight)
        if self.action:
            morceaux.append(f"À faire : {self.action}")
        if self.reserve:
            morceaux.append(self.reserve)
        return "\n".join(morceaux)


@dataclass
class Passage:
    """Résultat d'un passage de l'agent, pour la CLI et les tests."""

    candidats: list[Candidat] = field(default_factory=list)
    signales: list[dict] = field(default_factory=list)
    tus: list[dict] = field(default_factory=list)
    envoye: bool = False
    raison_silence: Optional[str] = None

    def resume(self) -> str:
        return (
            f"{len(self.candidats)} candidat(s) · {len(self.signales)} signalé(s) · "
            f"{len(self.tus)} tu(s)"
        )


class InsightAgent:
    """Veille quotidienne : arbitre, se souvient, fait rédiger, envoie."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        briefing: Optional[BriefingService] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.db = db
        self.settings = settings
        self.stats = StatsRepository(db)
        self.alerts = AlertRepository(db)
        self.journal = AgentRepository(db)
        self.market = MarketRepository(db)
        self.briefing = briefing
        self.notifier = notifier
        #: Garde-fou de l'Agent 3, consulté avant de rédiger.
        #:
        #: DÉPENDANCE PYTHON DIRECTE, pas un appel à `/quality/trust` : les deux
        #: agents tournent dans le même processus, et l'API est un conteneur
        #: SÉPARÉ du worker — un briefing dépendrait alors d'un service qui peut
        #: être arrêté pendant que la veille tourne.
        #:
        #: Construit ici et non passé en argument pour ne pas modifier la
        #: signature : tous les appelants existants continuent de fonctionner.
        from reviews.agents.quality.garde import construire_garde

        self.garde = construire_garde(db, settings)

    # ------------------------------------------------------------------ Public

    def run(self, dry_run: bool = False) -> Passage:
        """Un passage complet. Ne lève jamais : un agent muet vaut mieux qu'un crash.

        `dry_run` fait tout sauf appeler le modèle, envoyer et journaliser. Il
        sert à voir ce que l'agent AURAIT dit — indispensable pour régler les
        seuils sans consommer de quota ni réveiller le groupe Telegram.
        """
        passage = Passage()
        try:
            passage.candidats = arbitrer(self._candidats())
        except Exception:  # noqa: BLE001
            logger.exception("Agent d'insight : collecte des candidats en échec")
            passage.raison_silence = "collecte des candidats en échec"
            return passage

        selection = retenus(passage.candidats, MAX_SUJETS)
        if not selection:
            passage.raison_silence = "aucun mouvement ne franchit les seuils"
            logger.info("Agent d'insight : rien à signaler (%s)", passage.resume())
            return passage

        a_dire: list[tuple[Candidat, Redaction]] = []
        for candidat in selection:
            dernier = self.journal.last_report(AGENT, candidat.level, candidat.key)
            parler, pourquoi = should_report(
                dernier,
                candidat.score,
                cooldown_days=COOLDOWN_JOURS,
                aggravation_points=AGGRAVATION_POINTS,
            )
            if not parler:
                passage.tus.append({"entite": candidat.label, "raison": pourquoi})
                continue
            redaction = self._rediger(candidat, dry_run=dry_run)
            a_dire.append((candidat, redaction))
            passage.signales.append(
                {"entite": candidat.label, "raison": pourquoi,
                 "texte": redaction.texte(), "score": round(candidat.score, 1)}
            )

        if not a_dire:
            passage.raison_silence = "tout était déjà signalé et rien n'a empiré"
            logger.info("Agent d'insight : silence volontaire (%s)", passage.resume())
            return passage

        if dry_run:
            return passage

        # ENVOI PAR FILIALE, ET NON UN DIGEST BUNDLÉ (17 août 2026) : `_envoyer`
        # rend qui a réellement été acheminé, filiale par filiale. Un digest
        # unique n'aurait pu porter qu'un seul booléen pour trois envois
        # distincts — imprécis dès que l'un des trois échoue et pas les autres.
        acheminees = self._envoyer(a_dire)
        passage.envoye = any(acheminees.values())
        for candidat, redaction in a_dire:
            delivree = acheminees.get(candidat.key, False)
            report_id = self.journal.record(
                agent=AGENT,
                entity_level=candidat.level,
                entity_key=candidat.key,
                entity_label=candidat.label,
                score=candidat.score,
                text=redaction.texte(),
                payload=candidat.as_dict(),
                delivered=delivree,
            )
            logger.info(
                "Agent d'insight : %s signalé (score %.1f, journal #%s)",
                candidat.label, candidat.score, report_id,
            )
        return passage

    # -------------------------------------------------------------- Candidats

    def _candidats(self) -> list[Candidat]:
        """Rassemble les mouvements à arbitrer et leurs signaux d'appui.

        La source première est `movers` — la question « qu'est-ce qui a
        changé ? ». Les alertes n'ajoutent pas de candidats : elles ENRICHISSENT
        ceux qui existent, par le critère de persistance. Une alerte sans
        mouvement mesurable sur 90 jours est un pic trop court pour un briefing
        quotidien, et le fil d'alertes du dashboard le porte déjà.
        """
        f = StatsFilter(days=FENETRE_JOURS)
        data = self.stats.movers(f=f, level="subsidiary", limit=25, min_reviews=15)
        degradees = data.get("degraded") or []

        alertes = self._alertes_par_entite()
        voisins = self._degradees_par_pays(degradees)

        candidats: list[Candidat] = []
        for m in degradees:
            pays = m.get("country")
            label = m.get("label") or "?"
            candidats.append(
                Candidat(
                    level="subsidiary",
                    key=str(m.get("key")),
                    label=label,
                    pays=pays,
                    iso2=m.get("iso2"),
                    delta_negatifs=float(m.get("delta_negatifs") or 0.0),
                    # CONVERSION OBLIGATOIRE, pas une précaution de style :
                    # PostgreSQL rend les pourcentages en `Decimal`, et
                    # `Decimal - float` lève. Le champ est déclaré `float` sur
                    # le candidat ; le respecter ici évite que chaque calcul en
                    # aval ait à s'en méfier.
                    part_negatifs=(
                        None if m.get("part_negatifs") is None
                        else float(m["part_negatifs"])
                    ),
                    avis_clients=int(m.get("avis_clients") or 0),
                    avis_clients_avant=int(m.get("avis_clients_avant") or 0),
                    alertes_recentes=alertes.get(label, 0),
                    # L'entité elle-même est retirée de son propre voisinage :
                    # sans cela, toute filiale dégradée se verrait attribuer un
                    # bonus d'étendue pour sa seule présence.
                    voisins_degrades=max(0, voisins.get(pays, 0) - 1) if pays else 0,
                )
            )
        return candidats

    def _alertes_par_entite(self) -> dict[str, int]:
        """Nombre d'alertes critiques récentes, par nom de filiale."""
        lignes = self.alerts.list_recent(
            limit=200, severity="error", kind="business",
            max_age_days=FENETRE_ALERTES_JOURS,
        )
        compte: dict[str, int] = {}
        for a in lignes:
            nom = a.get("subsidiary") or a.get("company")
            if nom:
                compte[nom] = compte.get(nom, 0) + 1
        return compte

    @staticmethod
    def _degradees_par_pays(degradees: list[dict]) -> dict[str, int]:
        """Combien de filiales se dégradent notablement, par pays."""
        compte: dict[str, int] = {}
        for m in degradees:
            pays = m.get("country")
            if pays and float(m.get("delta_negatifs") or 0.0) >= VOISIN_SEUIL_POINTS:
                compte[pays] = compte.get(pays, 0) + 1
        return compte

    # -------------------------------------------------------------- Rédaction

    def _rediger(self, candidat: Candidat, dry_run: bool) -> Redaction:
        """Calcule les trois blocs du signalement. UN SEUL appel au modèle.

        REPLI ASSUMÉ SUR L'INSIGHT DÉTERMINISTE. Si le modèle est indisponible
        — pas de clé, quota épuisé, panne du fournisseur — l'agent ne se tait
        pas : le bloc Insight prend alors les signaux d'étendue déjà calculés
        (persistance, voisinage, marché), et le bloc Action reste vide plutôt
        que d'inventer une recommandation sans diagnostic. Un signal sans style
        vaut infiniment mieux qu'un silence dont personne ne saura qu'il est dû
        au fournisseur.
        """
        signal = self._signal(candidat)

        # LA QUALITÉ DES DONNÉES EST CONSULTÉE AVANT LA RÉDACTION, pas après.
        #
        # Sur une filiale jugée non fiable, il ne s'agit pas d'ajouter un
        # avertissement à une recommandation : il s'agit de ne pas en formuler.
        # Ni Insight ni Action ne sont produits — seul le Signal mesuré
        # subsiste, avec la réserve. Une phrase « action recommandée :
        # relancer une campagne » sous un taux calculé sur quatre avis envoie
        # travailler sur du bruit, et la réserve en dessous ne rattrape rien —
        # c'est l'action que le lecteur retient.
        #
        # `verdict` rend INDETERMINE, donc fiable, si le garde-fou est
        # désactivé ou si la filiale n'a jamais été évaluée : le comportement
        # d'avant cette intégration est alors conservé à l'identique.
        verdict = self.garde.verdict(candidat.key)
        if not verdict.fiable:
            return Redaction(signal=signal, reserve=verdict.mention)

        repli = Redaction(
            signal=signal, insight=self._insight_repli(candidat),
            reserve=verdict.mention,
        )
        if dry_run or self.briefing is None:
            return repli

        try:
            f = StatsFilter(days=FENETRE_JOURS, subsidiaries=(int(candidat.key),))
        except (TypeError, ValueError):
            return repli

        try:
            resultat = self.briefing.diagnose(f)
        except Exception:  # noqa: BLE001
            logger.warning("Diagnostic indisponible pour %s", candidat.label, exc_info=True)
            return repli

        if not resultat or not resultat.get("available"):
            return repli

        # CONTRAT DE `diagnose`, vérifié et non supposé : la cause est rendue
        # dans `text`, et les champs structurés — dont les recommandations —
        # vivent sous `payload["_reponse"]`. Lire `resultat["cause_probable"]`
        # ne lève pas : cela rend None, et l'agent retombait silencieusement sur
        # le repli déterministe en donnant l'impression que le modèle était
        # absent.
        cause = (resultat.get("text") or "").strip()
        if not cause:
            return repli

        structure = (resultat.get("payload") or {}).get("_reponse") or {}
        recos = [r for r in (structure.get("recommandations") or []) if r]
        # UNE SEULE action, et non deux comme dans l'ancien format joint : le
        # nouveau canal se lit d'un regard, la seconde recommandation n'y a
        # pas sa place. Le dossier complet (dashboard, onglet Comparer) porte
        # toujours la liste entière.
        action = recos[0] if recos else None

        return Redaction(signal=signal, insight=cause, action=action, reserve=verdict.mention)

    def _contexte_marche(self, candidat: Candidat) -> Optional[str]:
        """Une ligne de contexte marché, ou None si le pays n'en a pas.

        CE QUE CETTE LIGNE CHANGE, ET POURQUOI ELLE VAUT LE DÉTOUR
            Jusqu'ici le corpus ne contenait que de l'opinion. « La
            satisfaction recule de 20 points » ne disait pas si le réseau y
            était pour quelque chose. Avec la couverture 4G et le trafic data
            du pays, le lecteur tranche lui-même : un recul dans un pays couvert
            à 99,8 % n'a pas la même cause qu'un recul à 60 %.

        LES CHIFFRES SONT CALCULÉS, JAMAIS RÉDIGÉS PAR LE MODÈLE — même règle
        que partout. La variation vient de SQL, la phrase est un gabarit.

        MAILLE PAYS, ET IL FAUT QUE LA PHRASE LE DISE. Ces indicateurs ne
        descendent pas à l'opérateur : écrire « couverture 4G : 84,6 % » sous le
        nom d'une filiale laisserait croire que c'est SA couverture. Le nom du
        pays est donc systématiquement rappelé.
        """
        if not candidat.iso2:
            return None
        try:
            derniers = self.market.latest(candidat.iso2)
        except Exception:  # noqa: BLE001
            logger.warning("Contexte marché illisible pour %s", candidat.iso2, exc_info=True)
            return None
        if not derniers:
            return None

        morceaux: list[str] = []
        annees: list[int] = []
        for cle, libelle_court, suffixe in _MARCHE_AFFICHE:
            mesure = derniers.get(cle)
            if mesure is None:
                continue
            valeur = mesure["value"]
            texte = f"{libelle_court} {valeur:,.1f}{suffixe}".replace(",", " ")
            variation = mesure.get("variation_pct")
            if variation is not None:
                texte += f" ({variation:+.1f} %)"
            morceaux.append(texte)
            annees.append(int(mesure["year"]))

        if not morceaux:
            return None

        # L'ANNÉE EST CELLE DES INDICATEURS AFFICHÉS, jamais le maximum du
        # pays. Depuis l'ajout des prix — renseignés jusqu'en 2025 quand la
        # couverture s'arrête à 2024 —, prendre le maximum global datait de
        # 2025 une couverture 4G mesurée en 2024. Un chiffre juste sous une
        # année fausse est pire qu'un chiffre absent : il est invérifiable.
        #
        # Quand les indicateurs affichés ne partagent pas la même année, on
        # rend l'intervalle plutôt que d'en élire un au hasard.
        plus_ancienne, plus_recente = min(annees), max(annees)
        periode = (
            str(plus_recente)
            if plus_ancienne == plus_recente
            else f"{plus_ancienne}–{plus_recente}"
        )
        pays = candidat.pays or candidat.iso2
        return f"Contexte {pays} ({periode}) : " + " ; ".join(morceaux) + "."

    def _motifs_dominants(
        self, candidat: Candidat, limite: int = 3
    ) -> list[tuple[str, int]]:
        """De quoi les clients se plaignent, en clair — avec leur volume.

        C'ÉTAIT LE MANQUE LE PLUS COÛTEUX DU BRIEFING. « La satisfaction recule
        de 30 points » dit qu'il faut regarder ; « les plaintes portent sur la
        facturation » dit QUI doit regarder. Sans motif, le lecteur devait
        ouvrir le dashboard pour savoir s'il s'agissait du réseau, d'une
        application ou d'un litige — c'est-à-dire faire le travail que le
        briefing existe pour lui épargner.

        LE VOLUME ACCOMPAGNE DÉSORMAIS LE LIBELLÉ (17 août 2026) : le nouveau
        format Telegram dit « le principal problème concerne X (27 avis) »,
        et 27 est une mesure, jamais une estimation — elle doit donc venir
        d'ici, au même titre que le libellé, plutôt que d'être recomptée
        ailleurs au risque de diverger.

        Repli sur les mots du lexique quand l'analyse sémantique n'est pas
        encore passée : « can't, bad, useless » vaut mieux que rien.
        """
        try:
            f = StatsFilter(days=FENETRE_JOURS, subsidiaries=(int(candidat.key),))
        except (TypeError, ValueError):
            return []

        for dimension in ("aspects", "terms"):
            try:
                data = self.stats.themes(
                    f, polarity="negative", limit=limite, dimension=dimension
                )
            except Exception:  # noqa: BLE001
                logger.warning("Motifs indisponibles pour %s", candidat.label, exc_info=True)
                return []
            lignes = data.get("terms") or []
            if lignes:
                return [
                    (
                        aspect_label(r["term"]) if dimension == "aspects" else r["term"],
                        int(r.get("avis") or 0),
                    )
                    for r in lignes
                ]
        return []

    def _signal(self, candidat: Candidat) -> str:
        """Le fait mesuré, jamais soumis au modèle : un taux précis, et le
        motif dominant avec son volume.

        POURQUOI LE POURCENTAGE EXACT REMPLACE ICI LE « X SUR 10 » — décision
        du métier (17 août 2026), explicite : le canal Telegram veut désormais
        la valeur précise, pas la simplification par dixième. `nombre()`
        (chiffres.py) fait la conversion Decimal→français une fois pour tout
        le projet plutôt que de la répéter ici.
        """
        if candidat.part_negatifs is not None:
            ligne = (
                f"{nombre(candidat.part_negatifs, 1)} % des avis sont négatifs."
            )
        else:
            ligne = (
                f"La part d'avis négatifs progresse de "
                f"{nombre(candidat.delta_negatifs, 0)} points sur "
                f"{candidat.avis_clients} avis."
            )

        # LE MOTIF DOMINANT, UN SEUL — pas les trois de l'ancien format joint :
        # le Signal se lit d'un regard, la nuance entre motifs secondaires
        # appartient au dashboard.
        motifs = self._motifs_dominants(candidat, limite=1)
        if motifs:
            libelle, volume = motifs[0]
            ligne += f"\nLe principal problème concerne {libelle} ({volume} avis)."
        return ligne

    def _insight_repli(self, candidat: Candidat) -> Optional[str]:
        """Ce que l'agent sait dire sur l'ÉTENDUE du phénomène sans modèle.

        SERT DE BLOC INSIGHT QUAND LE MODÈLE EST INDISPONIBLE — jamais un bloc
        vide qui laisserait le lecteur sans la moindre piste. Persistance
        (alertes récentes), extension géographique (voisins dégradés) et
        contexte réseau/marché : trois signaux déjà calculés, jamais rédigés
        par le modèle, qui font une synthèse défendable même sans lui.
        """
        lignes: list[str] = []
        if candidat.alertes_recentes >= 2:
            lignes.append(
                f"Ce n'est pas un accident isolé : {candidat.alertes_recentes} alertes "
                f"critiques ces {FENETRE_ALERTES_JOURS} derniers jours."
            )
        if candidat.voisins_degrades >= 1 and candidat.pays:
            autres = candidat.voisins_degrades
            lignes.append(
                f"{autres} autre{'s' if autres > 1 else ''} filiale"
                f"{'s' if autres > 1 else ''} de {candidat.pays} se dégrade"
                f"{'nt' if autres > 1 else ''} en même temps — la cause est "
                "peut-être nationale."
            )
        marche = self._contexte_marche(candidat)
        if marche:
            lignes.append(marche)
        return "\n".join(lignes) if lignes else None

    # ----------------------------------------------------------------- Envoi

    def _envoyer(self, a_dire: list[tuple[Candidat, Redaction]]) -> dict[str, bool]:
        """Une notification PAR FILIALE, jamais un digest bundlé.

        ADAPTATION MÉTIER (17 août 2026). Le format précédent regroupait
        jusqu'à trois filiales sous un seul « 📊 Veille satisfaction » : la
        lecture demandait de dérouler tout le message pour trouver celle qui
        intéresse. Le nouveau format donne à chaque filiale son propre
        message, structuré en trois blocs qui se lisent d'un regard — Signal,
        Insight, Action — et rien de plus : la richesse contextuelle
        (alertes, voisinage, marché) sert d'Insight de repli quand le modèle
        est indisponible, mais ne s'ajoute pas EN PLUS de sa synthèse.

        Rend le sort de CHAQUE candidat, et non un booléen global : `run()` en
        a besoin pour journaliser `delivered` avec exactitude — un envoi
        réussi pour deux filiales sur trois ne doit pas marquer la troisième
        comme acheminée.
        """
        if self.notifier is None:
            logger.info("Agent d'insight : aucun canal configuré, briefing non envoyé")
            return {candidat.key: False for candidat, _ in a_dire}

        echapper = TelegramNotifier._echapper
        resultats: dict[str, bool] = {}
        for candidat, redaction in a_dire:
            lignes = [f"📊 <b>{echapper(candidat.label)}</b>", ""]

            signal_lignes = redaction.signal.split("\n")
            lignes.append(f"⚠️ <b>Signal :</b> {echapper(signal_lignes[0])}")
            lignes.extend(echapper(l) for l in signal_lignes[1:])

            if redaction.insight:
                lignes += ["", "💡 <b>Insight :</b>", echapper(redaction.insight)]
            if redaction.action:
                lignes += [
                    "", "➡️ <b>Action recommandée :</b>", echapper(redaction.action),
                ]
            if redaction.reserve:
                lignes += ["", echapper(redaction.reserve)]

            try:
                resultats[candidat.key] = self.notifier.send_text("\n".join(lignes))
            except Exception:  # noqa: BLE001
                # Un message non acheminé ne doit jamais faire tomber les
                # suivants : c'est la même règle que partout ailleurs dans ce
                # projet — un canal injoignable renvoie False, il n'interrompt
                # rien.
                logger.warning(
                    "Signalement non acheminé pour %s", candidat.label, exc_info=True
                )
                resultats[candidat.key] = False
        return resultats


def build_agent(db: Database, settings: Settings) -> InsightAgent:
    """Assemble l'agent avec ses dépendances réelles.

    Les deux dépendances externes sont OPTIONNELLES et le restent : sans clé de
    modèle, l'agent envoie des briefings factuels ; sans jeton Telegram, il
    journalise sans envoyer. Aucune des deux absences n'empêche le passage —
    exiger l'une ou l'autre transformerait une configuration incomplète en
    panne silencieuse de la veille.
    """
    briefing = None
    if settings.llm.enabled and settings.llm.api_key:
        from reviews.llm.briefing import BriefingService
        from reviews.llm.client import get_client
        from reviews.storage.briefing_repository import BriefingRepository

        briefing = BriefingService(
            db, BriefingRepository(db), StatsRepository(db), get_client(db)
        )
    else:
        logger.info(
            "Agent d'insight : aucune clé de modèle, les briefings seront factuels"
        )

    notifier = None
    cfg = settings.alerting
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notifier = TelegramNotifier(cfg)
    else:
        logger.info("Agent d'insight : Telegram non configuré, aucun envoi")

    return InsightAgent(db, settings, briefing=briefing, notifier=notifier)
