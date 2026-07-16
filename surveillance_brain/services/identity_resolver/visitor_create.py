"""identity_resolver/visitor_create.py — enroll a new face as a Visitor."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Classification, IdentityType, MatchedBy
from repositories import embedding_repo, identity_repo

from .types import ResolutionResult

logger = logging.getLogger(__name__)


async def _create_visitor(
    session: AsyncSession,
    face_embedding: Sequence[float],
    body_embedding: Optional[Sequence[float]],
    top_sim: float,
) -> ResolutionResult:
    """Enroll a brand-new face as a VISITOR immediately. Stores the first face
    template and confirms the visitor when a body was captured too ("clear face +
    body ⇒ Visitor"). If a later tracklet of the SAME person misses the online
    match and enrolls again, deferred clustering folds the duplicate back."""
    year = datetime.utcnow().year
    seq = await identity_repo.next_visitor_seq(session, year)
    label = f"VIS-{year}-{seq:04d}"
    new_identity = await identity_repo.create_identity(session, IdentityType.VISITOR, label)
    await identity_repo.insert_visitor(
        session, identity_id=new_identity.id, visitor_seq=seq,
        year=year, first_seen_at=datetime.utcnow(),
    )
    # Store the body vector TOO — not for identity (face-only, clothing never
    # merges people) but for the constrained same-camera body RE-LINK, which needs
    # something to match when this person's next tracklet has no usable face.
    await embedding_repo.store_embeddings(
        new_identity.id, face_embedding=face_embedding,
        body_embedding=body_embedding, source="auto_first_seen",
    )
    has_body = body_embedding is not None and len(body_embedding) > 0
    await identity_repo.set_visitor_flags(session, new_identity.id, True, has_body)

    # Confirm-on-capture: a clear face (we are past the gate) plus a body picture is
    # a proper capture → a real Visitor from the first sighting. A face with no body
    # is a weaker capture → stays provisional (Unknown) until re-identified.
    confirmed = has_body
    if confirmed:
        await identity_repo.confirm_visitor(session, new_identity.id)
    classification = Classification.VISITOR if confirmed else Classification.UNKNOWN
    logger.info("New face → %s %s (id=%d, top_sim=%.3f)",
                "VISITOR" if confirmed else "provisional (Unknown)",
                label, new_identity.id, top_sim)
    return ResolutionResult(
        classification=classification, identity_id=new_identity.id,
        matched_by=MatchedBy.NONE, similarity=None, label=label,
    )
