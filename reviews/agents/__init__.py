"""
Agents : les composants qui parlent sans qu'on les interroge.

La différence avec `reviews/llm/` tient en une phrase — `llm/` répond à une
question posée par un écran, `agents/` choisit lui-même la question, se
souvient de ce qu'il a déjà dit, et se déclenche seul.
"""

from reviews.agents.arbitrage import Candidat, arbitrer, retenus
from reviews.agents.campagne import Cible, arbitrer_cibles
from reviews.agents.campaign_agent import Campagne, CampaignAgent
from reviews.agents.insight_agent import AGENT, InsightAgent, Passage

#: `AGENT` est celui de l'agent de veille et n'est PAS réexporté pour l'agent de
#: campagne, qui définit le sien : deux constantes du même nom dans un même
#: espace donneraient une valeur qui dépend de l'ordre des imports.
__all__ = [
    "AGENT", "Campagne", "CampaignAgent", "Candidat", "Cible", "InsightAgent",
    "Passage", "arbitrer", "arbitrer_cibles", "retenus",
]
