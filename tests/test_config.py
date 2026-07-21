"""Tests de configuration (sans connexion)."""

import os

from reviews.config import Settings, get_settings


def test_defaults():
    s = Settings()
    assert s.database.port == 5432
    assert "postgresql://" in s.database.connection_string()
    assert s.database.user in s.database.connection_string()


def test_env_override(monkeypatch):
    monkeypatch.setenv("DB_HOST", "test.example.com")
    monkeypatch.setenv("DB_PORT", "9999")
    s = Settings()
    assert s.database.host == "test.example.com"
    assert s.database.port == 9999


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_enabled_scrapers_list():
    enabled = Settings().get_enabled_scrapers()
    assert isinstance(enabled, list)
