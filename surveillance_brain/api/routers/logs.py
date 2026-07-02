"""
api/routers/logs.py
===================
Historical data endpoints.

  - GET /logs/individual?identity_id=&from=&to=  → JSON array of sessions
  - GET /logs/facility?from=&to=                 → CSV download (StreamingResponse)

The facility CSV download is protected by Basic Auth — it exposes every
movement event across the facility, so it must not be publicly accessible.
The individual log is also admin-only for the same reason.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter

from api.auth import require_admin
from api.schemas import IndividualLogResponse, SessionLogEntry
from services import log_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ISO8601 timestamp: {value!r}",
        )


@router.get(
    "/individual",
    response_model=IndividualLogResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Per-identity historical session log (entry/exit/duration)",
)
async def individual_log(
    identity_id: int = Query(..., description="Surrogate identity PK"),
    frm: Optional[str] = Query(None, alias="from", description="ISO8601 start (inclusive)"),
    to: Optional[str] = Query(None, description="ISO8601 end (inclusive)"),
    _: str = Depends(require_admin),
) -> IndividualLogResponse:
    """Returns all presence_sessions for the identity, with computed durations."""
    try:
        start_date = _parse_dt(frm)
        end_date = _parse_dt(to)
        rows = await log_service.individual_log(identity_id, start_date, end_date)
        entries = TypeAdapter(list[SessionLogEntry]).validate_python(rows)
        return IndividualLogResponse(identity_id=identity_id, sessions=entries)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("individual_log failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Log query failed: {e}",
        )


@router.get(
    "/facility",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Facility-wide detection event log as CSV download",
)
async def facility_log(
    frm: str = Query(..., alias="from", description="ISO8601 start (inclusive)"),
    to: str = Query(..., description="ISO8601 end (inclusive)"),
    _: str = Depends(require_admin),
) -> StreamingResponse:
    """
    Returns a CSV file with one row per detection_event in the date range.

    Columns:
        event_id, detected_at, classification, confidence,
        identity_id, identity_label, identity_type,
        camera_id, camera_uid, zone_id,
        track_uuid, bbox, snapshot_url
    """
    try:
        start_date = _parse_dt(frm) or datetime(1970, 1, 1)
        end_date = _parse_dt(to) or datetime.utcnow()

        csv_str = await log_service.facility_log_csv(start_date, end_date)

        filename = f"facility_log_{start_date.date().isoformat()}_to_{end_date.date().isoformat()}.csv"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        return StreamingResponse(
            iter([csv_str]),
            media_type="text/csv",
            headers=headers,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("facility_log failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Log query failed: {e}",
        )
