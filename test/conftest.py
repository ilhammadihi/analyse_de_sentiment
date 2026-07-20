"""Tests pour la configuration."""

import pytest
import os
from pathlib import Path
from config import Settings


def test_settings_load():
    """Test chargement des settings."""
    settings = Settings()
    
    assert settings.database.host == "localhost"
    assert settings.database.port == 5432


def test_settings_from_env():
    """Test chargement depuis variables d'env."""
    os.environ["DB_HOST"] = "test.example.com"
    os.environ["DB_PORT"] = "9999"
    
    settings = Settings()
    
    assert settings.database.host == "test.example.com"
    assert settings.database.port == 9999
    
    # Cleanup
    del os.environ["DB_HOST"]
    del os.environ["DB_PORT"]


def test_enabled_scrapers():
    """Test la récupération des scrapers activés."""
    from config import settings
    
    enabled = settings.get_enabled_scrapers()
    
    # Au minimum Playstore et AppStore doivent être activés par défaut
    assert "playstore" in enabled or "appstore" in enabled