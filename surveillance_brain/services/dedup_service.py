"""
services/dedup_service.py
=========================
Duplicate-detection guard (module 6 in the RUNG01 notes).

Purpose:
    Part 1 emits one payload PER accepted person crop PER frame — a person
    standing in front of a camera for 10 seconds can generate hundreds of
    near-identical detections.  Without a guard we would spam the event
    ledger and the /live feed.

Strategy:
    A short-window Redis key `dedup:{identity_id}:{camera_id}` with a TTL of
    DUPLICATE_WINDOW_SECONDS.  The FIRST detection of an identity on a camera
    within the window is logged; subsequent ones are suppressed until the
    key expires (i.e. the person has been continuously present).  Presence
    tracking still runs on every detection — only the *ledger write* and the
    *live broadcast* are de-duplicated.

Also exposes `merge_identities()` — a best-effort admin helper to collapse
two identity rows that turned out to be the same person (e.g. a face-only
match and a body-only match that were never linked).
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import DetectionEvent, PresenceSession
from repositories import embedding_repo, identity_repo
from services import presence_cache

logger = logging.getLogger(__name__)


def _key(identity_id: int, camera_id: int) -> str:
    return f"dedup:{identity_id}:{camera_id}"


async def is_duplicate(identity_id: int, camera_id: int) -> bool:
    """
    Return True if this (identity, camera) pair was already logged inside
    the dedup window.  Otherwise register it and return False.

    Fails OPEN (returns False) if Redis is unavailable — better to log a
    possible duplicate than to silently drop a real event.
    """
    try:
        client = await presence_cache.get_client()
        key = _key(identity_id, camera_id)
        # SET NX EX — atomic "claim the window if not already claimed".
        was_set = await client.set(key, "1", nx=True, ex=config.DUPLICATE_WINDOW_SECONDS)
        # was_set is True when we created the key (i.e. NOT a duplicate).
        return not bool(was_set)
    except Exception as e:  # noqa: BLE001
        logger.warning("Dedup check failed (%s) — treating as non-duplicate", e)
        return False


async def merge_identities(session: AsyncSession, primary_id: int, duplicate_id: int) -> None:
    """
    Merge `duplicate_id` INTO `primary_id`.

    - Re-points detection_events + presence_sessions from duplicate → primary.
    - Deletes the duplicate's Qdrant vectors (primary keeps its own).
    - Deletes the duplicate identity row (CASCADE drops its extension row).

    Caller owns the transaction.  Intended for the admin path, not the hot
    ingest path.
    """
    if primary_id == duplicate_id:
        raise ValueError("primary_id and duplicate_id are the same")

    primary = await identity_repo.fetch_identity_by_id(session, primary_id)
    if primary is None:
        raise ValueError(f"primary identity_id={primary_id} not found")
    duplicate = await identity_repo.fetch_identity_by_id(session, duplicate_id)
    if duplicate is None:
        raise ValueError(f"duplicate identity_id={duplicate_id} not found")

    await session.execute(
        update(DetectionEvent)
        .where(DetectionEvent.identity_id == duplicate_id)
        .values(identity_id=primary_id)
    )
    await session.execute(
        update(PresenceSession)
        .where(PresenceSession.identity_id == duplicate_id)
        .values(identity_id=primary_id)
    )

    # Drop the duplicate's vectors, then the identity row itself.
    await embedding_repo.delete_embeddings_for_identity(duplicate_id)
    await session.delete(duplicate)
    await session.flush()

    logger.info("Merged identity %d into %d", duplicate_id, primary_id)
