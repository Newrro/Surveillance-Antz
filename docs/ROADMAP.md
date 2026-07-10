# Identity Accuracy Roadmap — anti-fragmentation

Tracking the work to stop the failure where **one person is split into many separate
Unknowns/Visitors**. Based on `docs/identity-architecture-research.md`. Phases 1–2 are shipped on
`live-integration-gpu`; Phase 3 is the next block.

Legend: `[x]` done · `[~]` partial · `[ ]` todo

---

## Phase 0 — input pixel quality ✅ (shipped 2026-07-10)

Upstream of every model upgrade below: garbage-in limits garbage-out. Crops were
taken from a frame hard-downscaled to 720p, so distant faces were interpolated blur
before AdaFace ever saw them.

- [x] **Native-resolution decode.** `FFmpegCameraStream` ffprobes each stream and
  decodes at native res capped to `GPU_DECODE_MAX_H` (1440) instead of forcing 720p.
- [x] **Decoupled detection.** The detector runs on a downscaled copy
  (`DETECT_MAX_SIDE=960`; RT-DETR resizes internally so GPU/VRAM cost is flat); boxes
  are scaled back and face/body crops are taken from the full-res frame. Full frames
  live in system RAM, not VRAM — safe on the 6 GB card.
- [x] **Full-scene verification snapshot.** `<stem>_full.jpg` (whole frame, box drawn)
  saved per track; UI photo popup shows it beside the face/body crops.
- [ ] **Validate live** that far-camera faces are now sharp and re-match rate improves;
  retune `FACE_MIN_SHARPNESS` / upscale caps now that real detail exists.

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
- [x] **Schedule consolidation** as a periodic in-process job — `workers/consolidation.py`, started
  from the Brain lifespan (independent of the flush scheduler, so it runs in native mode).
  **Log-only by default** (`CONSOLIDATE_APPLY=0`): auto-applying a borderline face match can fold two
  people together (eval saw a 0.58 cross-person plan). Opt in with `CONSOLIDATE_APPLY=1`, which then
  uses the stricter `CONSOLIDATE_AUTO_FACE_THRESHOLD` (0.62).
- [x] **UI button** — Settings → "Merge duplicate visitors": dry-run preview lists the clusters, then
  a confirmed Apply. Human-in-the-loop (the safe path vs unattended auto-merge).

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
- [x] **Tracklet temporal feature pooling** (shipped 2026-07-10) — `pipeline.py` pools every probe's
  face embedding into a **quality-weighted centroid** (`_pooled_face`) and identifies/enrolls from it,
  instead of a single best shot. Directly targets the measured face fragmentation (one person → 4+
  Visitor ids). Verified: pooled vector beats the single best shot on cosine to the true identity;
  zero VRAM. The single best crop is still kept for the saved thumbnail.
- [x] **Quality-weighted enrollment** — pooling weights each frame by its AdaFace-norm quality, and
  stored views now carry a `quality` payload (3d).
- [ ] **SKIP by design: face+body score fusion.** Explicitly NOT implemented — letting body influence
  identity conflicts with the face-only rule the site requires (uniformed staff → body/clothing merges
  strangers). Identity stays face-only; body remains picture + same-camera re-link only.
- [ ] Replace the AdaFace-norm quality proxy with a proper **FIQA model (CR-FIQA / SER-FIQ)**. STAGED —
  needs a model download + VRAM budgeting + a live-GPU cycle; validate against the labeled eval clip.

### 3c. True MTMCT (cross-camera)
- [ ] **Camera topology + travel-time (spatio-temporal) gating** so a re-link/merge is only allowed
  between cameras a person could physically move between in the elapsed time (research: a time window
  alone gave most of the benefit).
- [ ] **Online clustering** for cross-camera association (agglomerative / DBSCAN over recent gallery)
  with **k-reciprocal re-ranking**.
- [ ] Extend the constrained re-link to **adjacent cameras** (currently same-camera only) once
  topology exists.

### 3d. Gallery hygiene at scale
- [x] **View cap + timestamped/quality payloads** (shipped 2026-07-10) — Qdrant points now carry
  `created_at` + `quality`; `enforce_view_cap` keeps the newest `GALLERY_MAX_VIEWS` (12) per identity
  per modality, pruning oldest on each insert. Bounds vector-set growth and ages out stale views.
  Best-effort (never breaks ingestion). Centroids are the mean of stored views — already
  quality-robust now that enrollment feeds *pooled* vectors.
- [ ] **Temporal decay** weighting of stale views at match time (payload `created_at` now exists to
  build on). STAGED.
- [ ] Periodic dedup/merge sweep informed by 3c clustering (supersedes the Phase-2 face-only
  consolidator).
- [ ] Revisit vector store (FAISS/Milvus) only if Qdrant gallery size becomes a bottleneck.

### 3a / 3c — STAGED (heavy: model artifacts + live-GPU validation, one at a time)
- [ ] **3a FastReID/TransReID/SOLIDER** — body ReID upgrade. Lower priority now: identity is
  face-only, so body only sharpens the same-camera re-link + picture, not identity. Do after 3b-FIQA.
- [ ] **3c camera topology + travel-time gating / online clustering / k-reciprocal** — cross-camera.
  Note: for face-showing people, cross-camera already works via the shared face gallery; the
  body-based cross-camera association is the part ruled out by the uniform constraint. Limited upside
  here — revisit only if the eval shows cross-camera face misses.

### Validation harness (do first, reused across 3a–3d) ✅ (built 2026-07-10)
- [x] **Metrics engine + scorer + exporter** — `tools/mtmct_eval/` (pure-stdlib `metrics.py`
  with self-test, `score.py` CLI) + `surveillance_brain/scripts/export_tracks.py`. Unit = track
  (`detection_id`). Reports **IDF1/IDP/IDR**, **fragmentation** (ids/person) and **purity**
  (people/id) so a change that lifts IDF1 by *merging* people (the uniform risk) is caught by purity
  dropping. Verified end-to-end on live data. See `tools/mtmct_eval/README.md`.
- [ ] **Label a clip set** (manual, user): record a fixed multi-camera clip, run pipeline+Brain,
  fill `true_person` in the exported template. Reusable across every Phase-3 run. Blocks measuring
  3a–3d.
