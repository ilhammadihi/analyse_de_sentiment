"""
Gestion des retries exponentiels et timeouts.
Logique commune de résilience.
"""

import logging
import time
import concurrent.futures
from functools import wraps
from typing import Callable, Any, Optional
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration des retries."""
    max_attempts: int = 3
    backoff_factor: float = 2.0
    backoff_max: int = 120
    timeout: int = 30


class TimeoutError(Exception):
    """Exception levée en cas de timeout."""
    pass


def execute_with_retry(
    func: Callable,
    retry_config: RetryConfig,
    logger: logging.Logger,
    *args,
    use_thread_timeout: bool = True,
    **kwargs
) -> Any:
    """
    Exécute une fonction avec retry exponential et timeout.

    Le timeout est implémenté via un thread worker (portable Windows/Linux/macOS)
    plutôt que signal.alarm, qui n'existe pas sous Windows.

    Args:
        func: Fonction à exécuter
        retry_config: Configuration des retries
        logger: Logger pour les messages
        use_thread_timeout: Si False, appelle func() directement dans le thread
            appelant, sans timeout ni thread worker. Obligatoire pour les
            fonctions qui utilisent Playwright (API sync) : Playwright est lié
            au thread OS qui l'a démarré et refuse d'être relancé dans un
            thread différent ("Cannot switch to a different thread") ou même
            dans le même thread worker réutilisé entre tentatives
            ("Playwright Sync API inside the asyncio loop"). Dans ce cas, on
            s'appuie sur les timeouts internes de Playwright
            (page.goto(timeout=...), locator.wait_for(timeout=...), etc.).
        *args, **kwargs: Arguments de la fonction

    Returns:
        Résultat de la fonction

    Raises:
        Dernière exception après tous les retries échoués
    """
    attempt = 1
    last_exception = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1) if use_thread_timeout else None

    try:
        while attempt <= retry_config.max_attempts:
            try:
                logger.info(f"Tentative {attempt}/{retry_config.max_attempts}")

                if use_thread_timeout:
                    future = executor.submit(func, *args, **kwargs)
                    try:
                        result = future.result(timeout=retry_config.timeout)
                    except concurrent.futures.TimeoutError:
                        raise TimeoutError("Opération expirée")
                else:
                    result = func(*args, **kwargs)

                logger.info(f"Tentative {attempt} réussie")
                return result

            except Exception as e:
                last_exception = e

                if attempt == retry_config.max_attempts:
                    logger.error(
                        f"Tous les retries échoués après {attempt} tentatives",
                        extra={
                            "error": str(e),
                            "attempt": attempt,
                        }
                    )
                    raise

                # Backoff exponentiel
                backoff = min(
                    retry_config.backoff_factor ** (attempt - 1),
                    retry_config.backoff_max
                )

                logger.warning(
                    f"Tentative {attempt} échouée, retry dans {backoff}s",
                    extra={
                        "error": str(e),
                        "attempt": attempt,
                        "backoff": backoff,
                    }
                )

                time.sleep(backoff)
                attempt += 1

        raise last_exception
    finally:
        if executor:
            # wait=False : ne pas bloquer si une tentative est encore bloquée après un timeout
            executor.shutdown(wait=False)


def retry_decorator(max_attempts: int = 3, backoff_factor: float = 2.0):
    """Décorateur pour appliquer les retries à une fonction."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            retry_config = RetryConfig(
                max_attempts=max_attempts,
                backoff_factor=backoff_factor,
            )
            func_logger = logging.getLogger(func.__module__)
            return execute_with_retry(func, retry_config, func_logger, *args, **kwargs)
        return wrapper
    return decorator