"""
Tests de l'ARRÊT ANTICIPÉ de la pagination (collecte incrémentale).

Ce que ces tests protègent : le collecteur cesse de télécharger dès qu'un lot
est entièrement déjà en base. C'est une optimisation, donc le risque n'est pas
qu'elle échoue bruyamment mais qu'elle s'arrête TROP TÔT et fasse perdre des
avis en silence — un défaut qu'aucune erreur ne signalerait, et que seul un
comptage a posteriori révélerait.

Purs : aucune base, aucun réseau.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reviews.collectors.appstore import AppStoreScraper
from reviews.collectors.base import BaseCollector
from reviews.domain.models import Review, SourceEnum

MAINTENANT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _collecteur(repere=None):
    """AppStoreScraper avec un repère incrémental injecté, sans I/O."""
    c = AppStoreScraper()
    c.since = {("Orange Mali", "app_store", None): repere} if repere else {}
    return c


# ---------------------------------------------------------------------------
# La règle de comparaison
# ---------------------------------------------------------------------------


def test_marge_de_securite_appliquee_au_repere():
    """Le repère est reculé de 2 jours avant d'être utilisé.

    Les sources n'indexent pas toujours dans l'ordre chronologique : un avis
    publié hier peut n'apparaître qu'aujourd'hui. Sans cette marge il serait
    écarté définitivement, car le repère aurait déjà dépassé sa date.
    """
    c = _collecteur(MAINTENANT)
    cutoff = c.cutoff_for_key("Orange Mali", "app_store")
    assert cutoff == MAINTENANT - timedelta(days=2)


def test_aucun_repere_pour_une_filiale_inconnue():
    """Première collecte d'une filiale : aucun repère, donc aucun arrêt."""
    c = _collecteur(MAINTENANT)
    assert c.cutoff_for_key("MTN Zambie", "app_store") is None


def test_comparaison_tolerante_aux_fuseaux():
    """La base renvoie des dates avec fuseau, les collecteurs parfois sans.

    Une comparaison naïve lèverait un TypeError en pleine collecte.
    """
    aware = datetime(2026, 7, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 7, 1)
    assert BaseCollector.is_already_known(naive, aware) is True
    assert BaseCollector.is_already_known(aware, naive) is True


@pytest.mark.parametrize("created,attendu", [
    (MAINTENANT - timedelta(days=10), True),   # bien avant le repère
    (MAINTENANT - timedelta(days=3), True),    # avant, marge comprise
    (MAINTENANT - timedelta(days=1), False),   # dans la marge : à reprendre
    (MAINTENANT, False),                       # postérieur
    (None, False),                             # date absente : jamais écarté
])
def test_deja_connu(created, attendu):
    cutoff = MAINTENANT - timedelta(days=2)
    assert BaseCollector.is_already_known(created, cutoff) is attendu


# ---------------------------------------------------------------------------
# La décision d'arrêt
# ---------------------------------------------------------------------------


def test_arret_si_le_lot_est_entierement_connu():
    c = _collecteur(MAINTENANT)
    cutoff = c.cutoff_for_key("Orange Mali", "app_store")
    vieux = [MAINTENANT - timedelta(days=d) for d in (5, 8, 12)]
    assert c.batch_fully_known(vieux, cutoff) is True


def test_pas_d_arret_si_un_seul_avis_est_nouveau():
    """UN avis récent suffit à poursuivre la pagination.

    Les sources republient parfois un avis plus bas dans la liste. S'arrêter au
    premier avis connu ferait perdre tous ceux qui le suivent — c'est
    précisément le mode de défaillance silencieuse qu'on veut éviter.
    """
    c = _collecteur(MAINTENANT)
    cutoff = c.cutoff_for_key("Orange Mali", "app_store")
    melange = [MAINTENANT - timedelta(days=8), MAINTENANT, MAINTENANT - timedelta(days=9)]
    assert c.batch_fully_known(melange, cutoff) is False


def test_jamais_d_arret_sans_repere():
    """Première collecte : on doit descendre aussi loin que permis."""
    c = _collecteur()
    vieux = [MAINTENANT - timedelta(days=d) for d in (100, 200)]
    assert c.batch_fully_known(vieux, None) is False


def test_jamais_d_arret_sur_un_lot_vide():
    """Un lot vide ne prouve rien sur la suite : la fin de pagination est
    décidée par la source, pas par cette règle."""
    c = _collecteur(MAINTENANT)
    cutoff = c.cutoff_for_key("Orange Mali", "app_store")
    assert c.batch_fully_known([], cutoff) is False


# ---------------------------------------------------------------------------
# Le filet de sécurité final
# ---------------------------------------------------------------------------


def test_le_filtre_final_reste_actif_apres_un_arret():
    """L'arrêt anticipé évite de télécharger, il ne trie pas.

    Le dernier lot récupéré contient forcément des avis déjà connus — c'est
    même ce qui a déclenché l'arrêt. Le filtre final doit donc continuer à les
    écarter avant insertion.
    """
    c = _collecteur(MAINTENANT)
    avis = [
        Review(id="ancien", company="Orange Mali", source=SourceEnum.APP_STORE,
               text="deja en base", created_at=MAINTENANT - timedelta(days=30)),
        Review(id="nouveau", company="Orange Mali", source=SourceEnum.APP_STORE,
               text="a inserer", created_at=MAINTENANT),
    ]
    gardes = c._filter_already_known(avis)
    assert [r.id for r in gardes] == ["nouveau"]


def test_filiale_sans_repere_traversee_intacte():
    """Une filiale jamais collectée ne doit rien perdre au filtrage."""
    c = _collecteur(MAINTENANT)
    avis = [
        Review(id="a", company="MTN Zambie", source=SourceEnum.APP_STORE,
               text="premier", created_at=MAINTENANT - timedelta(days=400)),
    ]
    assert len(c._filter_already_known(avis)) == 1
