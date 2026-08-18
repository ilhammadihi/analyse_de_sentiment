"""Collecteurs : une classe par source. Registre nom → classe."""

from reviews.collectors.base import BaseCollector
from reviews.collectors.trustpilot import TrustpilotScraper
from reviews.collectors.playstore import PlayStoreScraper
from reviews.collectors.appstore import AppStoreScraper
from reviews.collectors.google_maps import GoogleMapsScraper
from reviews.collectors.rss_feed import RSSFeedScraper
from reviews.collectors.hellopeter import HelloPeterScraper
from reviews.collectors.gdelt import GDELTScraper
from reviews.collectors.press_feed import PressFeedScraper
from reviews.collectors.reddit import RedditScraper

# Registre utilisé par le pipeline pour instancier les collecteurs activés.
# Les clés doivent correspondre EXACTEMENT à celles de
# Settings.get_enabled_scrapers() : une clé qui n'existe que d'un côté donne
# un collecteur soit jamais lancé, soit « inconnu » à l'exécution.
COLLECTORS: dict[str, type[BaseCollector]] = {
    "trustpilot": TrustpilotScraper,
    "playstore": PlayStoreScraper,
    "appstore": AppStoreScraper,
    "googlemaps": GoogleMapsScraper,
    "rss_feed": RSSFeedScraper,
    "hellopeter": HelloPeterScraper,
    "gdelt": GDELTScraper,
    "press_feed": PressFeedScraper,
    "reddit": RedditScraper,
}

__all__ = [
    "BaseCollector",
    "TrustpilotScraper",
    "PlayStoreScraper",
    "AppStoreScraper",
    "GoogleMapsScraper",
    "RSSFeedScraper",
    "HelloPeterScraper",
    "GDELTScraper",
    "PressFeedScraper",
    "RedditScraper",
    "COLLECTORS",
]
