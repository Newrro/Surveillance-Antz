"""
api/routers/person.py
=====================
GET /person/{identity_id} — profile + visit history + photos.

Public dashboard read (returns only what an admin already sees on screen).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from api.schemas import PersonProfile
from services import search_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/person", tags=["person"])


@router.get(
    "/{identity_id}",
    response_model=PersonProfile,
    status_code=status.HTTP_200_OK,
    summary="Person profile — identity, history, sessions, photos",
)
async def get_person(identity_id: int) -> PersonProfile:
    profile = await search_service.get_person_profile(identity_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity_id={identity_id} not found",
        )
    return PersonProfile(**profile)
