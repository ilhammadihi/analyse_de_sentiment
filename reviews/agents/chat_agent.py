"""
Assistant conversationnel : répond à une question posée en français.

(Il a porté le nom d'« Agent 2 » avant que celui-ci ne soit attribué à
l'assistant de campagne, `campaign_agent.py`. Le nom a été retiré plutôt que
partagé : deux agents portant le même numéro rendent toute conversation sur le
projet ambiguë.)

L'ORDRE DES ÉTAPES EST LA GARANTIE, comme pour l'Agent 1
    question → TRADUCTION en paramètres → VALIDATION contre le contrat de
    filtre → EXÉCUTION par la plateforme → MISE EN PHRASE.

    Le modèle n'intervient qu'aux extrémités : il comprend la question, puis il
    rédige. Entre les deux, il ne voit ni la base ni le SQL, et aucun chiffre
    ne sort de lui. Inverser cet ordre — laisser le modèle interroger puis
    répondre — produirait des réponses plausibles et invérifiables, ce qui est
    exactement le mode de panne qu'on ne détecte pas en démonstration.

CE QUI EST VÉRIFIÉ MÉCANIQUEMENT, ET NON SEULEMENT DEMANDÉ DANS LE PROMPT
    « N'invente pas de chiffre » est une consigne, donc une probabilité. Après
    rédaction, tout nombre présent dans la phrase est confronté aux nombres
    fournis (`_chiffres_inventes`) ; s'il en apparaît un qui ne vient pas des
    mesures, la phrase du modèle est jetée et la réponse factuelle part à sa
    place. Le coût d'un rejet à tort est une réponse moins jolie ; le coût d'un
    chiffre inventé est la crédibilité de tout le dispositif.

CE QU'IL NE FAIT PAS
    Il ne collecte rien, n'écrit dans aucune table d'avis, ne recalcule aucun
    agrégat : il appelle les mêmes méthodes de repository que le dashboard.
    Deux lecteurs, l'un sur l'écran et l'autre sur Telegram, obtiennent donc le
    même chiffre — ce qui ne serait pas vrai d'une seconde implémentation.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from reviews.agents.chiffres import chiffres_autorises, chiffres_inventes
from reviews.agents.questions import (
    INTENTIONS,
    NIVEAUX,
    TRIS,
    Catalogue,
    Demande,
    QuestionRefusee,
    valider,
)
from reviews.config import Settings
from reviews.llm.client import LLMClient, LLMError, LLMUnavailable
from reviews.storage.db import Database
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Nom sous lequel cet agent signe dans `agent_reports` (colonne `agent`,
#: prévue au pluriel dès la migration 013 pour cet usage).
AGENT = "chat"

#: Durée de validité du catalogue en mémoire, en secondes.
#:
#: NI ZÉRO NI L'INFINI. À zéro, chaque question rejouerait les cinq requêtes de
#: `filter_options()` — un coût inutile pour des dimensions qui bougent
#: quelques fois par mois. À l'infini, une filiale ajoutée en base resterait
#: inconnue du robot jusqu'au prochain redémarrage du processus, et le robot
#: répondrait « je ne connais pas » sur une entité que le dashboard affiche
#: déjà : une incohérence entre deux écrans, impossible à comprendre pour qui
#: la constate.
CATALOGUE_TTL_SECONDES = 3600


# ---------------------------------------------------------------------------
# Réponse
# ---------------------------------------------------------------------------


@dataclass
class Reponse:
    """Ce que l'agent rend, pour Telegram, la CLI et les tests."""

    texte: str

    #: Ce qui a été compris. `None` quand la question a été refusée avant
    #: exécution — c'est la première chose à regarder quand une réponse
    #: surprend.
    demande: Optional[Demande] = None

    #: Renseigné quand la réponse est un refus assumé, pas une mesure.
    refus: Optional[str] = None

    appels_llm: int = 0
    lignes: int = 0

    #: Vrai si le modèle a rédigé la phrase finale, faux si c'est le gabarit.
    #: Utile en démonstration : distingue « le modèle est absent » de « le
    #: modèle a inventé un chiffre et a été écarté ».
    redige_par_modele: bool = False

    def resume(self) -> str:
        if self.refus:
            return f"refus · {self.refus[:60]}"
        return (
            f"{self.lignes} ligne(s) · {self.appels_llm} appel(s) modèle · "
            f"{'rédigé' if self.redige_par_modele else 'gabarit'}"
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEME_TRADUCTION = """\
Tu es un traducteur de questions vers des PARAMÈTRES. Tu n'as accès à aucune \
donnée et tu ne calcules rien : une autre partie du programme exécutera la \
mesure et rédigera les chiffres.

Tu réponds UNIQUEMENT par un objet JSON de cette forme :

{{
  "intention": {intentions} ou null,
  "niveau":    "subsidiary" | "operator" | "country" | "region",
  "tri":       {tris},
  "operateur": nom exact d'un opérateur de la liste, ou null,
  "pays":      nom exact d'un pays de la liste, ou null,
  "region":    nom exact d'une région de la liste, ou null,
  "jours":     entier (fenêtre demandée) ou null,
  "limite":    entier (nombre de lignes demandé) ou null,
  "pourquoi":  si intention vaut null, une phrase française disant ce qui \
manque pour répondre
}}

Ce que veut dire chaque valeur :
{gloses}

RÈGLES ABSOLUES
- N'invente JAMAIS un nom absent des listes ci-dessous. Si la question cite une \
entité que tu n'y trouves pas, mets "intention": null et explique-le.
- Une filiale est un opérateur ET un pays : « Orange Mali » se traduit par \
"operateur": "Orange", "pays": "Mali" — jamais par un nom de filiale.
- Si la question ne se réduit pas à un classement (prévision, cause, question \
ouverte), mets "intention": null. Ne te rabats pas sur le classement le plus \
proche.
- N'invente aucun seuil ni aucune valeur par défaut : laisse null ce que la \
question ne dit pas. Les valeurs par défaut sont décidées par le programme.

{vocabulaire}

EXEMPLES
Question : « quelle filiale d'Orange revient le plus ces jours-ci ? »
{{"intention": "classement", "niveau": "subsidiary", "tri": "volume", \
"operateur": "Orange", "pays": null, "region": null, "jours": null, "limite": null}}

Question : « quels sont les 3 pays où les clients sont les plus mécontents \
sur les 90 derniers jours ? »
{{"intention": "classement", "niveau": "country", "tri": "negatifs", \
"operateur": null, "pays": null, "region": null, "jours": 90, "limite": 3}}

Question : « combien Orange va-t-il perdre d'abonnés l'an prochain ? »
{{"intention": null, "pourquoi": "je n'ai que des avis clients déjà publiés, \
aucune donnée d'abonnés ni de prévision"}}
"""

_SYSTEME_REDACTION = """\
Tu rédiges une réponse courte, en français, à partir de FAITS DÉJÀ CALCULÉS.

RÈGLES ABSOLUES
- N'écris aucun nombre qui ne figure pas dans les faits fournis. Tu peux \
arrondir un pourcentage, jamais en produire un nouveau.
- Ne calcule rien : ni écart, ni total, ni moyenne, ni pourcentage.
- N'explique aucune cause. Les faits disent ce qui est mesuré, pas pourquoi.
- Deux à trois phrases au maximum, sans salutation ni formule de politesse. \
La réponse est lue sur un téléphone.
- Ne répète pas la période : le programme l'ajoute lui-même sous ta réponse.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ChatAgent:
    """Traduit une question, la fait exécuter, et met le résultat en phrase."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        client: Optional[LLMClient] = None,
        stats: Optional[StatsRepository] = None,
    ):
        self.db = db
        self.settings = settings
        self.stats = stats or StatsRepository(db)
        self.client = client
        self._catalogue: Optional[Catalogue] = None
        self._catalogue_charge_a: float = 0.0

    # ------------------------------------------------------------- Catalogue

    def catalogue(self) -> Catalogue:
        """Ce que la base connaît, rechargé périodiquement."""
        peremime = (
            time.monotonic() - self._catalogue_charge_a > CATALOGUE_TTL_SECONDES
        )
        if self._catalogue is None or peremime:
            self._catalogue = Catalogue.depuis(self.stats.filter_options())
            self._catalogue_charge_a = time.monotonic()
        return self._catalogue

    # ----------------------------------------------------------------- Public

    def repondre(self, question: str) -> Reponse:
        """Une question, une réponse. Ne lève jamais.

        UN AGENT CONVERSATIONNEL QUI PLANTE NE REND PAS D'ERREUR : il ne répond
        rien du tout, et l'utilisateur reste devant un silence qu'il attribuera
        au réseau. Toute panne devient donc une phrase, et la trace technique
        part dans les journaux.
        """
        question = (question or "").strip()
        if not question:
            return Reponse(
                texte="Posez-moi une question, par exemple : « quelle filiale "
                "d'Orange revient le plus ces jours-ci ? »",
                refus="question vide",
            )

        appels = 0
        try:
            brut, appels = self._traduire(question)
        except LLMUnavailable as exc:
            # Distinguée d'une panne : il n'y a rien à réessayer, et la cause
            # est une configuration, pas un incident. Le texte de `client` est
            # déjà écrit pour un lecteur non technique.
            return Reponse(texte=str(exc), refus="modèle indisponible")
        except LLMError as exc:
            logger.warning("Traduction de question en échec : %s", exc)
            return Reponse(
                texte="Je n'arrive pas à joindre le service qui comprend les "
                "questions. Réessayez dans quelques minutes.",
                refus="modèle en erreur",
            )

        try:
            demande = valider(brut, self.catalogue())
        except QuestionRefusee as exc:
            # LE REFUS EST UNE RÉPONSE, pas un échec. Il est renvoyé tel quel :
            # « je ne connais pas cet opérateur » vaut infiniment mieux qu'un
            # classement sur un périmètre approchant, que rien ne signalerait.
            return Reponse(texte=str(exc), refus=str(exc), appels_llm=appels)

        try:
            resultat = self._executer(demande)
        except Exception:  # noqa: BLE001
            logger.exception("Exécution de la demande en échec : %s", demande.as_dict())
            return Reponse(
                texte="La mesure a échoué de mon côté. L'incident est journalisé.",
                demande=demande,
                refus="exécution en échec",
                appels_llm=appels,
            )

        lignes = resultat.get("rows") or []
        factuel = self._factuel(demande, lignes)
        if not lignes:
            # Rien à rédiger : une absence de données ne se met pas en phrase,
            # elle se dit. Faire rédiger un vide par le modèle coûterait un
            # appel pour produire une périphrase.
            return Reponse(
                texte=factuel + "\n\n" + self._pied(demande),
                demande=demande,
                appels_llm=appels,
                lignes=0,
            )

        texte, appels_redaction, redige = self._mettre_en_phrase(
            question, factuel, lignes
        )
        return Reponse(
            texte=texte + "\n\n" + self._pied(demande),
            demande=demande,
            appels_llm=appels + appels_redaction,
            lignes=len(lignes),
            redige_par_modele=redige,
        )

    # ------------------------------------------------------------- Traduction

    def _traduire(self, question: str) -> tuple[dict, int]:
        """Question -> paramètres bruts, par le modèle."""
        if self.client is None:
            raise LLMUnavailable(
                "Aucun modèle n'est configuré : je ne peux pas interpréter une "
                "question en langage naturel."
            )

        systeme = _SYSTEME_TRADUCTION.format(
            intentions=" | ".join(f'"{k}"' for k in INTENTIONS),
            tris=" | ".join(f'"{k}"' for k in TRIS),
            gloses=self._gloses(),
            vocabulaire=self.catalogue().vocabulaire(),
        )
        brut = self.client.complete_json(
            system=systeme,
            user=question,
            # Température nulle : deux fois la même question doit donner deux
            # fois les mêmes paramètres. Une traduction qui varie ferait varier
            # le chiffre affiché sans qu'aucune donnée ait bougé — c'est ce
            # qu'on reproche en premier à un robot, et c'est indéfendable.
            temperature=0.0,
            # Court : la sortie est un objet de huit champs. Un plafond large
            # laisserait le modèle commenter sa réponse, ce qu'`extract_json`
            # rattraperait, mais en payant des jetons pour du texte jeté.
            max_tokens=300,
        )
        if not isinstance(brut, dict):
            raise LLMError(f"Traduction inattendue : {type(brut).__name__}")
        logger.info("Question traduite : %s", json.dumps(brut, ensure_ascii=False))
        return brut, 1

    @staticmethod
    def _gloses() -> str:
        """Les listes blanches, écrites pour le modèle.

        CONSTRUITES DEPUIS LES DICTIONNAIRES, jamais recopiées dans le prompt :
        une valeur ajoutée au contrat et oubliée dans le texte serait une valeur
        que le modèle n'utiliserait jamais, sans que rien ne le signale — un tri
        mort dont personne ne saurait qu'il l'est.
        """
        lignes = [f"- intention {k} : {v}" for k, v in INTENTIONS.items()]
        lignes += [f"- niveau {k} : {v}" for k, v in NIVEAUX.items()]
        lignes += [f"- tri {k} : {v}" for k, v in TRIS.items()]
        return "\n".join(lignes)

    # --------------------------------------------------------------- Exécution

    def _executer(self, demande: Demande) -> dict:
        """Appelle la plateforme. AUCUN SQL n'est composé ici.

        C'est le point où l'architecture se voit : la demande validée est passée
        telle quelle à la méthode de repository que le dashboard appelle déjà.
        """
        return self.stats.ranking(
            f=demande.filtre,
            level=demande.niveau,
            sort=demande.tri,
            min_reviews=demande.min_avis,
            limit=demande.limite,
        )

    # --------------------------------------------------------------- Rédaction

    def _factuel(self, demande: Demande, lignes: list[dict]) -> str:
        """La réponse que l'agent sait donner sans aucun modèle.

        ELLE EST TOUJOURS CALCULÉE, même quand le modèle est disponible : c'est
        elle qui porte les chiffres, elle qui sert de repli, et elle qui définit
        l'ensemble des nombres qu'une rédaction a le droit d'employer. La
        version rédigée n'est qu'une reformulation de ce texte.
        """
        if not lignes:
            return (
                f"Aucun avis client sur ce périmètre pendant les "
                f"{demande.jours} derniers jours."
            )

        entetes = {
            "volume": "par nombre d'avis clients",
            "negatifs": "par part d'avis négatifs",
            "note_asc": "de la plus mauvaise note à la meilleure",
            "note_desc": "de la meilleure note à la plus mauvaise",
        }
        out = [f"Classement {entetes.get(demande.tri, '')} :"]
        for rang, ligne in enumerate(lignes, 1):
            out.append(f"{rang}. {ligne.get('label') or '?'} — {_mesures(ligne)}")
        return "\n".join(out)

    def _mettre_en_phrase(
        self, question: str, factuel: str, lignes: list[dict]
    ) -> tuple[str, int, bool]:
        """Fait reformuler le texte factuel par le modèle.

        Returns:
            (texte, appels consommés, rédigé par le modèle).

        Trois raisons de rendre le factuel : pas de modèle, appel en échec, ou
        chiffre inventé. Dans les trois cas la réponse reste juste — c'est la
        propriété qui autorise à brancher un modèle sur ce chemin.
        """
        if self.client is None or not self.client.available:
            return factuel, 0, False

        try:
            reponse = self.client.complete(
                system=_SYSTEME_REDACTION,
                user=f"Question posée : {question}\n\nFaits mesurés :\n{factuel}",
                json_mode=False,
                max_tokens=300,
                # Un peu de latitude sur la formulation, mais pas sur le fond :
                # les chiffres sont vérifiés juste après.
                temperature=0.3,
            )
        except (LLMUnavailable, LLMError) as exc:
            logger.info("Rédaction indisponible, repli sur le factuel : %s", exc)
            return factuel, 0, False

        texte = (reponse.text or "").strip()
        if not texte:
            return factuel, 1, False

        inventes = _chiffres_inventes(texte, _chiffres_autorises(lignes))
        if inventes:
            # LE GARDE-FOU DE LA RÈGLE « LE MODÈLE NE CALCULE JAMAIS ». Le
            # prompt le lui interdit déjà ; ceci le VÉRIFIE. Un pourcentage
            # calculé de tête par un modèle est faux une fois sur cinq et
            # indiscernable d'un chiffre mesuré une fois écrit.
            logger.warning(
                "Rédaction écartée, chiffres absents des mesures : %s", inventes
            )
            return factuel, 1, False
        return texte, 1, True

    def _pied(self, demande: Demande) -> str:
        """Périmètre et fenêtre, AJOUTÉS PAR LE PROGRAMME.

        Jamais confiés au modèle, pour deux raisons. La première est la règle du
        projet : un taux lu sans son périmètre est un taux mal interprété, et
        cette ligne ne doit donc pas dépendre de la bonne volonté d'une
        rédaction. La seconde est mécanique : les dates contiennent des nombres,
        que la vérification des chiffres inventés devrait alors autoriser — en
        y ouvrant une brèche large de quatre chiffres.
        """
        f = demande.filtre.describe()
        return (
            f"— Périmètre : {demande.portee} "
            f"({_jour(f['from'])} → {_jour(f['to'])}), avis clients."
        )


# ---------------------------------------------------------------------------
# Formatage et vérification des chiffres
# ---------------------------------------------------------------------------

def _mesures(ligne: dict) -> str:
    """Les mesures d'une ligne de classement, en une phrase.

    TROIS AU PLUS. `ranking` en rend une quinzaine ; les aligner produirait un
    tableau que personne ne lit sur un téléphone. Le volume dit si le chiffre
    est crédible, la part de négatifs dit ce qui va mal, la note dit le
    ressenti — au-delà, on documente au lieu de répondre.
    """
    morceaux = [f"{int(ligne.get('avis_clients') or 0)} avis"]
    part = ligne.get("part_negatifs")
    if part is not None:
        # CONVERSION OBLIGATOIRE : PostgreSQL rend les pourcentages en
        # `Decimal`, que le formatage flottant refuse. Même piège que celui qui
        # avait fait tomber l'Agent 1 sur `Decimal - float`.
        morceaux.append(f"{_nombre(float(part))} % négatifs")
    note = ligne.get("note_moyenne")
    if note is not None:
        morceaux.append(f"note {_nombre(float(note))}/5")
    return ", ".join(morceaux)


def _nombre(valeur: float) -> str:
    """Un nombre à une décimale, à la française (virgule)."""
    return f"{valeur:.1f}".replace(".", ",")


def _jour(iso: str) -> str:
    """« 2026-07-14 » -> « 14/07 ». L'année est dans le contexte de la question."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m")
    except (TypeError, ValueError):
        return iso


#: Colonnes d'une ligne de classement qu'une réponse a le droit de citer.
#:
#: EXPLICITES, et non « tout ce qui est numérique » : une ligne de `ranking`
#: porte aussi des identifiants et des horodatages, que le lecteur ne verra
#: jamais. Les autoriser reviendrait à ne plus rien vérifier.
_CLES_MESUREES = (
    "avis_clients", "part_negatifs", "note_moyenne", "total",
    "positifs", "negatifs", "neutres", "articles_presse",
)


def _chiffres_autorises(lignes: list[dict]) -> set[float]:
    """Tous les nombres qu'une rédaction a le droit d'employer.

    La vérification elle-même vit dans `agents/chiffres.py`, partagée avec
    l'assistant de campagne : recopiée, l'une des deux copies finirait par
    oublier la tolérance d'arrondi ou les rangs, et la règle centrale du projet
    ne serait plus appliquée que d'un côté.
    """
    return chiffres_autorises(lignes, _CLES_MESUREES)


def _chiffres_inventes(texte: str, autorises: set[float]) -> list[str]:
    """Nombres de `texte` qui ne viennent d'aucune mesure fournie."""
    return chiffres_inventes(texte, autorises)


# ---------------------------------------------------------------------------
# Fabrique
# ---------------------------------------------------------------------------


def build_chat_agent(db: Database, settings: Settings) -> ChatAgent:
    """Assemble l'agent avec ses dépendances réelles.

    Le modèle est OPTIONNEL au sens où son absence ne casse rien — mais
    contrairement à l'Agent 1, elle rend cet agent-ci muet : comprendre une
    question libre est précisément ce qu'on lui demande, et aucun gabarit ne
    s'en charge. Sans clé, il répond donc qu'il ne peut pas comprendre, ce qui
    est une réponse honnête et non une panne.
    """
    client = None
    if settings.llm.enabled and settings.llm.api_key:
        from reviews.llm.client import get_client

        client = get_client(db)
    else:
        logger.info("Agent conversationnel : aucune clé de modèle, robot muet.")
    return ChatAgent(db, settings, client=client)
