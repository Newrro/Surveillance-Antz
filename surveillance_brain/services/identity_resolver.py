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
    camera_id: Optional[int] = None,
    detected_at: Optional[datetime] = None,
) -> ResolutionResult:
    """
    Open-set 1:N FACE identification (IDENTITY_REDESIGN.md Phase A). Identity is
    face-only and precision-first; body_embedding is ignored.

    1. Gate: low detection_conf OR no face → UNKNOWN (no id).
    2. Rank distinct gallery identities by cosine; s1/s2 = top-1/top-2.
    3. ASSIGN (+confirm) iff s1 ≥ FACE_ASSIGN_THRESHOLD and (s1−s2) ≥ FACE_MARGIN.
    4. NEW provisional visitor iff s1 < FACE_NEW_THRESHOLD (hidden until re-matched).
    5. Otherwise ABSTAIN → UNKNOWN (ambiguous — never guess, never merge).
    """
    # ---- 1. GATE ----------------------------------------------------- #
    if detection_conf < config.DETECTION_CONF_THRESHOLD:
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    has_face_now = face_embedding is not None and len(face_embedding) > 0
    # IDENTITY IS FACE-ONLY (IDENTITY_REDESIGN.md). Body/clothing NEVER creates or
    # joins an identity — uniforms make it actively harmful. A faceless sighting is
    # Unknown, full stop. body_embedding is accepted for signature compatibility but
    # ignored for identity.
    if not has_face_now:
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    # ---- 2. OPEN-SET 1:N against the face gallery -------------------- #
    # Rank DISTINCT identities by best cosine; s1/s2 = top-1/top-2.
    face_hits = await embedding_repo.search_face(face_embedding, limit=10)
    best_by_id: dict[int, float] = {}
    for iid, sim in face_hits:
        if iid not in best_by_id or sim > best_by_id[iid]:
            best_by_id[iid] = sim
    ranked = sorted(best_by_id.items(), key=lambda kv: kv[1], reverse=True)
    id1, s1 = (ranked[0] if ranked else (None, 0.0))
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0

    # ---- 3. CONFIDENT re-match → ASSIGN (+ confirm) ------------------ #
    # High score AND a clear margin over the runner-up. The margin is what rejects
    # look-alikes and kills over-merge magnets.
    if id1 is not None and s1 >= config.FACE_ASSIGN_THRESHOLD and (s1 - s2) >= config.FACE_MARGIN:
        identity = await identity_repo.fetch_identity_by_id(session, id1)
        if identity is not None:
            await _confirm_and_learn(session, identity, face_embedding, s1)
            classification, label = await _classify(session, identity)
            return ResolutionResult(
                classification=classification, identity_id=identity.id,
                matched_by=MatchedBy.FACE, similarity=s1, label=label,
            )
        logger.warning("Face match id=%d gone — treating as new", id1)

    # ---- 4. CLEARLY NEW → provisional visitor (hidden until re-ID) --- #
    # Below the new-person floor to EVERYONE → a face we've never seen. Enroll a
    # PROVISIONAL visitor (confirmed_at NULL → shows as Unknown) that becomes a real
    # Visitor only when re-identified confidently on a later tracklet (step 3).
    if s1 < config.FACE_NEW_THRESHOLD:
        return await _create_provisional_visitor(session, face_embedding)

    # ---- 5. ABSTAIN → Unknown --------------------------------------- #
    # Ambiguous: matches something in the fog, or top-1/top-2 too close. We do NOT
    # assign (would risk a wrong name) and do NOT mint (would risk a duplicate).
    # Precision-first: leave it Unknown.
    return ResolutionResult(
        classification=Classification.UNKNOWN, identity_id=None,
        matched_by=MatchedBy.NONE, similarity=s1,
    )


async def _create_provisional_visitor(
    session: AsyncSession, face_embedding: Sequence[float]
) -> ResolutionResult:
    """Enroll a brand-new face as a PROVISIONAL visitor. confirmed_at stays NULL, so
    _classify reports it as Unknown (hidden from the report) until it is
    re-identified confidently on a later tracklet — then it's promoted to a real
    Visitor. Body is NEVER stored: identity is face-only."""
    year = datetime.utcnow().year
    seq = await identity_repo.next_visitor_seq(session, year)
    label = f"VIS-{year}-{seq:04d}"
    new_identity = await identity_repo.create_identity(session, IdentityType.VISITOR, label)
    await identity_repo.insert_visitor(
        session, identity_id=new_identity.id, visitor_seq=seq,
        year=year, first_seen_at=datetime.utcnow(),
    )
    await embedding_repo.store_embeddings(
        new_identity.id, face_embedding=face_embedding, source="auto_first_seen",
    )
    await identity_repo.set_visitor_flags(session, new_identity.id, True, False)
    logger.info("New face → PROVISIONAL %s (id=%d) — hidden until re-identified",
                label, new_identity.id)
    return ResolutionResult(
        classification=Classification.UNKNOWN, identity_id=new_identity.id,
        matched_by=MatchedBy.NONE, similarity=None, label=label,
    )


async def _classify(session: AsyncSession, identity):
    """Event label for a matched identity: Employee, Visitor (confirmed = seen and
    re-identified by face), or Unknown (a provisional visitor not yet re-matched)."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return Classification.EMPLOYEE, identity.display_label
    confirmed = await identity_repo.is_confirmed_visitor(session, identity.id)
    return (Classification.VISITOR if confirmed else Classification.UNKNOWN), identity.display_label


async def _confirm_and_learn(
    session: AsyncSession, identity, face_embedding: Sequence[float], sim: float
) -> None:
    """On a CONFIDENT face re-match: (1) promote a provisional visitor to CONFIRMED —
    being re-identified is exactly the bar for a trustworthy, re-identifiable Visitor;
    (2) progressively learn — add this face as a new gallery view when it's not a
    near-duplicate, so future angles still match. Employees are already confirmed."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return
    flags = await identity_repo.get_visitor_flags(session, identity.id)
    if flags is None:
        return
    _has_face, _has_body, confirmed = flags

    if sim < config.LEARN_SIMILARITY_CEILING:      # add a fresh view (skip near-dupes)
        await embedding_repo.store_embeddings(identity.id, face_embedding=face_embedding, source="relearn")
        await identity_repo.set_visitor_flags(session, identity.id, True, False)

    if not confirmed:
        if await identity_repo.confirm_visitor(session, identity.id):
            logger.info("CONFIRMED Visitor %s (id=%d): re-identified by face (sim=%.3f)",
                        identity.display_label, identity.id, sim)
