"""identity_repo/labels.py — serialized label-sequence allocation."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


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
