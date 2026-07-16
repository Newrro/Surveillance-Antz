from __future__ import annotations

from pydantic import BaseModel


class _MatchingSettings(BaseModel):
    # ---- Matching thresholds --------------------------------------------
    # Part 1 sends `detection_conf` in the range 0.0–1.0.  Below this the
    # detection is classified UNKNOWN and NO vector search is performed.
    # This gates IDENTITY, not detection quality — gating high dumped real
    # gate-distance people (detected at 0.5–0.8) into un-deduped Unknown rows.
    DETECTION_CONF_THRESHOLD: float = 0.50

    # Cosine similarity floor (1 - cosine_distance) for a FACE match.
    # AdaFace on CCTV: same-person ~0.35+, impostors <0.2. 0.65 was so strict
    # face never won and everything fell back to body ReID.
    FACE_SIMILARITY_THRESHOLD: float = 0.42

    # ── OPEN-SET 1:N face identification (IDENTITY_REDESIGN.md Phase A) ──────
    # Identity is FACE-ONLY and tuned for PRECISION. Three-way decision on the
    # tracklet's face template vs the gallery, where s1/s2 = top-1/top-2 cosine to
    # DISTINCT identities:
    #   assign to id1  iff  s1 >= FACE_ASSIGN_THRESHOLD AND (s1 - s2) >= FACE_MARGIN
    #   new provisional iff s1 <  FACE_NEW_THRESHOLD
    #   otherwise      ABSTAIN → Unknown (ambiguous; never guess, never merge)
    # The MARGIN is what rejects look-alikes; the abstain band is what stops the
    # over-merge magnets. Calibrate on tools/mtmct_eval (drive FPIR down).
    FACE_ASSIGN_THRESHOLD: float = 0.62      # confident same-person (assign + confirm)
    FACE_MARGIN: float = 0.08                # top-1 must beat top-2 by this
    FACE_NEW_THRESHOLD: float = 0.45         # below this to everyone → clearly new person
    # Quality floor (AdaFace-norm × sharpness proxy) a face must clear to be used
    # for identity at all. Low-quality faces contribute nothing (→ tracklet may be
    # faceless → Unknown). This is the single biggest precision lever.
    FACE_MIN_QUALITY: float = 18.0

    # ── Self-calibrating open-set matching (2026 architecture) ──────────────
    # Identity is decided RECALL-FIRST online, then corrected by deferred face
    # clustering (dedup_service.consolidate_visitors). The online match threshold
    # is NOT a fixed magic number: it self-calibrates per deployment to sit safely
    # ABOVE the observed nearest-impostor cosine (calibration_service), so it adapts
    # to whatever the face-similarity scale is on the cameras it's shipped to — no
    # hand-tuning, no site-specific data needed. See calibration_service.py.
    IDENTITY_SELF_CALIBRATE: bool = True
    CALIB_WARMUP: int = 150              # nearest-impostor samples before trusting calibration
    CALIB_MATCH_K: float = 2.0          # online match thr  = mean(s2) + K·std(s2)
    CALIB_MERGE_K: float = 3.0          # cluster-merge thr = mean(s2) + K·std(s2) (more conservative)
    # A good FACE capture makes someone a VISITOR immediately (the user's model:
    # "detected properly ⇒ Visitor; only truly un-captured people are Unknown").
    # Duplicates from an online miss are folded back by deferred clustering, so the
    # gallery is one-identity-per-person in steady state. Cold-start floors below are
    # used only until CALIB_WARMUP impostor samples have accrued.
    FACE_MATCH_THRESHOLD_DEFAULT: float = 0.44   # cold-start online assign floor
    FACE_MATCH_MARGIN: float = 0.05              # top-1 must beat top-2 by this (look-alike guard)
    FACE_MATCH_MIN: float = 0.38                 # clamp: never assign below this
    FACE_MATCH_MAX: float = 0.62                 # clamp: never demand more than this
    FACE_MERGE_THRESHOLD_DEFAULT: float = 0.50   # cold-start cluster-merge floor
    FACE_MERGE_MIN: float = 0.46
    FACE_MERGE_MAX: float = 0.66

    # Cosine similarity floor for a BODY ReID match — used only as a
    # fallback when the face embedding is absent or below threshold.
    # OSNet is noisy, so this must be conservative: too low merges different
    # people into one identity across cameras. 0.85 favours splitting over
    # merging — confidence over coverage (fragments are cleaned nightly).
    BODY_SIMILARITY_THRESHOLD: float = 0.85

    # Constrained body RE-LINK (identity_resolver step 4). Before creating a NEW
    # visitor from an unmatched face, we check the body vector against identities
    # seen on the SAME camera within BODY_MERGE_WINDOW_SECONDS. A hit at/above
    # BODY_MERGE_THRESHOLD re-links to that identity instead of minting a
    # duplicate. Much higher than the plain body fallback + gated by camera and a
    # short time window, so it re-joins a fragmented sighting of ONE person
    # without merging two strangers in similar clothing.
    BODY_MERGE_THRESHOLD: float = 0.82
    BODY_MERGE_WINDOW_SECONDS: int = 90

    # Track-sticky identity: Part 1's detection_id is STABLE per tracker track
    # (camera-scoped). Once a track has been resolved to an identity, later
    # payloads of the SAME track re-use that identity for this long — a track
    # that re-emits (better face, or a no-face fallback) must never mint a
    # second id or log an id-less Unknown for a person we already named.
    TRACK_STICKY_TTL_SECONDS: int = 900

    # Offline gallery consolidation (Phase 2, consolidate_identities script).
    # Two VISITOR identities whose FACE centroids match at/above this cosine are
    # judged the same person and merged (the older id kept). Higher than the live
    # match floor (0.42) so an offline batch merge is conservative — it only
    # collapses clear duplicates that slipped through, never near-strangers.
    CONSOLIDATE_FACE_THRESHOLD: float = 0.55

    # Scheduled consolidation (Phase 2): run the face-centroid merge as a periodic
    # IN-PROCESS job (must be in-process — it shares the embedded Qdrant the Brain
    # holds). SAFE DEFAULT: log the merge plan but DO NOT apply — auto-merging a
    # borderline face match can fold two people together (measured a 0.58 cross-
    # person plan). Flip CONSOLIDATE_APPLY=1 only once the threshold is trusted;
    # otherwise use the Settings → "Merge duplicate visitors" button (human review).
    # Gallery hygiene (Phase 3d): cap the stored views per identity per modality.
    # Newest kept, oldest pruned on each new insert — bounds vector-set growth and
    # lets stale views age out (a person's look drifts over days). 0 = unbounded.
    GALLERY_MAX_VIEWS: int = 12

    CONSOLIDATE_ENABLE: bool = True
    # Deferred clustering is now the SOURCE OF TRUTH for identity (it folds the
    # duplicate Visitors an online miss creates), so it applies by default and runs
    # often — the gallery converges to one-identity-per-person within a cycle. It
    # uses best-of-set face matching at the self-calibrated, conservative merge
    # threshold (calibration_service.merge_threshold), well above the impostor
    # ceiling, so auto-merge stays precise. Set CONSOLIDATE_APPLY=0 to fall back to
    # log-only if you ever want to review before merging.
    CONSOLIDATE_APPLY: bool = True
    CONSOLIDATE_EVERY_MINUTES: int = 2
    # A STRICTER threshold for unattended auto-apply than the manual button uses.
    CONSOLIDATE_AUTO_FACE_THRESHOLD: float = 0.62

    # Progressive learning: when a matched sighting scores BELOW this, store its
    # embedding(s) as an additional view for that identity, so future sightings
    # from a new angle (or with the face now visible) still match — this is what
    # stops one person fragmenting into many ids. Above it the view is a near-
    # duplicate we already have, so we skip it (keeps the vector set from bloating).
    LEARN_SIMILARITY_CEILING: float = 0.92
    # …and the FLOOR: a match may ASSIGN recall-first just above the calibrated
    # threshold (~0.44), but LEARNING that view into the gallery needs more
    # confidence — a borderline assign that turns out wrong should mislabel one
    # event, not graft a wrong face into the gallery and cascade (live-verified
    # 2026-07-14: a garbage crop assigned at 0.47 was learned and contaminated
    # the identity's templates).
    LEARN_SIMILARITY_FLOOR: float = 0.55

    # Must match Part 1's model output dimension (face + body share it here).
    EMBEDDING_DIMENSIONS: int = 512
