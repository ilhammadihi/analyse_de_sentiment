"""Fournisseurs de dépendances pour l'API (repositories câblés sur la BD)."""

from reviews.storage.db import get_database
from reviews.storage.repository import (
    ReviewRepository, RunRepository, AlertRepository, StatsRepository,
)


def get_review_repo() -> ReviewRepository:
    return ReviewRepository(get_database())


def get_run_repo() -> RunRepository:
    return RunRepository(get_database())


def get_alert_repo() -> AlertRepository:
    return AlertRepository(get_database())


def get_stats_repo() -> StatsRepository:
    return StatsRepository(get_database())


def get_market_repo():
    """Indicateurs de marché (abonnés, trafic, couverture réseau)."""
    from reviews.storage.market_repository import MarketRepository

    return MarketRepository(get_database())


def get_operator_market_repo():
    """Indicateurs de marché PAR OPÉRATEUR (régulateurs nationaux)."""
    from reviews.storage.operator_market_repository import OperatorMarketRepository

    return OperatorMarketRepository(get_database())


def get_insight_service():
    """Service de synthèse en langage naturel.

    Les imports sont LOCAUX à la fonction, et c'est délibéré : la couche LLM est
    optionnelle, et l'API doit démarrer même si rien n'y est configuré. Un import
    au niveau du module ferait dépendre le démarrage de tout le service d'une
    fonctionnalité accessoire.
    """
    from reviews.llm.client import get_client
    from reviews.llm.insights import InsightService

    db = get_database()
    return InsightService(db, StatsRepository(db), get_client(db))


def get_campaign_agent():
    """Assistant de campagne (Agent 2), câblé comme la CLI et Telegram le câblent.

    `build_campaign_agent` est le MÊME constructeur que celui utilisé par les
    deux autres surfaces : c'est ce qui garantit qu'une campagne proposée depuis
    le web applique exactement les mêmes règles, les mêmes seuils et les mêmes
    garde-fous que celle proposée depuis un terminal.

    Imports LOCAUX, comme pour les services ci-dessus : sans clé d'API ni canal
    Telegram, l'API doit démarrer normalement — l'agent fonctionne alors en mode
    gabarit, et `/campaigns/status` dit ce qui manque.
    """
    from reviews.agents.campaign_agent import build_campaign_agent
    from reviews.config import get_settings

    return build_campaign_agent(get_database(), get_settings())


def get_campaign_dossier():
    """Composeur du CAMPAIGN REPORT en treize sections.

    Séparé de l'agent, et volontairement : il ne DÉCIDE rien et n'appelle aucun
    modèle. Il relit ce que la proposition a figé et le range dans les sections
    attendues. Le brancher sur l'agent aurait laissé croire qu'il peut recalculer.
    """
    from reviews.agents.dossier import DossierDeCampagne
    from reviews.storage.campaign_repository import CampaignRepository

    db = get_database()
    return DossierDeCampagne(CampaignRepository(db), StatsRepository(db))


def get_quality_repo():
    """Tables de l'Agent 3 (constats, candidates, affirmations, instantanés)."""
    from reviews.storage.quality_repository import QualityRepository

    return QualityRepository(get_database())


def get_quality_agent():
    """Gardien de la qualité (Agent 3), câblé comme la CLI et le planificateur.

    `build_quality_agent` est le MÊME constructeur que celui des deux autres
    surfaces, pour la raison déjà retenue pour l'agent de campagne : un
    diagnostic obtenu depuis le web doit appliquer exactement les mêmes seuils
    et les mêmes garde-fous que celui obtenu depuis un terminal.

    Imports LOCAUX : sans clé de modèle ni canal Telegram, l'API doit démarrer
    normalement — l'agent fonctionne alors en règles déterministes.
    """
    from reviews.agents.quality.guardian import build_quality_agent
    from reviews.config import get_settings

    return build_quality_agent(get_database(), get_settings())


def get_briefing_service():
    """Service de résumé de période et de diagnostic de cause racine.

    Imports LOCAUX pour la même raison que ci-dessus : sans clé d'API, l'API
    doit démarrer normalement et ces écrans se contenter d'afficher pourquoi la
    synthèse manque.
    """
    from reviews.llm.briefing import BriefingService
    from reviews.llm.client import get_client
    from reviews.storage.briefing_repository import BriefingRepository

    db = get_database()
    return BriefingService(
        db, BriefingRepository(db), StatsRepository(db), get_client(db)
    )
