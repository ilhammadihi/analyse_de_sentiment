"""Collecteurs : une classe par source. Registre nom → classe."""

from reviews.collectors.base import BaseCollector
from reviews.collectors.trustpilot import TrustpilotScraper
from reviews.collectors.playstore import PlayStoreScraper
from reviews.collectors.appstore import AppStoreScraper
from reviews.collectors.google_maps import GoogleMapsScraper
from reviews.collectors.rss_feed import RSSFeedScraper

# Registre utilisé par le pipeline pour instancier les collecteurs activés.
COLLECTORS: dict[str, type[BaseCollector]] = {
    "trustpilot": TrustpilotScraper,
    "playstore": PlayStoreScraper,
    "appstore": AppStoreScraper,
    "googlemaps": GoogleMapsScraper,
    "rss_feed": RSSFeedScraper,
}

__all__ = [
    "BaseCollector",
    "TrustpilotScraper",
    "PlayStoreScraper",
    "AppStoreScraper",
    "GoogleMapsScraper",
    "RSSFeedScraper",
    "COLLECTORS",
]
