"""
services/ingestion_service.py
=============================
Orchestrator for a single detection payload (Part 1 → Part 2).

Pipeline per accepted detection:
    1. Resolve the camera by camera_id (uid string).
    2. identity_resolver.resolve(...)        → classification + identity_id
    3. session_tracker.on_detection(...)     → presence session + Redis
    4. dedup_service.is_duplicate(...)       → suppress repeat ledger writes
    5. event_repo.insert_detection_event(...) → permanent ledger row
    6. live_broadcaster.publish(...)         → WS /live real-time feed

Returns the "event object" defined by the Part 2 → Part 3 contract, which
is also the response body sent back to Part 1.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Classification, MatchedBy
from repositories import camera_repo, event_repo, identity_repo
from services import (
    dedup_service,
    identity_resolver,
    live_broadcaster,
    session_tracker,
)

logger = logging.getLogger(__name__)


async def ingest(
    session: AsyncSession,
    camera_id: str,
    detected_at: datetime,
    detection_conf: float,
    face_embedding: Optional[Sequence[float]] = None,
    body_embedding: Optional[Sequence[float]] = None,
    detection_id: Optional[str] = None,
    snapshot_path: Optional[str] = None,
    clip_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ingest one detection.  `camera_id` is the string camera_uid from Part 1.
    Raises ValueError on an unknown/inactive camera (route → HTTP 400).
    """
    # ---- Resolve the camera (auto-register unknown cameras) ---------- #
    camera = await camera_repo.fetch_camera_by_uid(session, camera_id)
    if camera is None:
        # Auto-register any camera Part 1 sends so a newly-added feed (e.g. the
        # Turret) flows into logs/reports immediately — no manual seeding needed.
        logger.info("Auto-registering new camera %r", camera_id)
        camera = await camera_repo.insert_camera(
            session,
            camera_uid=camera_id,
            name=camera_id.replace("-", " ").title(),
            zone_id="AUTO",
        )
    if not camera.is_active:
        raise ValueError(f"Camera {camera_id!r} is inactive — rejecting payload")

    # ---- 1. Resolve identity ----------------------------------------- #
    resolution = await identity_resolver.resolve(
        session,
        detection_conf=detection_conf,
        face_embedding=face_embedding,
        body_embedding=body_embedding,
        camera_id=camera.id,
        detected_at=detected_at,
        detection_id=detection_id,
    )
    logger.info(
        "Resolved: cls=%s id=%s label=%s by=%s",
        resolution.classification.value,
        resolution.identity_id,
        resolution.label,
        resolution.matched_by.value,
    )
    # Track-sticky: remember which identity this tracker track resolved to, so a
    # later payload of the SAME track (re-emit / no-face fallback) keeps this id
    # instead of minting a duplicate or an id-less Unknown.
    await identity_resolver.remember_track_identity(detection_id, resolution.identity_id)

    # ---- 2. Presence tracking (known identities only) ---------------- #
    await session_tracker.on_detection(
        session,
        identity_id=resolution.identity_id,
        camera_id=camera.id,
        detected_at=detected_at,
    )

    # ---- 3. Duplicate guard ------------------------------------------ #
    # Known identities dedup on (identity, camera). Unknowns have no identity_id,
    # so they dedup on the STABLE per-track detection_id — otherwise one lingering
    # unrecognised person spams a new Unknown row/card on every re-emit.
    duplicate = False
    if resolution.identity_id is not None:
        duplicate = await dedup_service.is_duplicate(resolution.identity_id, camera.id)
    elif detection_id:
        duplicate = await dedup_service.is_duplicate_unknown(detection_id, camera.id)

    # ---- Progressive learning ---------------------------------------- #
    # Template enrollment + progressive learning now live INSIDE identity_resolver
    # (_create_visitor stores the first face template; _assign_and_learn adds a fresh
    # view on a non-near-duplicate re-match). Centralising it there keeps the gallery
    # face-only and avoids the double-store this block used to cause. Body vectors are
    # deliberately NOT stored — identity is face-only; the body is kept only as the
    # snapshot picture.

    # ---- Resolve display name (for the event object) ----------------- #
    name: Optional[str] = None
    if resolution.identity_id is not None:
        name = await identity_repo.get_name_for_identity(session, resolution.identity_id)

    # ---- 4. Ledger write (skipped for duplicates) -------------------- #
    event_db_id: Optional[int] = None
    if not duplicate:
        row = await event_repo.insert_detection_event(
            session,
            detection_id=detection_id,
            identity_id=resolution.identity_id,
            classification=resolution.classification,
            camera_id=camera.id,
            detected_at=detected_at,
            detection_conf=detection_conf,
            matched_by=resolution.matched_by,
            similarity=resolution.similarity,
            snapshot_path=snapshot_path,
            clip_path=clip_path,
        )
        event_db_id = row.id

    # ---- Build the Part 3 event object ------------------------------- #
    event = _event_object(
        event_db_id=event_db_id,
        detection_id=detection_id,
        detected_at=detected_at,
        camera_uid=camera.camera_uid,
        camera_id=camera.id,
        classification=resolution.classification,
        identity_id=resolution.identity_id,
        label=resolution.label,
        name=name,
        detection_conf=detection_conf,
        matched_by=resolution.matched_by,
        similarity=resolution.similarity,
        snapshot_path=snapshot_path,
        clip_path=clip_path,
        duplicate=duplicate,
    )

    # ---- 5. Live broadcast (skipped for duplicates) ------------------ #
    if not duplicate:
        await live_broadcaster.publish(event)

    return event


def _event_object(
    *,
    event_db_id: Optional[int],
    detection_id: Optional[str],
    detected_at: datetime,
    camera_uid: str,
    camera_id: int,
    classification: Classification,
    identity_id: Optional[int],
    label: Optional[str],
    name: Optional[str],
    detection_conf: float,
    matched_by: MatchedBy,
    similarity: Optional[float],
    snapshot_path: Optional[str],
    clip_path: Optional[str],
    duplicate: bool,
) -> Dict[str, Any]:
    """Shape the canonical event object shared with Part 3 (and Part 1's reply)."""
    return {
        "event_id": event_db_id,
        "detection_id": detection_id,
        "time": detected_at.isoformat(),
        "camera": camera_uid,
        "camera_id": camera_id,
        "person_id": label,                       # e.g. "EMP-2026-0001"
        "identity_id": identity_id,
        "label": classification.value.capitalize(),  # "Employee" | "Visitor" | "Unknown"
        "name": name,
        "confidence": detection_conf,
        "matched_by": matched_by.value,
        "similarity": similarity,
        "snapshot": snapshot_path,
        "clip": clip_path,
        "duplicate": duplicate,
    }
