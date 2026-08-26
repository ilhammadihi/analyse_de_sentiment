"""
Dossier de campagne — le document en treize sections, tel que demandé.

CE QUE CE MODULE EST, ET CE QU'IL N'EST PAS
    Il ne DÉCIDE rien et ne MESURE rien de neuf. Tout ce qu'il rend a été
    calculé, arbitré et figé au moment de la proposition (`campaigns.payload`,
    migration 016). Ce module ne fait que RANGER ces éléments dans les treize
    sections attendues, et les mettre en Markdown.

    C'est une séparation voulue, et pas une préférence de style : un rapport qui
    recalculerait ses chiffres afficherait, six semaines après, des mesures
    différentes de celles qui ont justifié la campagne. La décision et le
    document qui la défend doivent reposer sur les mêmes nombres.

AUCUN APPEL DE MODÈLE, VOLONTAIREMENT
    Comme `CampaignAgent.fiche()`, dont ce module est le prolongement structuré.
    Deux exécutions rendent exactement le même dossier, et il reste produisible
    le jour où le fournisseur est indisponible ou le quota épuisé. Les phrases
    rédigées par le modèle — nom, accroche, message, contenus — sont RELUES
    depuis la base, jamais redemandées.

LA SEULE LECTURE FAITE ICI, ET POURQUOI ELLE EST UNE EXCEPTION
    La section « Data Evidence » va rechercher les identifiants d'avis du
    segment. Elle est la seule à interroger la base au moment du rendu, parce
    qu'une liste d'identifiants figée serait périmée dès la collecte suivante et
    n'aurait plus permis de remonter aux avis. La requête reprend EXACTEMENT les
    critères stockés — entité, fenêtre, polarité, motif — de sorte que ce qu'elle
    ramène soit bien le segment décrit plus haut, et non un échantillon voisin.

CE QUI N'EST JAMAIS ÉCRIT ICI
    Aucune projection chiffrée. « Expected Impact » énonce ce qui sera MESURÉ et
    contre quel repère, avec la valeur de départ ; il ne promet pas de points de
    satisfaction. La plateforme ne collecte ni envoi, ni ouverture, ni clic : un
    impact prévu serait un nombre inventé, indétectable dans un document par
    ailleurs exact — donc la façon la plus sûre de discréditer l'ensemble.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from reviews.domain.aspects import label as aspect_label
from reviews.domain.marketing import (
    CANAUX,
    OBJECTIFS,
    SEGMENTS,
    STRATEGIES,
)
from reviews.storage.campaign_repository import CampaignRepository
from reviews.storage.filters import StatsFilter
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Identifiants d'avis rapportés en preuve.
#:
#: Assez pour vérifier par sondage, pas assez pour transformer le dossier en
#: export de base. Qui veut la liste complète a le filtre : la section rend les
#: critères exacts qui la reproduisent.
MAX_PREUVES = 12

#: Titres officiels des sections, dans l'ordre imposé. En anglais parce que
#: c'est ainsi que le livrable a été spécifié ; le CONTENU reste en français,
#: comme le reste de ce que lit l'équipe.
TITRES: tuple[tuple[str, str], ...] = (
    ("executive_summary", "Executive Summary"),
    ("context", "Context"),
    ("customer_insights", "Customer Insights"),
    ("main_problems", "Main Problems"),
    ("target_segment", "Target Segment"),
    ("marketing_objective", "Marketing Objective"),
    ("campaign_strategy", "Campaign Strategy"),
    ("campaign_content", "Campaign Content"),
    ("recommended_channels", "Recommended Channels"),
    ("kpis", "KPIs"),
    ("expected_impact", "Expected Impact"),
    ("data_evidence", "Data Evidence"),
    ("limitations", "Limitations"),
)


@dataclass
class Section:
    """Une section du dossier : un titre, des lignes, parfois des couples clé/valeur.

    `champs` et `lignes` sont séparés parce qu'ils s'affichent différemment —
    une définition en deux colonnes, une énumération en liste — et que le
    dashboard doit pouvoir les distinguer sans découper du texte.
    """

    cle: str
    numero: int
    titre: str
    champs: list[tuple[str, str]] = field(default_factory=list)
    lignes: list[str] = field(default_factory=list)
    texte: str = ""

    def vide(self) -> bool:
        return not (self.champs or self.lignes or self.texte)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cle": self.cle,
            "numero": self.numero,
            "titre": self.titre,
            "champs": [{"label": k, "valeur": v} for k, v in self.champs],
            "lignes": list(self.lignes),
            "texte": self.texte,
        }

    def markdown(self) -> str:
        out = [f"## {self.numero}. {self.titre}", ""]
        if self.texte:
            out += [self.texte, ""]
        if self.champs:
            out += [f"- **{k}** : {v}" for k, v in self.champs] + [""]
        if self.lignes:
            out += [f"- {ligne}" for ligne in self.lignes] + [""]
        return "\n".join(out)


class DossierDeCampagne:
    """Compose le dossier en treize sections d'une campagne enregistrée."""

    def __init__(
        self,
        campagnes: CampaignRepository,
        stats: Optional[StatsRepository] = None,
    ):
        self.campagnes = campagnes
        #: Facultatif : sans lui, le dossier reste complet mais la section
        #: « Data Evidence » ne peut pas citer d'identifiants d'avis. Elle le
        #: dit alors, plutôt que de laisser croire qu'il n'y en avait aucun.
        self.stats = stats

    # ----------------------------------------------------------------- Public

    def composer(self, campaign_id: int) -> dict[str, Any]:
        """Rend le dossier complet. Ne lève jamais.

        Une campagne inconnue est une réponse (`available: false`), pas une
        panne : l'appelant affiche la raison. Un 500 enverrait chercher un
        incident là où il n'y a qu'un identifiant erroné.
        """
        try:
            campagne = self.campagnes.par_id(campaign_id)
        except Exception:  # noqa: BLE001
            logger.exception("Lecture de la campagne %s en échec", campaign_id)
            return {
                "available": False,
                "raison": "La campagne n'a pas pu être lue. Réessayez dans un instant.",
            }

        if campagne is None:
            return {
                "available": False,
                "raison": f"Campagne n°{campaign_id} inconnue.",
            }

        try:
            sections = self._sections(campagne)
        except Exception:  # noqa: BLE001
            # Un dossier partiel vaut mieux qu'une erreur : la campagne est en
            # base, la décision a été prise, et l'équipe doit pouvoir la relire.
            logger.exception("Composition du dossier %s en échec", campaign_id)
            return {
                "available": False,
                "raison": "Le dossier n'a pas pu être composé pour cette campagne.",
            }

        titre = campagne.get("name") or f"Campagne n°{campaign_id}"
        entete = [
            f"# Campaign Report — {titre}",
            "",
            f"*{campagne['entity_label']} · proposée le "
            f"{campagne['created_at'].strftime('%d/%m/%Y')} · statut : "
            f"{campagne['status']}*",
            "",
        ]
        markdown = "\n".join(entete) + "\n".join(s.markdown() for s in sections)

        return {
            "available": True,
            "campaign_id": campaign_id,
            "titre": titre,
            "entite": campagne["entity_label"],
            "statut": campagne["status"],
            "cree_le": campagne["created_at"].isoformat(),
            "sections": [s.as_dict() for s in sections],
            "markdown": markdown,
        }

    # -------------------------------------------------------------- Sections

    def _sections(self, campagne: dict) -> list[Section]:
        payload = campagne.get("payload") or {}
        cible = payload.get("cible") or {}
        objectif = OBJECTIFS.get(campagne["objective"])
        segment = SEGMENTS.get(campagne["segment"])
        canal = CANAUX.get(campagne["channel"])
        contexte = payload.get("contexte") or {}

        constructeurs = (
            self._resume,
            self._contexte,
            self._insights,
            self._problemes,
            self._segment,
            self._objectif,
            self._strategie,
            self._contenu,
            self._canaux,
            self._kpis,
            self._impact,
            self._preuves,
            self._limites,
        )
        contexte_complet = _Contexte(
            campagne=campagne,
            payload=payload,
            cible=cible,
            objectif=objectif,
            segment=segment,
            canal=canal,
            externe=contexte,
        )

        sections: list[Section] = []
        for numero, ((cle, titre), construire) in enumerate(
            zip(TITRES, constructeurs), start=1
        ):
            section = Section(cle=cle, numero=numero, titre=titre)
            construire(section, contexte_complet)
            if section.vide():
                # Une section vide est dite, jamais supprimée : la numérotation
                # du livrable est fixe, et un lecteur qui cherche « 11. Expected
                # Impact » doit la trouver, fût-elle sans matière.
                section.texte = "Aucune donnée disponible pour cette section."
            sections.append(section)
        return sections

    # --- 1 ---------------------------------------------------------------
    def _resume(self, s: Section, c: "_Contexte") -> None:
        parts = [
            f"Campagne « {c.campagne['name'] or 'sans nom'} » proposée pour "
            f"{c.campagne['entity_label']}."
        ]
        if c.segment and c.objectif:
            parts.append(
                f"Elle vise {int(c.campagne['segment_size'])} avis clients du "
                f"segment « {c.segment.label} », avec pour objectif "
                f"{c.objectif.label.lower()}."
            )
        if c.campagne.get("problem"):
            parts.append(c.campagne["problem"])
        s.texte = " ".join(parts)

    # --- 2 ---------------------------------------------------------------
    def _contexte(self, s: Section, c: "_Contexte") -> None:
        debut = (
            c.campagne["created_at"].date() - timedelta(days=c.campagne["window_days"])
        )
        s.champs = [
            ("Entité", c.campagne["entity_label"]),
            ("Niveau", c.campagne["entity_level"]),
            (
                "Période analysée",
                f"{c.campagne['window_days']} jours "
                f"({debut.isoformat()} → {c.campagne['created_at'].date().isoformat()})",
            ),
            ("Statut", c.campagne["status"]),
        ]
        if c.cible.get("pays"):
            s.champs.append(("Pays", str(c.cible["pays"])))
        if c.campagne.get("brief"):
            s.champs.append(("Demande de l'utilisateur", c.campagne["brief"]))
        if c.externe.get("marche"):
            s.lignes += [f"Contexte marché : {m}" for m in c.externe["marche"]]
        if c.externe.get("presse"):
            s.lignes += [f"Presse : {p}" for p in c.externe["presse"]]
        if c.externe.get("insight_veille"):
            s.lignes.append(f"Veille (Agent 1) : {c.externe['insight_veille']}")

    # --- 3 ---------------------------------------------------------------
    def _insights(self, s: Section, c: "_Contexte") -> None:
        for label, cle, suffixe in (
            ("Avis clients sur la période", "avis_clients", ""),
            ("Avis négatifs", "negatifs", ""),
            ("Part de négatifs", "part_negatifs", " %"),
            ("Avis positifs", "positifs", ""),
            ("Part de positifs", "part_positifs", " %"),
            ("Note moyenne", "note_moyenne", " / 5"),
        ):
            valeur = c.cible.get(cle)
            if valeur is not None:
                s.champs.append((label, f"{valeur}{suffixe}"))

        variation = c.cible.get("variation_negatifs_points")
        if variation is not None:
            sens = "hausse" if float(variation) > 0 else "baisse"
            s.champs.append(
                ("Évolution des négatifs", f"{variation:+} points ({sens})")
            )

        composition = c.cible.get("composition") or {}
        if composition:
            détail = " · ".join(
                f"{source} {nombre}" for source, nombre in composition.items()
            )
            s.lignes.append(f"Composition des sources : {détail}")

    # --- 4 ---------------------------------------------------------------
    def _problemes(self, s: Section, c: "_Contexte") -> None:
        if c.campagne.get("problem"):
            s.texte = c.campagne["problem"]
        motif = c.cible.get("motif_dominant")
        if motif:
            ligne = f"Motif dominant : {aspect_label(motif)}"
            if c.cible.get("motif_avis") is not None:
                ligne += f" — {c.cible['motif_avis']} avis"
            if c.cible.get("part_motif") is not None:
                ligne += f", soit {c.cible['part_motif']} % des mécontents"
            s.lignes.append(ligne)

    # --- 5 ---------------------------------------------------------------
    def _segment(self, s: Section, c: "_Contexte") -> None:
        if c.segment:
            s.champs = [
                ("Segment", c.segment.label),
                ("Critère", c.segment.critere),
                ("Taille mesurée", f"{int(c.campagne['segment_size'])} avis clients"),
            ]
        justification = []
        if c.cible.get("part_motif") is not None and c.cible.get("motif_dominant"):
            justification.append(
                f"Le segment est retenu parce que les plaintes portant sur "
                f"« {aspect_label(c.cible['motif_dominant'])} » représentent "
                f"{c.cible['part_motif']} % des avis négatifs de la période."
            )
        elif c.cible.get("part_negatifs") is not None:
            justification.append(
                f"Le segment est retenu parce que {c.cible['part_negatifs']} % "
                f"des avis clients de la période sont négatifs."
            )
        # RAPPEL NÉCESSAIRE, pas une précaution de style : sans lui, « segment »
        # se lit comme un fichier d'abonnés adressable, ce qu'il n'est pas.
        justification.append(
            "Un segment désigne ici un ensemble d'AVIS, pas d'abonnés : la "
            "plateforme ne collecte que des avis publics et ne dispose "
            "d'aucune coordonnée client."
        )
        s.texte = " ".join(justification)

    # --- 6 ---------------------------------------------------------------
    def _objectif(self, s: Section, c: "_Contexte") -> None:
        if not c.objectif:
            return
        s.champs = [
            ("Objectif", c.objectif.label),
            ("Définition", c.objectif.definition),
            ("Ce qu'il n'est pas", c.objectif.exclusion),
        ]
        mesure = c.payload.get("objectif_mesure")
        if mesure and mesure != c.campagne["objective"]:
            autre = OBJECTIFS.get(mesure)
            s.lignes.append(
                f"Les mesures désignaient plutôt « {autre.label if autre else mesure} » ; "
                "c'est la demande de l'utilisateur qui a été suivie."
            )

    # --- 7 ---------------------------------------------------------------
    def _strategie(self, s: Section, c: "_Contexte") -> None:
        retenue = c.campagne.get("strategy")
        for strategie in c.campagne.get("strategies") or []:
            cle = strategie.get("cle", "")
            marque = " (retenu)" if retenue and cle == retenue else ""
            s.lignes.append(
                f"Option {cle} — {strategie.get('label', '')}{marque} : "
                f"{strategie.get('angle', '')}"
            )
        if not s.lignes and retenue:
            connue = STRATEGIES.get(retenue)
            if connue:
                s.lignes.append(f"Option {retenue} — {connue.label} : {connue.angle}")
        s.champs.append(("Ton de communication", c.campagne.get("tone") or "factuel"))

    # --- 8 ---------------------------------------------------------------
    def _contenu(self, s: Section, c: "_Contexte") -> None:
        s.champs = [
            ("Nom de la campagne", c.campagne["name"] or "(sans nom)"),
            ("Accroche", c.campagne["hook"]),
            ("Message principal", c.campagne["message"]),
        ]
        for numero, action in enumerate(c.payload.get("actions") or [], start=1):
            s.lignes.append(f"Action {numero} : {action}")

        for cle, bloc in (c.campagne.get("contents") or {}).items():
            if not isinstance(bloc, dict):
                continue
            morceaux = " / ".join(
                f"{champ} : {valeur}" for champ, valeur in bloc.items() if valeur
            )
            if morceaux:
                s.lignes.append(f"[{cle}] {morceaux}")
        if not (c.campagne.get("contents") or {}):
            s.lignes.append(
                "Les déclinaisons SMS, notification, e-mail, réseaux sociaux et "
                "annonce n'ont pas encore été produites pour cette campagne."
            )

    # --- 9 ---------------------------------------------------------------
    def _canaux(self, s: Section, c: "_Contexte") -> None:
        if c.canal:
            s.champs = [
                ("Canal recommandé", c.canal.label),
                ("Longueur maximale", f"{c.canal.max_caracteres} caractères"),
                ("Pourquoi ce canal", c.canal.note),
            ]
        composition = c.cible.get("composition") or {}
        if composition:
            dominante = max(composition, key=lambda k: composition[k])
            s.lignes.append(
                f"Le canal se déduit de la source où le segment s'exprime "
                f"réellement — ici {dominante}. Sans fichier client, c'est le "
                "seul ciblage honnête disponible."
            )

    # --- 10 --------------------------------------------------------------
    def _kpis(self, s: Section, c: "_Contexte") -> None:
        if not c.objectif:
            return
        if c.objectif.mesurable:
            s.champs = [
                ("KPI de suivi", c.objectif.kpi_label),
                ("Sens attendu", c.objectif.sens),
                ("Maille", c.objectif.maille),
            ]
        else:
            s.champs = [
                ("KPI de suivi", f"{c.objectif.kpi_label} — NON MESURABLE ici"),
                ("Ce qu'il faudrait", c.objectif.donnee_manquante),
            ]
        s.lignes.append(
            "Aucun KPI d'ouverture, de clic ou de conversion n'est proposé : la "
            "plateforme ne collecte aucune donnée d'envoi."
        )

    # --- 11 --------------------------------------------------------------
    def _impact(self, s: Section, c: "_Contexte") -> None:
        """Ce qui sera mesuré, contre quoi, et à partir de quelle valeur.

        JAMAIS DE PROJECTION CHIFFRÉE. Un « -8 points attendus » serait un
        nombre inventé au milieu d'un document exact, donc indétectable et
        d'autant plus dommageable.
        """
        if not c.objectif or not c.objectif.mesurable:
            s.texte = (
                "L'atteinte de cet objectif n'est pas mesurable avec les données "
                "de la plateforme ; aucun impact ne sera donc chiffré."
            )
            return
        depart = c.cible.get("part_negatifs")
        s.texte = (
            f"Le bilan comparera {c.objectif.kpi_label} du segment visé avant et "
            f"après la campagne, sur deux fenêtres de durée égale "
            f"({c.campagne['window_days']} jours), avec l'évolution du pays comme "
            "repère — faute de groupe témoin, c'est le seul garde-fou contre "
            "l'attribution automatique."
        )
        if depart is not None:
            s.champs.append(("Valeur de départ", f"{depart} % d'avis négatifs"))
        s.lignes.append(
            "Aucun impact n'est projeté : la plateforme mesure la satisfaction "
            "constatée, elle ne prédit pas."
        )

    # --- 12 --------------------------------------------------------------
    def _preuves(self, s: Section, c: "_Contexte") -> None:
        s.champs = [
            ("Entité", f"{c.campagne['entity_level']} = {c.campagne['entity_key']}"),
            ("Fenêtre", f"{c.campagne['window_days']} jours"),
            ("Polarité du segment", c.segment.polarite if c.segment else "—"),
        ]
        if c.cible.get("motif_dominant"):
            s.champs.append(("Motif", c.cible["motif_dominant"]))

        identifiants = self._identifiants(c)
        if identifiants is None:
            s.lignes.append(
                "Les identifiants d'avis ne peuvent pas être rendus (accès aux "
                "agrégats indisponible). Les critères ci-dessus les reproduisent."
            )
        elif identifiants:
            s.lignes.append(
                f"Avis à l'origine de cette campagne ({len(identifiants)} cités "
                f"sur {int(c.campagne['segment_size'])}) : "
                + ", ".join(identifiants)
            )
        else:
            s.lignes.append(
                "Aucun avis ne remonte aujourd'hui sur ces critères — la fenêtre "
                "d'origine est passée. Les critères ci-dessus restent la "
                "référence."
            )

    # --- 13 --------------------------------------------------------------
    def _limites(self, s: Section, c: "_Contexte") -> None:
        s.lignes.append(
            "La plateforme ne collecte que des avis PUBLICS : aucun envoi, "
            "aucune ouverture, aucun clic, aucun numéro, aucune adresse."
        )
        s.lignes.append(
            "Un segment est un ensemble d'avis, pas d'abonnés : il n'est pas "
            "adressable directement."
        )
        for avertissement in c.externe.get("indisponibles") or []:
            s.lignes.append(avertissement)
        if not c.campagne.get("written_by_llm"):
            s.lignes.append(
                "Les textes de cette campagne ont été composés par gabarit et "
                "non rédigés par le modèle."
            )
        if c.objectif and not c.objectif.mesurable:
            s.lignes.append(
                f"L'objectif retenu n'est pas mesurable ici : il faudrait "
                f"{c.objectif.donnee_manquante}."
            )

    # --------------------------------------------------------------- Preuves

    def _identifiants(self, c: "_Contexte") -> Optional[list[str]]:
        """Identifiants d'avis du segment, relus au moment du rendu.

        Renvoie None quand la lecture est impossible — distinct d'une liste
        vide, qui signifie « la requête a abouti et n'a rien trouvé ». Les deux
        appellent une phrase différente dans le dossier.
        """
        if self.stats is None or not c.segment:
            return None
        try:
            depuis = c.campagne["created_at"].date()
            f = StatsFilter(
                days=c.campagne["window_days"],
                date_to=min(depuis, date.today()),
                **_axe_entite(c.campagne),
            )
            data = self.stats.verbatims(
                f, polarity=c.segment.polarite, limit=MAX_PREUVES
            )
        except Exception:  # noqa: BLE001
            logger.warning("Preuves indisponibles pour la campagne", exc_info=True)
            return None
        return [str(r["review_id"]) for r in data.get("reviews") or []]


@dataclass
class _Contexte:
    """Ce qu'une section a besoin de connaître, rassemblé une fois.

    Évite que chaque constructeur de section refasse les mêmes `.get()` sur le
    payload — treize fois la même déstructuration finirait par diverger.
    """

    campagne: dict
    payload: dict
    cible: dict
    objectif: Any
    segment: Any
    canal: Any
    externe: dict


def _axe_entite(campagne: dict) -> dict[str, tuple]:
    """Traduit l'entité de la campagne en axe du contrat de filtre.

    Les identifiants stockés sont ceux du contrat de filtre (migration 016) : un
    pays se désigne par son ISO alpha-2, une filiale par son identifiant
    entier. La conversion est donc directe — mais un identifiant non entier ne
    doit pas faire échouer le dossier entier, d'où le repli sur un périmètre
    vide plutôt qu'une exception.
    """
    niveau = campagne["entity_level"]
    cle = campagne["entity_key"]
    try:
        if niveau == "subsidiary":
            return {"subsidiaries": (int(cle),)}
        if niveau == "operator":
            return {"operators": (int(cle),)}
    except (TypeError, ValueError):
        return {}
    if niveau == "country":
        return {"countries": (str(cle),)}
    if niveau == "region":
        return {"regions": (str(cle),)}
    return {}
