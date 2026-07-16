"""identity_resolver/resolve.py — the open-set 1:N face resolution orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import Classification, IdentityType, MatchedBy
from repositories import embedding_repo, identity_repo
from services import calibration_service

from .types import ResolutionResult
from .track_sticky import get_track_identity
from .helpers import _classify, _assign_and_learn
from .body_relink import _relink_by_body
from .visitor_create import _create_visitor

logger = logging.getLogger(__name__)


async def resolve(
    session: AsyncSession,
    detection_conf: float,
    face_embedding: Optional[Sequence[float]] = None,
    body_embedding: Optional[Sequence[float]] = None,
    camera_id: Optional[int] = None,
    detected_at: Optional[datetime] = None,
    detection_id: Optional[str] = None,
) -> ResolutionResult:
    """Open-set 1:N FACE identification, recall-first + self-calibrating.

    1. GATE: low detection_conf → UNKNOWN (no id — a non-capture).
       No face → track-sticky reuse, else constrained body RE-LINK, else Unknown.
    2. Best-of-set search; s1/s2 = top-1/top-2 cosine to DISTINCT identities.
       Feed s2 to the calibrator ONLY when it is below the current threshold —
       an above-threshold s2 is almost always our OWN duplicate (same person
       fragmented across two ids), not an impostor, and counting it poisons the
       impostor statistics upward.
    3. ASSIGN to id1 iff s1 ≥ match_threshold() AND ((s1−s2) ≥ margin OR s2 is
       ALSO ≥ threshold). The margin guards against look-alikes — but when both
       top candidates clear the floor they are (near-always) fragments of the
       SAME person, so refusing to assign would mint a THIRD id. Assign to the
       best and let deferred clustering fold the other one.
    4. Otherwise: same-track sticky id (a re-emit must not mint a duplicate),
       else ENROLL a new Visitor NOW from this good capture.
    """
    # ---- 1. GATE ----------------------------------------------------- #
    if detection_conf < config.DETECTION_CONF_THRESHOLD:
        logger.info("resolve: GATED conf=%.2f < %.2f → Unknown (no id)",
                    detection_conf, config.DETECTION_CONF_THRESHOLD)
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    has_face_now = face_embedding is not None and len(face_embedding) > 0
    if not has_face_now:
        # No clear face this time — but NOT necessarily a stranger. In order:
        #   a. the SAME track was already identified → keep that identity (a
        #      person we named must never also produce id-less Unknown rows);
        #   b. constrained body RE-LINK — same camera, short window, high cosine;
        #   c. genuinely uncapturable → Unknown, no identity.
        sticky = await _resolve_sticky_track(session, detection_id)
        if sticky is not None:
            return sticky
        relinked = await _relink_by_body(session, body_embedding, camera_id, detected_at)
        if relinked is not None:
            return relinked
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

    thr = calibration_service.match_threshold()
    # The 2nd-best distinct identity is an impostor sample ONLY when it sits below
    # the decision floor. Above it, s2 is nearly always a duplicate of the SAME
    # person (fragmented gallery) — feeding those high cosines to the calibrator
    # dragged the impostor mean/std up, raising the threshold, causing MORE misses
    # and more duplicates: a self-poisoning loop.
    #
    # Gate against the FIXED cold-start default, NOT the live threshold: gating on
    # the moving thr creates the opposite spiral (thr drops → sampling window drops
    # → mean drops → thr pins at the clamp floor — observed live 2026-07-14, match
    # floor stuck at 0.38). A fixed reference keeps the sample support stable.
    if len(ranked) > 1 and s2 < config.FACE_MATCH_THRESHOLD_DEFAULT:
        calibration_service.observe_impostor(s2)

    # One structured line per decision — the primary debugging surface. Shows the
    # best/second-best identity cosines, the LIVE self-calibrated threshold, and the
    # margin, so "why did this match / why a new id?" is answerable straight from logs.
    logger.info("resolve: s1=%.3f(id=%s) s2=%.3f thr=%.3f margin=%.3f cands=%d",
                s1, id1, s2, thr, config.FACE_MATCH_MARGIN, len(ranked))

    # ---- 3. CONFIDENT re-match → ASSIGN (re-use id) ------------------ #
    # Margin: top-1 must beat top-2 clearly — UNLESS top-2 also clears the floor,
    # in which case the two ids are (near-always) fragments of the same person;
    # assign to the best one and let deferred clustering fold the other.
    if id1 is not None and s1 >= thr and ((s1 - s2) >= config.FACE_MATCH_MARGIN or s2 >= thr):
        identity = await identity_repo.fetch_identity_by_id(session, id1)
        if identity is not None:
            if s2 >= thr and (s1 - s2) < config.FACE_MATCH_MARGIN:
                logger.info("resolve: duplicate-hint — ids %s both ≥ thr; clustering will fold",
                            [i for i, _ in ranked[:2]])
            await _assign_and_learn(session, identity, face_embedding, s1,
                                    body_embedding=body_embedding)
            classification, label = await _classify(session, identity)
            logger.info("resolve: → ASSIGN id=%d (%s) cls=%s by=face sim=%.3f",
                        identity.id, identity.display_label, classification.value, s1)
            return ResolutionResult(
                classification=classification, identity_id=identity.id,
                matched_by=MatchedBy.FACE, similarity=s1, label=label,
            )
        logger.warning("resolve: face match id=%d gone from DB — enrolling as new", id1)

    # ---- 4a. Same track already has an id → REUSE it (learn the view) - #
    # A track re-emitting with a better pooled face must NEVER mint a second id:
    # it is by construction the same physical person the tracker followed.
    cached_id = await get_track_identity(detection_id)
    if cached_id is not None:
        identity = await identity_repo.fetch_identity_by_id(session, cached_id)
        if identity is not None:
            await _assign_and_learn(session, identity, face_embedding, s1,
                                    body_embedding=body_embedding)
            classification, label = await _classify(session, identity)
            logger.info("resolve: → TRACK-STICKY id=%d (%s) cls=%s (track=%s, s1=%.3f)",
                        identity.id, identity.display_label, classification.value,
                        detection_id, s1)
            return ResolutionResult(
                classification=classification, identity_id=identity.id,
                matched_by=MatchedBy.FACE, similarity=s1, label=label,
            )

    # ---- 4b. Face matched nobody — but was this person JUST here? ----- #
    # Mint-averting constrained body re-link (the other half of the no-face
    # re-link, and the original anti-fragmentation design): same camera + short
    # window + high body cosine ⇒ the tracker broke and re-pooled a WORSE face
    # angle of the SAME person (verified live: one person minted twice 10 s
    # apart in identical clothes). Attribute the sighting instead of minting a
    # duplicate. The face view is NOT learned — a body link is not face proof,
    # and learning it could poison the gallery if the link is ever wrong.
    relink = await _relink_by_body(session, body_embedding, camera_id, detected_at,
                                   context="mint-avert")
    if relink is not None:
        return relink

    # ---- 4c. NO confident match → ENROLL a new Visitor NOW ----------- #
    # A well-captured face with no existing match is a NEW person. We do NOT leave
    # them Unknown (the old bug) — being captured clearly IS the bar for a Visitor.
    return await _create_visitor(session, face_embedding, body_embedding, top_sim=s1)


async def _resolve_sticky_track(
    session: AsyncSession, detection_id: Optional[str]
) -> Optional[ResolutionResult]:
    """A no-face payload whose track was ALREADY identified keeps its identity —
    the person the tracker followed did not change because their face turned away."""
    cached_id = await get_track_identity(detection_id)
    if cached_id is None:
        return None
    identity = await identity_repo.fetch_identity_by_id(session, cached_id)
    if identity is None:
        return None
    classification, label = await _classify(session, identity)
    logger.info("resolve: NO-FACE → TRACK-STICKY id=%d (%s) cls=%s (track=%s)",
                identity.id, identity.display_label, classification.value, detection_id)
    return ResolutionResult(
        classification=classification, identity_id=identity.id,
        matched_by=MatchedBy.BODY, similarity=None, label=label,
    )
