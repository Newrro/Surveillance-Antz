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

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.auth import require_admin
from api.schemas import (
    EmployeeIn,
    EmployeeListResponse,
    EmployeeOut,
    EmployeePhotoIn,
    EmployeeRecord,
    ImportEmployeesRequest,
)
from services import enrollment_service, import_service

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
            images_b64=body.images, email=body.email,
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
    "/import",
    status_code=status.HTTP_200_OK,
    summary="Bulk roster import: XLSX/CSV or ZIP (roster + photos). dry_run previews.",
)
async def import_employees(body: ImportEmployeesRequest, actor: str = Depends(require_admin)) -> dict:
    """external_id is the idempotency key — re-imports UPDATE, never duplicate.
    dry_run=true returns the full row-level validation preview without writing.
    The apply run is audited (action='import')."""
    try:
        return await import_service.run_import(
            filename=body.filename, content_b64=body.content_b64,
            dry_run=body.dry_run, actor=actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Employee import failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Import failed: {e}",
        )


@router.get(
    "/import/{job_id}",
    status_code=status.HTTP_200_OK,
    summary="Fetch a completed import's summary by job id",
)
async def import_status(job_id: str, _: str = Depends(require_admin)) -> dict:
    summary = import_service.job_status(job_id)
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"no import job {job_id!r} in this process")
    return summary
