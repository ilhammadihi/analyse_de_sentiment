"""
Couche LLM : analyse sémantique des avis et synthèses en langage naturel.

TOUT CE MODULE EST OPTIONNEL. Sans clé d'API, `LLMClient.available` est faux,
les services renvoient un refus explicite, et le reste de la plateforme —
collecte, lexique, dashboard, alertes — fonctionne exactement comme avant.
C'est la contrainte qui a dicté l'architecture : le service tourne sans
surveillance sur un environnement de test, et une dépendance externe qui tombe
ne doit jamais y faire tomber une page.
"""

from reviews.llm.client import LLMClient, LLMError, LLMUnavailable, get_client

__all__ = ["LLMClient", "LLMError", "LLMUnavailable", "get_client"]
