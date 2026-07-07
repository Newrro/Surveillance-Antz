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
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    has_face_now = face_embedding is not None and len(face_embedding) > 0
    has_body_now = body_embedding is not None and len(body_embedding) > 0
    if not has_face_now and not has_body_now:
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    # ---- 2. MATCH (face authoritative; body = same-session fallback) - #
    match = await feature_matcher.find_match(face_embedding, body_embedding)

    if match is not None:
        identity = await identity_repo.fetch_identity_by_id(session, match.identity_id)
        if identity is not None:
            # Fill in any modality we don't yet have, and CONFIRM (Unknown →
            # Visitor) once this re-seen person has BOTH a face and a body on file.
            await _note_and_maybe_confirm(
                session, identity, has_face_now, has_body_now,
                face_embedding, body_embedding,
            )
            classification, label = await _classify(session, identity)
            return ResolutionResult(
                classification=classification,
                identity_id=identity.id,
                matched_by=match.matched_by,
                similarity=match.similarity,
                label=label,
            )
        logger.warning("Vector match id=%d gone — enrolling new person", match.identity_id)

    # ---- 3. NEW PERSON → UNKNOWN (enrolled, never confirmed on 1st sight) - #
    # No confident match. We DON'T call them a Visitor — a Visitor is someone we
    # have re-recognised with a clear face AND body on file (branch 2). We still
    # enroll ONE persistent identity + its embeddings so (a) dedup is keyed on
    # this id (no per-frame flood) and (b) they can be recognised next time.
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
    await embedding_repo.store_embeddings(
        new_identity.id,
        face_embedding=face_embedding,
        body_embedding=body_embedding,
        source="auto_first_seen",
    )
    await identity_repo.set_visitor_flags(session, new_identity.id, has_face_now, has_body_now)

    logger.info("New person → UNKNOWN, enrolled as %s (id=%d)", label, new_identity.id)
    return ResolutionResult(
        classification=Classification.UNKNOWN,
        identity_id=new_identity.id,
        matched_by=MatchedBy.NONE,
        similarity=None,
        label=label,
    )


async def _classify(session: AsyncSession, identity):
    """Event label for a matched identity: Employee, Visitor (confirmed), or
    Unknown (a visitor row not yet confirmed — face+body not both on file)."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return Classification.EMPLOYEE, identity.display_label
    confirmed = await identity_repo.is_confirmed_visitor(session, identity.id)
    return (Classification.VISITOR if confirmed else Classification.UNKNOWN), identity.display_label


async def _note_and_maybe_confirm(
    session: AsyncSession,
    identity,
    saw_face: bool,
    saw_body: bool,
    face_embedding: Optional[Sequence[float]],
    body_embedding: Optional[Sequence[float]],
) -> None:
    """On a re-match, backfill a modality we don't have yet (so has_face ⟺ we
    actually store a face vector), then CONFIRM the visitor once BOTH a face and
    a body are on file — that's the bar for a trustworthy, re-identifiable
    Visitor. Employees are already confirmed people; nothing to do."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return
    flags = await identity_repo.get_visitor_flags(session, identity.id)
    if flags is None:
        return
    has_face, has_body, confirmed = flags

    store_face = saw_face and not has_face
    store_body = saw_body and not has_body
    if store_face or store_body:
        await embedding_repo.store_embeddings(
            identity.id,
            face_embedding=face_embedding if store_face else None,
            body_embedding=body_embedding if store_body else None,
            source="fill",
        )
        await identity_repo.set_visitor_flags(session, identity.id, store_face, store_body)
        has_face = has_face or store_face
        has_body = has_body or store_body

    if not confirmed and has_face and has_body:
        if await identity_repo.confirm_visitor(session, identity.id):
            logger.info("CONFIRMED Visitor %s (id=%d): face + body on file",
                        identity.display_label, identity.id)
