"""
Package des implémentations de scrapers.
"""

from .trustpilot import TrustpilotScraper
from .playstore import PlayStoreScraper
from .appstore import AppStoreScraper
from .google_maps import GoogleMapsScraper
from .rss_feed import RSSFeedScraper

__all__ = [
    "TrustpilotScraper",
    "PlayStoreScraper",
    "AppStoreScraper",
    "GoogleMapsScraper",
    "RSSFeedScraper",
]