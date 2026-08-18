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

    while attempt <= retry_config.max_attempts:
        try:
            logger.info(f"Tentative {attempt}/{retry_config.max_attempts}")
            if not use_thread_timeout:
                return func(*args, **kwargs)
            return _run_with_timeout(func, retry_config.timeout, *args, **kwargs)
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


def _run_with_timeout(func: Callable, timeout: int, *args, **kwargs) -> Any:
    """Exécute `func` dans un thread dédié, en abandonnant après `timeout`.

    Un executor NEUF est créé à chaque appel. Auparavant un unique executor
    (max_workers=1) était partagé par toutes les tentatives : Python ne sachant
    pas tuer un thread, celui d'une tentative expirée continuait de tourner et
    monopolisait l'unique worker. Les tentatives suivantes restaient alors en
    file d'attente et expiraient sans avoir rien exécuté — une fois la première
    expirée, l'échec des autres était mécaniquement garanti.

    Le thread abandonné poursuit son travail en arrière-plan jusqu'à son terme
    (limite du langage) ; on ne l'attend simplement plus.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError("Opération expirée")
    finally:
        # wait=False : ne pas bloquer sur le thread abandonné, sinon le retry
        # attendrait exactement ce qu'on vient d'abandonner.
        executor.shutdown(wait=False)
