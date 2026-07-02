"""
api/routers/live.py
===================
WS /live — real-time event stream (Part 2 → Part 3).

Each connected client receives every accepted, non-duplicate detection as
a JSON event object the moment it is ingested.  Backed by
services.live_broadcaster (in-process pub/sub).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import live_broadcaster

logger = logging.getLogger(__name__)

router = APIRouter(tags=["live"])


@router.websocket("/live")
async def live_feed(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("Live WS connected (subscribers=%d)", live_broadcaster.subscriber_count() + 1)
    try:
        async with live_broadcaster.Subscription() as sub:
            # Greet the client so it knows the stream is open.
            await websocket.send_json({"type": "connected"})
            async for event in sub.stream():
                await websocket.send_json({"type": "event", **event})
    except WebSocketDisconnect:
        logger.info("Live WS disconnected")
    except Exception as e:  # noqa: BLE001
        logger.warning("Live WS error: %s", e)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
