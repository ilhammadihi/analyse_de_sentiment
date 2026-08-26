"""
Flux temps réel (Server-Sent Events).

Le dashboard s'abonne à /stream et reçoit, à intervalle régulier, un snapshot
{overview, alerts} sans avoir à interroger l'API en boucle. C'est le mécanisme
« temps réel » du socle (near-real-time piloté par les runs de collecte).
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from reviews.storage.db import get_database
from reviews.storage.repository import StatsRepository, AlertRepository

router = APIRouter(tags=["realtime"])

_INTERVAL_SECONDS = 5


async def _snapshot() -> dict:
    db = get_database()
    overview = await asyncio.to_thread(StatsRepository(db).overview)
    alerts = await asyncio.to_thread(AlertRepository(db).list_recent, 10)
    return {"overview": overview, "alerts": alerts}


@router.get("/stream")
async def stream(request: Request):
    """Émet un événement SSE toutes les N secondes avec l'état courant."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = jsonable_encoder(await _snapshot())
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(_INTERVAL_SECONDS)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
