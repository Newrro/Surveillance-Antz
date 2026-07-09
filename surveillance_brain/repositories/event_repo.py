"""
repositories/event_repo.py
==========================
CRUD for the `detection_events` ledger.

One row per accepted detection payload (Part 1 → Part 2).  This table is
the source of truth for the facility-wide event log that Part 3 (the UI)
reads via GET /events, and the per-person history behind GET /person/{id}.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Classification, DetectionEvent, MatchedBy


async def identities_seen_on_camera_since(
    session: AsyncSession,
    camera_id: int,
    since: datetime,
) -> set[int]:
    """identity_ids that have a detection on `camera_id` at or after `since`.

    Used by the constrained body RE-LINK: an incoming unmatched face is only
    re-linked to an existing identity by body similarity if that identity was
    actually seen on THIS camera within the recent window — so we can never merge
    two strangers who were never co-present in the same place and time."""
    stmt = (
        select(DetectionEvent.identity_id)
        .where(
            DetectionEvent.camera_id == camera_id,
            DetectionEvent.detected_at >= since,
            DetectionEvent.identity_id.is_not(None),
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}


async def insert_detection_event(
    session: AsyncSession,
    identity_id: Optional[int],
    classification: Classification,
    camera_id: Optional[int],
    detected_at: datetime,
    detection_conf: float,
    detection_id: Optional[str] = None,
    matched_by: MatchedBy = MatchedBy.NONE,
    similarity: Optional[float] = None,
    snapshot_path: Optional[str] = None,
    clip_path: Optional[str] = None,
) -> DetectionEvent:
    """Insert a single detection event row."""
    obj = DetectionEvent(
        detection_id=detection_id,
        identity_id=identity_id,
        classification=classification,
        camera_id=camera_id,
        detected_at=detected_at,
        detection_conf=detection_conf,
        matched_by=matched_by,
        similarity=similarity,
        snapshot_path=snapshot_path,
        clip_path=clip_path,
    )
    session.add(obj)
    await session.flush()
    return obj


async def fetch_events(
    session: AsyncSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    classification: Optional[Classification] = None,
    camera_id: Optional[int] = None,
    limit: int = 500,
    offset: int = 0,
) -> List[DetectionEvent]:
    """
    Filtered event query for GET /events?from=&to=&label=&camera=.
    Newest first.  Bounded by `limit` so the UI never pulls the whole table.
    """
    stmt = select(DetectionEvent).order_by(DetectionEvent.detected_at.desc())
    if start_date is not None:
        stmt = stmt.where(DetectionEvent.detected_at >= start_date)
    if end_date is not None:
        stmt = stmt.where(DetectionEvent.detected_at <= end_date)
    if classification is not None:
        stmt = stmt.where(DetectionEvent.classification == classification)
    if camera_id is not None:
        stmt = stmt.where(DetectionEvent.camera_id == camera_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def fetch_events_for_identity(
    session: AsyncSession,
    identity_id: int,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 1000,
) -> List[DetectionEvent]:
    """All detection events for a specific identity — GET /person/{id} history."""
    stmt = (
        select(DetectionEvent)
        .where(DetectionEvent.identity_id == identity_id)
        .order_by(DetectionEvent.detected_at.desc())
        .limit(limit)
    )
    if start_date is not None:
        stmt = stmt.where(DetectionEvent.detected_at >= start_date)
    if end_date is not None:
        stmt = stmt.where(DetectionEvent.detected_at <= end_date)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def bulk_insert_detection_events(
    session: AsyncSession, events: Sequence[DetectionEvent]
) -> int:
    """Bulk insert — used when Part 1 batches payloads.  Returns rowcount."""
    if not events:
        return 0
    session.add_all(events)
    await session.flush()
    return len(events)
