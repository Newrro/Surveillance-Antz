"""
services/conversion_service.py
==============================
Admin-driven identity lifecycle transitions:
    promote_visitor_to_employee(identity_id, name, dept)
    demote_employee_to_visitor(identity_id)

CRITICAL DESIGN — Surrogate Key Preservation:
    The `identities.id` row is NEVER deleted or recreated.  We mutate
    its `identity_type` and `display_label` in place, and swap the
    1:1 extension row (visitors → employees or vice versa).  All
    historical detection_events and presence_sessions stay linked to
    the same identity_id, preserving an unbroken movement log.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import IdentityType
from repositories import identity_repo
from db.connection import get_session

logger = logging.getLogger(__name__)


async def promote_visitor_to_employee(
    identity_id: int,
    name: str,
    department: str,
    email: Optional[str] = None,
) -> str:
    """
    Promote a visitor to an employee.

    Algorithm (per spec):
        1. Begin DB Transaction.
        2. Fetch identities row.  Assert identity_type == 'visitor'.
        3. Calculate next employee sequence for the current year.
        4. Generate new label: f"EMP-{year}-{seq:04d}".
        5. Update identities row: identity_type='employee', display_label=new_label.
        6. Insert into employees: identity_id, employee_seq, name, department.
        7. Delete from visitors where identity_id = identity_id.
           (The old VIS ID is permanently retired.)
        8. Commit Transaction.

    Returns the new EMP- label.
    """
    async with get_session() as session:
        identity = await identity_repo.fetch_identity_by_id(session, identity_id)
        if identity is None:
            raise ValueError(f"identity_id={identity_id} not found")

        if identity.identity_type != IdentityType.VISITOR:
            raise ValueError(
                f"identity_id={identity_id} is not a visitor "
                f"(actual type={identity.identity_type}) — cannot promote"
            )

        year = datetime.utcnow().year
        seq = await identity_repo.next_employee_seq(session, year)
        new_label = f"EMP-{year}-{seq:04d}"

        # Mutate identities row in place — surrogate key preserved.
        await identity_repo.update_identity_type_and_label(
            session, identity_id, IdentityType.EMPLOYEE, new_label
        )

        # Insert the employee extension row.
        await identity_repo.insert_employee(
            session,
            identity_id=identity_id,
            employee_seq=seq,
            year=year,
            name=name,
            department=department,
            email=email,
        )

        # Delete the old visitor extension row — VIS- label is retired.
        await identity_repo.delete_visitor(session, identity_id)

        logger.info(
            "Promoted identity %d: visitor → employee (%s)",
            identity_id, new_label,
        )
        return new_label


async def demote_employee_to_visitor(identity_id: int) -> str:
    """
    Demote an employee back to a visitor.

    Algorithm (per spec):
        1. Begin DB Transaction.
        2. Fetch identities row.  Assert identity_type == 'employee'.
        3. Calculate next visitor sequence for the current year.
        4. Generate new label: f"VIS-{year}-{seq:04d}".
        5. Update identities row: identity_type='visitor', display_label=new_label.
        6. Insert into visitors: identity_id, visitor_seq, first_seen_at=NOW().
        7. Delete from employees where identity_id = identity_id.
        8. Commit Transaction.

    Returns the new VIS- label.
    """
    async with get_session() as session:
        identity = await identity_repo.fetch_identity_by_id(session, identity_id)
        if identity is None:
            raise ValueError(f"identity_id={identity_id} not found")

        if identity.identity_type != IdentityType.EMPLOYEE:
            raise ValueError(
                f"identity_id={identity_id} is not an employee "
                f"(actual type={identity.identity_type}) — cannot demote"
            )

        year = datetime.utcnow().year
        seq = await identity_repo.next_visitor_seq(session, year)
        new_label = f"VIS-{year}-{seq:04d}"

        await identity_repo.update_identity_type_and_label(
            session, identity_id, IdentityType.VISITOR, new_label
        )

        await identity_repo.insert_visitor(
            session,
            identity_id=identity_id,
            visitor_seq=seq,
            year=year,
            first_seen_at=datetime.utcnow(),
        )

        await identity_repo.delete_employee(session, identity_id)

        logger.info(
            "Demoted identity %d: employee → visitor (%s)",
            identity_id, new_label,
        )
        return new_label


# ---------------------------------------------------------------------------
# V2 stub — Right to be Forgotten
# ---------------------------------------------------------------------------
async def anonymize_identity(identity_id: int) -> None:
    """
    V2 PII deletion workflow (forwarded instruction in the spec).

    Algorithm:
        1. Delete vector embeddings from feature_embeddings.
        2. Anonymize detection_events + presence_sessions
           (set identity_id to NULL, classification to 'anonymous').
        3. Delete the identities / employees / visitors rows
           (CASCADE handles the 1:1 extensions).
        4. If this person enters again, the AI will not recognize them
           and a fresh VIS- id will be issued.

    NOTE: This is currently a stub.  Wire it up to a DELETE /identities/{id}
    endpoint in V2 once the admin UI is ready to call it.
    """
    raise NotImplementedError(
        "anonymize_identity is a V2 feature — not yet implemented. "
        "See 'Forwarded Instructions → Frontend/Admin UI Team' in the spec."
    )
