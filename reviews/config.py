"""
Configuration centralisée, chargée depuis l'environnement (.env) et validée
avec Pydantic v2.

Rien n'est lu au moment de l'import : la configuration n'est construite que
lors du premier appel à get_settings() (mise en cache). Aucune connexion
réseau/BD n'est ouverte ici.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = SettingsConfigDict(
    env_file=".env", env_prefix="", extra="ignore", populate_by_name=True
)


class DatabaseConfig(BaseSettings):
    """Configuration PostgreSQL."""
    model_config = _ENV
    host: str = Field(default="localhost", validation_alias="DB_HOST")
    port: int = Field(default=5432, validation_alias="DB_PORT")
    name: str = Field(default="telecom_db", validation_alias="DB_NAME")
    user: str = Field(default="telecom_user", validation_alias="DB_USER")
    password: str = Field(default="telecom_password", validation_alias="DB_PASSWORD")
    ssl_mode: str = Field(default="prefer", validation_alias="DB_SSL_MODE")
    pool_size: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    max_overflow: int = Field(default=10, validation_alias="DB_MAX_OVERFLOW")

    def connection_string(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.name}?sslmode={self.ssl_mode}"
        )


class LoggingConfig(BaseSettings):
    model_config = _ENV
    level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    file: str = Field(default="data/logs/pipeline.log", validation_alias="LOG_FILE")
    format: str = Field(default="json", validation_alias="LOG_FORMAT")  # json | text


class ScrapingConfig(BaseSettings):
    model_config = _ENV
    request_timeout: int = Field(default=30, validation_alias="REQUEST_TIMEOUT")
    retry_max_attempts: int = Field(default=3, validation_alias="RETRY_MAX_ATTEMPTS")
    retry_backoff_factor: float = Field(default=2, validation_alias="RETRY_BACKOFF_FACTOR")
    retry_backoff_max: int = Field(default=120, validation_alias="RETRY_BACKOFF_MAX")


class TrustpilotConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_TRUSTPILOT")
    cache_path: str = Field(default="data/state/tp_state.json", validation_alias="TRUSTPILOT_CACHE_PATH")
    max_pages: int = Field(default=10, validation_alias="TRUSTPILOT_MAX_PAGES")


class PlayStoreConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_PLAYSTORE")
    retry_strategies: int = Field(default=3, validation_alias="PLAYSTORE_RETRY_STRATEGIES")


class AppStoreConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_APPSTORE")
    max_pages: int = Field(default=5, validation_alias="APPSTORE_MAX_PAGES")
    fallback_country: str = Field(default="fr", validation_alias="APPSTORE_FALLBACK_COUNTRY")


class GoogleMapsConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=False, validation_alias="ENABLE_GOOGLEMAPS")
    max_reviews: int = Field(default=300, validation_alias="GOOGLEMAPS_MAX_REVIEWS")
    headless: bool = Field(default=True, validation_alias="GOOGLEMAPS_HEADLESS")


class RSSFeedConfig(BaseSettings):
    model_config = _ENV
    enabled: bool = Field(default=False, validation_alias="ENABLE_RSS_FEED")


class SchedulerConfig(BaseSettings):
    """Planification du pipeline (APScheduler)."""
    model_config = _ENV
    enabled: bool = Field(default=False, validation_alias="ENABLE_SCHEDULER")
    interval_minutes: int = Field(default=60, validation_alias="SCHEDULER_INTERVAL_MINUTES")
    run_on_start: bool = Field(default=True, validation_alias="SCHEDULER_RUN_ON_START")
    timezone: str = Field(default="Africa/Casablanca", validation_alias="SCHEDULER_TIMEZONE")


class AlertingConfig(BaseSettings):
    """Alerting temps réel : seuils + canaux de notification."""
    model_config = _ENV
    enabled: bool = Field(default=True, validation_alias="ENABLE_MONITORING")
    alert_zero_reviews: bool = Field(default=True, validation_alias="ALERT_ZERO_REVIEWS")

    # Seuils métier
    negative_ratio_threshold: float = Field(default=0.5, validation_alias="ALERT_NEGATIVE_RATIO")
    min_reviews_for_ratio: int = Field(default=10, validation_alias="ALERT_MIN_REVIEWS_FOR_RATIO")

    # Canal e-mail (SMTP)
    smtp_host: Optional[str] = Field(default=None, validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=587, validation_alias="SMTP_PORT")
    smtp_user: Optional[str] = Field(default=None, validation_alias="SMTP_USER")
    smtp_password: Optional[str] = Field(default=None, validation_alias="SMTP_PASSWORD")
    smtp_from: Optional[str] = Field(default=None, validation_alias="SMTP_FROM")
    alert_email: Optional[str] = Field(default=None, validation_alias="ALERT_EMAIL")

    # Canal webhook (Slack / Discord / Teams)
    webhook_url: Optional[str] = Field(default=None, validation_alias="ALERT_WEBHOOK_URL")


class APIConfig(BaseSettings):
    """Configuration du service API (FastAPI)."""
    model_config = _ENV
    host: str = Field(default="0.0.0.0", validation_alias="API_HOST")
    port: int = Field(default=8000, validation_alias="API_PORT")
    cors_origins: str = Field(default="*", validation_alias="API_CORS_ORIGINS")  # CSV

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class Settings(BaseSettings):
    """Configuration globale de l'application."""

    model_config = _ENV

    debug: bool = Field(default=False, validation_alias="DEBUG")
    base_dir: Path = Path(__file__).resolve().parent.parent
    data_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "data"
    )

    # Sous-configurations (default_factory => rien n'est lu tant que Settings
    # n'est pas construit)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    trustpilot: TrustpilotConfig = Field(default_factory=TrustpilotConfig)
    playstore: PlayStoreConfig = Field(default_factory=PlayStoreConfig)
    appstore: AppStoreConfig = Field(default_factory=AppStoreConfig)
    googlemaps: GoogleMapsConfig = Field(default_factory=GoogleMapsConfig)
    rss_feed: RSSFeedConfig = Field(default_factory=RSSFeedConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    api: APIConfig = Field(default_factory=APIConfig)

    @field_validator("data_dir", mode="before")
    @classmethod
    def create_data_dir(cls, v):
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        (path / "state").mkdir(exist_ok=True)
        (path / "logs").mkdir(exist_ok=True)
        return path

    def get_enabled_scrapers(self) -> list[str]:
        mapping = {
            "trustpilot": self.trustpilot.enabled,
            "playstore": self.playstore.enabled,
            "appstore": self.appstore.enabled,
            "googlemaps": self.googlemaps.enabled,
            "rss_feed": self.rss_feed.enabled,
        }
        return [name for name, enabled in mapping.items() if enabled]


@lru_cache
def get_settings() -> Settings:
    """Retourne l'instance unique de configuration (construite à la demande)."""
    return Settings()
