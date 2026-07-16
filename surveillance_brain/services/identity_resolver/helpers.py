"""identity_resolver/helpers.py — classify a matched identity + progressive learn."""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import Classification, IdentityType
from repositories import embedding_repo, identity_repo

logger = logging.getLogger(__name__)


async def _classify(session: AsyncSession, identity):
    """Event label for a matched identity: Employee, Visitor (confirmed), or Unknown
    (a provisional visitor — captured a face but no body yet — not yet confirmed)."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return Classification.EMPLOYEE, identity.display_label
    confirmed = await identity_repo.is_confirmed_visitor(session, identity.id)
    return (Classification.VISITOR if confirmed else Classification.UNKNOWN), identity.display_label


async def _assign_and_learn(
    session: AsyncSession, identity, face_embedding: Sequence[float], sim: float,
    body_embedding: Optional[Sequence[float]] = None,
) -> None:
    """On a confident re-match: (1) learn — add this face as a fresh gallery view
    when it isn't a near-duplicate, so future angles keep matching (this is what
    keeps a returning person on ONE id); (2) refresh the body vector (today's
    clothes) so the same-camera body RE-LINK can follow this person's faceless
    tracklets; (3) confirm a provisional visitor, since being re-identified is
    proof of a re-identifiable person. Employees are already confirmed and carry
    no visitor row."""
    # Learn only inside the confidence band: above the FLOOR (a borderline assign
    # must not graft a possibly-wrong face into the gallery) and below the CEILING
    # (a near-duplicate view adds nothing).
    if config.LEARN_SIMILARITY_FLOOR <= sim < config.LEARN_SIMILARITY_CEILING:
        await embedding_repo.store_embeddings(identity.id, face_embedding=face_embedding,
                                              body_embedding=body_embedding, source="relearn")

    if identity.identity_type == IdentityType.EMPLOYEE:
        return
    await identity_repo.set_visitor_flags(session, identity.id, True, False)
    if not await identity_repo.is_confirmed_visitor(session, identity.id):
        if await identity_repo.confirm_visitor(session, identity.id):
            logger.info("CONFIRMED Visitor %s (id=%d): re-identified by face (sim=%.3f)",
                        identity.display_label, identity.id, sim)
