"""
repositories/audit_repo.py
==========================
Append-only audit trail for every admin mutation: merge, unmerge, hide,
unhide, reassign, split, import, erase. Each row records WHO (actor, from
Basic-auth), WHAT (action + subject), and enough JSON `details` context to
explain — and where possible undo — the action.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AuditLog


async def record(
    session: AsyncSession,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str | int,
    details: Optional[dict[str, Any]] = None,
) -> AuditLog:
    row = AuditLog(
        actor=actor or "system",
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id),
        details=json.dumps(details, default=str) if details is not None else None,
    )
    session.add(row)
    await session.flush()
    return row


async def fetch(session: AsyncSession, audit_id: int) -> Optional[AuditLog]:
    result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
    return result.scalar_one_or_none()


async def list_recent(
    session: AsyncSession,
    action: Optional[str] = None,
    subject_id: Optional[str] = None,
    limit: int = 100,
) -> List[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if subject_id:
        stmt = stmt.where(AuditLog.subject_id == str(subject_id))
    result = await session.execute(stmt)
    return list(result.scalars().all())


def details_of(row: AuditLog) -> dict:
    """Parsed `details` JSON ({} when empty/corrupt)."""
    try:
        return json.loads(row.details) if row.details else {}
    except (TypeError, ValueError):
        return {}
