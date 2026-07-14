"""
api/routers/events.py
=====================
  POST /events   — Part 1 → Part 2 ingest (one detection payload)
  GET  /events   — Part 2 → Part 3 event feed (filtered log list)

POST is intentionally open (edge devices authenticate via network
segmentation); GET is a dashboard read.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_admin
from api.schemas import (
    DeleteEventsRequest,
    DetectionEventIn,
    EventOut,
    EventsResponse,
    HideEventsRequest,
    ReassignEventsRequest,
    SplitCaseRequest,
    UnhideEventsRequest,
)
from db.connection import get_db, get_session
from db.models import Classification, DetectionEvent, IdentityType
from repositories import audit_repo, event_repo, identity_repo
from services import ingestion_service, log_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ISO8601 timestamp: {value!r}",
        )


def _parse_label(value: Optional[str]) -> Optional[Classification]:
    if not value:
        return None
    try:
        return Classification(value.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid label {value!r} — expected employee|visitor|unknown",
        )


@router.post(
    "",
    response_model=EventOut,
    status_code=status.HTTP_200_OK,
    summary="Ingest a single detection payload from Part 1",
)
async def ingest_event(
    payload: DetectionEventIn,
    session: AsyncSession = Depends(get_db),
) -> EventOut:
    """Resolve identity, track presence, log the event, broadcast to /live."""
    try:
        event = await ingestion_service.ingest(
            session,
            camera_id=payload.camera_id,
            detected_at=payload.timestamp,
            detection_conf=payload.detection_conf,
            face_embedding=payload.face_embedding,
            body_embedding=payload.body_embedding,
            detection_id=payload.detection_id,
            snapshot_path=payload.snapshot_path,
            clip_path=payload.clip_path,
            track_uuid=payload.track_uuid,
            bbox=payload.bbox,
            frame_w=payload.frame_w,
            frame_h=payload.frame_h,
            face_path=payload.face_path,
            body_path=payload.body_path,
            full_frame_path=payload.full_frame_path,
            full_frame_annotated_path=payload.full_frame_annotated_path,
        )
        await session.commit()
        return EventOut(**event)
    except ValueError as e:
        logger.warning("Ingest rejected: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Ingest failed unexpectedly")
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal ingest failure: {e}",
        )


@router.get(
    "",
    response_model=EventsResponse,
    status_code=status.HTTP_200_OK,
    summary="Filtered event log (from/to/label/camera)",
)
async def list_events(
    frm: Optional[str] = Query(None, alias="from", description="ISO8601 start (inclusive)"),
    to: Optional[str] = Query(None, description="ISO8601 end (inclusive)"),
    label: Optional[str] = Query(None, description="employee|visitor|unknown"),
    camera: Optional[str] = Query(None, description="camera_uid, e.g. GATE-01"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> EventsResponse:
    """Newest-first list of detection events for the UI log table."""
    start_date = _parse_dt(frm)
    end_date = _parse_dt(to)
    classification = _parse_label(label)
    events = await log_service.list_events(
        start_date=start_date,
        end_date=end_date,
        label=classification,
        camera_uid=camera,
        limit=limit,
        offset=offset,
    )
    return EventsResponse(count=len(events), events=[EventOut(**e) for e in events])


@router.post(
    "/hide",
    status_code=status.HTTP_200_OK,
    summary="Soft-delete sightings (hidden from every feed, kept for audit)",
)
async def hide_events(body: HideEventsRequest, actor: str = Depends(require_admin)) -> dict:
    """The DEFAULT report delete: hides one bad sighting with a mandatory
    reason. Nothing is erased — the person's history and files survive, and
    the action is auditable and reversible (/events/unhide)."""
    ids = list(dict.fromkeys(body.event_ids))
    async with get_session() as session:
        hidden = await event_repo.hide_events(session, ids, reason=body.reason, actor=actor)
        await audit_repo.record(session, actor=actor, action="hide",
                                subject_type="events", subject_id=",".join(map(str, ids)),
                                details={"reason": body.reason, "count": hidden})
        await session.commit()
    logger.info("Hid %d sighting(s) (%s): %s", hidden, actor, ids)
    return {"status": "ok", "hidden": hidden}


@router.post(
    "/unhide",
    status_code=status.HTTP_200_OK,
    summary="Reverse a previous hide",
)
async def unhide_events(body: UnhideEventsRequest, actor: str = Depends(require_admin)) -> dict:
    ids = list(dict.fromkeys(body.event_ids))
    async with get_session() as session:
        restored = await event_repo.unhide_events(session, ids)
        await audit_repo.record(session, actor=actor, action="unhide",
                                subject_type="events", subject_id=",".join(map(str, ids)),
                                details={"count": restored})
        await session.commit()
    return {"status": "ok", "restored": restored}


@router.post(
    "/reassign",
    status_code=status.HTTP_200_OK,
    summary="Move sightings onto another person (fix a wrong association)",
)
async def reassign_events(body: ReassignEventsRequest, actor: str = Depends(require_admin)) -> dict:
    """Re-points the chosen sightings at `target_identity_id`. Neither person's
    other history is touched; audited and reversible by reassigning back."""
    ids = list(dict.fromkeys(body.event_ids))
    async with get_session() as session:
        target = await identity_repo.fetch_identity_by_id(session, body.target_identity_id)
        if target is None:
            raise HTTPException(status_code=404,
                                detail=f"target identity {body.target_identity_id} not found")
        cls = Classification(target.identity_type.value) \
            if target.identity_type != IdentityType.UNKNOWN else Classification.UNKNOWN
        moved = await event_repo.reassign_events(session, ids, target.id, classification=cls)
        await audit_repo.record(session, actor=actor, action="reassign",
                                subject_type="events", subject_id=",".join(map(str, ids)),
                                details={"target_identity_id": target.id,
                                         "target_label": target.display_label, "count": moved})
        await session.commit()
    logger.info("Reassigned %d sighting(s) → %s (%s)", moved, target.display_label, actor)
    return {"status": "ok", "moved": moved, "target_label": target.display_label}


@router.post(
    "/split-case",
    status_code=status.HTTP_200_OK,
    summary="Detach sightings into a brand-new Unknown case (mark as incorrectly associated)",
)
async def split_case(body: SplitCaseRequest, actor: str = Depends(require_admin)) -> dict:
    """For 'this sighting is NOT that person, and I don't know who it is':
    moves the sightings onto a fresh Unknown case instead of deleting them."""
    ids = list(dict.fromkeys(body.event_ids))
    async with get_session() as session:
        year = datetime.utcnow().year
        seq = await identity_repo.next_unknown_seq(session, year)
        label = f"UNK-{year}-{seq:04d}"
        case_identity = await identity_repo.create_identity(session, IdentityType.UNKNOWN, label)
        await identity_repo.insert_unknown_case(session, identity_id=case_identity.id,
                                                unknown_seq=seq, year=year, track_uuid=None)
        moved = await event_repo.reassign_events(session, ids, case_identity.id,
                                                 classification=Classification.UNKNOWN)
        await audit_repo.record(session, actor=actor, action="split-case",
                                subject_type="events", subject_id=",".join(map(str, ids)),
                                details={"new_case_id": case_identity.id, "label": label,
                                         "count": moved})
        await session.commit()
    logger.info("Split %d sighting(s) into new case %s (%s)", moved, label, actor)
    return {"status": "ok", "moved": moved, "case_id": case_identity.id, "label": label}


@router.post(
    "/delete",
    status_code=status.HTTP_200_OK,
    summary="PERMANENTLY erase sightings (explicit, audited — not the default delete)",
)
async def delete_events(body: DeleteEventsRequest, actor: str = Depends(require_admin)) -> dict:
    """Hard delete, kept for storage hygiene / GDPR-style erasure. The report
    UI's normal delete uses /events/hide; this endpoint is explicit and audited."""
    ids = list(dict.fromkeys(body.event_ids))
    async with get_session() as session:
        result = await session.execute(
            sa_delete(DetectionEvent).where(DetectionEvent.id.in_(ids))
        )
        await audit_repo.record(session, actor=actor, action="erase",
                                subject_type="events", subject_id=",".join(map(str, ids)),
                                details={"count": result.rowcount or 0})
        await session.commit()
    deleted = result.rowcount or 0
    logger.info("ERASED %d sighting(s) (%s): %s", deleted, actor, ids)
    return {"status": "ok", "deleted": deleted}
