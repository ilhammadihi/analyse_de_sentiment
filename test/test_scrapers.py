"""Tests pour les scrapers."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from models import Review, SourceEnum
from scrapers.playstore import PlayStoreScraper
from scrapers.appstore import AppStoreScraper


class TestPlayStoreScraper:
    """Tests pour PlayStore scraper."""
    
    @pytest.fixture
    def scraper(self):
        return PlayStoreScraper()
    
    def test_scraper_initialization(self, scraper):
        """Test initialisation du scraper."""
        assert scraper.name == "playstore"
        assert scraper.retry_config is not None
    
    @patch('google_play_scraper.reviews')
    def test_parse_reviews(self, mock_reviews, scraper):
        """Test parsing des avis."""
        raw = [
            {
                "reviewId": "test-1",
                "content": "Bon app",
                "score": 5,
                "userName": "User1",
                "at": "2024-01-15T10:30:00Z"
            }
        ]
        
        parsed = scraper._parse_reviews(raw, "Test App")
        
        assert len(parsed) == 1
        assert parsed[0].id == "test-1"
        assert parsed[0].text == "Bon app"
        assert parsed[0].rating == 5


class TestAppStoreScraper:
    """Tests pour App Store scraper."""
    
    @pytest.fixture
    def scraper(self):
        return AppStoreScraper()
    
    def test_scraper_initialization(self, scraper):
        """Test initialisation du scraper."""
        assert scraper.name == "appstore"
        assert len(scraper.APPS) == 4
    
    def test_parse_rating(self, scraper):
        """Test parsing de la note."""
        rating = scraper._parse_rating("Rated 4 stars out of five stars")
        assert rating == 4
    
    def test_parse_rating_none(self, scraper):
        """Test parsing de rating None."""
        rating = scraper._parse_rating(None)
        assert rating is None