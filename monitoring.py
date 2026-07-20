"""
Monitoring et alertes pour le pipeline.
Détecte les anomalies et déclenche des notifications.
"""

import logging
from typing import Optional
from datetime import datetime
from dataclasses import dataclass
from config import settings
from models import PipelineRun, ScraperResult

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """Représente une alerte."""
    severity: str  # 'info', 'warning', 'error'
    title: str
    message: str
    timestamp: datetime


class Monitor:
    """Monitore les exécutions du pipeline."""
    
    def __init__(self):
        self.alerts: list[Alert] = []
    
    def check_run_health(self, run: PipelineRun) -> list[Alert]:
        """
        Vérifie la santé d'une exécution.
        
        Args:
            run: Exécution à vérifier
            
        Returns:
            Liste des alertes détectées
        """
        self.alerts = []
        
        if not settings.monitoring.enabled:
            return self.alerts
        
        # Alerte si aucun avis collecté
        if settings.monitoring.alert_zero_reviews and run.total_reviews == 0:
            self.alerts.append(Alert(
                severity="error",
                title="Aucun avis collecté",
                message=f"Le run {run.run_id} n'a collecté aucun avis",
                timestamp=datetime.utcnow(),
            ))
        
        # Alerte si trop de doublons
        if run.total_reviews > 0 and run.total_duplicates > run.total_reviews * 0.5:
            self.alerts.append(Alert(
                severity="warning",
                title="Taux de doublons élevé",
                message=f"{run.total_duplicates}/{run.total_reviews} doublons ({run.total_duplicates/run.total_reviews*100:.1f}%)",
                timestamp=datetime.utcnow(),
            ))
        elif run.total_reviews == 0 and run.total_duplicates > 0:
            self.alerts.append(Alert(
                severity="warning",
                title="Taux de doublons élevé",
                message=f"{run.total_duplicates} doublons, 0 nouvel avis inséré (100% doublons)",
                timestamp=datetime.utcnow(),
            ))
        
        # Alerte si trop d'erreurs
        if run.total_errors > 0:
            self.alerts.append(Alert(
                severity="warning",
                title="Erreurs de collecte",
                message=f"{run.total_errors} erreurs pendant la collecte",
                timestamp=datetime.utcnow(),
            ))
        
        # Alerte si le run échoue
        if run.status == "failed":
            self.alerts.append(Alert(
                severity="error",
                title="Run échouée",
                message=f"Le run {run.run_id} a échoué : {run.error_message}",
                timestamp=datetime.utcnow(),
            ))
        
        # Alerte si durée anormale
        if run.duration_seconds and run.duration_seconds > 3600:  # 1 heure
            self.alerts.append(Alert(
                severity="warning",
                title="Durée d'exécution anormale",
                message=f"Le run a pris {run.duration_seconds/60:.1f} minutes",
                timestamp=datetime.utcnow(),
            ))
        
        return self.alerts
    
    def check_scraper_health(self, result: ScraperResult) -> list[Alert]:
        """Vérifie la santé d'un scraper."""
        alerts = []
        
        # Alerte si scraper échoue
        if result.status == "failed":
            alerts.append(Alert(
                severity="error",
                title=f"Scraper {result.scraper_name} échouée",
                message=result.error_message or "Erreur inconnue",
                timestamp=datetime.utcnow(),
            ))
        
        # Alerte si peu d'avis collectés
        if result.inserted_count == 0 and result.status == "success":
            alerts.append(Alert(
                severity="warning",
                title=f"{result.scraper_name} : zéro avis",
                message="Aucun avis collecté",
                timestamp=datetime.utcnow(),
            ))
        
        return alerts
    
    def send_alerts(self, alerts: list[Alert]):
        """Envoie les alertes (log, email, etc.)."""
        for alert in alerts:
            level_map = {
                "info": logging.INFO,
                "warning": logging.WARNING,
                "error": logging.ERROR,
            }
            
            logger.log(
                level_map.get(alert.severity, logging.INFO),
                f"[{alert.severity.upper()}] {alert.title}",
                extra={
                    "alert_title": alert.title,
                    "alert_message": alert.message,
                    "alert_severity": alert.severity,
                }
            )
            
            # TODO: Implémenter l'envoi d'email si configuré
            # if settings.monitoring.alert_email:
            #     send_email(settings.monitoring.alert_email, alert)