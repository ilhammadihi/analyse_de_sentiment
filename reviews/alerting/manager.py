"""
Gestionnaire d'alerting : évalue les règles, persiste les alertes et les
notifie sur les canaux configurés. Point d'entrée unique pour le pipeline.
"""

import logging
from typing import Optional

from reviews.config import AlertingConfig
from reviews.domain.models import PipelineRun, Alert
from reviews.alerting import rules
from reviews.alerting.notifiers import Notifier, build_notifiers
from reviews.storage.repository import AlertRepository

logger = logging.getLogger(__name__)


class AlertManager:
    """Orchestre règles → persistance → notification."""

    def __init__(
        self,
        cfg: AlertingConfig,
        alert_repo: Optional[AlertRepository] = None,
        notifiers: Optional[list[Notifier]] = None,
    ):
        self.cfg = cfg
        self.alert_repo = alert_repo
        self.notifiers = notifiers if notifiers is not None else build_notifiers(cfg)

    def process(self, run: PipelineRun) -> list[Alert]:
        """Évalue les alertes d'un run, les notifie et les persiste."""
        alerts = rules.evaluate(run, self.cfg)
        for alert in alerts:
            channels = [n.name for n in self.notifiers if n.send(alert)]
            if self.alert_repo is not None:
                try:
                    self.alert_repo.insert(alert, notified=channels)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Persistance alerte échouée : %s", e)
        if alerts:
            logger.info("%d alerte(s) déclenchée(s) pour le run %s",
                        len(alerts), run.run_id)
        return alerts
