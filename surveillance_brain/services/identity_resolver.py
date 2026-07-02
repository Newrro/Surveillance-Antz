"""
services/identity_resolver.py
=============================
The brain of the classification logic.

Given (detection_conf, face_embedding, body_embedding), decides:
    - UNKNOWN  (detection_conf below threshold) — fast exit, no vector search
    - EMPLOYEE (vector match against an existing employee identity)
    - VISITOR  (vector match against an existing visitor identity)
    - VISITOR  (no vector match — brand-new visitor auto-enrolled)

`detection_conf` is a 0.0–1.0 probability from Part 1 (NOT a percentage).

Returns a ResolutionResult consumed by ingestion_service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import Classification, IdentityType, MatchedBy
from repositories import embedding_repo, identity_repo
from services import feature_matcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolutionResult:
    """Returned by resolve()."""
    classification: Classification
    identity_id: Optional[int]
    matched_by: MatchedBy = MatchedBy.NONE
    similarity: Optional[float] = None
    label: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"<ResolutionResult cls={self.classification.value} id={self.identity_id} "
            f"by={self.matched_by.value} sim={self.similarity} label={self.label}>"
        )


async def resolve(
    session: AsyncSession,
    detection_conf: float,
    face_embedding: Optional[Sequence[float]] = None,
    body_embedding: Optional[Sequence[float]] = None,
) -> ResolutionResult:
    """
    Algorithm:

    1. Unknown Gate:
         If detection_conf < DETECTION_CONF_THRESHOLD (0.80):
             return UNKNOWN, identity_id=None (no vector search).

    2. Match Phase (face primary, body fallback):
         feature_matcher.find_match(face, body).
         On hit → EMPLOYEE or VISITOR based on the identity's current type.

    3. New Visitor Creation:
         No match → brand-new person.
             seq   = MAX(visitor_seq)+1 for the current year
             label = VIS-{year}-{seq:04d}
             insert identities(type=visitor) + visitors(...)
             store face + body embeddings in Qdrant
         Return VISITOR + new identity_id.
    """
    # ---- 1. UNKNOWN GATE --------------------------------------------- #
    if detection_conf < config.DETECTION_CONF_THRESHOLD:
        logger.info(
            "detection_conf %.3f < threshold %.2f → UNKNOWN",
            detection_conf, config.DETECTION_CONF_THRESHOLD,
        )
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    # ---- 2. MATCH PHASE ---------------------------------------------- #
    match = await feature_matcher.find_match(face_embedding, body_embedding)

    if match is not None:
        identity = await identity_repo.fetch_identity_by_id(session, match.identity_id)
        if identity is None:
            logger.warning(
                "Vector match returned identity_id=%d but row is gone — "
                "creating new visitor instead",
                match.identity_id,
            )
        else:
            classification = (
                Classification.EMPLOYEE
                if identity.identity_type == IdentityType.EMPLOYEE
                else Classification.VISITOR
            )
            return ResolutionResult(
                classification=classification,
                identity_id=identity.id,
                matched_by=match.matched_by,
                similarity=match.similarity,
                label=identity.display_label,
            )

    # ---- 3. NEW VISITOR CREATION ------------------------------------- #
    year = datetime.utcnow().year
    seq = await identity_repo.next_visitor_seq(session, year)
    label = f"VIS-{year}-{seq:04d}"

    new_identity = await identity_repo.create_identity(session, IdentityType.VISITOR, label)
    await identity_repo.insert_visitor(
        session,
        identity_id=new_identity.id,
        visitor_seq=seq,
        year=year,
        first_seen_at=datetime.utcnow(),
    )

    # Persist whichever embeddings we received so this visitor is
    # recognised next time (face and/or body).
    await embedding_repo.store_embeddings(
        new_identity.id,
        face_embedding=face_embedding,
        body_embedding=body_embedding,
        source="auto_first_seen",
    )

    logger.info("Created new visitor: %s (id=%d)", label, new_identity.id)
    return ResolutionResult(
        classification=Classification.VISITOR,
        identity_id=new_identity.id,
        matched_by=MatchedBy.NONE,
        similarity=None,
        label=label,
    )
