"""
Point d'entrée principal du pipeline.
Gestion des arguments CLI et démarrage.
"""

import sys
import logging
import argparse
from pathlib import Path

# Setup du path
sys.path.insert(0, str(Path(__file__).parent))

from logger import setup_logging, get_logger
from config import settings
from database import db
from pipeline import Pipeline

logger = get_logger("main")


def main():
    """Point d'entrée principal."""
    
    # Parser des arguments CLI
    parser = argparse.ArgumentParser(
        description="Pipeline de collecte d'avis clients",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Exécuter le pipeline immédiatement",
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simuler l'exécution sans insérer en BD",
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Afficher les logs en détail (DEBUG)",
    )
    
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialiser la base de données",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    if args.verbose:
        import os
        os.environ["LOG_LEVEL"] = "DEBUG"
        settings.logging.level = "DEBUG"
    
    setup_logging()
    logger.info("Pipeline d'avis clients démarrée")
    
    try:
        # Initialiser la BD si demandé
        if args.init_db:
            logger.info("Initialisation de la base de données...")
            db.create_tables()
            logger.info("✓ Base de données initialisée")
            return 0
        
        # Exécuter le pipeline
        if args.run_now:
            logger.info("Exécution du pipeline")
            pipeline = Pipeline()
            result = pipeline.run(dry_run=args.dry_run)
            
            # Afficher le résumé
            print("\n" + "="*60)
            print("RÉSUMÉ DE L'EXÉCUTION")
            print("="*60)
            print(f"Run ID : {result.run_id}")
            print(f"Status : {result.status}")
            print(f"Total avis : {result.total_reviews}")
            print(f"Doublons : {result.total_duplicates}")
            print(f"Erreurs : {result.total_errors}")
            print(f"Durée : {result.duration_seconds:.2f}s")
            print("="*60 + "\n")
            
            # Afficher les détails par scraper
            for scraper_name, scraper_result in result.scraper_results.items():
                print(f"\n[{scraper_name.upper()}]")
                print(f"  Status: {scraper_result.status}")
                print(f"  Insérés: {scraper_result.inserted_count}")
                print(f"  Doublons: {scraper_result.duplicate_count}")
                print(f"  Erreurs: {scraper_result.error_count}")
                print(f"  Durée: {scraper_result.duration_seconds:.2f}s")
                if scraper_result.error_message:
                    print(f"  Erreur: {scraper_result.error_message}")
            
            return 0 if result.status == "success" else 1
        
        else:
            # Mode scheduler (TODO : implémenter avec APScheduler)
            logger.warning("Mode scheduler non implémenté")
            print("Utiliser --run-now pour exécuter immédiatement")
            print("Utiliser --help pour voir les options")
            return 0
    
    except KeyboardInterrupt:
        logger.info("Pipeline interrompue par l'utilisateur")
        return 130
    except Exception as e:
        logger.error(f"Erreur fatale : {e}", exc_info=True)
        return 1
    finally:
        db.close_all()


if __name__ == "__main__":
    sys.exit(main())