"""
Orchestration principale du pipeline.
Coordonne l'exécution séquentielle ou parallèle des scrapers.
"""

import logging
import uuid
from datetime import datetime
from database import db
from models import PipelineRun, ScraperResult
from monitoring import Monitor
from config import settings
from scrapers import (
    TrustpilotScraper,
    PlayStoreScraper,
    AppStoreScraper,
    GoogleMapsScraper,
    RSSFeedScraper,
)

logger = logging.getLogger(__name__)


class Pipeline:
    """Orchestrateur principal du pipeline."""
    
    # Mappage nom → classe
    SCRAPER_CLASSES = {
        "trustpilot": TrustpilotScraper,
        "playstore": PlayStoreScraper,
        "appstore": AppStoreScraper,
        "googlemaps": GoogleMapsScraper,
        "rss_feed": RSSFeedScraper,
    }
    
    def __init__(self):
        self.db = db
        self.monitor = Monitor()
        self.logger = logging.getLogger("pipeline")
    
    def run(self, dry_run: bool = False) -> PipelineRun:
        """
        Exécute le pipeline complet.
        
        Args:
            dry_run: Si True, ne pas insérer en BD
            
        Returns:
            Résultat de l'exécution
        """
        run_id = str(uuid.uuid4())
        
        self.logger.info(f"Démarrage du pipeline", extra={"run_id": run_id})
        
        # Enregistrer le début
        try:
            self.db.create_tables()  # Créer les tables si elles n'existent pas
            if not dry_run:
                self.db.start_run(run_id)
        except Exception as e:
            self.logger.error(f"Erreur initialisation BD : {e}")
            raise
        
        # Créer le résultat global
        run = PipelineRun(
            run_id=run_id,
            started_at=datetime.utcnow(),
            status="running",
        )
        
        try:
            # Récupérer les scrapers activés
            enabled_scrapers = settings.get_enabled_scrapers()
            self.logger.info(f"Scrapers activés : {enabled_scrapers}")
            
            if not enabled_scrapers:
                self.logger.warning("Aucun scraper activé")
                raise ValueError("Aucun scraper activé")
            
            # Exécuter chaque scraper
            total_inserted = 0
            total_duplicates = 0
            total_errors = 0
            
            for scraper_name in enabled_scrapers:
                try:
                    scraper_class = self.SCRAPER_CLASSES.get(scraper_name)
                    if not scraper_class:
                        self.logger.error(f"Scraper inconnu : {scraper_name}")
                        continue
                    
                    # Instantier et exécuter le scraper
                    scraper = scraper_class()
                    result = scraper.run(run_id, dry_run=dry_run)
                    
                    # Stocker le résultat
                    run.scraper_results[scraper_name] = result
                    total_inserted += result.inserted_count
                    total_duplicates += result.duplicate_count
                    total_errors += result.error_count
                    
                    # Vérifier la santé du scraper
                    alerts = self.monitor.check_scraper_health(result)
                    self.monitor.send_alerts(alerts)
                    
                except Exception as e:
                    self.logger.error(f"Erreur scraper {scraper_name} : {e}", exc_info=True)
                    run.scraper_results[scraper_name] = ScraperResult(
                        scraper_name=scraper_name,
                        reviews=[],
                        started_at=datetime.utcnow(),
                        ended_at=datetime.utcnow(),
                        status="failed",
                        error_message=str(e),
                    )
            
            # Résumer les résultats
            run.ended_at = datetime.utcnow()
            run.status = "success"
            run.total_reviews = total_inserted
            run.total_duplicates = total_duplicates
            run.total_errors = total_errors
            
            # Enregistrer la fin du run
            if not dry_run:
                self.db.end_run(run_id, run.status, run.model_dump(mode="json"))
            
            # Vérifier la santé globale
            alerts = self.monitor.check_run_health(run)
            self.monitor.send_alerts(alerts)
            
            self.logger.info(f"Pipeline terminée avec succès", extra={
                "run_id": run_id,
                "total_reviews": total_inserted,
                "total_duplicates": total_duplicates,
                "total_errors": total_errors,
                "duration": run.duration_seconds,
            })
            
            return run
            
        except Exception as e:
            self.logger.error(f"Erreur pipeline : {e}", exc_info=True)
            run.ended_at = datetime.utcnow()
            run.status = "failed"
            run.error_message = str(e)
            
            if not dry_run:
                self.db.end_run(run_id, "failed", {"error": str(e)})
            
            raise