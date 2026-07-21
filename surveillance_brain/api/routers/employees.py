"""
api/routers/employees.py
========================
  GET  /employees   — list enrolled employees (dashboard read)
  POST /employees   — enroll a new employee (admin only)

Enrollment stores the Part-1-provided embedding in Qdrant.  The Brain runs
no ML model, so the embedding must be supplied in the request body.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from api.auth import require_admin
from api.schemas import (
    EmployeeIn,
    EmployeeListResponse,
    EmployeeOut,
    EmployeePhotoIn,
    EmployeeRecord,
    EmployeeUpdateIn,
)
from services import enrollment_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get(
    "",
    response_model=EmployeeListResponse,
    status_code=status.HTTP_200_OK,
    summary="List enrolled employees",
)
async def list_employees(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> EmployeeListResponse:
    rows = await enrollment_service.list_employees(limit=limit, offset=offset)
    return EmployeeListResponse(count=len(rows), employees=[EmployeeRecord(**r) for r in rows])


@router.post(
    "",
    response_model=EmployeeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Enroll a new employee (admin only)",
)
async def enroll_employee(
    body: EmployeeIn,
    _: str = Depends(require_admin),
) -> EmployeeOut:
    try:
        result = await enrollment_service.enroll_employee(
            name=body.name,
            department=body.department,
            face_embedding=body.face_embedding,
            email=body.email,
            body_embedding=body.body_embedding,
            photo_path=body.photo_path,
        )
        return EmployeeOut(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Employee enrollment failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Enrollment failed: {e}",
        )


@router.post(
    "/enroll-photo",
    response_model=EmployeeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Enroll an employee from uploaded face photo(s) (admin only)",
)
async def enroll_employee_photo(
    body: EmployeePhotoIn,
    _: str = Depends(require_admin),
) -> EmployeeOut:
    """Feed face photo(s) → the Brain embeds them (via the AI face model) and
    enrolls the employee. This is the 'upload a photo of my staff' path."""
    try:
        result = await enrollment_service.enroll_employee_from_images(
            name=body.name, department=body.department,
            images_b64=body.images, email=body.email, external_id=body.external_id,
        )
        return EmployeeOut(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Photo enrollment failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Photo enrollment failed: {e}",
        )


@router.post(
    "/{identity_id}",
    response_model=EmployeeOut,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Edit an employee's profile fields (admin only)",
)
async def update_employee(
    body: EmployeeUpdateIn,
    identity_id: int = Path(..., ge=1),
    _: str = Depends(require_admin),
) -> EmployeeOut:
    """Patch name / department / employee-id (external_id) / email for an existing
    employee. The profile PHOTO is changed separately via POST /identities/{id}/photo."""
    try:
        result = await enrollment_service.update_employee(
            identity_id,
            name=body.name, department=body.department,
            external_id=body.external_id, email=body.email,
        )
        return EmployeeOut(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Employee update failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Update failed: {e}",
        )
