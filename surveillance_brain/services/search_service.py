"""
services/search_service.py
==========================
Two admin/UI-facing reads:

  find_live(query)          — "Where is X right now?"  (Redis-backed, sub-ms)
  get_person_profile(id)    — full profile + visit history + photos
                              (backs GET /person/{id})

find_live combines a Postgres name/label lookup with a Redis presence
check; the profile joins the identity row, its 1:1 extension, recent
detection events, and presence sessions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from db.connection import get_session
from db.models import Camera
from repositories import camera_repo, event_repo, identity_repo, session_repo
from services import media_paths, presence_cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Live "where is X right now?"
# ---------------------------------------------------------------------------
async def find_live(query_string: str) -> Dict[str, Any]:
    query_string = (query_string or "").strip()
    if not query_string:
        return {"status": "not_found", "reason": "empty_query"}

    async with get_session() as session:
        identity = await identity_repo.find_identity_by_query(session, query_string)

    if identity is None:
        return {"status": "not_found", "query": query_string}

    cached = await presence_cache.get(identity.id)
    if cached is None:
        return {
            "status": "not_in_facility",
            "identity_id": identity.id,
            "label": identity.display_label,
        }

    async with get_session() as session:
        camera: Optional[Camera] = await camera_repo.fetch_camera_by_id(session, cached["camera_id"])

    return {
        "status": "inside",
        "identity_id": identity.id,
        "label": identity.display_label,
        "camera_id": cached["camera_id"],
        "zone_id": cached["zone_id"],
        "last_seen": cached["last_seen"],
        "stream_url": camera.stream_url if camera else None,
    }


# ---------------------------------------------------------------------------
# Person profile — GET /person/{id}
# ---------------------------------------------------------------------------
async def get_person_profile(identity_id: int, history_limit: int = 100) -> Optional[Dict[str, Any]]:
    """
    Return the full profile for an identity, or None if it doesn't exist.

    Shape:
        {
          "identity_id": int,
          "label": str,
          "type": "employee" | "visitor",
          "name": str | None,
          "department": str | None,
          "email": str | None,
          "first_seen_at": iso | None,
          "live": {status...},                # from find_live
          "photos": [snapshot_path, ...],     # distinct, most-recent first
          "history": [ event objects... ],    # recent detection events
          "sessions": [ session summaries ],  # entry/exit/duration
        }
    """
    async with get_session() as session:
        identity = await identity_repo.fetch_identity_by_id(session, identity_id)
        if identity is None:
            return None

        emp = await identity_repo.fetch_employee(session, identity_id)
        vis = await identity_repo.fetch_visitor(session, identity_id)
        events = await event_repo.fetch_events_for_identity(session, identity_id, limit=history_limit)
        sessions = await session_repo.fetch_sessions_for_identity(session, identity_id)
        # camera_id → uid map so the UI resolves friendly trail locations (the raw
        # events only carry the numeric camera_id).
        cams = await camera_repo.fetch_all_cameras(session, active_only=False)
        cam_uid_by_id = {c.id: c.camera_uid for c in cams}

    name = emp.name if emp else (vis.name if vis else None)
    department = emp.department if emp else None
    email = emp.email if emp else None
    first_seen_at = (
        vis.first_seen_at.isoformat() if vis and vis.first_seen_at
        else (emp.hired_at.isoformat() if emp and emp.hired_at else None)
    )

    # Soft-deleted sightings leave every feed (kept in the DB for audit only).
    events = [e for e in events if e.hidden_at is None]

    # Distinct snapshot paths, most-recent first.
    photos: List[str] = []
    for e in events:
        if e.snapshot_path and e.snapshot_path not in photos:
            photos.append(e.snapshot_path)

    history = [
        {
            "event_id": e.id,
            "detection_id": e.detection_id,
            "track_uuid": e.track_uuid,
            "time": e.detected_at.isoformat() if e.detected_at else None,
            "camera_id": e.camera_id,
            "camera": cam_uid_by_id.get(e.camera_id),
            "label": e.classification.value.capitalize(),
            "confidence": e.detection_conf,
            "matched_by": e.matched_by.value,
            "similarity": e.similarity,
            # One immutable evidence set per sighting — EXPLICIT paths only.
            # Never derive a companion file from another file's name.
            "face": e.face_path,
            "body": e.body_path,
            "full_frame": e.full_frame_path,
            "full_frame_annotated": e.full_frame_annotated_path,
            "snapshot": e.snapshot_path,
            "clip": e.clip_path,
            "bbox": ([e.bbox_x1, e.bbox_y1, e.bbox_x2, e.bbox_y2]
                     if e.bbox_x1 is not None else None),
            "frame_w": e.frame_w,
            "frame_h": e.frame_h,
        }
        for e in events
    ]

    session_summaries = []
    for s in sessions:
        duration = None
        if s.exit_at and s.entry_at:
            duration = (s.exit_at - s.entry_at).total_seconds()
        session_summaries.append({
            "session_id": s.id,
            "entry_at": s.entry_at.isoformat() if s.entry_at else None,
            "exit_at": s.exit_at.isoformat() if s.exit_at else None,
            "duration_seconds": duration,
            "status": s.status.value,
        })

    live = await presence_cache.get(identity_id)

    return {
        "identity_id": identity.id,
        "label": identity.display_label,
        "type": identity.identity_type.value,
        "name": name,
        "department": department,
        "email": email,
        "first_seen_at": first_seen_at,
        "live": {"status": "inside", **live} if live else {"status": "not_in_facility"},
        "profile": media_paths.profile_rel(identity.id),   # durable Tier-A photo (if any)
        "photos": photos,
        "history": history,
        "sessions": session_summaries,
    }
