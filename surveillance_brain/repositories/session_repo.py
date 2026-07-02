"""
repositories/session_repo.py
============================
CRUD for `presence_sessions`.

A "session" represents one continuous period of presence inside the
facility, from entry to exit.  The session_tracker service decides when
to open / close these rows; this repo just performs the SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PresenceSession, SessionStatus


async def fetch_open_session(
    session: AsyncSession, identity_id: int
) -> Optional[PresenceSession]:
    """
    Return the currently-open session (status='inside') for an identity,
    or None if the person is not currently inside the facility.

    There should logically be at most one open session per identity at a
    time — enforced by the session_tracker logic, not by a DB constraint
    (a future migration could add a partial unique index if needed).
    """
    result = await session.execute(
        select(PresenceSession)
        .where(PresenceSession.identity_id == identity_id)
        .where(PresenceSession.status == SessionStatus.INSIDE)
        .order_by(PresenceSession.entry_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def insert_session(
    session: AsyncSession,
    identity_id: int,
    entry_at: datetime,
    entry_camera_id: int,
) -> PresenceSession:
    """Open a new presence session."""
    obj = PresenceSession(
        identity_id=identity_id,
        entry_at=entry_at,
        entry_camera_id=entry_camera_id,
        status=SessionStatus.INSIDE,
    )
    session.add(obj)
    await session.flush()
    return obj


async def close_session(
    session: AsyncSession,
    session_id: int,
    exit_at: datetime,
    exit_camera_id: int,
) -> None:
    """Mark a session as exited — used by session_tracker exit logic."""
    await session.execute(
        update(PresenceSession)
        .where(PresenceSession.id == session_id)
        .values(
            exit_at=exit_at,
            exit_camera_id=exit_camera_id,
            status=SessionStatus.EXITED,
        )
    )


async def fetch_sessions_for_identity(
    session: AsyncSession,
    identity_id: int,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[PresenceSession]:
    """Return all sessions for an identity, optionally filtered by date range."""
    stmt = (
        select(PresenceSession)
        .where(PresenceSession.identity_id == identity_id)
        .order_by(PresenceSession.entry_at.desc())
    )
    if start_date is not None:
        stmt = stmt.where(PresenceSession.entry_at >= start_date)
    if end_date is not None:
        stmt = stmt.where(PresenceSession.entry_at <= end_date)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def fetch_all_open_sessions(session: AsyncSession) -> List[PresenceSession]:
    """Used by the midnight flush worker to find dangling sessions."""
    result = await session.execute(
        select(PresenceSession).where(PresenceSession.status == SessionStatus.INSIDE)
    )
    return list(result.scalars().all())


async def fetch_facility_sessions(
    session: AsyncSession,
    start_date: datetime,
    end_date: datetime,
) -> List[PresenceSession]:
    """All sessions (any identity) overlapping the date range — for facility logs."""
    stmt = (
        select(PresenceSession)
        .where(PresenceSession.entry_at >= start_date)
        .where(PresenceSession.entry_at <= end_date)
        .order_by(PresenceSession.entry_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
