# Identity Accuracy Roadmap — anti-fragmentation

Tracking the work to stop the failure where **one person is split into many separate
Unknowns/Visitors**. Based on `docs/identity-architecture-research.md`. Phases 1–2 are shipped on
`live-integration-gpu`; Phase 3 is the next block.

Legend: `[x]` done · `[~]` partial · `[ ]` todo

---

## Phase 1 — stop fragmentation at the source ✅ (shipped)

- [x] **Appearance-stable tracker.** Replaced IoU-only `SimpleTracker` with **OC-SORT**
  (`MotionTracker`, BoxMOT) — Kalman motion + observation-centric recovery, no re-ID model, no extra
  VRAM. Stable IDs through occlusion. `TRACKER=simple` reverts.
- [x] **Threshold fix (biggest single cause).** `.env` overrode `config.py` with a stale **face 0.65 /
  det 0.80**; realigned to the intended **0.42 / 0.50 / 0.85**. The 0.65 face floor was why the same
  person never re-matched.
- [x] **Best-shot tracklet identity.** Probe a track a few frames, keep the highest-quality face
  (AdaFace feature-norm × sharpness), resolve from the best shot; re-emit only on a clearly better
  face. Fixed a bug where the extractor discarded AdaFace's norm and measured the already-normalized
  vector (≈1).
- [x] **Constrained body re-link (Brain).** Before minting a duplicate Visitor on a face miss, re-link
  by body if the same person was seen on the **same camera within 90 s at cosine ≥ 0.82**. Camera +
  time + high-cosine gate = safe. Verified live (`body sim=0.891` join).

## Phase 2 — clean up what slips through ✅ (shipped)

- [x] **Offline gallery consolidator.** `dedup_service.consolidate_visitors` folds Visitors with
  matching **face-gallery centroids** (≥ 0.55) into the oldest id, wiring `merge_identities`. Exposed
  as `POST /admin/consolidate` (dry-run default, `?apply=true`), in-process so it shares the embedded
  Qdrant.
- [ ] **Schedule consolidation** as a periodic in-process job (e.g. hourly / nightly with the midnight
  flush) instead of manual-only.
- [ ] **UI button** for consolidate (dry-run preview → apply) in the admin panel.

---

## Phase 3 — production-grade multi-camera identity 🔜 (next)

Heavier models + true cross-camera association. Each item needs its own VRAM budgeting and a test
cycle on the 6 GB card — do **not** land these together; validate one at a time against a day of real
footage.

### 3a. Re-ID model upgrade (body signal quality)
- [ ] Evaluate **FastReID** (or **TransReID** / **SOLIDER**) to replace OSNet — measure Market-1501 /
  MSMT17 rank-1 + latency on the 4050, pick the best that fits alongside AdaFace + RT-DETR in 6 GB.
- [ ] Raise/retune `BODY_*` thresholds for the new embedding's score distribution.

### 3b. Face quality & fusion
- [ ] Replace the AdaFace-norm quality proxy with a proper FIQA model (**CR-FIQA** or **SER-FIQ**) for
  best-shot selection and multi-shot aggregation.
- [ ] **Quality-weighted score fusion** of face (primary) + body (secondary) with explicit decision
  rules, instead of face-first-then-body.
- [ ] Tracklet **temporal feature pooling** (weighted centroid over a track's frames) rather than a
  single best shot.

### 3c. True MTMCT (cross-camera)
- [ ] **Camera topology + travel-time (spatio-temporal) gating** so a re-link/merge is only allowed
  between cameras a person could physically move between in the elapsed time (research: a time window
  alone gave most of the benefit).
- [ ] **Online clustering** for cross-camera association (agglomerative / DBSCAN over recent gallery)
  with **k-reciprocal re-ranking**.
- [ ] Extend the constrained re-link to **adjacent cameras** (currently same-camera only) once
  topology exists.

### 3d. Gallery hygiene at scale
- [ ] **Quality-weighted centroids** + **temporal decay** of stale views; cap views/identity.
- [ ] Periodic dedup/merge sweep informed by 3c clustering (supersedes the Phase-2 face-only
  consolidator).
- [ ] Revisit vector store (FAISS/Milvus) only if Qdrant gallery size becomes a bottleneck.

### Validation harness (do first, reused across 3a–3d)
- [ ] Label a small multi-camera clip set with ground-truth identities; script IDF1 / ID-switch /
  fragment-count metrics so each change is measured, not eyeballed.
