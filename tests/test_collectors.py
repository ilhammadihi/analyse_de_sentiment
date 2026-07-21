"""Tests de parsing des collecteurs (sans appel réseau)."""

from reviews.collectors.playstore import PlayStoreScraper
from reviews.collectors.appstore import AppStoreScraper
from reviews.collectors.google_maps import GoogleMapsScraper


class TestPlayStore:
    def test_init(self):
        s = PlayStoreScraper()
        assert s.name == "playstore"
        assert s.retry_config is not None

    def test_parse_reviews(self):
        raw = [{"reviewId": "test-1", "content": "Bon app", "score": 5,
                "userName": "User1", "at": "2024-01-15T10:30:00Z"}]
        parsed = PlayStoreScraper()._parse_reviews(raw, {"name": "Test App"})
        assert len(parsed) == 1
        assert parsed[0].id == "test-1"
        assert parsed[0].text == "Bon app"
        assert parsed[0].rating == 5


class TestAppStore:
    def test_init(self):
        s = AppStoreScraper()
        assert s.name == "appstore"
        assert len(s.APPS) == 4


class TestGoogleMaps:
    def test_parse_rating(self):
        s = GoogleMapsScraper()
        assert s._parse_rating("Rated 4 stars out of five stars") == 4
        assert s._parse_rating(None) is None
