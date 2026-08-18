"""
Boucle d'écoute Telegram : reçoit les messages, renvoie les réponses.

Elle sert DEUX agents et c'est délibéré : l'assistant conversationnel (`/q`) et
l'assistant de campagne (`/campagne`, `/campagnes`, `/valider`, `/rejeter`,
`/rapport`). Un robot Telegram ne peut être interrogé que par UN processus à la
fois — c'est la contrainte du long polling rappelée plus bas —, donc une seconde
boucle pour le second agent ne serait pas un choix d'architecture mais une
panne : les deux se voleraient les messages.

POURQUOI LE LONG POLLING ET NON UN WEBHOOK
    Un webhook exige une URL publique en HTTPS, donc d'exposer l'API sur
    Internet — ou de faire tourner un tunnel — pour un service qui vit
    aujourd'hui derrière un pare-feu. Vérifié le 11 août 2026 : aucun webhook
    n'est posé sur le robot (`getWebhookInfo` rend une URL vide), le long
    polling est donc disponible immédiatement et sans exposition.

    Contrepartie assumée : UN SEUL processus peut interroger `getUpdates` à la
    fois. Un second reçoit HTTP 409 et les deux se volent les messages. C'est
    la raison pour laquelle cette boucle a sa propre commande et n'est pas
    démarrée par le planificateur en même temps que le reste.

POURQUOI UNE COMMANDE `/q` ET NON L'ÉCOUTE DE TOUT
    Vérifié le 11 août 2026 : le robot a `can_read_all_group_messages: false`.
    En groupe, il ne reçoit donc que ce qui lui est explicitement adressé.
    Désactiver ce mode le ferait recevoir tout le bavardage du groupe, et
    chaque message coûterait un appel de modèle pour découvrir que ce n'était
    pas une question. `/q` est à la fois ce que Telegram nous impose et ce que
    le budget nous conseille.

CE QUI EST DÉCIDÉ EN PYTHON, JAMAIS DEMANDÉ AU MODÈLE
    Qui a le droit de poser une question, combien par jour, et quoi faire d'un
    message qui n'en est pas une. Aucune de ces décisions n'atteint le modèle :
    elles sont prises avant qu'il ne soit appelé, ce qui est aussi ce qui les
    rend gratuites.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from reviews.agents.chat_agent import AGENT, ChatAgent, Reponse
from reviews.config import AlertingConfig, ChatConfig, Settings
from reviews.storage.agent_repository import AgentRepository
from reviews.storage.db import Database

logger = logging.getLogger(__name__)

#: Marge entre l'attente demandée à Telegram et le délai d'expiration HTTP.
#:
#: Le long polling tient la connexion ouverte `poll_timeout` secondes ; si le
#: délai HTTP était égal ou inférieur, `requests` couperait AVANT la réponse du
#: serveur, à chaque tour et sans qu'aucun message ne soit perdu — une boucle
#: qui semble fonctionner mais rejoue une connexion toutes les N secondes.
_MARGE_HTTP_SECONDES = 10

#: Attente après une panne réseau, avant de reprendre l'écoute.
#:
#: Fixe et courte : une coupure de réseau dure des secondes, et un
#: ralentissement exponentiel ferait rater des questions longtemps après le
#: rétablissement. Le seul cas qu'il faut vraiment espacer est le 409, traité à
#: part parce qu'il ne se résout jamais tout seul.
_PAUSE_APRES_ERREUR = 5.0

#: Longueur maximale d'un message Telegram (4096 caractères, limite de l'API).
#: Une réponse de classement en fait ~400 ; la borne n'existe que pour ne
#: jamais transformer une réponse anormalement longue en envoi refusé.
_MAX_CARACTERES = 4000


# ---------------------------------------------------------------------------
# Message entrant
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageEntrant:
    """Une mise à jour Telegram, réduite à ce dont la boucle a besoin."""

    update_id: int
    chat_id: int
    user_id: int
    auteur: str
    texte: str
    prive: bool

    @classmethod
    def depuis(cls, update: dict) -> Optional["MessageEntrant"]:
        """Extrait un message d'une mise à jour, ou None si ce n'en est pas une.

        Telegram appelle « update » beaucoup de choses : messages édités,
        réactions, membres qui rejoignent, boutons pressés. Tout ce qui n'est
        pas un message texte est ignoré ici — silencieusement, parce que ce
        n'est pas une anomalie mais le fonctionnement normal d'un groupe.
        """
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            return None
        texte = message.get("text")
        if not texte:
            return None
        chat = message.get("chat") or {}
        auteur_brut = message.get("from") or {}
        if chat.get("id") is None:
            return None
        return cls(
            update_id=int(update.get("update_id", 0)),
            chat_id=int(chat["id"]),
            user_id=int(auteur_brut.get("id") or 0),
            auteur=(
                auteur_brut.get("username")
                or auteur_brut.get("first_name")
                or "inconnu"
            ),
            texte=texte,
            prive=chat.get("type") == "private",
        )


@dataclass(frozen=True)
class Consigne:
    """Ce qu'il faut faire d'un message reçu."""

    question: Optional[str] = None
    reponse_immediate: Optional[str] = None

    #: Commande de campagne reconnue, et son argument. SÉPARÉS DE `question`
    #: parce qu'ils ne coûtent pas la même chose : une question passe toujours
    #: par le modèle, `/valider 12` ne l'appelle jamais. Les confondre ferait
    #: décompter du quota une commande gratuite.
    commande: Optional[str] = None
    argument: str = ""

    @property
    def ignorer(self) -> bool:
        return (
            self.question is None
            and self.reponse_immediate is None
            and self.commande is None
        )

    @property
    def coute_un_appel(self) -> bool:
        """Vrai si traiter ce message consommera du budget de modèle."""
        return self.question is not None or self.commande in _COMMANDES_PAYANTES


_AIDE = (
    "Je réponds à des questions sur la satisfaction des clients des opérateurs "
    "suivis, et je propose des campagnes fondées sur ce que disent les avis.\n\n"
    "QUESTIONS — /q\n"
    "/q quelle filiale d'Orange revient le plus ces jours-ci ?\n"
    "/q quels sont les 3 pays où les clients sont les plus mécontents ?\n\n"
    "CAMPAGNES\n"
    "/campagne — je choisis moi-même la cible et je propose une campagne\n"
    "/campagne pour Orange au Mali, plutôt rassurant, par SMS\n"
    "/campagnes — les dernières propositions et leur statut\n"
    "/fiche 12 — le dossier complet de la campagne\n"
    "/contenus 12 — décliner en SMS, e-mail, réseaux, annonce\n"
    "/revoir 12 plus agressif commercialement — une autre version\n"
    "/option 12 B — rejouer sous un autre angle (A, B ou C)\n"
    "/valider 12 · /rejeter 12 — décider d'une proposition\n"
    "/rapport 12 — ce que la satisfaction du segment visé est devenue\n\n"
    "Je ne réponds qu'à partir des avis déjà collectés : je ne fais aucune "
    "prévision, je dis quand je ne sais pas, et aucune campagne n'est envoyée à "
    "un client sans validation humaine."
)

#: Commandes de campagne, et si elles attendent un argument obligatoire.
_COMMANDES_CAMPAGNE: dict[str, bool] = {
    "/campagne": False,   # la description est facultative
    "/campagnes": False,
    "/valider": True,
    "/rejeter": True,
    "/rapport": True,
    "/fiche": True,
    "/contenus": True,
    "/revoir": True,      # « /revoir 12 plus agressif commercialement »
    "/option": True,      # « /option 12 B »
}

#: Commandes qui consomment un appel de modèle. Les autres sont gratuites : les
#: soumettre au plafond interdirait de décider d'une campagne à quelqu'un qui a
#: simplement beaucoup interrogé le robot dans la journée.
_COMMANDES_PAYANTES = frozenset({"campagne", "contenus", "revoir", "option"})


def analyser(texte: str, prive: bool) -> Consigne:
    """Décide quoi faire d'un message, sans appeler le modèle.

    LE TRI EST GRATUIT ET LE MODÈLE EST CHER : à 200 appels par jour, laisser
    un « ok merci » atteindre le traducteur consomme la veille du lendemain.
    Tout ce qui peut être écarté ou traité sans modèle l'est ici.

    Le suffixe `@robot` est retiré : dans un groupe, Telegram remet la commande
    telle qu'elle a été tapée, et un utilisateur qui passe par l'autocomplétion
    envoie « /q@Digiwise_alert_bot … ». Sans ce nettoyage, la question
    commencerait par le nom du robot — que le traducteur prendrait pour un mot
    de la question.
    """
    texte = (texte or "").strip()
    if not texte:
        return Consigne()

    if texte.startswith("/"):
        commande, _, reste = texte.partition(" ")
        commande = commande.split("@", 1)[0].lower()
        reste = reste.strip()

        if commande in ("/q", "/question"):
            return Consigne(question=reste) if reste else Consigne(
                reponse_immediate="Posez la question après la commande, par "
                "exemple : /q quelle filiale d'Orange revient le plus ?"
            )
        if commande in _COMMANDES_CAMPAGNE:
            if _COMMANDES_CAMPAGNE[commande] and not reste:
                return Consigne(
                    reponse_immediate=f"Indiquez le numéro de la proposition, "
                    f"par exemple : {commande} 12. La liste est donnée par "
                    "/campagnes."
                )
            return Consigne(commande=commande.lstrip("/"), argument=reste)
        if commande in ("/start", "/help", "/aide"):
            return Consigne(reponse_immediate=_AIDE)
        # Une commande inconnue reste sans réponse EN GROUPE : plusieurs robots
        # y cohabitent, et répondre « commande inconnue » à la commande d'un
        # autre robot ferait de celui-ci une nuisance.
        return Consigne(
            reponse_immediate="Je ne connais pas cette commande. Tapez /aide."
        ) if prive else Consigne()

    # Hors commande : accepté en conversation privée seulement. Là, tout message
    # est forcément adressé au robot, et exiger `/q` d'un encadrant qui teste
    # depuis son téléphone n'apporterait rien.
    return Consigne(question=texte) if prive else Consigne()


# ---------------------------------------------------------------------------
# Canal Telegram
# ---------------------------------------------------------------------------


class CanalTelegram:
    """Le strict nécessaire de l'API Telegram : lire les messages, en envoyer.

    SÉPARÉ DE `TelegramNotifier` À DESSEIN. Celui-ci POUSSE des alertes vers une
    conversation fixée par la configuration ; celui-là RÉPOND dans la
    conversation d'où vient la question. Fondre les deux obligerait le notifieur
    d'alertes à porter un `chat_id` variable, c'est-à-dire à pouvoir écrire
    ailleurs que dans le groupe configuré — précisément ce qu'on ne veut pas
    d'un canal d'alerte.
    """

    def __init__(self, cfg: AlertingConfig, poll_timeout: int = 25):
        self.token = cfg.telegram_bot_token
        self.poll_timeout = poll_timeout
        self._session = requests.Session()

    @property
    def utilisable(self) -> bool:
        return bool(self.token)

    def _url(self, methode: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{methode}"

    def mises_a_jour(self, offset: Optional[int]) -> tuple[list[dict], Optional[int]]:
        """Une salve de mises à jour, et l'offset à demander au tour suivant.

        Rend `([], offset inchangé)` sur panne : la boucle appelante doit
        continuer de tourner. Une exception ici arrêterait l'écoute pour une
        coupure de quelques secondes, et personne ne s'en apercevrait avant la
        première question sans réponse.
        """
        params: dict[str, Any] = {
            "timeout": self.poll_timeout,
            # Économie de bande passante ET de surprises : on ne demande que
            # les messages. Sans ce filtre, chaque réaction emoji d'un membre du
            # groupe produirait une mise à jour à traiter puis à jeter.
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset

        try:
            resp = self._session.get(
                self._url("getUpdates"),
                params=params,
                timeout=self.poll_timeout + _MARGE_HTTP_SECONDES,
            )
        except requests.RequestException as exc:
            logger.warning("Écoute Telegram interrompue : %s", exc)
            return [], offset

        if resp.status_code == 409:
            # NE SE RÉSOUT JAMAIS SEUL, et c'est la panne la plus déroutante du
            # long polling : une autre instance interroge le même robot, ou un
            # webhook est posé. Les deux processus se volent alors les messages
            # et une question sur deux reste sans réponse.
            logger.error(
                "Un autre processus écoute déjà ce robot (HTTP 409). "
                "Arrêtez-le, ou retirez le webhook, avant de relancer."
            )
            time.sleep(_PAUSE_APRES_ERREUR)
            return [], offset

        if resp.status_code >= 400:
            logger.warning(
                "Écoute Telegram refusée (HTTP %s) : %s",
                resp.status_code, resp.text[:200],
            )
            time.sleep(_PAUSE_APRES_ERREUR)
            return [], offset

        try:
            updates = resp.json().get("result") or []
        except ValueError:
            logger.warning("Réponse Telegram illisible.")
            return [], offset

        if not updates:
            return [], offset
        # L'offset suivant vaut le dernier identifiant + 1. Le transmettre
        # ACQUITTE les mises à jour précédentes côté serveur : c'est ce qui
        # évite qu'un redémarrage ne rejoue les questions déjà traitées.
        return updates, max(int(u.get("update_id", 0)) for u in updates) + 1

    def vider_le_retard(self) -> Optional[int]:
        """Acquitte tout ce qui attend, sans le traiter. Rend l'offset de départ.

        INDISPENSABLE AU DÉMARRAGE. Telegram conserve les messages non acquittés
        pendant 24 heures. Sans ce vidage, démarrer la boucle un lundi matin
        ferait répondre d'un coup à toutes les questions du week-end : une
        rafale de messages non sollicités dans le groupe, et autant d'appels de
        modèle consommés pour des questions que plus personne ne se pose.

        `offset=-1` ne rend que la DERNIÈRE mise à jour en attente ; lui ajouter
        1 acquitte tout le reste sans l'avoir lu.
        """
        try:
            resp = self._session.get(
                self._url("getUpdates"),
                params={"offset": -1, "timeout": 0},
                timeout=15,
            )
            updates = resp.json().get("result") or []
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Retard non purgé (%s) : la boucle démarrera à zéro.", exc)
            return None
        if not updates:
            return None
        dernier = int(updates[-1].get("update_id", 0))
        logger.info("%s message(s) en attente ignoré(s) au démarrage.", len(updates))
        return dernier + 1

    def envoyer(self, chat_id: int, texte: str) -> bool:
        """Envoie une réponse. EN TEXTE BRUT, sans `parse_mode`.

        Ce n'est pas un renoncement à la mise en forme, c'est une leçon déjà
        payée : en mode HTML, un seul « < » non échappé fait refuser TOUT
        l'envoi par l'API, pas seulement le caractère fautif. Or une réponse de
        refus cite la question de l'utilisateur (« je ne connais pas
        "<...>" »), qui contient exactement ce qu'on ne contrôle pas. Un
        classement se lit très bien sans gras ; une réponse jamais partie ne se
        lit pas du tout.
        """
        try:
            resp = self._session.post(
                self._url("sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": texte[:_MAX_CARACTERES],
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Réponse Telegram refusée (HTTP %s) : %s",
                    resp.status_code, resp.text[:200],
                )
                return False
            return True
        except requests.RequestException as exc:
            logger.warning("Réponse Telegram non envoyée : %s", exc)
            return False


# ---------------------------------------------------------------------------
# Boucle
# ---------------------------------------------------------------------------


class BoucleConversation:
    """Écoute, filtre, fait répondre l'agent, journalise."""

    def __init__(
        self,
        agent: ChatAgent,
        canal: CanalTelegram,
        cfg: ChatConfig,
        journal: Optional[AgentRepository] = None,
        chats_autorises: Optional[set[int]] = None,
        campagne: Optional[Any] = None,
    ):
        self.agent = agent
        self.canal = canal
        self.cfg = cfg
        self.journal = journal
        self.chats_autorises = chats_autorises or set()
        #: Assistant de campagne, ou None. OPTIONNEL À DESSEIN : son absence ne
        #: doit pas empêcher les questions de fonctionner. Typé `Any` pour ne pas
        #: importer `campaign_agent` au chargement du module — la boucle est
        #: démarrée par une commande qui ne s'en sert pas toujours.
        self.campagne = campagne
        self.offset: Optional[int] = None

    # ------------------------------------------------------------------ Public

    def demarrer(self) -> None:
        """Écoute jusqu'à interruption. Ne rend la main que sur Ctrl-C."""
        if not self.canal.utilisable:
            logger.error("Aucun jeton Telegram : la boucle ne démarre pas.")
            return
        self.offset = self.canal.vider_le_retard()
        logger.info(
            "Agent conversationnel à l'écoute (%s conversation(s) autorisée(s), "
            "%s question(s)/jour/personne).",
            len(self.chats_autorises) or "toutes",
            self.cfg.daily_questions_per_user,
        )
        try:
            while True:
                self.tour()
        except KeyboardInterrupt:
            logger.info("Agent conversationnel arrêté.")

    def tour(self) -> int:
        """Un cycle d'écoute. Rend le nombre de messages traités.

        SÉPARÉ DE `demarrer` POUR ÊTRE TESTABLE : un tour se joue avec un canal
        factice, sans réseau, sans attente et sans quota. C'est ce qui permet de
        vérifier le filtrage et l'acheminement, qui sont précisément les
        comportements dont une régression serait invisible — un robot qui ne
        répond plus ne lève aucune exception.
        """
        updates, self.offset = self.canal.mises_a_jour(self.offset)
        traites = 0
        for update in updates:
            message = MessageEntrant.depuis(update)
            if message is None:
                continue
            reponse = self._traiter(message)
            if reponse is not None:
                self.canal.envoyer(message.chat_id, reponse)
                traites += 1
        return traites

    # ---------------------------------------------------------------- Interne

    def _traiter(self, message: MessageEntrant) -> Optional[str]:
        """Le texte à renvoyer, ou None s'il n'y a rien à dire."""
        if not self._autorise(message.chat_id):
            # SILENCE, ET NON UN REFUS POLI. L'identifiant du robot est public :
            # n'importe qui peut lui écrire. Répondre « vous n'êtes pas
            # autorisé » confirmerait à un inconnu que le robot est vivant et
            # l'inviterait à insister ; ne rien dire ne coûte rien et
            # n'apprend rien.
            logger.info(
                "Message ignoré, conversation non autorisée (chat %s, %s).",
                message.chat_id, message.auteur,
            )
            return None

        consigne = analyser(message.texte, message.prive)
        if consigne.ignorer:
            return None
        if consigne.reponse_immediate:
            return consigne.reponse_immediate

        # LE PLAFOND NE S'APPLIQUE QU'À CE QUI COÛTE. `/valider 12` ne touche
        # aucun modèle : le soumettre au quota interdirait de décider d'une
        # campagne à quelqu'un qui a simplement beaucoup interrogé le robot dans
        # la journée — un blocage que rien ne justifierait.
        if consigne.coute_un_appel and self._quota_restant(message.user_id) <= 0:
            # Le plafond protège le budget du LENDEMAIN, pas celui de la
            # journée : le quota de modèle est quotidien et partagé avec
            # l'analyse sémantique et l'agent de veille. Un après-midi de jeu
            # d'une seule personne priverait la veille du matin suivant.
            return (
                f"Vous avez atteint votre plafond de "
                f"{self.cfg.daily_questions_per_user} demandes sur 24 heures. "
                "Il se libère au fil des heures."
            )

        if consigne.commande:
            return self._campagne(message, consigne)

        logger.info("Question de %s : %s", message.auteur, message.texte[:200])
        reponse = self.agent.repondre(consigne.question or "")
        self._journaliser(message, reponse)
        logger.info("Réponse à %s : %s", message.auteur, reponse.resume())
        return reponse.texte

    # -------------------------------------------------------------- Campagnes

    def _campagne(self, message: MessageEntrant, consigne: Consigne) -> str:
        """Traite une commande de campagne. Rend toujours un texte à envoyer.

        AUCUNE EXCEPTION NE REMONTE : une commande qui plante n'enverrait rien du
        tout, et l'utilisateur attribuerait le silence au réseau. Toute panne
        devient une phrase, la trace part dans les journaux.
        """
        if self.campagne is None:
            return (
                "L'assistant de campagne n'est pas configuré sur cette instance."
            )
        try:
            return self._executer_campagne(message, consigne)
        except Exception:  # noqa: BLE001
            logger.exception("Commande de campagne en échec : %s", consigne.commande)
            return "La commande a échoué de mon côté. L'incident est journalisé."

    def _executer_campagne(self, message: MessageEntrant, consigne: Consigne) -> str:
        commande, argument = consigne.commande, consigne.argument
        depot = self.campagne.campagnes

        if commande == "campagne":
            logger.info(
                "Campagne demandée par %s : %s", message.auteur, argument[:200] or "—"
            )
            campagne = self.campagne.proposer(argument)
            self._journaliser_commande(message, consigne, campagne.texte())
            if campagne.refus:
                return campagne.refus
            pied = (
                f"\n\nProposition n°{campagne.campaign_id} — /valider "
                f"{campagne.campaign_id} ou /rejeter {campagne.campaign_id}."
                if campagne.campaign_id
                else "\n\n(Proposition non enregistrée : voir les journaux.)"
            )
            return campagne.texte() + pied + (
                "\nRien n'est envoyé à un client sans validation."
            )

        if commande == "campagnes":
            lignes = depot.lister(limit=8)
            if not lignes:
                return "Aucune campagne proposée pour l'instant. Tapez /campagne."
            return "\n".join(
                f"#{c['campaign_id']} · {c['status']} · {c['entity_label']} · "
                f"{c['objective']} · {int(c['segment_size'])} avis\n   "
                f"{c['hook']}"
                for c in lignes
            )

        if commande in ("valider", "rejeter"):
            numero = _numero(argument)
            if numero is None:
                return f"Numéro de proposition attendu, par exemple : /{commande} 12."
            statut = "approved" if commande == "valider" else "rejected"
            if not depot.decider(numero, statut, message.auteur):
                return (
                    f"La proposition n°{numero} n'existe pas, ou elle a déjà été "
                    "décidée. /campagnes donne la liste."
                )
            self._journaliser_commande(
                message, consigne, f"campagne n°{numero} -> {statut}"
            )
            verbe = "validée" if statut == "approved" else "écartée"
            return (
                f"Proposition n°{numero} {verbe}. "
                + (
                    f"Le bilan sera disponible dès demain : /rapport {numero}."
                    if statut == "approved"
                    else "Elle ne sera pas reproposée pour cette entité avant "
                    "deux semaines."
                )
            )

        if commande in ("rapport", "fiche", "contenus"):
            numero = _numero(argument)
            if numero is None:
                return f"Numéro de campagne attendu : /{commande} 12."
            methode = {
                "rapport": self.campagne.rapport,
                "fiche": self.campagne.fiche,
                "contenus": self.campagne.contenus,
            }[commande]
            resultat = methode(numero)
            if commande == "contenus":
                self._journaliser_commande(message, consigne, f"contenus n°{numero}")
            return resultat.get("texte") or resultat.get("raison") or "Rien à dire."

        if commande in ("revoir", "option"):
            # L'ARGUMENT PORTE DEUX CHOSES : le numéro, puis la consigne. Les
            # séparer ici plutôt que d'exiger deux commandes distinctes, parce
            # que « /revoir 12 plus agressif » est ce qu'on tape naturellement.
            numero_brut, _, reste = argument.partition(" ")
            numero = _numero(numero_brut)
            if numero is None:
                return (
                    "Numéro de campagne attendu, par exemple : "
                    "/revoir 12 plus agressif commercialement."
                    if commande == "revoir"
                    else "Numéro et angle attendus, par exemple : /option 12 B."
                )
            reste = reste.strip()
            if commande == "option":
                angle = reste.upper()[:1]
                if angle not in ("A", "B", "C"):
                    return "Angle attendu : A, B ou C. Exemple : /option 12 B."
                campagne = self.campagne.reviser(numero, "", strategie=angle)
            else:
                if not reste:
                    return (
                        "Dites ce qu'il faut changer, par exemple : "
                        "/revoir 12 plus empathique."
                    )
                campagne = self.campagne.reviser(numero, reste)

            self._journaliser_commande(message, consigne, campagne.texte())
            if campagne.refus:
                return campagne.refus
            return (
                campagne.texte()
                + f"\n\nVersion n°{campagne.campaign_id}, révision de la n°{numero}"
                + f" (ton : {campagne.ton}).\n"
                + f"/valider {campagne.campaign_id} ou /rejeter "
                f"{campagne.campaign_id}."
            )

        return "Je ne connais pas cette commande. Tapez /aide."

    def _autorise(self, chat_id: int) -> bool:
        """Une conversation vide de liste blanche accepte tout le monde.

        C'est un défaut DANGEREUX, et il n'est jamais atteint en exploitation :
        `build_boucle` retombe sur le groupe d'alerte configuré quand la liste
        est vide. Le laisser permissif ici garde la classe utilisable en test
        sans configuration ; la décision de sécurité est prise à la
        construction, en un seul endroit.
        """
        return not self.chats_autorises or chat_id in self.chats_autorises

    def _quota_restant(self, user_id: int) -> int:
        """Questions encore disponibles pour cette personne sur 24 heures.

        FENÊTRE GLISSANTE ET NON « DEPUIS MINUIT ». Un plafond calendaire se
        contourne sans même y penser : vingt questions à 23 h 50, vingt autres à
        00 h 05, et le budget de modèle du lendemain est consommé avant que
        l'agent de veille ne parle à 8 h. La fenêtre glissante n'a pas de
        rebord, et elle évite au passage toute question de fuseau horaire entre
        le planificateur (Casablanca) et la base (UTC).

        Sans journal, aucun décompte n'est possible : on laisse passer plutôt
        que de bloquer. Un plafond qui se ferme à cause d'une panne de base
        serait une panne plus grave que celle qu'il prévient.
        """
        if self.journal is None:
            return self.cfg.daily_questions_per_user
        depuis = datetime.now(timezone.utc) - timedelta(hours=24)
        pose = self.journal.count_since(AGENT, depuis, utilisateur=str(user_id))
        return self.cfg.daily_questions_per_user - pose

    def _journaliser(self, message: MessageEntrant, reponse: Reponse) -> None:
        """Consigne la question, ce qui en a été compris, et la réponse.

        DEUX USAGES, ET LE SECOND EST LE PLUS IMPORTANT. Le premier est le
        décompte du quota. Le second est la mise au point : quand une réponse
        surprend, la question seule ne suffit pas à comprendre — il faut les
        PARAMÈTRES retenus, que le modèle ne redonnera pas deux fois à
        l'identique. Ils sont donc écrits ici, avec la question.
        """
        if self.journal is None:
            return
        demande = reponse.demande
        self.journal.record(
            agent=AGENT,
            # Le sujet d'une réponse conversationnelle est le NIVEAU interrogé,
            # dans le vocabulaire du contrat de filtre — cohérent avec ce
            # qu'écrit l'agent de veille dans la même table.
            entity_level=demande.niveau if demande else "question",
            entity_key=str(message.user_id),
            entity_label=message.auteur,
            # Aucune note d'arbitrage ici : cet agent ne classe rien, il répond.
            # La colonne est obligatoire, elle reste à zéro.
            score=0.0,
            text=reponse.texte,
            payload={
                "utilisateur": str(message.user_id),
                "auteur": message.auteur,
                "chat_id": message.chat_id,
                "question": message.texte,
                "demande": demande.as_dict() if demande else None,
                "refus": reponse.refus,
                "appels_llm": reponse.appels_llm,
                "redige_par_modele": reponse.redige_par_modele,
            },
            delivered=True,
        )

    def _journaliser_commande(
        self, message: MessageEntrant, consigne: Consigne, texte: str
    ) -> None:
        """Consigne une interaction de campagne dans le journal de CONVERSATION.

        ÉCRIT SOUS LE MÊME NOM D'AGENT QUE LES QUESTIONS, et ce n'est pas une
        approximation : ce journal décrit ce qui s'est passé DANS LA
        CONVERSATION, pas ce qu'un agent a produit. La campagne elle-même vit
        dans sa propre table, avec son cycle de vie.

        La conséquence est voulue : le plafond par personne couvre alors
        l'ensemble de ce qu'elle déclenche. Deux compteurs séparés laisseraient
        une seule personne consommer deux fois le quota prévu, et le budget de
        modèle est partagé avec l'analyse sémantique et le briefing du matin.
        """
        if self.journal is None:
            return
        self.journal.record(
            agent=AGENT,
            entity_level="commande",
            entity_key=str(message.user_id),
            entity_label=message.auteur,
            score=0.0,
            text=texte[:2000],
            payload={
                "utilisateur": str(message.user_id),
                "auteur": message.auteur,
                "chat_id": message.chat_id,
                "agent_appele": "campaign",
                "commande": consigne.commande,
                "argument": consigne.argument,
            },
            delivered=True,
        )


def _numero(argument: str) -> Optional[int]:
    """Le numéro de proposition contenu dans un argument, ou None.

    Tolère « #12 » et « 12. » : c'est ce qu'on recopie naturellement depuis la
    liste, où les identifiants sont préfixés d'un dièse. Exiger l'entier nu
    ferait échouer la commande la plus évidente à taper.
    """
    brut = (argument or "").strip().lstrip("#").rstrip(".")
    try:
        return int(brut)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fabrique
# ---------------------------------------------------------------------------


def build_boucle(db: Database, settings: Settings) -> Optional[BoucleConversation]:
    """Assemble la boucle, ou None si la configuration ne le permet pas.

    LA LISTE BLANCHE RETOMBE SUR LE GROUPE D'ALERTE quand elle n'est pas
    renseignée, et jamais sur « tout le monde ». Le nom d'un robot Telegram est
    public et sa conversation privée est ouverte à quiconque le trouve : sans
    liste blanche, un inconnu obtiendrait les chiffres de satisfaction de tout
    le parc et consommerait le quota de modèle de l'équipe. Un défaut ouvert
    aurait été une porte ouverte.
    """
    from reviews.agents.chat_agent import build_chat_agent

    cfg = settings.chat
    alerting = settings.alerting
    if not alerting.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN absent : pas de boucle conversationnelle.")
        return None

    autorises = set(cfg.allowed_chat_ids_list())
    if not autorises and alerting.telegram_chat_id:
        try:
            autorises = {int(alerting.telegram_chat_id)}
        except ValueError:
            logger.warning(
                "TELEGRAM_CHAT_ID illisible : aucune conversation autorisée."
            )
    if not autorises:
        logger.error(
            "Aucune conversation autorisée (ni CHAT_ALLOWED_CHAT_IDS ni "
            "TELEGRAM_CHAT_ID) : le robot n'écouterait personne."
        )
        return None

    from reviews.agents.campaign_agent import build_campaign_agent

    campagne = build_campaign_agent(db, settings)
    # SON NOTIFIEUR EST RETIRÉ, et c'est nécessaire, pas cosmétique. Dans une
    # conversation, la proposition part par la RÉPONSE au message. Laisser le
    # notifieur en place ferait partir un second exemplaire vers le groupe
    # d'alerte — sans la demande qui l'a provoqué, donc incompréhensible pour
    # ceux qui le reçoivent. Le notifieur ne sert qu'au passage automatique du
    # planificateur, où il n'y a personne à qui répondre.
    campagne.notifier = None

    return BoucleConversation(
        agent=build_chat_agent(db, settings),
        canal=CanalTelegram(alerting, poll_timeout=cfg.poll_timeout),
        cfg=cfg,
        journal=AgentRepository(db),
        chats_autorises=autorises,
        campagne=campagne,
    )
