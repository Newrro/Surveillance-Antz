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
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import Classification, IdentityType, MatchedBy
from repositories import embedding_repo, event_repo, identity_repo
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
    camera_id: Optional[int] = None,
    detected_at: Optional[datetime] = None,
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

    # ---- 2. NO FACE → UNKNOWN (never identify by clothing) ----------- #
    # IDENTITY IS FACE-ONLY. Body ReID (OSNet) encodes appearance/clothing, so two
    # different people in similar clothes match — using it to assign identity
    # MERGES strangers. A body-only sighting therefore stays UNKNOWN (no identity,
    # deduped by its stable per-track detection_id in ingestion — no flood). Body
    # is still stored on face-identified people below, but ONLY for their picture.
    if not has_face_now:
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    # ---- 3. FACE MATCH (the only way to recognise a person) ---------- #
    face_hits = await embedding_repo.search_face(face_embedding, limit=5)
    if face_hits and face_hits[0][1] >= config.FACE_SIMILARITY_THRESHOLD:
        best_id, best_sim = face_hits[0]
        identity = await identity_repo.fetch_identity_by_id(session, best_id)
        if identity is not None:
            await _note_and_maybe_confirm(
                session, identity, has_face_now, has_body_now,
                face_embedding, body_embedding,
            )
            classification, label = await _classify(session, identity)
            return ResolutionResult(
                classification=classification,
                identity_id=identity.id,
                matched_by=MatchedBy.FACE,
                similarity=best_sim,
                label=label,
            )
        logger.warning("Face match id=%d gone — enrolling new person", best_id)

    # ---- 3b. CONSTRAINED BODY RE-LINK (prevent duplicate visitors) --- #
    # The face didn't match, so we're about to mint a NEW visitor. But the SAME
    # person's face often fails to re-match frame-to-frame (angle, motion blur),
    # which is exactly how one person fragments into VIS-A, VIS-B, VIS-C. Before
    # creating a duplicate, check whether this body strongly matches an identity
    # seen on the SAME camera within the last BODY_MERGE_WINDOW_SECONDS. If so it's
    # the same person (same clothes, same place, seconds apart) → re-link instead
    # of minting a new id. Constrained by camera + short window + a HIGH cosine so
    # it can never merge two different strangers in similar clothing.
    relinked = await _constrained_body_relink(
        session, has_face_now, has_body_now, face_embedding, body_embedding,
        camera_id, detected_at,
    )
    if relinked is not None:
        return relinked

    # ---- 4. NEW FACE → UNKNOWN (enrolled; confirmed on re-match) ----- #
    # A clear face we don't recognise → a new person. Enroll their FACE (the
    # identity key) + body (their picture). They stay UNKNOWN until seen again by
    # FACE, at which point (face + body on file) they're confirmed a Visitor.
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

    logger.info("New face → UNKNOWN, enrolled as %s (id=%d)", label, new_identity.id)
    return ResolutionResult(
        classification=Classification.UNKNOWN,
        identity_id=new_identity.id,
        matched_by=MatchedBy.NONE,
        similarity=None,
        label=label,
    )


async def _constrained_body_relink(
    session: AsyncSession,
    has_face_now: bool,
    has_body_now: bool,
    face_embedding: Optional[Sequence[float]],
    body_embedding: Optional[Sequence[float]],
    camera_id: Optional[int],
    detected_at: Optional[datetime],
) -> Optional[ResolutionResult]:
    """Re-link an about-to-be-duplicated sighting to a recent same-camera identity
    by BODY similarity, under strict constraints (same camera + short time window +
    HIGH cosine). Returns a ResolutionResult on a re-link, else None.

    This is deliberately NOT identity-by-clothing: it only fires when we ALSO have
    a face this frame (a real person we simply failed to face-match) and only
    re-joins someone already seen here moments ago — it can't invent an identity
    for a faceless stranger, nor merge people across cameras or time."""
    if not has_body_now or not has_face_now or body_embedding is None:
        return None
    if camera_id is None or detected_at is None:
        return None

    body_hits = await embedding_repo.search_body(body_embedding, limit=5)
    body_hits = [(i, s) for (i, s) in body_hits if s >= config.BODY_MERGE_THRESHOLD]
    if not body_hits:
        return None

    since = detected_at - timedelta(seconds=config.BODY_MERGE_WINDOW_SECONDS)
    recent = await event_repo.identities_seen_on_camera_since(session, camera_id, since)
    if not recent:
        return None

    for cand_id, sim in body_hits:            # body_hits is best-first
        if cand_id not in recent:
            continue
        identity = await identity_repo.fetch_identity_by_id(session, cand_id)
        if identity is None:
            continue
        # Same person → backfill this fresh face onto them and classify as usual.
        await _note_and_maybe_confirm(
            session, identity, has_face_now, has_body_now, face_embedding, body_embedding,
        )
        classification, label = await _classify(session, identity)
        logger.info(
            "Body RE-LINK: sighting joined to %s (id=%d) — body sim=%.3f, seen on "
            "camera %s within %ds (no duplicate visitor created)",
            label, cand_id, sim, camera_id, config.BODY_MERGE_WINDOW_SECONDS,
        )
        return ResolutionResult(
            classification=classification,
            identity_id=identity.id,
            matched_by=MatchedBy.BODY,
            similarity=sim,
            label=label,
        )
    return None


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
