"""
repositories/identity_repo.py
=============================
CRUD for the `identities`, `employees`, and `visitors` tables.

Zero business logic — thin wrappers around SQLAlchemy statements.  The
service layer orchestrates multiple repo calls inside transactions.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Employee, Identity, IdentityType, Visitor


# ---------------------------------------------------------------------------
# identities
# ---------------------------------------------------------------------------
async def fetch_identity_by_id(session: AsyncSession, identity_id: int) -> Optional[Identity]:
    result = await session.execute(select(Identity).where(Identity.id == identity_id))
    return result.scalar_one_or_none()


async def fetch_identity_by_label(session: AsyncSession, label: str) -> Optional[Identity]:
    result = await session.execute(select(Identity).where(Identity.display_label == label))
    return result.scalar_one_or_none()


async def list_visitor_identities(session: AsyncSession) -> List[Identity]:
    """All visitor identities, oldest first (lowest id = the one we keep on merge)."""
    result = await session.execute(
        select(Identity)
        .where(Identity.identity_type == IdentityType.VISITOR)
        .order_by(Identity.id.asc())
    )
    return list(result.scalars().all())


async def create_identity(
    session: AsyncSession,
    identity_type: IdentityType,
    display_label: str,
) -> Identity:
    """Insert a new identities row and return it (PK populated via flush)."""
    identity = Identity(identity_type=identity_type, display_label=display_label)
    session.add(identity)
    await session.flush()
    return identity


async def update_identity_type_and_label(
    session: AsyncSession,
    identity_id: int,
    new_type: IdentityType,
    new_label: str,
) -> None:
    """
    Mutate an existing identities row in place (promote/demote).  The
    surrogate id is preserved; only the discriminator + label change.
    """
    identity = await session.get(Identity, identity_id)
    if identity is None:
        raise ValueError(f"identity_id={identity_id} not found")
    identity.identity_type = new_type
    identity.display_label = new_label
    await session.flush()


# ---------------------------------------------------------------------------
# visitors
# ---------------------------------------------------------------------------

async def _serialize_label_allocation(session: AsyncSession, namespace: str) -> None:
    """Transaction-scoped Postgres advisory lock around MAX(seq)+1 label
    allocation. Two concurrent ingests (the immediate-observation worker and
    the identity worker POST in parallel) otherwise compute the same next
    sequence and collide on the unique display_label (observed live:
    UniqueViolationError on identities_display_label_key). The lock releases
    automatically at commit/rollback."""
    from sqlalchemy import text
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:ns))"), {"ns": f"label:{namespace}"}
    )


async def next_visitor_seq(session: AsyncSession, year: int) -> int:
    """SELECT MAX(visitor_seq)+1 FROM visitors WHERE year=? (serialized). 1 if empty."""
    await _serialize_label_allocation(session, "visitor")
    result = await session.execute(
        select(func.max(Visitor.visitor_seq)).where(Visitor.year == year)
    )
    return (result.scalar() or 0) + 1


async def insert_visitor(
    session: AsyncSession,
    identity_id: int,
    visitor_seq: int,
    year: int,
    name: Optional[str] = None,
    first_seen_at: Optional[datetime] = None,
) -> Visitor:
    visitor = Visitor(
        identity_id=identity_id,
        visitor_seq=visitor_seq,
        year=year,
        name=name,
        first_seen_at=first_seen_at or datetime.utcnow(),
    )
    session.add(visitor)
    await session.flush()
    return visitor


async def delete_visitor(session: AsyncSession, identity_id: int) -> int:
    result = await session.execute(
        Visitor.__table__.delete().where(Visitor.identity_id == identity_id)
    )
    return result.rowcount or 0


async def fetch_visitor(session: AsyncSession, identity_id: int) -> Optional[Visitor]:
    return await session.get(Visitor, identity_id)


# ---------------------------------------------------------------------------
# employees
# ---------------------------------------------------------------------------
async def next_employee_seq(session: AsyncSession, year: int) -> int:
    await _serialize_label_allocation(session, "employee")
    result = await session.execute(
        select(func.max(Employee.employee_seq)).where(Employee.year == year)
    )
    return (result.scalar() or 0) + 1


async def insert_employee(
    session: AsyncSession,
    identity_id: int,
    employee_seq: int,
    year: int,
    name: str,
    department: str,
    email: Optional[str] = None,
    external_id: Optional[str] = None,
) -> Employee:
    employee = Employee(
        identity_id=identity_id,
        employee_seq=employee_seq,
        year=year,
        name=name,
        department=department,
        email=email,
        external_id=external_id,
    )
    session.add(employee)
    await session.flush()
    return employee


async def delete_employee(session: AsyncSession, identity_id: int) -> int:
    result = await session.execute(
        Employee.__table__.delete().where(Employee.identity_id == identity_id)
    )
    return result.rowcount or 0


async def fetch_employee(session: AsyncSession, identity_id: int) -> Optional[Employee]:
    return await session.get(Employee, identity_id)


async def list_employees(session: AsyncSession, limit: int = 500, offset: int = 0) -> List[Employee]:
    """All employees — GET /employees."""
    stmt = select(Employee).order_by(Employee.year.desc(), Employee.employee_seq.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Lookups used by search / enrichment
# ---------------------------------------------------------------------------
async def get_name_for_identity(session: AsyncSession, identity_id: int) -> Optional[str]:
    """Return the display name (employee.name or visitor.name) if any."""
    emp = await session.get(Employee, identity_id)
    if emp is not None:
        return emp.name
    vis = await session.get(Visitor, identity_id)
    if vis is not None:
        return vis.name
    return None


# ---------------------------------------------------------------------------
# visitor confirmation state (Unknown → Visitor)
# ---------------------------------------------------------------------------
async def get_visitor_flags(session: AsyncSession, identity_id: int):
    """Return (has_face, has_body, is_confirmed) for a visitor, or None if the
    identity isn't a visitor row (e.g. an employee)."""
    vis = await session.get(Visitor, identity_id)
    if vis is None:
        return None
    return (vis.has_face, vis.has_body, vis.confirmed_at is not None)


async def set_visitor_flags(session: AsyncSession, identity_id: int,
                            add_face: bool, add_body: bool) -> None:
    """OR the has_face / has_body flags on a visitor (never clears them)."""
    vis = await session.get(Visitor, identity_id)
    if vis is None:
        return
    if add_face:
        vis.has_face = True
    if add_body:
        vis.has_body = True


async def confirm_visitor(session: AsyncSession, identity_id: int) -> bool:
    """Mark a visitor CONFIRMED (Unknown → Visitor) if not already. Returns True
    on the transition."""
    vis = await session.get(Visitor, identity_id)
    if vis is not None and vis.confirmed_at is None:
        vis.confirmed_at = datetime.utcnow()
        return True
    return False


async def is_confirmed_visitor(session: AsyncSession, identity_id: int) -> bool:
    vis = await session.get(Visitor, identity_id)
    return bool(vis is not None and vis.confirmed_at is not None)


async def find_identity_by_query(session: AsyncSession, query: str) -> Optional[Identity]:
    """
    Search identities by:
      - exact display_label (indexed, O(1))
      - employee name (case-insensitive ILIKE)
      - visitor name (case-insensitive ILIKE)
    Returns the first match or None.  Used by search_service.
    """
    ident = await fetch_identity_by_label(session, query.strip())
    if ident is not None:
        return ident

    like = f"%{query.strip()}%"

    emp_result = await session.execute(
        select(Identity)
        .join(Employee, Employee.identity_id == Identity.id)
        .where(Employee.name.ilike(like))
        .limit(1)
    )
    ident = emp_result.scalar_one_or_none()
    if ident is not None:
        return ident

    vis_result = await session.execute(
        select(Identity)
        .join(Visitor, Visitor.identity_id == Identity.id)
        .where(Visitor.name.ilike(like))
        .limit(1)
    )
    return vis_result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# unknown_cases — persistent case per unidentified human track
# ---------------------------------------------------------------------------
async def next_unknown_seq(session: AsyncSession, year: int) -> int:
    from db.models import UnknownCase
    await _serialize_label_allocation(session, "unknown")
    result = await session.execute(
        select(func.coalesce(func.max(UnknownCase.unknown_seq), 0)).where(UnknownCase.year == year)
    )
    return int(result.scalar_one()) + 1


async def insert_unknown_case(
    session: AsyncSession,
    identity_id: int,
    unknown_seq: int,
    year: int,
    track_uuid: Optional[str],
    first_seen_at: Optional[datetime] = None,
):
    from db.models import UnknownCase
    case = UnknownCase(
        identity_id=identity_id, unknown_seq=unknown_seq, year=year,
        track_uuid=track_uuid, first_seen_at=first_seen_at or datetime.utcnow(),
    )
    session.add(case)
    await session.flush()
    return case


async def fetch_unknown_case_by_track(session: AsyncSession, track_uuid: str):
    from db.models import UnknownCase
    result = await session.execute(
        select(UnknownCase).where(UnknownCase.track_uuid == track_uuid)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# identity search (admin report operations — merge target picker etc.)
# ---------------------------------------------------------------------------
async def search_identities(session: AsyncSession, query: str, limit: int = 20) -> List[dict]:
    """Search by display_label OR employee/visitor name (ILIKE). Returns light
    dicts for pickers: {identity_id, label, name, type}. Unlike
    find_identity_by_query (single best hit, used by live search), this returns
    a ranked LIST for report operations."""
    like = f"%{query.strip()}%"
    out: List[dict] = []
    seen: set[int] = set()

    label_rows = await session.execute(
        select(Identity).where(Identity.display_label.ilike(like)).limit(limit)
    )
    for ident in label_rows.scalars():
        seen.add(ident.id)
        out.append({"identity_id": ident.id, "label": ident.display_label,
                    "name": None, "type": ident.identity_type.value})

    emp_rows = await session.execute(
        select(Identity, Employee.name)
        .join(Employee, Employee.identity_id == Identity.id)
        .where(Employee.name.ilike(like)).limit(limit)
    )
    for ident, name in emp_rows.all():
        if ident.id not in seen:
            seen.add(ident.id)
            out.append({"identity_id": ident.id, "label": ident.display_label,
                        "name": name, "type": ident.identity_type.value})

    vis_rows = await session.execute(
        select(Identity, Visitor.name)
        .join(Visitor, Visitor.identity_id == Identity.id)
        .where(Visitor.name.ilike(like)).limit(limit)
    )
    for ident, name in vis_rows.all():
        if ident.id not in seen:
            seen.add(ident.id)
            out.append({"identity_id": ident.id, "label": ident.display_label,
                        "name": name, "type": ident.identity_type.value})

    # Fill display names for the label-matched rows (best effort, single queries)
    for row in out:
        if row["name"] is None:
            row["name"] = await get_name_for_identity(session, row["identity_id"])
    return out[:limit]


async def fetch_employee_by_external_id(session: AsyncSession, external_id: str) -> Optional[Employee]:
    result = await session.execute(
        select(Employee).where(Employee.external_id == external_id)
    )
    return result.scalar_one_or_none()
