"""
api/routers/stats.py
====================
  GET /stats/occupancy — live headcount for the Grid: today's unique visits
  (entered via the configured ENTRY cameras) and how many are currently inside
  (not last-seen on an EXIT camera, and seen within the recency window).

Open (read-only), like /events and /identities/roster, so the dashboard can poll it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from services import occupancy_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/occupancy", status_code=status.HTTP_200_OK, summary="Today's visits + currently inside")
async def occupancy() -> dict:
    try:
        return await occupancy_service.occupancy()
    except Exception as e:  # noqa: BLE001
        logger.exception("occupancy failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"occupancy failed: {e}",
        )
