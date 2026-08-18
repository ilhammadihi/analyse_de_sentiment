"""Collecte incrémentale : ne pas re-proposer ce qui est déjà en base."""

from datetime import datetime, timedelta, timezone

import pytest

from reviews.collectors.base import BaseCollector
from reviews.domain.models import Review, SourceEnum


class _FakeCollector(BaseCollector):
    """Collecteur de test : renvoie les avis qu'on lui donne."""

    USES_THREAD_TIMEOUT = False

    def __init__(self, reviews):
        # Nom volontairement DIFFÉRENT de la valeur SourceEnum ("app_store") :
        # c'est le cas réel des collecteurs, et la 1re version du filtre s'y
        # trompait — elle cherchait le repère avec le nom du collecteur et ne
        # le trouvait jamais, désactivant l'incrémental sans aucune erreur.
        super().__init__("appstore")
        self._reviews = reviews

    def collect(self):
        return self._reviews


def _review(company: str, created_at: datetime, rid: str = "x") -> Review:
    return Review(
        id=rid,
        company=company,
        source=SourceEnum.APP_STORE,
        text="Un avis de test suffisamment long",
        created_at=created_at,
    )


NOW = datetime(2026, 7, 26, 12, 0, 0)


def test_sans_repere_tout_est_collecte():
    """Sans repère (1er run, ou base vide), on ne filtre rien."""
    reviews = [_review("Orange Mali", NOW - timedelta(days=30), "a")]
    result = _FakeCollector(reviews).run()

    assert result.status == "success"
    assert len(result.reviews) == 1


def test_les_avis_anterieurs_au_repere_sont_ecartes():
    old = _review("Orange Mali", NOW - timedelta(days=30), "vieux")
    recent = _review("Orange Mali", NOW, "recent")

    collector = _FakeCollector([old, recent])
    collector.since = {("Orange Mali", "app_store", None): NOW - timedelta(days=10)}
    result = collector.run()

    assert [r.id for r in result.reviews] == ["recent"]


def test_la_marge_de_securite_rattrape_les_avis_indexes_en_retard():
    """Une source peut indexer aujourd'hui un avis publié avant le repère.

    Sans marge, cet avis serait définitivement perdu ; la dédup en base
    absorbe sans coût ceux qui seraient re-proposés inutilement.
    """
    juste_avant = _review("Orange Mali", NOW - timedelta(days=1), "retardataire")

    collector = _FakeCollector([juste_avant])
    collector.since = {("Orange Mali", "app_store", None): NOW}
    result = collector.run()

    assert [r.id for r in result.reviews] == ["retardataire"]


def test_le_repere_est_isole_par_entreprise():
    """Le repère d'une filiale ne doit pas filtrer les avis d'une autre."""
    autre = _review("MTN Ghana", NOW - timedelta(days=30), "autre-filiale")

    collector = _FakeCollector([autre])
    collector.since = {("Orange Mali", "app_store", None): NOW}
    result = collector.run()

    assert [r.id for r in result.reviews] == ["autre-filiale"]


def test_dates_avec_et_sans_fuseau_ne_font_pas_planter():
    """La base renvoie des dates avec fuseau, les collecteurs parfois sans.

    Régression : comparer les deux lève TypeError et ferait échouer tout le run.
    """
    naive = _review("Orange Mali", NOW - timedelta(days=30), "naive")

    collector = _FakeCollector([naive])
    collector.since = {
        ("Orange Mali", "app_store", None): datetime(2026, 7, 20, tzinfo=timezone.utc)
    }
    result = collector.run()

    assert result.status == "success"
    assert result.reviews == []
