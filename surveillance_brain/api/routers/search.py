"""
api/routers/search.py
=====================
GET /search — the "Where is Pritvi?" endpoint.

Public (no admin auth) — admins query this constantly from the dashboard,
and it returns no PII beyond what's already on screen.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from api.schemas import SearchResponse
from services import search_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


@router.get(
    "",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Find where a person is right now (Redis-backed, sub-ms)",
)
async def search(
    q: str = Query(..., min_length=1, max_length=128, description="Name, EMP-ID, or VIS-ID"),
) -> SearchResponse:
    """
    Live presence search.

    1. Resolve `q` → identity_id via Postgres (matches Name / EMP-ID / VIS-ID).
    2. Hit Redis for the current camera/zone.
    3. Return:
         - {"status": "inside", "camera_id": ..., "stream_url": ..., ...}
         - {"status": "not_in_facility", ...}
         - {"status": "not_found"}
    """
    try:
        result = await search_service.find_live(q)
        return SearchResponse(**result)
    except Exception as e:
        logger.exception("Search failed for q=%r", q)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {e}",
        )
