"""identity_repo/lookups.py — display-name lookup."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Employee, Visitor


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
