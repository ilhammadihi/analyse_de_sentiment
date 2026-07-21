"""
Résilience : retries exponentiels + timeout portable.

Le timeout est implémenté via un thread worker (portable Windows/Linux/macOS)
plutôt que signal.alarm, absent sous Windows.
"""

import logging
import time
import concurrent.futures
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class RetryConfig:
    """Configuration des retries."""
    max_attempts: int = 3
    backoff_factor: float = 2.0
    backoff_max: int = 120
    timeout: int = 30


class TimeoutError(Exception):
    """Levée quand une tentative dépasse le timeout."""


def execute_with_retry(
    func: Callable,
    retry_config: RetryConfig,
    logger: logging.Logger,
    *args,
    use_thread_timeout: bool = True,
    **kwargs,
) -> Any:
    """Exécute `func` avec retries exponentiels et timeout optionnel.

    use_thread_timeout=False : appelle func() directement, sans thread worker
    ni timeout. Obligatoire pour Playwright (API sync), lié au thread OS qui l'a
    démarré et qui refuse d'être relancé dans un autre thread. Dans ce cas on
    s'appuie sur les timeouts internes de Playwright (page.goto(timeout=...)).
    """
    attempt = 1
    last_exception: Exception | None = None
    executor = (
        concurrent.futures.ThreadPoolExecutor(max_workers=1)
        if use_thread_timeout else None
    )

    try:
        while attempt <= retry_config.max_attempts:
            try:
                logger.info(f"Tentative {attempt}/{retry_config.max_attempts}")
                if use_thread_timeout:
                    future = executor.submit(func, *args, **kwargs)
                    try:
                        return future.result(timeout=retry_config.timeout)
                    except concurrent.futures.TimeoutError:
                        raise TimeoutError("Opération expirée")
                else:
                    return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt == retry_config.max_attempts:
                    logger.error(
                        f"Tous les retries échoués après {attempt} tentatives : {e}"
                    )
                    raise
                backoff = min(
                    retry_config.backoff_factor ** (attempt - 1),
                    retry_config.backoff_max,
                )
                logger.warning(f"Tentative {attempt} échouée ({e}), retry dans {backoff}s")
                time.sleep(backoff)
                attempt += 1
        raise last_exception  # pragma: no cover
    finally:
        if executor:
            executor.shutdown(wait=False)
