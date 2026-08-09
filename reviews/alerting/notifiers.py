"""
Canaux de notification des alertes : log, e-mail (SMTP), webhook (Slack/Discord),
Telegram.

Chaque notifieur est indépendant et ne fait JAMAIS échouer le pipeline : un
canal injoignable renvoie False et le passage continue. Une alerte non notifiée
reste en base et reste visible dans le dashboard — perdre la collecte parce
qu'un jeton a expiré serait un échange perdant.
"""

import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from typing import Optional

import requests

from reviews.config import AlertingConfig
from reviews.domain.models import Alert

logger = logging.getLogger(__name__)

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class Notifier(ABC):
    name: str

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Envoie l'alerte. Retourne True si envoyée, False sinon (jamais d'exception)."""
        raise NotImplementedError


class LogNotifier(Notifier):
    name = "log"

    def send(self, alert: Alert) -> bool:
        level = _LEVELS.get(alert.severity.value if hasattr(alert.severity, "value")
                            else alert.severity, logging.INFO)
        logger.log(level, "[ALERTE %s] %s — %s",
                   alert.severity, alert.title, alert.message)
        return True


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, cfg: AlertingConfig):
        self.cfg = cfg

    def send(self, alert: Alert) -> bool:
        try:
            msg = MIMEText(alert.message, _charset="utf-8")
            msg["Subject"] = f"[{alert.severity}] {alert.title}"
            msg["From"] = self.cfg.smtp_from or self.cfg.smtp_user
            msg["To"] = self.cfg.alert_email
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=10) as server:
                server.starttls()
                if self.cfg.smtp_user:
                    server.login(self.cfg.smtp_user, self.cfg.smtp_password or "")
                server.send_message(msg)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Envoi e-mail échoué : %s", e)
            return False


class WebhookNotifier(Notifier):
    """Compatible Slack / Discord / Teams (payload {"text": ...})."""
    name = "webhook"

    def __init__(self, cfg: AlertingConfig):
        self.cfg = cfg

    def send(self, alert: Alert) -> bool:
        try:
            emoji = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(
                alert.severity.value if hasattr(alert.severity, "value") else alert.severity, "")
            text = f"{emoji} *{alert.title}*\n{alert.message}"
            resp = requests.post(self.cfg.webhook_url, json={"text": text}, timeout=10)
            return resp.status_code < 400
        except Exception as e:  # noqa: BLE001
            logger.warning("Envoi webhook échoué : %s", e)
            return False


class TelegramNotifier(Notifier):
    """Pousse l'alerte dans une conversation Telegram.

    POURQUOI UN CANAL À PART PLUTÔT QUE LE WEBHOOK GÉNÉRIQUE. L'API Telegram
    n'accepte pas le `{"text": ...}` de Slack : elle attend `chat_id` et `text`
    sur une URL qui porte le jeton du robot. Le faire passer par
    `WebhookNotifier` aurait demandé d'y introduire une exception par
    fournisseur — exactement ce que cette classe évite.

    FILTRAGE PAR GRAVITÉ, contrairement aux autres canaux. Un e-mail se relit
    plus tard, une notification de téléphone interrompt. Pousser chaque « info »
    apprend à les ignorer, et c'est alors la critique qui passe inaperçue. Le
    seuil par défaut est donc « warning ».
    """

    name = "telegram"

    #: Ordre de gravité, du moins au plus grave. Sert au filtrage par seuil.
    _RANG = {"info": 0, "warning": 1, "error": 2}

    #: Types poussés par défaut. LA GRAVITÉ NE SUFFIT PAS À TRIER.
    #:
    #: `scraper_zero` est un « warning », et c'est de loin le type le plus
    #: fréquent de la base — 196 occurrences contre 12 alertes métier. Filtré
    #: sur la seule gravité, le groupe aurait reçu des dizaines de « zéro nouvel
    #: avis » par jour, et le pic de mécontentement qu'on veut réellement lire
    #: serait passé au milieu sans être vu. C'est la panne d'attention que le
    #: canal est censé éviter, pas provoquer.
    #:
    #: Retenus : le métier (`negative_spike`, seul signal qu'un responsable
    #: doive lire) et les pannes franches, qui appellent une intervention.
    #: Tout le reste continue d'être persisté et reste visible dans le
    #: dashboard — filtrer ici ne perd aucune information.
    _TYPES_PAR_DEFAUT = frozenset({"negative_spike", "run_failed", "scraper_failed"})

    def __init__(self, cfg: AlertingConfig):
        self.cfg = cfg
        self._seuil = self._RANG.get(cfg.telegram_min_severity, 1)
        self._types = self._types_autorises(cfg)

    @classmethod
    def _types_autorises(cls, cfg: AlertingConfig) -> Optional[frozenset[str]]:
        """Types poussés : ceux de la config si elle en impose, sinon le défaut.

        `*` désactive le filtre — utile pour déboguer un canal, jamais en
        exploitation.
        """
        brut = (cfg.telegram_alert_types or "").strip()
        if not brut:
            return cls._TYPES_PAR_DEFAUT
        if brut == "*":
            return None
        return frozenset(t.strip() for t in brut.split(",") if t.strip())

    @staticmethod
    def _severite(alert: Alert) -> str:
        return alert.severity.value if hasattr(alert.severity, "value") else alert.severity

    @staticmethod
    def _echapper(texte: str) -> str:
        """Échappe ce que le mode HTML de Telegram interpréterait.

        Sans cela, un avis contenant « < » ou « & » — fréquent dans un message
        d'alerte qui cite un verbatim — fait rejeter tout l'envoi par l'API,
        pas seulement le caractère fautif.
        """
        return (
            (texte or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def send(self, alert: Alert) -> bool:
        if self._types is not None and alert.type not in self._types:
            return False

        severite = self._severite(alert)
        if self._RANG.get(severite, 0) < self._seuil:
            return False

        emoji = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(severite, "")
        corps = (
            f"{emoji} <b>{self._echapper(alert.title)}</b>\n"
            f"{self._echapper(alert.message)}"
        )

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self.cfg.telegram_chat_id,
                    "text": corps,
                    "parse_mode": "HTML",
                    # Une alerte n'a pas d'aperçu de lien à déplier : il
                    # occuperait la moitié de l'écran pour rien.
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code >= 400:
                # Le corps de la réponse porte la cause exacte — « chat not
                # found » quand le destinataire n'a jamais écrit au robot, par
                # exemple. La journaliser évite de chercher côté réseau.
                logger.warning(
                    "Envoi Telegram refusé (HTTP %s) : %s",
                    resp.status_code, resp.text[:200],
                )
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Envoi Telegram échoué : %s", e)
            return False


def build_notifiers(cfg: AlertingConfig) -> list[Notifier]:
    """Construit la liste des notifieurs actifs selon la configuration."""
    notifiers: list[Notifier] = [LogNotifier()]
    if cfg.smtp_host and cfg.alert_email:
        notifiers.append(EmailNotifier(cfg))
    if cfg.webhook_url:
        notifiers.append(WebhookNotifier(cfg))
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notifiers.append(TelegramNotifier(cfg))
    return notifiers
