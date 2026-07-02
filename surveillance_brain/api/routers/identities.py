"""
api/routers/identities.py
=========================
Admin CRUD for identities — currently just promote/demote.

Both endpoints are protected by HTTP Basic Auth (api.auth.require_admin).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_admin
from api.schemas import ConversionResponse, PromoteRequest
from db.connection import get_db
from services import conversion_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/identities", tags=["identities"])


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
