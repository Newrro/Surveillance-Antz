"""
services/enrollment_service.py
==============================
Employee enrollment (backs POST /employees) + listing (GET /employees).

Boundary note:
    The Brain does NOT run any ML model.  Face/body embedding EXTRACTION is
    Part 1's responsibility.  So enrollment accepts an already-computed
    `face_embedding` (and optional `body_embedding`) in the request body —
    the UI uploads a photo to Part 1's extractor, gets the embedding back,
    then calls this endpoint.  A `photo_path` is stored for display only.

Creating an employee here mints a brand-new identity (EMP-YYYY-NNNN).  To
convert an existing visitor who is already in the system, use
conversion_service.promote_visitor_to_employee instead — that preserves
the person's movement history.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from db.connection import get_session
from db.models import IdentityType
from repositories import embedding_repo, identity_repo

logger = logging.getLogger(__name__)


async def enroll_employee(
    name: str,
    department: str,
    face_embedding: Sequence[float],
    email: Optional[str] = None,
    body_embedding: Optional[Sequence[float]] = None,
    photo_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Register a new employee and store their embeddings in Qdrant.
    Returns {identity_id, label, name, department, email}.
    """
    async with get_session() as session:
        year = datetime.utcnow().year
        seq = await identity_repo.next_employee_seq(session, year)
        label = f"EMP-{year}-{seq:04d}"

        identity = await identity_repo.create_identity(session, IdentityType.EMPLOYEE, label)
        await identity_repo.insert_employee(
            session,
            identity_id=identity.id,
            employee_seq=seq,
            year=year,
            name=name,
            department=department,
            email=email,
        )
        # Store embeddings AFTER the identity row exists so the Qdrant
        # payload's identity_id is valid.  (Qdrant write happens inside the
        # DB transaction window; if the commit later fails the vectors are
        # orphaned but harmless — they only match a non-existent id and the
        # resolver defensively re-creates.)
        await embedding_repo.store_embeddings(
            identity.id,
            face_embedding=face_embedding,
            body_embedding=body_embedding,
            source="enroll",
        )
        identity_id = identity.id

    logger.info("Enrolled employee %s (%s) id=%d", name, label, identity_id)
    return {
        "identity_id": identity_id,
        "label": label,
        "name": name,
        "department": department,
        "email": email,
        "photo_path": photo_path,
    }


async def list_employees(limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
    """Return all employees for GET /employees."""
    async with get_session() as session:
        employees = await identity_repo.list_employees(session, limit=limit, offset=offset)
        # Grab the labels in the same session.
        out: List[Dict[str, Any]] = []
        for e in employees:
            identity = await identity_repo.fetch_identity_by_id(session, e.identity_id)
            out.append({
                "identity_id": e.identity_id,
                "label": identity.display_label if identity else None,
                "name": e.name,
                "department": e.department,
                "email": e.email,
                "hired_at": e.hired_at.isoformat() if e.hired_at else None,
            })
    return out
