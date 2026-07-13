"""
services/identity_resolver.py
=============================
The brain of the classification logic — 2026 self-calibrating, recall-first design.

Given (detection_conf, face_embedding), decide one of:
    - UNKNOWN  (no id)  — detection gated out, OR no face captured. "Unknown" is
                          reserved for people the system could NOT capture properly
                          (no clear face). These are ephemeral (deduped on the
                          per-track detection_id upstream); they never enroll.
    - EMPLOYEE          — the face matched an enrolled employee.
    - VISITOR           — the face matched an existing visitor (re-used id), OR it's
                          a new, well-captured face → enrolled as a Visitor NOW.

DESIGN (why this replaces the old "confirm only on a 0.62 re-match" model)
    Distant-CCTV faces of the SAME person score only ~0.35-0.55 cosine, below any
    safe fixed threshold — so the old model left well-captured people stuck as
    "Unknown" forever. Fixes:

    1. RECALL-FIRST online matching at a SELF-CALIBRATED threshold that floats just
       above the observed nearest-impostor similarity (calibration_service), so a
       returning face actually re-matches its own gallery without hand-tuning.
    2. Multi-template gallery + best-of-set matching (search_face already returns
       per-vector hits; we take the max per identity), which tolerates the wide
       intra-person variation of low-res faces far better than one centroid.
    3. CONFIRM-ON-CAPTURE: a good face (+ body) is a Visitor immediately — being
       captured well IS the bar, not being lucky enough to re-match at 0.62.
    4. Duplicates from an online miss are folded back by DEFERRED CLUSTERING
       (dedup_service.consolidate_visitors), the source of truth for identity, so
       the gallery converges to one-identity-per-person in steady state.

`detection_conf` is a 0.0-1.0 probability from Part 1 (NOT a percentage).
`body_embedding` is accepted for signature compatibility but NEVER used to create
or join an identity (clothing/uniforms cause false merges) — identity is face-only.
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
from services import calibration_service

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
    """Open-set 1:N FACE identification, recall-first + self-calibrating.

    1. GATE: low detection_conf OR no face → UNKNOWN (no id — a non-capture).
    2. Best-of-set search; s1/s2 = top-1/top-2 cosine to DISTINCT identities.
       Feed s2 (a clean impostor sample) to the calibrator.
    3. ASSIGN to id1 iff s1 ≥ match_threshold() AND (s1−s2) ≥ margin → re-use id,
       learn the fresh view, confirm. (Returning person keeps ONE id.)
    4. Otherwise ENROLL a new Visitor NOW from this good capture (confirmed if a
       body was captured too). Duplicates are folded by deferred clustering.
    """
    # ---- 1. GATE ----------------------------------------------------- #
    if detection_conf < config.DETECTION_CONF_THRESHOLD:
        logger.info("resolve: GATED conf=%.2f < %.2f → Unknown (no id)",
                    detection_conf, config.DETECTION_CONF_THRESHOLD)
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    has_face_now = face_embedding is not None and len(face_embedding) > 0
    if not has_face_now:
        # No clear face → we could not capture this person properly → Unknown, no
        # identity. (Body/clothing never creates or joins an identity.)
        logger.info("resolve: NO-FACE (body_only=%s) → Unknown (no id)",
                    body_embedding is not None)
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    # ---- 2. OPEN-SET best-of-set search ------------------------------ #
    face_hits = await embedding_repo.search_face(face_embedding, limit=10)
    best_by_id: dict[int, float] = {}
    for iid, sim in face_hits:
        if iid not in best_by_id or sim > best_by_id[iid]:
            best_by_id[iid] = sim
    ranked = sorted(best_by_id.items(), key=lambda kv: kv[1], reverse=True)
    id1, s1 = (ranked[0] if ranked else (None, 0.0))
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0

    # The 2nd-best distinct identity is (almost always) a different person → a clean
    # impostor sample. This is what lets the threshold self-tune per deployment.
    if len(ranked) > 1:
        calibration_service.observe_impostor(s2)

    thr = calibration_service.match_threshold()
    # One structured line per decision — the primary debugging surface. Shows the
    # best/second-best identity cosines, the LIVE self-calibrated threshold, and the
    # margin, so "why did this match / why a new id?" is answerable straight from logs.
    logger.info("resolve: s1=%.3f(id=%s) s2=%.3f thr=%.3f margin=%.3f cands=%d",
                s1, id1, s2, thr, config.FACE_MATCH_MARGIN, len(ranked))

    # ---- 3. CONFIDENT re-match → ASSIGN (re-use id) ------------------ #
    if id1 is not None and s1 >= thr and (s1 - s2) >= config.FACE_MATCH_MARGIN:
        identity = await identity_repo.fetch_identity_by_id(session, id1)
        if identity is not None:
            await _assign_and_learn(session, identity, face_embedding, s1)
            classification, label = await _classify(session, identity)
            logger.info("resolve: → ASSIGN id=%d (%s) cls=%s by=face sim=%.3f",
                        identity.id, identity.display_label, classification.value, s1)
            return ResolutionResult(
                classification=classification, identity_id=identity.id,
                matched_by=MatchedBy.FACE, similarity=s1, label=label,
            )
        logger.warning("resolve: face match id=%d gone from DB — enrolling as new", id1)

    # ---- 4. NO confident match → ENROLL a new Visitor NOW ------------ #
    # A well-captured face with no existing match is a NEW person. We do NOT leave
    # them Unknown (the old bug) — being captured clearly IS the bar for a Visitor.
    return await _create_visitor(session, face_embedding, body_embedding, top_sim=s1)


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
    await embedding_repo.store_embeddings(
        new_identity.id, face_embedding=face_embedding, source="auto_first_seen",
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


async def _classify(session: AsyncSession, identity):
    """Event label for a matched identity: Employee, Visitor (confirmed), or Unknown
    (a provisional visitor — captured a face but no body yet — not yet confirmed)."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return Classification.EMPLOYEE, identity.display_label
    confirmed = await identity_repo.is_confirmed_visitor(session, identity.id)
    return (Classification.VISITOR if confirmed else Classification.UNKNOWN), identity.display_label


async def _assign_and_learn(
    session: AsyncSession, identity, face_embedding: Sequence[float], sim: float
) -> None:
    """On a confident re-match: (1) learn — add this face as a fresh gallery view
    when it isn't a near-duplicate, so future angles keep matching (this is what
    keeps a returning person on ONE id); (2) confirm a provisional visitor, since
    being re-identified is proof of a re-identifiable person. Employees are already
    confirmed and carry no visitor row."""
    if sim < config.LEARN_SIMILARITY_CEILING:      # skip near-duplicate views
        await embedding_repo.store_embeddings(identity.id, face_embedding=face_embedding, source="relearn")

    if identity.identity_type == IdentityType.EMPLOYEE:
        return
    await identity_repo.set_visitor_flags(session, identity.id, True, False)
    if not await identity_repo.is_confirmed_visitor(session, identity.id):
        if await identity_repo.confirm_visitor(session, identity.id):
            logger.info("CONFIRMED Visitor %s (id=%d): re-identified by face (sim=%.3f)",
                        identity.display_label, identity.id, sim)
