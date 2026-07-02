"""
services/session_tracker.py
===========================
Bridges detection events and the presence_sessions table + Redis cache.

Decides, on every detection:
    - Is this an EXIT?  → close the open session, evict from Redis
    - Is this an ENTRY or ZONE MOVE?  → touch Redis; open a new session
      if none exists, otherwise leave the existing session open.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from repositories import camera_repo, session_repo
from services import presence_cache

logger = logging.getLogger(__name__)


async def on_detection(
    session: AsyncSession,
    identity_id: Optional[int],
    camera_id: Optional[int],
    detected_at: datetime,
) -> Optional[int]:
    """
    Per-spec algorithm.

    Args:
        session       — async SQLAlchemy session (caller owns the txn)
        identity_id   — None for UNKNOWNs; we abort tracking entirely
        camera_id     — DB PK of the camera that emitted the event
        detected_at   — timezone-aware UTC timestamp

    Returns:
        presence_session.id if a session was opened/closed, None otherwise.
    """
    # ---- Unknowns are not tracked -------------------------------------- #
    if identity_id is None:
        return None

    if camera_id is None:
        logger.warning("on_detection called with camera_id=None for identity_id=%d", identity_id)
        return None

    camera = await camera_repo.fetch_camera_by_id(session, camera_id)
    if camera is None:
        logger.warning("Camera id=%d not found — skipping tracking", camera_id)
        return None

    # ------------------------------------------------------------------ #
    # EXIT LOGIC
    # ------------------------------------------------------------------ #
    if camera.is_exit_camera:
        open_session = await session_repo.fetch_open_session(session, identity_id)
        if open_session is not None:
            await session_repo.close_session(
                session,
                session_id=open_session.id,
                exit_at=detected_at,
                exit_camera_id=camera_id,
            )
            logger.info(
                "Closed session %d for identity %d (exit camera=%s)",
                open_session.id, identity_id, camera.camera_uid,
            )
        else:
            # Defensive — exit detected without an open session (e.g. system
            # was offline during entry).  Nothing to close, just evict.
            logger.info(
                "Exit detected for identity %d but no open session — evicting Redis only",
                identity_id,
            )

        await presence_cache.evict(identity_id)
        return open_session.id if open_session else None

    # ------------------------------------------------------------------ #
    # ENTRY / CONTINUATION LOGIC
    # ------------------------------------------------------------------ #
    # Always refresh Redis — even if the session is already open, this
    # updates the "last seen" camera/zone so /search returns current data.
    await presence_cache.touch(
        identity_id=identity_id,
        camera_id=camera_id,
        zone_id=camera.zone_id,
    )

    open_session = await session_repo.fetch_open_session(session, identity_id)
    if open_session is None:
        new_session = await session_repo.insert_session(
            session,
            identity_id=identity_id,
            entry_at=detected_at,
            entry_camera_id=camera_id,
        )
        logger.info(
            "Opened session %d for identity %d (entry camera=%s zone=%s)",
            new_session.id, identity_id, camera.camera_uid, camera.zone_id,
        )
        return new_session.id

    # Open session exists — do nothing to Postgres.
    # Redis already refreshed above.
    logger.debug(
        "Continuation: identity %d still inside (session=%d, camera=%s)",
        identity_id, open_session.id, camera.camera_uid,
    )
    return open_session.id
