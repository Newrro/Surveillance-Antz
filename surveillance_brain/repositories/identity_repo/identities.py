"""identity_repo/identities.py — the identities table."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Identity, IdentityType


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
