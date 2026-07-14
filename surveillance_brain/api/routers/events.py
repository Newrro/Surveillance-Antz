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
from api.schemas import DeleteEventsRequest, DetectionEventIn, EventOut, EventsResponse
from db.connection import get_db, get_session
from db.models import Classification, DetectionEvent
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
    "/delete",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Delete individual sightings (detection_events) by id",
)
async def delete_events(body: DeleteEventsRequest, _: str = Depends(require_admin)) -> dict:
    """Remove specific sightings from a person's history (admin). Does not touch the
    person's identity or face gallery — just drops the chosen detection rows."""
    ids = [i for i in dict.fromkeys(body.event_ids)]
    async with get_session() as session:
        result = await session.execute(
            sa_delete(DetectionEvent).where(DetectionEvent.id.in_(ids))
        )
        await session.commit()
    deleted = result.rowcount or 0
    logger.info("Deleted %d sighting(s): %s", deleted, ids)
    return {"status": "ok", "deleted": deleted}
