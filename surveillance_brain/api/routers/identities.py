"""
api/routers/identities.py
=========================
Admin CRUD for identities — currently just promote/demote.

Both endpoints are protected by HTTP Basic Auth (api.auth.require_admin).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

import base64

from api.auth import require_admin
from api.schemas import (
    ConversionResponse,
    DeleteIdentitiesRequest,
    EventOut,
    IdentityPhotoIn,
    MergeRequest,
    NameRequest,
    PromoteRequest,
    RosterResponse,
)
from db.connection import get_db, get_session
from services import conversion_service, dedup_service, log_service, media_paths

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/identities", tags=["identities"])


@router.get(
    "/roster",
    response_model=RosterResponse,
    status_code=status.HTTP_200_OK,
    summary="One aggregated row per person for the Report/roster (bounded by headcount)",
)
async def roster(
    unknown_days: int = Query(2, ge=0, le=365, description="recent window (days) for faceless Unknowns"),
    unknown_limit: int = Query(500, ge=0, le=5000, description="max Unknown rows (0 = none)"),
) -> RosterResponse:
    """Complete list of identified people (each = their latest sighting + first_seen
    + sighting_count) plus a bounded recent window of Unknowns. Lets the UI show
    every person's last-seen without pulling the whole event history into the browser.
    Per-person history is loaded lazily via GET /person/{identity_id}."""
    people = await log_service.get_roster(unknown_days=unknown_days, unknown_limit=unknown_limit)
    return RosterResponse(count=len(people), people=[EventOut(**p) for p in people])


@router.post(
    "/{identity_id}/promote",
    response_model=ConversionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Promote a visitor to an employee (preserves identity_id + history)",
)
async def promote(
    identity_id: int,
    body: PromoteRequest,
    _: str = Depends(require_admin),
) -> ConversionResponse:
    """
    Visitor → Employee conversion.

    The underlying `identities.id` row is mutated in place; only the
    `identity_type` and `display_label` change.  All historical
    detection_events and presence_sessions stay linked to the same id.

    Requires HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD env vars).
    """
    try:
        new_label = await conversion_service.promote_visitor_to_employee(
            identity_id=identity_id,
            name=body.name,
            department=body.department,
            email=body.email,
        )
        return ConversionResponse(
            identity_id=identity_id,
            new_label=new_label,
            new_type="employee",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Promote failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Promote failed: {e}",
        )


@router.post(
    "/{identity_id}/demote",
    response_model=ConversionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Demote an employee back to a visitor (preserves identity_id + history)",
)
async def demote(
    identity_id: int,
    _: str = Depends(require_admin),
) -> ConversionResponse:
    """
    Employee → Visitor conversion.

    The employee extension row is deleted and a fresh visitor extension
    row is inserted.  The display_label flips from `EMP-YYYY-NNNN` to
    `VIS-YYYY-NNNN` (next available seq for the current year).
    """
    try:
        new_label = await conversion_service.demote_employee_to_visitor(identity_id)
        return ConversionResponse(
            identity_id=identity_id,
            new_label=new_label,
            new_type="visitor",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Demote failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Demote failed: {e}",
        )


@router.post(
    "/merge",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Manually merge duplicate identities into one (folds duplicates → primary)",
)
async def merge(body: MergeRequest, _: str = Depends(require_admin)) -> dict:
    """Fold every id in `duplicate_ids` INTO `primary_id`: their sightings +
    sessions are re-pointed to the primary and the duplicate identities (and their
    vectors) are deleted. The primary keeps its id, label, name and history.

    Used by the dashboard's manual 'select + merge' — the human decides these are
    the same person, so there's no similarity gate (unlike the auto-consolidator)."""
    dups = [d for d in dict.fromkeys(body.duplicate_ids) if d != body.primary_id]
    if not dups:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="need at least one duplicate id distinct from primary_id")
    merged: list[int] = []
    async with get_session() as session:
        for dup in dups:
            try:
                await dedup_service.merge_identities(session, primary_id=body.primary_id, duplicate_id=dup)
                merged.append(dup)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await session.commit()
    logger.info("Manual merge: folded %s into %d", merged, body.primary_id)
    return {"status": "ok", "primary_id": body.primary_id, "merged": merged, "count": len(merged)}


@router.post(
    "/delete",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Permanently delete identities (sightings, sessions, vectors, row)",
)
async def delete_identities(body: DeleteIdentitiesRequest, _: str = Depends(require_admin)) -> dict:
    """Hard-delete each id: its detection_events, presence_sessions, embeddings and
    the identity row. Used by the dashboard's 'select + delete'. Irreversible."""
    ids = list(dict.fromkeys(body.identity_ids))
    deleted: list[int] = []
    async with get_session() as session:
        for iid in ids:
            try:
                await dedup_service.purge_identity(session, iid)
                deleted.append(iid)
            except ValueError:
                continue                     # already gone — skip, keep deleting the rest
        await session.commit()
    logger.info("Manual delete: purged %s", deleted)
    return {"status": "ok", "deleted": deleted, "count": len(deleted)}


@router.post(
    "/{identity_id}/name",
    status_code=status.HTTP_200_OK,
    summary="Rename a person (set a friendly name; keeps the id + VIS/EMP label)",
)
async def set_name(identity_id: int, body: NameRequest) -> dict:
    """Give a person a human name (e.g. 'Akash') while keeping their
    identity_id and display_label (VIS-2026-0001). Open like /events so the
    dashboard can rename without Basic auth."""
    try:
        name = await conversion_service.set_person_name(identity_id, body.name)
        return {"identity_id": identity_id, "name": name}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Rename failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rename failed: {e}",
        )


@router.post(
    "/{identity_id}/photo",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Set a person's fixed profile photo (pins it; pipeline won't overwrite)",
)
async def set_photo(identity_id: int, body: IdentityPhotoIn, _: str = Depends(require_admin)) -> dict:
    """Replace the durable profile avatar (storage/profiles/<id>.jpg) with an uploaded
    image and LOCK it, so the pipeline never overwrites it with a captured face. Works
    for employees and visitors alike (manual thumbnail choice)."""
    try:
        raw = base64.b64decode(body.image.split(",", 1)[-1])   # tolerate data: URL prefix
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid base64 image")
    profile = media_paths.write_profile_from_bytes(identity_id, raw)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not write profile photo",
        )
    return {"identity_id": identity_id, "profile": profile}
