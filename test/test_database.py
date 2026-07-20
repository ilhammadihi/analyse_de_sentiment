"""Tests pour la base de données."""

import pytest
from database import Database


@pytest.fixture
def db():
    """Fixture pour la base de données."""
    return Database()


def test_db_singleton():
    """Test que Database est un singleton."""
    db1 = Database()
    db2 = Database()
    assert db1 is db2


def test_connection_string():
    """Test génération de la connection string."""
    from config import settings
    
    conn_str = settings.database.connection_string()
    assert "postgresql://" in conn_str
    assert settings.database.user in conn_str