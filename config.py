"""
Configuration centralisée du pipeline.
Chargée depuis les variables d'environnement et validée avec Pydantic.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic import Field, validator
from pydantic_settings import BaseSettings
import logging

logger = logging.getLogger(__name__)


class DatabaseConfig(BaseSettings):
    """Configuration PostgreSQL."""
    host: str = Field(default="localhost", validation_alias="DB_HOST")
    port: int = Field(default=5432, validation_alias="DB_PORT")
    name: str = Field(default="telecom_db", validation_alias="DB_NAME")
    user: str = Field(default="telecom_user", validation_alias="DB_USER")
    password: str = Field(default="telecom_password", validation_alias="DB_PASSWORD")
    ssl_mode: str = Field(default="prefer", validation_alias="DB_SSL_MODE")
    pool_size: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    max_overflow: int = Field(default=10, validation_alias="DB_MAX_OVERFLOW")

    def connection_string(self) -> str:
        """Génère la chaîne de connexion PostgreSQL."""
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.name}?"
            f"sslmode={self.ssl_mode}"
        )

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"


class LoggingConfig(BaseSettings):
    """Configuration du logging."""
    level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    file: str = Field(default="data/logs/pipeline.log", validation_alias="LOG_FILE")
    format: str = Field(default="json", validation_alias="LOG_FORMAT")  # json ou text

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"


class ScrapingConfig(BaseSettings):
    """Configuration générale du scraping."""
    request_timeout: int = Field(default=30, validation_alias="REQUEST_TIMEOUT")
    retry_max_attempts: int = Field(default=3, validation_alias="RETRY_MAX_ATTEMPTS")
    retry_backoff_factor: float = Field(default=2, validation_alias="RETRY_BACKOFF_FACTOR")
    retry_backoff_max: int = Field(default=120, validation_alias="RETRY_BACKOFF_MAX")

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"


class TrustpilotConfig(BaseSettings):
    """Configuration Trustpilot."""
    enabled: bool = Field(default=True, validation_alias="ENABLE_TRUSTPILOT")
    cache_path: str = Field(default="data/state/_tp_state.json", validation_alias="TRUSTPILOT_CACHE_PATH")
    max_pages: int = Field(default=10, validation_alias="TRUSTPILOT_MAX_PAGES")

    class Config:
        env_file = ".env"
        extra = "ignore"


class PlayStoreConfig(BaseSettings):
    """Configuration Google Play Store."""
    enabled: bool = Field(default=True, validation_alias="ENABLE_PLAYSTORE")
    retry_strategies: int = Field(default=3, validation_alias="PLAYSTORE_RETRY_STRATEGIES")

    class Config:
        env_file = ".env"
        extra = "ignore"


class AppStoreConfig(BaseSettings):
    """Configuration Apple App Store."""
    enabled: bool = Field(default=True, validation_alias="ENABLE_APPSTORE")
    max_pages: int = Field(default=5, validation_alias="APPSTORE_MAX_PAGES")
    fallback_country: str = Field(default="fr", validation_alias="APPSTORE_FALLBACK_COUNTRY")

    class Config:
        env_file = ".env"
        extra = "ignore"


class GoogleMapsConfig(BaseSettings):
    """Configuration Google Maps."""
    enabled: bool = Field(default=False, validation_alias="ENABLE_GOOGLEMAPS")
    max_reviews: int = Field(default=300, validation_alias="GOOGLEMAPS_MAX_REVIEWS")
    headless: bool = Field(default=True, validation_alias="GOOGLEMAPS_HEADLESS")

    class Config:
        env_file = ".env"
        extra = "ignore"


class RSSFeedConfig(BaseSettings):
    """Configuration RSS Feed."""
    enabled: bool = Field(default=False, validation_alias="ENABLE_RSS_FEED")

    class Config:
        env_file = ".env"
        extra = "ignore"


class SchedulerConfig(BaseSettings):
    """Configuration du scheduler."""
    enabled: bool = Field(default=False, validation_alias="ENABLE_SCHEDULER")
    hour: int = Field(default=2, validation_alias="SCHEDULER_HOUR")
    minute: int = Field(default=0, validation_alias="SCHEDULER_MINUTE")
    timezone: str = Field(default="Africa/Casablanca", validation_alias="SCHEDULER_TIMEZONE")

    class Config:
        env_file = ".env"
        extra = "ignore"


class MonitoringConfig(BaseSettings):
    """Configuration du monitoring."""
    enabled: bool = Field(default=True, validation_alias="ENABLE_MONITORING")
    alert_zero_reviews: bool = Field(default=True, validation_alias="ALERT_THRESHOLD_ZERO_REVIEWS")
    alert_email: Optional[str] = Field(default=None, validation_alias="ALERT_EMAIL")

    class Config:
        env_file = ".env"
        extra = "ignore"


class Settings(BaseSettings):
    """Configuration globale de l'application."""

    # Mode
    debug: bool = Field(default=False)

    # Chemins (config.py est à la racine du projet)
    base_dir: Path = Path(__file__).resolve().parent
    data_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parent / "data")
    
    # Sous-configurations
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    scraping: ScrapingConfig = ScrapingConfig()
    trustpilot: TrustpilotConfig = TrustpilotConfig()
    playstore: PlayStoreConfig = PlayStoreConfig()
    appstore: AppStoreConfig = AppStoreConfig()
    googlemaps: GoogleMapsConfig = GoogleMapsConfig()
    rss_feed: RSSFeedConfig = RSSFeedConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    monitoring: MonitoringConfig = MonitoringConfig()

    @validator("data_dir", pre=True, always=True)
    def create_data_dir(cls, v):
        """Crée le dossier data s'il n'existe pas."""
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        (path / "state").mkdir(exist_ok=True)
        (path / "logs").mkdir(exist_ok=True)
        return path

    def get_enabled_scrapers(self) -> list[str]:
        """Retourne la liste des scrapers activés."""
        scrapers = []
        if self.trustpilot.enabled:
            scrapers.append("trustpilot")
        if self.playstore.enabled:
            scrapers.append("playstore")
        if self.appstore.enabled:
            scrapers.append("appstore")
        if self.googlemaps.enabled:
            scrapers.append("googlemaps")
        if self.rss_feed.enabled:
            scrapers.append("rss_feed")
        return scrapers

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"


# Instance globale (singleton)
settings = Settings()