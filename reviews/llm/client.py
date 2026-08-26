"""
Client LLM générique, au format « chat/completions » d'OpenAI.

POURQUOI CE FORMAT PLUTÔT QUE L'API NATIVE DU FOURNISSEUR
    Le besoin exprimé est un fournisseur GRATUIT capable de tourner sans
    surveillance sur un environnement de test. Or aucun quota gratuit n'est
    stable : Google ne publie plus les limites du niveau gratuit de Gemini
    (elles se consultent par projet dans AI Studio) et les a déjà réduites sans
    préavis en décembre 2025.

    Le code ne doit donc pas être marié à un fournisseur. Gemini expose une
    couche compatible OpenAI, comme Groq, Mistral, OpenRouter, Together, ou un
    modèle servi localement par vLLM/Ollama. En parlant ce dialecte-là, changer
    de fournisseur — le jour où le quota disparaît, ou pour la mise en
    production — est une affaire de DEUX VARIABLES D'ENVIRONNEMENT, sans une
    ligne de code modifiée ni un redéploiement d'image.

    Écrit avec `requests`, déjà présent pour les collecteurs : aucune dépendance
    nouvelle, donc aucune image Docker à reconstruire pour cette fonctionnalité.

CE QUE LE CLIENT GARANTIT À SES APPELANTS
    1. Il ne lève jamais d'exception réseau brute : tout devient `LLMUnavailable`
       (rien à tenter, la configuration ne le permet pas) ou `LLMError` (tenté,
       échoué). Un appelant n'a que deux cas à traiter.
    2. Il ne dépasse pas le budget quotidien configuré, et ce budget est compté
       EN BASE — un compteur en mémoire repartirait de zéro à chaque
       redémarrage du worker, c'est-à-dire précisément après l'incident qui
       aurait épuisé le quota.
    3. Il espace ses appels (limite par minute) et respecte un `Retry-After`.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import requests

from reviews.config import LLMConfig, get_settings
from reviews.storage.db import Database

logger = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """Aucun appel n'est possible : pas de clé, désactivé, ou budget épuisé.

    Distincte de `LLMError` parce qu'elle appelle une réponse différente : il
    n'y a rien à réessayer, et l'interface doit expliquer la cause à
    l'utilisateur plutôt que d'afficher une erreur technique.
    """


class LLMError(Exception):
    """L'appel a été tenté et a échoué (réseau, quota, réponse illisible)."""


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0


#: Extrait un objet JSON d'une réponse, même enrobée.
#:
#: NÉCESSAIRE MÊME AVEC UN SCHÉMA DEMANDÉ : la compatibilité OpenAI des
#: fournisseurs est inégale, et plusieurs renvoient le JSON dans un bloc de code
#: Markdown malgré la consigne. Reparser proprement coûte trois lignes ; ne pas
#: le faire coûte un lot d'avis perdu à chaque réponse enrobée.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse un JSON éventuellement entouré de texte ou de balises Markdown."""
    candidate = text.strip()
    fenced = _FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Dernier recours : le plus grand fragment délimité par des accolades ou des
    # crochets. Couvre le cas « Voici le résultat : {...} », courant sur les
    # petits modèles.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Réponse du modèle illisible : aucun JSON exploitable.")


class LLMClient:
    """Appelle un service de complétion et tient la comptabilité de ses appels."""

    def __init__(self, cfg: LLMConfig, db: Optional[Database] = None):
        self.cfg = cfg
        self.db = db
        self._session = requests.Session()
        # Verrou de processus : l'API sert plusieurs requêtes en parallèle, et
        # deux synthèses demandées en même temps enverraient deux appels
        # simultanés — la limite par minute d'un niveau gratuit est basse, elle
        # se franchit à deux.
        self._lock = threading.Lock()
        self._last_call = 0.0

    # ------------------------------------------------------------ Disponibilité

    @property
    def available(self) -> bool:
        """Vrai si un appel peut être tenté (activé et clé présente)."""
        return bool(self.cfg.enabled and self.cfg.api_key)

    def unavailable_reason(self) -> Optional[str]:
        """Pourquoi la fonctionnalité est indisponible, en français, ou None.

        Rendu tel quel dans l'interface. Un dashboard qui affiche « erreur 500 »
        sur une fonctionnalité simplement pas configurée envoie chercher une
        panne qui n'existe pas.
        """
        if not self.cfg.enabled:
            return "L'analyse par IA est désactivée (ENABLE_LLM=false)."
        if not self.cfg.api_key:
            return (
                "Aucune clé d'API n'est configurée. Renseignez LLM_API_KEY dans "
                "le fichier .env pour activer les synthèses en langage naturel."
            )
        if self.remaining_budget() <= 0:
            return (
                f"Budget quotidien atteint ({self.cfg.daily_call_budget} appels). "
                "Les synthèses déjà calculées restent consultables ; les "
                "nouvelles reprendront demain."
            )
        return None

    # ------------------------------------------------------------------ Budget

    def usage_today(self) -> dict:
        """Consommation du jour POUR CE PROFIL, lue en base.

        LE PROFIL EST DANS LA CLAUSE, ET C'EST TOUT L'INTÉRÊT. Le budget
        quotidien est un garde-fou par usage : l'Agent 3 ne doit pas pouvoir
        assécher le quota dont dépend l'analyse sémantique, et réciproquement.
        Sans ce prédicat, les deux se compteraient sur la même ligne et le
        premier arrivé consommerait le budget de l'autre (migration 022).
        """
        empty = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}
        if self.db is None:
            return empty
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    "SELECT calls, tokens_in, tokens_out, errors "
                    "FROM llm_usage WHERE day = %s AND profil = %s",
                    (date.today(), self.cfg.profil),
                )
                row = cur.fetchone()
                return dict(row) if row else empty
        except Exception:  # noqa: BLE001
            # Une table absente ou une base indisponible ne doit pas empêcher un
            # appel : le budget est un garde-fou, pas une condition de service.
            logger.warning("Consommation LLM illisible, budget non appliqué.", exc_info=True)
            return empty

    def remaining_budget(self) -> int:
        return max(0, self.cfg.daily_call_budget - self.usage_today()["calls"])

    def _record(self, *, calls: int = 0, tokens_in: int = 0, tokens_out: int = 0, errors: int = 0) -> None:
        """Incrémente les compteurs du jour. Jamais bloquant."""
        if self.db is None:
            return
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO llm_usage
                        (day, profil, calls, tokens_in, tokens_out, errors)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (day, profil) DO UPDATE SET
                        calls      = llm_usage.calls      + EXCLUDED.calls,
                        tokens_in  = llm_usage.tokens_in  + EXCLUDED.tokens_in,
                        tokens_out = llm_usage.tokens_out + EXCLUDED.tokens_out,
                        errors     = llm_usage.errors     + EXCLUDED.errors,
                        updated_at = now()
                    """,
                    (date.today(), self.cfg.profil, calls, tokens_in, tokens_out, errors),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Consommation LLM non enregistrée.", exc_info=True)

    # ------------------------------------------------------------------- Appel

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        """Envoie une complétion et renvoie le texte produit.

        Raises:
            LLMUnavailable: rien n'a été tenté (pas de clé, désactivé, budget).
            LLMError: l'appel a été tenté et a échoué.
        """
        if not self.available:
            raise LLMUnavailable(self.unavailable_reason() or "IA indisponible.")
        if self.remaining_budget() <= 0:
            raise LLMUnavailable(
                f"Budget quotidien d'appels atteint ({self.cfg.daily_call_budget})."
            )

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Température basse : on demande une classification et un résumé
            # fidèle à des chiffres fournis, pas de la rédaction. Le même
            # périmètre doit produire deux fois la même réponse, sinon la
            # synthèse mise en cache contredirait celle qu'un second lecteur
            # aurait obtenue.
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if json_mode:
            # Envoyé « au mieux » : tous les fournisseurs compatibles OpenAI ne
            # l'implémentent pas. C'est pourquoi la consigne de format est AUSSI
            # écrite dans le prompt et la réponse reparsée défensivement.
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[str] = None
        for attempt in range(1, self.cfg.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.post(
                    url, json=payload, headers=headers, timeout=self.cfg.timeout
                )
            except requests.RequestException as exc:
                last_error = f"réseau : {exc}"
                logger.warning("Appel LLM en échec (tentative %s) : %s", attempt, exc)
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                self._record(calls=1)
                return self._parse(resp.json())

            # 429 (quota/débit) et 5xx sont transitoires : on réessaie. Le reste
            # (401 clé invalide, 400 requête malformée, 404 modèle inconnu) ne
            # s'améliorera pas en insistant — insister brûlerait du quota pour
            # rien et masquerait une erreur de configuration.
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                retry_after = _retry_after_seconds(resp)
                logger.warning(
                    "Appel LLM refusé (%s), tentative %s/%s.",
                    resp.status_code, attempt, self.cfg.max_retries,
                )
                self._backoff(attempt, override=retry_after)
                continue

            self._record(errors=1)
            detail = resp.text[:300]
            if resp.status_code in (401, 403):
                raise LLMUnavailable(
                    "Clé d'API refusée par le fournisseur — vérifiez LLM_API_KEY."
                )
            raise LLMError(f"Appel LLM refusé (HTTP {resp.status_code}) : {detail}")

        self._record(errors=1)
        raise LLMError(
            f"Appel LLM en échec après {self.cfg.max_retries} tentatives ({last_error}). "
            "Quota gratuit probablement épuisé — réessayez plus tard."
        )

    def complete_json(self, *, system: str, user: str, **kwargs) -> Any:
        """Complétion dont la réponse est parsée en JSON."""
        response = self.complete(system=system, user=user, **kwargs)
        return extract_json(response.text)

    # -------------------------------------------------------------- Internes

    def _parse(self, body: dict) -> LLMResponse:
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Réponse du fournisseur inattendue : {exc}") from exc
        if text is None:
            # Arrive quand la réponse a été coupée par un filtre de sécurité ou
            # par max_tokens avant le premier caractère.
            raise LLMError("Le modèle a renvoyé une réponse vide.")
        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        if tokens_in or tokens_out:
            self._record(tokens_in=tokens_in, tokens_out=tokens_out)
        return LLMResponse(
            text=text,
            model=body.get("model") or self.cfg.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def _throttle(self) -> None:
        """Espace deux appels d'au moins `min_interval_seconds`.

        Le niveau gratuit se compte en appels par MINUTE, pas seulement par jour.
        Sans cet espacement, un backfill par lots consomme sa minute en deux
        secondes puis passe le reste du temps à encaisser des 429.
        """
        with self._lock:
            wait = self.cfg.min_interval_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _backoff(self, attempt: int, override: Optional[float] = None) -> None:
        delay = override if override is not None else min(
            self.cfg.retry_backoff_max, 2.0**attempt
        )
        time.sleep(delay)


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    """Délai demandé par le serveur, s'il en indique un.

    Le respecter est ce qui distingue un client qui se rétablit d'un client qui
    entretient son propre blocage.
    """
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 120.0)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fabrique
# ---------------------------------------------------------------------------


def get_client(db: Optional[Database] = None) -> LLMClient:
    """Construit un client depuis la configuration courante.

    Non mis en cache : le client porte une session HTTP et un verrou, que
    l'API (multi-thread) et le worker (mono-thread) n'ont pas intérêt à
    partager. Sa construction ne coûte rien et n'ouvre aucune connexion.
    """
    return LLMClient(get_settings().llm, db=db)
