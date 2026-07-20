"""Tests pour le pipeline."""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime
from models import PipelineRun, ScraperResult
from pipeline import Pipeline


class TestPipeline:
    """Tests pour le pipeline."""
    
    @pytest.fixture
    def pipeline(self):
        return Pipeline()
    
    def test_pipeline_initialization(self, pipeline):
        """Test initialisation du pipeline."""
        assert pipeline.db is not None
        assert pipeline.monitor is not None
    
    def test_scraper_classes_mapping(self, pipeline):
        """Test que tous les scrapers sont enregistrés."""
        assert "playstore" in pipeline.SCRAPER_CLASSES
        assert "appstore" in pipeline.SCRAPER_CLASSES
        assert "trustpilot" in pipeline.SCRAPER_CLASSES
    
    @patch('database.db.start_run')
    @patch('database.db.end_run')
    def test_run_dry_mode(self, mock_end, mock_start, pipeline):
        """Test exécution en mode dry-run."""
        mock_start.return_value = {"run_id": "test-run"}
        
        # Désactiver tous les scrapers sauf un
        from config import settings
        settings.playstore.enabled = True
        settings.appstore.enabled = False
        settings.trustpilot.enabled = False
        
        # Mock la collecte
        with patch.object(Pipeline, 'run') as mock_run:
            mock_run.return_value = PipelineRun(
                run_id="test-run",
                started_at=datetime.utcnow(),
                status="success",
            )