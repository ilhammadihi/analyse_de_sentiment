"""
Application FastAPI — colonne vertébrale du projet.

Consommée aujourd'hui par le dashboard (REST + SSE), et demain par les agents
IA de la phase 2 (mêmes endpoints). Lancement :
    uvicorn reviews.api.main:app     ou     python -m reviews serve
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from reviews.config import get_settings
from reviews.log_setup import setup_logging
from reviews.storage.db import get_database
from reviews.api.routes import (
    stats,
    reviews as reviews_routes,
    alerts,
    runs,
    insights,
    campaigns,
    quality,
)
from reviews.api.realtime import router as realtime_router

logger = logging.getLogger("api")


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()

    app = FastAPI(
        title="Plateforme d'analyse de sentiment — API",
        description="Collecte, analyse de sentiment, alerting. Backbone du dashboard et des agents IA.",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(stats.router)
    app.include_router(reviews_routes.router)
    app.include_router(alerts.router)
    app.include_router(runs.router)
    app.include_router(insights.router)
    app.include_router(campaigns.router)
    app.include_router(quality.router)
    app.include_router(realtime_router)

    @app.get("/health", tags=["system"])
    def health():
        """Sonde de santé (utilisée par le healthcheck Docker)."""
        db_ok = get_database().ping()
        return {"status": "ok" if db_ok else "degraded", "database": db_ok}

    @app.get("/", tags=["system"])
    def root():
        return {"service": "reviews-platform", "docs": "/docs", "health": "/health"}

    logger.info("API initialisée")
    return app


app = create_app()
