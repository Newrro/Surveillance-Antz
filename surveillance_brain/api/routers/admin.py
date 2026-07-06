"""
api/routers/admin.py — administrative maintenance endpoints.

POST /admin/reset — wipe the whole database (people, events, sessions, embeddings,
live presence) for a clean slate. Cameras are KEPT (they're infrastructure, not
data). Protected by HTTP Basic Auth (api.auth.require_admin).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text

from api.auth import require_admin
from db import vector_store
from db.connection import get_session
from services import presence_cache

logger = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])

# Everything except the camera registry. RESTART IDENTITY resets the serial ids
# so the next visitor is VIS-2026-0001 again; CASCADE clears the 1:1 extensions.
_WIPE_SQL = (
    "TRUNCATE detection_events, presence_sessions, employees, visitors, identities "
    "RESTART IDENTITY CASCADE"
)


@router.post("/reset")
async def reset_database(_: str = Depends(require_admin)) -> dict:
    """Delete all people/events/sessions/embeddings/presence. Keeps cameras."""
    async with get_session() as session:
        await session.execute(text(_WIPE_SQL))
        await session.commit()
    await vector_store.clear_all()               # drop face + body vectors
    try:
        redis = await presence_cache.get_client()
        await redis.flushdb()                    # clear live presence + dedup guards
    except Exception as exc:  # noqa: BLE001 — Redis is best-effort here
        logger.warning("reset: redis flush failed: %s", exc)
    logger.warning("DATABASE RESET via /admin/reset — all people/events cleared")
    return {"status": "ok", "message": "Database wiped. Cameras kept."}
