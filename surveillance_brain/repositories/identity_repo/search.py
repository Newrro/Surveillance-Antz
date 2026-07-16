"""identity_repo/search.py — identity search for live locate + report pickers."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Employee, Identity, Visitor

from .identities import fetch_identity_by_label
from .lookups import get_name_for_identity


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
