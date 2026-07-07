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


async def clear_unknowns() -> int:
    """Delete every UNCONFIRMED person (Unknowns: visitor rows with no
    confirmed_at) — their identity, embeddings, sessions, and all UNKNOWN-labelled
    sightings. CONFIRMED Visitors and Employees are kept, so a real visitor is
    still recognised tomorrow. Returns how many Unknowns were removed."""
    async with get_session() as session:
        rows = await session.execute(
            text("SELECT identity_id FROM visitors WHERE confirmed_at IS NULL")
        )
        ids = [r[0] for r in rows]
        # Drop all Unknown sightings (covers the unconfirmed ids + any orphans).
        await session.execute(text("DELETE FROM detection_events WHERE classification = 'unknown'"))
        if ids:
            await session.execute(
                text("DELETE FROM presence_sessions WHERE identity_id = ANY(:ids)"), {"ids": ids}
            )
            # CASCADE removes the visitors extension rows.
            await session.execute(text("DELETE FROM identities WHERE id = ANY(:ids)"), {"ids": ids})
        await session.commit()

    for iid in ids:                              # drop their face/body vectors
        try:
            await vector_store.delete_for_identity(iid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("clear_unknowns: vector delete failed for %d: %s", iid, exc)
    logger.info("Cleared %d unknown(s)", len(ids))
    return len(ids)


@router.post("/clear-unknowns")
async def clear_unknowns_endpoint(_: str = Depends(require_admin)) -> dict:
    """Manual 'clear today's unknowns' — same as the automatic midnight sweep."""
    removed = await clear_unknowns()
    return {"status": "ok", "removed": removed, "message": f"Cleared {removed} unknown(s). Visitors kept."}
