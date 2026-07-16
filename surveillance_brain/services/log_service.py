"""
services/log_service.py
=======================
Historical reporting + the event feed behind GET /events.

  list_events(...)        → enriched event objects (JSON) for the UI + archive
  individual_log(...)     → per-identity session log (entry/exit/duration)
  facility_log_csv(...)   → facility-wide CSV export (StreamingResponse)

"Enriched" means the raw detection_events row is joined with identities +
employees/visitors + cameras so the UI gets person_id / name / camera_uid
without extra round-trips.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from services import media_paths

from db.connection import get_session
from db.models import (
    Camera,
    Classification,
    DetectionEvent,
    Employee,
    Identity,
    Visitor,
)
from repositories import camera_repo, session_repo

logger = logging.getLogger(__name__)


def _enriched_stmt():
    """Shared joined SELECT for the event feed."""
    return (
        select(
            DetectionEvent.id.label("event_id"),
            DetectionEvent.detection_id,
            DetectionEvent.detected_at,
            DetectionEvent.classification,
            DetectionEvent.detection_conf,
            DetectionEvent.matched_by,
            DetectionEvent.similarity,
            DetectionEvent.snapshot_path,
            DetectionEvent.clip_path,
            DetectionEvent.identity_id,
            Identity.display_label.label("person_id"),
            Identity.identity_type,
            Employee.name.label("emp_name"),
            Visitor.name.label("vis_name"),
            DetectionEvent.camera_id,
            Camera.camera_uid,
            Camera.zone_id,
        )
        .join(Identity, Identity.id == DetectionEvent.identity_id, isouter=True)
        .join(Employee, Employee.identity_id == Identity.id, isouter=True)
        .join(Visitor, Visitor.identity_id == Identity.id, isouter=True)
        .join(Camera, Camera.id == DetectionEvent.camera_id, isouter=True)
    )


def _row_to_event(r: Any) -> Dict[str, Any]:
    return {
        "event_id": r.event_id,
        "detection_id": r.detection_id,
        "time": r.detected_at.isoformat() if r.detected_at else None,
        "camera": r.camera_uid,
        "camera_id": r.camera_id,
        "zone_id": r.zone_id,
        "person_id": r.person_id,
        "identity_id": r.identity_id,
        "label": r.classification.value.capitalize() if r.classification else None,
        "name": r.emp_name or r.vis_name,
        "confidence": r.detection_conf,
        "matched_by": r.matched_by.value if r.matched_by else None,
        "similarity": r.similarity,
        "snapshot": r.snapshot_path,
        "profile": media_paths.profile_rel(r.identity_id),   # durable Tier-A photo (if any)
        "clip": r.clip_path,
    }


# ---------------------------------------------------------------------------
# GET /events feed
# ---------------------------------------------------------------------------
async def list_events(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    label: Optional[Classification] = None,
    camera_uid: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Filtered, enriched event list (newest first) for GET /events."""
    async with get_session() as session:
        camera_id: Optional[int] = None
        if camera_uid:
            cam = await camera_repo.fetch_camera_by_uid(session, camera_uid)
            if cam is None:
                return []  # unknown camera → no events
            camera_id = cam.id

        stmt = _enriched_stmt().order_by(DetectionEvent.detected_at.desc())
        if start_date is not None:
            stmt = stmt.where(DetectionEvent.detected_at >= start_date)
        if end_date is not None:
            stmt = stmt.where(DetectionEvent.detected_at <= end_date)
        if label is not None:
            stmt = stmt.where(DetectionEvent.classification == label)
        if camera_id is not None:
            stmt = stmt.where(DetectionEvent.camera_id == camera_id)
        stmt = stmt.limit(limit).offset(offset)

        result = await session.execute(stmt)
        rows = result.all()

    return [_row_to_event(r) for r in rows]


# ---------------------------------------------------------------------------
# Individual session log
# ---------------------------------------------------------------------------
async def individual_log(
    identity_id: int,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[dict]:
    """Per-identity presence sessions with computed durations."""
    async with get_session() as session:
        sessions = await session_repo.fetch_sessions_for_identity(
            session, identity_id, start_date, end_date
        )

    out: List[dict] = []
    for s in sessions:
        duration_seconds: Optional[float] = None
        if s.exit_at is not None and s.entry_at is not None:
            duration_seconds = (s.exit_at - s.entry_at).total_seconds()
        out.append({
            "session_id": s.id,
            "entry_at": s.entry_at.isoformat() if s.entry_at else None,
            "exit_at": s.exit_at.isoformat() if s.exit_at else None,
            "entry_camera_id": s.entry_camera_id,
            "exit_camera_id": s.exit_camera_id,
            "duration_seconds": duration_seconds,
            "status": s.status.value,
        })
    return out


# ---------------------------------------------------------------------------
# Facility-wide CSV export
# ---------------------------------------------------------------------------
async def facility_log_csv(start_date: datetime, end_date: datetime) -> str:
    """One CSV row per detection_event in the range (joined + enriched)."""
    async with get_session() as session:
        stmt = (
            _enriched_stmt()
            .where(DetectionEvent.detected_at >= start_date)
            .where(DetectionEvent.detected_at <= end_date)
            .order_by(DetectionEvent.detected_at.asc())
        )
        result = await session.execute(stmt)
        rows = result.all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "event_id", "detection_id", "detected_at", "label", "confidence",
        "matched_by", "similarity",
        "identity_id", "person_id", "name", "identity_type",
        "camera_id", "camera_uid", "zone_id",
        "snapshot_path", "clip_path",
    ])
    for r in rows:
        writer.writerow([
            r.event_id,
            r.detection_id or "",
            r.detected_at.isoformat() if r.detected_at else "",
            r.classification.value if r.classification else "",
            r.detection_conf,
            r.matched_by.value if r.matched_by else "",
            r.similarity if r.similarity is not None else "",
            r.identity_id if r.identity_id is not None else "",
            r.person_id or "",
            r.emp_name or r.vis_name or "",
            r.identity_type.value if r.identity_type else "",
            r.camera_id if r.camera_id is not None else "",
            r.camera_uid or "",
            r.zone_id or "",
            r.snapshot_path or "",
            r.clip_path or "",
        ])

    logger.info("facility_log_csv: %d rows", len(rows))
    return buf.getvalue()
