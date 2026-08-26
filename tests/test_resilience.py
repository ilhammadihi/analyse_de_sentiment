"""Tests du mécanisme de retry/timeout (sans appel réseau)."""

import logging
import threading

import pytest

from reviews.processing.resilience import (
    RetryConfig,
    TimeoutError as ResilienceTimeout,
    execute_with_retry,
)

logger = logging.getLogger("test.resilience")


def test_succes_au_premier_essai():
    cfg = RetryConfig(max_attempts=3, timeout=5)
    assert execute_with_retry(lambda: "ok", cfg, logger) == "ok"


def test_retry_puis_succes():
    appels = []

    def echoue_une_fois():
        appels.append(1)
        if len(appels) == 1:
            raise ValueError("boom")
        return "ok"

    cfg = RetryConfig(max_attempts=3, backoff_factor=1, timeout=5)
    assert execute_with_retry(echoue_une_fois, cfg, logger) == "ok"
    assert len(appels) == 2


def test_tous_les_essais_echouent():
    cfg = RetryConfig(max_attempts=2, backoff_factor=1, timeout=5)
    with pytest.raises(ValueError):
        execute_with_retry(lambda: (_ for _ in ()).throw(ValueError("ko")), cfg, logger)


def test_timeout_leve_une_erreur_dediee():
    liberer = threading.Event()
    cfg = RetryConfig(max_attempts=1, timeout=1)
    try:
        with pytest.raises(ResilienceTimeout):
            execute_with_retry(lambda: liberer.wait(timeout=10), cfg, logger)
    finally:
        liberer.set()


def test_une_tentative_expiree_ne_condamne_pas_les_suivantes():
    """Régression : l'executor était partagé entre tentatives (max_workers=1).

    Le thread d'une tentative expirée — que Python ne sait pas tuer — gardait
    l'unique worker, si bien que les tentatives suivantes attendaient en file
    et expiraient sans jamais s'exécuter. Une fois la 1re expirée, l'échec des
    autres était garanti.
    """
    appels = []
    liberer = threading.Event()

    def lent_puis_rapide():
        rang = len(appels)
        appels.append(rang)
        if rang == 0:
            liberer.wait(timeout=10)   # dépasse volontairement le timeout
            return "trop tard"
        return "ok"

    cfg = RetryConfig(max_attempts=2, backoff_factor=1, timeout=1)
    try:
        assert execute_with_retry(lent_puis_rapide, cfg, logger) == "ok"
        assert len(appels) == 2       # la 2e tentative a bien tourné
    finally:
        liberer.set()


def test_sans_thread_timeout_appelle_directement():
    """use_thread_timeout=False : obligatoire pour Playwright (API sync)."""
    courant = threading.current_thread()
    vu = {}

    def ou_suis_je():
        vu["thread"] = threading.current_thread()
        return "ok"

    cfg = RetryConfig(max_attempts=1, timeout=1)
    assert execute_with_retry(ou_suis_je, cfg, logger, use_thread_timeout=False) == "ok"
    assert vu["thread"] is courant   # exécuté dans le thread appelant
