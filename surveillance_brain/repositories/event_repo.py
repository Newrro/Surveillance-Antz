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
    track_uuid: Optional[str] = None,
    bbox: Optional[Sequence[float]] = None,      # (x1, y1, x2, y2) pixels
    frame_w: Optional[int] = None,
    frame_h: Optional[int] = None,
    face_path: Optional[str] = None,
    body_path: Optional[str] = None,
    full_frame_path: Optional[str] = None,
    full_frame_annotated_path: Optional[str] = None,
) -> DetectionEvent:
    """Insert a single detection event row (a SIGHTING — identity optional).
    The media paths form ONE immutable evidence set from the same captured
    moment; they are stored verbatim and never rewritten."""
    bx = list(bbox) if bbox and len(bbox) >= 4 else [None, None, None, None]
    obj = DetectionEvent(
        detection_id=detection_id,
        track_uuid=track_uuid,
        bbox_x1=bx[0], bbox_y1=bx[1], bbox_x2=bx[2], bbox_y2=bx[3],
        frame_w=frame_w, frame_h=frame_h,
        face_path=face_path, body_path=body_path,
        full_frame_path=full_frame_path,
        full_frame_annotated_path=full_frame_annotated_path,
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


# ---------------------------------------------------------------------------
# Sighting operations (soft-delete / reassign) — used by report actions
# ---------------------------------------------------------------------------
async def hide_events(
    session: AsyncSession,
    event_ids: list[int],
    reason: str,
    actor: str = "admin",
) -> int:
    """Soft-delete sightings: they stay in the DB (audit) but leave every feed."""
    from sqlalchemy import update
    result = await session.execute(
        update(DetectionEvent)
        .where(DetectionEvent.id.in_(event_ids), DetectionEvent.hidden_at.is_(None))
        .values(hidden_at=datetime.utcnow(), hidden_reason=reason, hidden_by=actor)
    )
    return result.rowcount or 0


async def unhide_events(session: AsyncSession, event_ids: list[int]) -> int:
    from sqlalchemy import update
    result = await session.execute(
        update(DetectionEvent)
        .where(DetectionEvent.id.in_(event_ids))
        .values(hidden_at=None, hidden_reason=None, hidden_by=None)
    )
    return result.rowcount or 0


async def reassign_events(
    session: AsyncSession,
    event_ids: list[int],
    new_identity_id: Optional[int],
    classification=None,
) -> int:
    """Move sightings to another person (or an Unknown case). Fixes a wrong match
    without touching the rest of either person's history."""
    from sqlalchemy import update
    values: dict = {"identity_id": new_identity_id}
    if classification is not None:
        values["classification"] = classification
    result = await session.execute(
        update(DetectionEvent).where(DetectionEvent.id.in_(event_ids)).values(**values)
    )
    return result.rowcount or 0


async def fetch_events_by_ids(session: AsyncSession, event_ids: list[int]) -> list[DetectionEvent]:
    result = await session.execute(
        select(DetectionEvent).where(DetectionEvent.id.in_(event_ids))
    )
    return list(result.scalars().all())


async def event_ids_for_identity(session: AsyncSession, identity_id: int) -> list[int]:
    result = await session.execute(
        select(DetectionEvent.id).where(DetectionEvent.identity_id == identity_id)
    )
    return [r[0] for r in result.all()]
