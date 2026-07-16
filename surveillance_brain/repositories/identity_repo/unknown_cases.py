"""identity_repo/unknown_cases.py — persistent unknown-person cases."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .labels import _serialize_label_allocation


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
