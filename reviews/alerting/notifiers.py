"""
Canaux de notification des alertes : log, e-mail (SMTP), webhook (Slack/Discord).
Chaque notifieur est indépendant et ne fait jamais échouer le pipeline.
"""

import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText

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


def build_notifiers(cfg: AlertingConfig) -> list[Notifier]:
    """Construit la liste des notifieurs actifs selon la configuration."""
    notifiers: list[Notifier] = [LogNotifier()]
    if cfg.smtp_host and cfg.alert_email:
        notifiers.append(EmailNotifier(cfg))
    if cfg.webhook_url:
        notifiers.append(WebhookNotifier(cfg))
    return notifiers
