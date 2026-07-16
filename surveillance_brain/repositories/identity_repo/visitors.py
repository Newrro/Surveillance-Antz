"""identity_repo/visitors.py — visitors table + confirmation state."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Visitor

from .labels import _serialize_label_allocation


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
