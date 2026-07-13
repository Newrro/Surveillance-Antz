# Surveillance Antz — System Guide

> Project RUNG01 · A complete walkthrough of the codebase: every part, every file,
> and the ideas that hold them together. Reflects the `live-integration-gpu` branch.
>
> A rich, navigable HTML version of this guide lives at [`docs/architecture.html`](docs/architecture.html).

```
  Cameras ──▶ Part 1: Perception ──▶ Part 2: Brain ──▶ Part 3: Interface
  (RTSP/HTTP/360°)  (detect · segment ·   (identity · tracking ·   (live grid ·
                     embed · identify)     database · API)          logs · profiles)
```

---

## Table of contents

- **Foundations** — [What this system is](#what-this-system-is) · [Architecture & data flow](#architecture--data-flow) · [Life of one detection](#life-of-one-detection) · [The two contracts](#the-two-contracts) · [Repository map](#repository-map)
- **Part 1 · Perception** — [Overview & models](#part-1--perception) · [Pipeline loop](#ingest--the-pipeline-loop) · [Identity engine](#the-identity-engine--feature_id) · [File reference](#part-1-file-reference)
- **Part 2 · Brain** — [Overview & layers](#part-2--brain) · [Database schema](#database-schema) · [Identity resolution](#identity-resolution--self-calibrating-recall-first-cluster-corrected) · [API surface](#api-surface) · [File reference](#part-2-file-reference)
- **Part 3 · Interface** — [Overview & files](#part-3--interface)
- **Shared & Ops** — [Camera registry](#the-camera-registry) · [Running the system](#running-the-system) · [Storage & retention](#storage--retention) · [Gotchas](#gotchas--teaching-notes) · [Glossary](#glossary)

---

## What this system is

Surveillance Antz is a facility surveillance system split into **three independently-buildable
parts** that talk to each other only through **two agreed JSON shapes**. That split is the single
most important idea in the project: each part can be built, tested, and run on its own against mock
data, because the boundaries between them are frozen contracts rather than shared code.

| Part | Folder | Responsibility |
|---|---|---|
| **1 — Perception** | `surveillance_AI/` | Reads cameras, finds people, turns each into a **face** and **body** embedding, assigns a local identity, POSTs a detection to the Brain. GPU-heavy. |
| **2 — Brain** | `surveillance_brain/` | FastAPI service. Re-resolves identity via vector search, records history in Postgres, tracks live presence in Redis, streams events over a WebSocket. Runs **no** ML. |
| **3 — Interface** | `surveillance_UI/` | The "Sentinel" operator console. A single-page app that reads only the Brain's API: live grid, event log, person profiles, enrollment. |

Two more folders are shared infrastructure rather than "parts": `surveillance_Camera_config/` (the
camera registry both Part 1 and Part 2 read) and `contracts/` (the two JSON schemas). The
`storage/` tree holds media (snapshots/clips) and log exports, referenced by path across parts.

---

## Architecture & data flow

The parts are strictly one-directional: cameras feed Perception, Perception POSTs to the Brain, the
Interface reads the Brain. Nothing flows backwards. Two runtime facts surprise most newcomers —
learn them early:

- **The Brain does no machine learning.** All the neural networks (person detection, face/body
  embeddings) live in Part 1. The Brain only does *vector search* over embeddings it is handed, plus
  bookkeeping. This is why the Brain runs fine in a plain container with no GPU.
- **The dashboard grid is not decoded by the UI.** The perception pipeline is the single RTSP
  consumer. It draws the real detection boxes onto each frame and writes a small annotated JPEG to
  shared memory (`/dev/shm/sentinel/<camera>.jpg`). The UI bridge just re-serves those JPEGs. So the
  picture the operator sees *is* the detector's own output, and no frame is decoded twice.

> **Mental model.** Perception answers *"what did a camera just see?"* and hands over raw
> embeddings. The Brain answers *"who was that, and where are they now?"* The Interface answers
> *"show me."* The two contracts sit exactly on those two boundaries.

---

## Life of one detection

The complete journey of a single person walking past a gate camera, end to end. This is the spine
of the whole system — everything else is detail hanging off these steps.

1. **A camera thread grabs a frame.** In Part 1, `nvr_stream.py` runs one background thread per
   camera keeping only the latest frame (GPU/NVDEC decode when available). Dead cameras fail fast and
   reconnect.
2. **The detector finds people.** `detector.py` runs RT-DETR (batched, fp16) and returns person
   boxes, filtered by an "upright, tall enough" shape test to reject clutter.
3. **A tracker gives each person a stable ID.** `tracker.py` runs **OC-SORT** (a Kalman-motion
   tracker via BoxMOT, `MotionTracker`) so a person keeps one track ID through brief occlusions and
   turns — far fewer spurious new tracks than the old IoU-only association. Identity is resolved *once
   per track* instead of flickering every frame. (`TRACKER=simple` reverts to the IoU tracker.)
4. **Identity is extracted & resolved on the BEST shot.** On `identify`-role cameras, `feature_id/`
   makes a **face** embedding (AdaFace, primary) and a **body** embedding (OSNet, fallback). Rather
   than resolving off the first (often blurry / side-on) frame, the identity worker *probes the track
   a few frames and keeps the highest-quality face* (scored by AdaFace's own feature-norm × alignment
   sharpness), then resolves from that best shot — so the same person re-matches one gallery entry
   reliably instead of fragmenting.
5. **A snapshot is saved (once per track).** A body crop and face crop are written under
   `storage/img/<camera>/`; the face thumbnail is refreshed when a better shot appears. The path rides
   along in the payload.
6. **Part 1 POSTs the detection.** The Part 1 → Part 2 contract (camera, timestamp, confidence, the
   512-d embeddings, snapshot path) hits the Brain's `POST /events`.
7. **The Brain re-resolves identity (recall-first, self-calibrating).** `identity_resolver` gates on
   confidence and requires a face (no face → Unknown, no id — that's a non-capture, not a person to
   name). It runs a **best-of-set** face search in Qdrant (the max cosine over each identity's *several*
   stored templates), and compares the top score against a threshold that **self-calibrates** per
   deployment to sit just above the impostor similarity the cameras actually produce. A confident match
   re-uses that identity (Employee/Visitor); otherwise the well-captured face is enrolled as a **new
   Visitor immediately** (confirmed the moment a body was captured too). Any duplicate an occasional
   online miss creates is folded back within minutes by deferred clustering (step 12).
8. **Presence & dedup.** Redis records "this identity is at this camera now" (TTL'd); a 30-second
   window suppresses duplicate log rows. An `is_exit_camera` sighting closes the presence session
   instead.
9. **History is written.** A row lands in the Postgres `detection_events` ledger (unless deduped).
10. **The event is broadcast.** The resolved Part 2 → Part 3 event object fans out over `WS /live`.
11. **The dashboard updates.** Part 3 folds the event into its in-memory people model and repaints
    badges/logs — while the camera tile shows the annotated frame the pipeline already wrote to
    shared memory.
12. **Deferred clustering corrects the gallery (background, every ~2 min).** A worker re-clusters the
    Visitor face templates and folds any duplicates of one person into their oldest id. This — not the
    single online decision — is the *source of truth* for identity, and it's what makes the recall-first
    online step safe: guess fast now, self-correct continuously.

---

## The two contracts

Everything hinges on two JSON shapes in `contracts/`. Freezing them on day one is what let three
teams build in parallel. Validate against them with `jsonschema.validate`.

### Part 1 → Part 2 · the detection payload (`POST /events`)

"What a camera saw." At least one embedding is required (`anyOf`); face is matched first, body is
the fallback.

| Field | Type | Meaning |
|---|---|---|
| `camera_id` | string (req) | Camera UID; must exist in the cameras table. |
| `timestamp` | ISO8601 (req) | UTC time of the detection. |
| `detection_conf` | 0.0–1.0 (req) | Detector's person-confidence. Below the gate → forced Unknown, no vector search. |
| `face_embedding` | 512 floats | AdaFace vector when a face is visible (primary signal). |
| `body_embedding` | 512 floats | OSNet vector (fallback / stored as picture). |
| `detection_id` | string | Stable id so a lingering stranger dedups correctly. |
| `snapshot_path` / `clip_path` | string | Media under the shared `storage/` tree. |

### Part 2 → Part 3 · the resolved event (`GET /events`, `WS /live`)

"Who that was." The Brain's answer after identity + tracking. This is exactly what the UI's
`upsertPerson` maps into its people model.

| Field | Meaning |
|---|---|
| `time`, `camera`, `label` | Required. Label is `Employee` / `Visitor` / `Unknown`. |
| `person_id` | Display id, e.g. `EMP-2026-0001`. Null for Unknown. |
| `identity_id` | Internal surrogate key — survives promote/demote. Null for Unknown. |
| `name`, `confidence`, `matched_by`, `similarity` | Who, how sure, and by which signal (`face`/`body`/`none`). |
| `event_id`, `duplicate` | Postgres row id (null if duplicate); dedup flag. |
| `zone_id`, `snapshot`, `clip` | Location + media pointers. |

> **On thresholds — there is no single magic number any more.** Detection is still gated at
> **0.50**. But the *face-match* threshold is **self-calibrated at runtime** (see
> [Identity resolution](#identity-resolution--self-calibrating-recall-first-cluster-corrected)); it
> floats to just above the observed impostor similarity rather than being hand-set. The old fixed
> `FACE_ASSIGN_THRESHOLD 0.62` / `FACE_NEW_THRESHOLD 0.45` are retired (kept in `config.py`, unused).
> `.env` still overrides `config.py`, so when debugging accuracy, check `GET /admin/calibration` for
> the *live* thresholds rather than trusting any prose or default (see Gotchas).

---

## Repository map

```
Surveillance-Antz/
├── surveillance_AI/            Part 1 — Perception (Python, GPU)
│   ├── nvr_stream.py           camera ingest (1 thread/cam, NVDEC)
│   ├── detector.py             RT-DETR person detection (batched, fp16)
│   ├── tracker.py              IoU tracker — stable id per person
│   ├── segmenter.py            SAM 2 masks (optional)
│   ├── pano.py                 360° → flat perspective views
│   ├── pipeline.py             THE producer: detect→identify→emit→preview
│   ├── prune_storage.py        snapshot retention (age + size cap)
│   ├── live_view.py            standalone detection viewer (QA)
│   ├── emit_example.py         mock producer (synthetic embeddings)
│   ├── models/                 model weights (gitignored)
│   └── feature_id/             the identity engine (face + body)
├── surveillance_brain/         Part 2 — Brain (FastAPI)
│   ├── api/                    routers, schemas, auth, app factory
│   ├── services/               business logic (ingestion, resolver, …)
│   ├── repositories/           thin SQL / vector wrappers
│   ├── db/                     ORM models, connection, Qdrant store
│   ├── workers/                scheduled jobs (flush, archive, retention)
│   ├── scripts/                seed.py, prune_events.py
│   └── alembic/                database migrations
├── surveillance_UI/            Part 3 — Interface (HTML/CSS/JS)
│   ├── index.html app.js api.js data.js styles.css
│   └── server.py               RTSP→MJPEG bridge + static server
├── surveillance_Camera_config/ shared camera registry (+ loader.py)
├── contracts/                  the two JSON schemas
├── storage/                    shared media + log exports
├── run.sh                      native GPU deployment orchestrator
├── run_all.sh                  dev/demo orchestrator (simulator)
└── INTEGRATION.md              all-three-parts integration guide
```

---

## Part 1 — Perception

`surveillance_AI/` turns raw camera pixels into an identified person and a detection payload. It is
the only GPU-heavy part. Its guiding principle is **face-first, body-fallback**: faces are far more
discriminative than body appearance, so a visible face gives a stable identity; the body vector is a
weaker backup.

| Stage | Model | File |
|---|---|---|
| Detection — find people | **RT-DETR** (transformer; swappable to FasterRCNN) | `detector.py` |
| Segmentation — mask a person | **SAM 2** (optional; off in live mode) | `segmenter.py` |
| Face embedding — PRIMARY id | **AdaFace** IR-101 / WebFace12M + MTCNN detector | `feature_id/face_extractor.py` |
| Body embedding — FALLBACK id | **OSNet** (torchreid, market1501) | `feature_id/extractor.py` |

> **Per-camera roles.** Each camera has a `role`: `detect` (person boxes only — cheap, for
> path/perimeter cams) or `identify` (full detect → embed → identity — for high-res gate cams). This
> is how the heavy models run only where recognition actually matters.

### Ingest & the pipeline loop

`pipeline.py` is "the producer" and the heart of Part 1. It runs several cooperating threads:

- **Detection loop** — gathers every camera whose interval has elapsed and runs them through *one
  batched* RT-DETR forward pass, then updates each camera's tracker and stores the current boxes as
  "annotations."
- **Identity worker** — a *throttled, low-priority* background thread (the grid runs on a separate
  high-priority CUDA stream and never waits on it). Each pass it picks the most-overdue stable track,
  crops the person, extracts face+body embeddings, and does **best-shot** resolution: it probes the
  track over a few frames, keeps the highest-quality face (AdaFace feature-norm × sharpness), emits
  identity from that best shot to the Brain (or the local gallery offline), and re-emits only if a
  clearly better face turns up. Once matched to a real identity the track's label is *locked* so it
  never churns. Labels may lag a few seconds by design — accuracy over immediacy.
- **Preview writers** — one thread per camera that resizes the latest frame to tile size, draws the
  current boxes on it, and writes the annotated JPEG to `/dev/shm/sentinel/<cam>.jpg`. This decouples
  smooth display FPS from the slower detection rate.

Supporting cast: `nvr_stream.py` owns camera capture (with a GPU-decode subprocess variant,
`FFmpegCameraStream`, using `hevc_cuvid`); `pano.py` carves one 360° equirectangular stream into
four flat views that behave like ordinary cameras.

### The identity engine — `feature_id/`

This package answers "who is this crop?" from two independent 512-d signals. Its public face is one
class, `Identifier` (in `identify.py`), which owns a face extractor, a body extractor, and the
gallery.

**The gallery** (`gallery.py`) is the local people database: a single human-readable
`data/gallery.json` file. Each `Person` holds a label, a name, and two lists of embedding "views" —
`face_views` and `body_views`. Matching a probe against a person takes the **maximum cosine over that
person's views**, which is the mechanism behind *progressive confidence*: more stored angles → a
better chance a new sighting matches one closely.

```json
{ "visitor_counter": 3,
  "people": [
    { "id": "EMP-001", "label": "Employee", "name": "Asha R.",
      "num_face_views": 2, "num_body_views": 1,
      "face_views": [[512 floats], "..."],
      "body_views": [[512 floats], "..."] } ] }
```

**The decision** (in `identify_features`):

1. **Quality gate.** If `detection_conf` < the threshold (0.50) → Unknown (`low_confidence`). The
   system refuses to guess on a weak detection.
2. **No features.** If neither a face nor a body embedding could be made → Unknown (`no_features`).
3. **Face match (primary).** Best face cosine ≥ `FACE_MATCH_THRESHOLD` (0.30, per-camera overridable)
   → return that person's stored label + confidence.
4. **Body match (fallback).** Else best body cosine ≥ `BODY_MATCH_THRESHOLD` (0.75) → recognize by
   body.
5. **No match.** Real features but nobody matched → auto-enroll a new Visitor.

The *label* (Employee vs Visitor) is never decided by the matcher — it's whatever was stored on the
matched person. `enroll.py` writes Employee; auto-enrolled and unmatched people are Visitor.

**Progressive learning.** When a person is recognized but not near-perfectly (`similarity <
LEARN_CEILING`, 0.92), the new embedding is a fresh angle worth remembering, so it's added as a new
view. Above the ceiling it's a redundant duplicate; below the match threshold it wouldn't have
matched at all. When a person exceeds `MAX_VIEWS_PER_PERSON` (10), the *most redundant* view is
dropped. Cross-signal back-fill also happens: a face-matched person with no body view gains the
current body vector, and vice-versa.

**Extraction detail.** `face_extractor.py` uses MTCNN to find the largest face and its 5 landmarks,
similarity-transforms it to a 112×112 aligned crop, gates on size/detector-confidence/*sharpness*,
then runs AdaFace (architecture vendored in `adaface_net.py`) to an L2-normalized 512-vec — or
returns `None` when there is no usable face. AdaFace also returns its **feature norm**, which rises
with face quality (sharp, frontal, well-lit); the extractor exposes it as `last_norm` and the pipeline
uses it as the free **best-shot quality score**. (Subtlety: the model's first output is already
unit-normalized, so quality must read the *second* output — the norm — not `‖feature‖`.) `extractor.py`
runs OSNet to a normalized body vector; comparison everywhere is a plain dot product because vectors
are pre-normalized.

### Part 1 file reference

| File | What it does |
|---|---|
| `pipeline.py` | The producer. Batched detection, per-track identity worker, per-camera preview writers, optional `POST /events`. |
| `detector.py` | `PersonDetector` — RT-DETR (default) or FasterRCNN. `detect()` / `detect_batch()`, fp16, shape filters. |
| `tracker.py` | `MotionTracker` (OC-SORT/BoxMOT, default) + `SimpleTracker` (IoU fallback). `Track` carries label, resolved-lock, best-shot buffers, and snapshot paths. |
| `nvr_stream.py` | Camera ingest. `CameraStream` (OpenCV) & `FFmpegCameraStream` (NVDEC); `start_streams()` factory; raw grid viewer. |
| `pano.py` | `PanoStream` + `PanoView` — remap one 360° feed into flat perspective cameras. |
| `segmenter.py` | `SAM2Segmenter` — per-person masks; lazy import so Part 1 runs without SAM 2 installed. |
| `prune_storage.py` | Bounds `storage/img/` by age (7 days) and a hard size cap (5 GB), oldest-first. Runs as a loop. |
| `live_view.py` | Standalone grid/fullscreen detection viewer with mask toggle. QA tool — no identity. |
| `emit_example.py` | Mock producer — synthetic embeddings POSTed to the Brain to exercise Parts 2/3. |
| `feature_id/identify.py` | `Identifier` — the face-primary/body-fallback decision engine + progressive learning. |
| `feature_id/gallery.py` | `Person` + `Gallery` — the single-JSON people DB, max-over-views matching, view pruning. |
| `feature_id/face_extractor.py` | MTCNN detect → align → sharpness gate → AdaFace 512-vec (or None). |
| `feature_id/extractor.py` | OSNet body embedding + `cosine_similarity`. |
| `feature_id/adaface_net.py` | Vendored AdaFace IR-network architecture (no weights). |
| `feature_id/config.py` | Every tunable: model paths, thresholds, quality gates, learning knobs, device. |
| `feature_id/enroll.py` | CLI to register a staff photo as an Employee (seeds face + body views). |
| `feature_id/demo.py` | Offline synthetic self-test of the body path. |

---

## Part 2 — Brain

`surveillance_brain/` is a FastAPI service with a clean four-layer shape: `api/` routers →
`services/` business logic → `repositories/` thin data wrappers → `db/` (Postgres models,
connections, and the Qdrant vector store). It ingests detections, re-resolves identity by vector
search, records history, tracks live presence, and streams events. It holds three datastores:

- **Postgres 16** — structured, permanent history: identities, people, cameras, sessions, and the
  `detection_events` ledger. No embeddings here.
- **Qdrant** — the vector DB. Two collections (`faces` and `bodies`) of 512-d cosine vectors, keyed
  by `identity_id`. Can run embedded (in-process) or as a server.
- **Redis** — live "who is where now" presence (TTL'd) and the duplicate-suppression window.
  Ephemeral by design.

### Database schema

Six tables. The keystone is `identities`: a **permanent surrogate key** with a one-to-one extension
into either `employees` or `visitors`. Promoting a visitor to an employee *mutates the type in place*
— the id and all its history and vectors survive.

| Table | Holds | Notable |
|---|---|---|
| `identities` | Surrogate id, type (visitor/employee), unique `display_label` (VIS/EMP-YYYY-NNNN). | Never recreated; 1:1 to employee/visitor. |
| `employees` | name, department, email, hired_at. | PK = identity_id. |
| `visitors` | name?, first_seen_at, and `has_face`/`has_body`/`confirmed_at`. | The confirmation flags are the Unknown→Visitor gate. |
| `cameras` | camera_uid, name, zone_id, `is_exit_camera`, stream_url, is_active. | Seeded from the shared registry. |
| `presence_sessions` | entry/exit time + camera, status (inside/exited). | Historical presence; Redis holds the live view. |
| `detection_events` | The ledger: per-detection row with classification, matched_by, similarity, snapshot. | identity_id nullable (Unknowns). The one unbounded table. |

### Identity resolution — self-calibrating, recall-first, cluster-corrected

This is the Brain's brain (`services/identity_resolver.py` + `services/calibration_service.py` +
`services/dedup_service.py`). It is **face-only for identity**: clothing/body appearance never creates
or joins an identity (uniforms make it actively harmful); the body vector is kept only as the snapshot
*picture*.

**Why the design looks the way it does.** On distant CCTV, two faces of the *same* person score only
~0.35–0.55 cosine, and impostors sit not far below — the distributions overlap and the absolute scale
shifts with every camera/lens/lighting. So there is **no fixed threshold that ships well**: set it high
and well-captured people stay "Unknown" forever; set it low and different people merge. The system
escapes that bind with three coordinated ideas rather than a better number:

**1 · Multi-template gallery + best-of-set matching.** Each identity accumulates *several* face
templates (one per good tracklet), capped at `GALLERY_MAX_VIEWS` (12), newest kept. A probe is scored
against an identity by the **maximum cosine over its templates** — far more tolerant of pose/lighting
swings than one averaged centroid. (`embedding_repo.search_face` returns per-vector hits; the resolver
takes the max per identity.)

**2 · A self-calibrating threshold (`calibration_service.py`).** The online match floor is **not**
hand-set — it floats to `mean(s2) + K·std(s2)`, where `s2` is the *second-best distinct identity's*
cosine on every query. Since at most one gallery identity is the true match, that stream of `s2` values
is a clean, decision-independent sample of the **impostor** distribution. Placing the bar a couple of
standard deviations above it means "match only if clearly more similar than a typical stranger" — which
adapts automatically to whatever similarity scale the deployed cameras produce, with **no site-specific
tuning and no camera data required up front**. Until `CALIB_WARMUP` (150) impostor samples accrue it
uses the cold-start defaults; thereafter the live value (clamped to `[FACE_MATCH_MIN, FACE_MATCH_MAX]`
= `[0.38, 0.62]`) is used. `merge_threshold()` uses a larger `K` so clustering is stricter than the
online guess.

**3 · Recall-first online, then deferred clustering as the source of truth.** The single online
decision is allowed to be imperfect, because a background job continuously re-clusters and corrects it.

The **online** decision (`resolve`), per detection:

1. **Confidence gate.** Below `DETECTION_CONF_THRESHOLD` (0.50) → Unknown, no id.
2. **No face → Unknown.** A faceless sighting is a *non-capture* — Unknown, no id (deduped upstream on
   the per-track `detection_id`). "Unknown" now means "not captured well enough to identify," which is
   exactly the operator's mental model.
3. **Best-of-set face search.** Rank distinct identities by max template cosine; `s1`/`s2` = top-1/top-2.
   Feed `s2` to the calibrator.
4. **Confident re-match → ASSIGN.** If `s1 ≥ match_threshold()` **and** `(s1 − s2) ≥ FACE_MATCH_MARGIN`
   (0.05) → re-use that identity (a returning person keeps one id), learn the fresh view (unless it's a
   near-duplicate, `LEARN_SIMILARITY_CEILING` 0.92), and confirm it.
5. **No confident match → ENROLL a new Visitor now.** A well-captured face with no match is a new
   person — enrolled immediately (`VIS-YYYY-NNNN`, first template stored). It's a **confirmed Visitor**
   the moment a body was captured too ("clear face + body ⇒ Visitor"); a face with no body stays
   provisional (shown as Unknown) until re-identified.

The **deferred** correction (`dedup_service.consolidate_visitors`, run every `CONSOLIDATE_EVERY_MINUTES`
= 2 by the `workers/consolidation.py` loop, `CONSOLIDATE_APPLY` = True): a **best-of-set** clustering
pass over all Visitor templates — each template queries the face index; an edge to another Visitor at
≥ `merge_threshold()` unions them (keep-oldest). Connected components are folded via `merge_identities`.
This is what makes "no duplicate visitors" true: any fragment an online miss created is merged back
within a cycle, so the gallery converges to **one identity per person in steady state**. It runs
in-process (it shares the Brain's embedded Qdrant, which a standalone script can't open) and can also
be triggered by hand via `POST /admin/consolidate`.

A person moves through three states:

- **Unknown** — either a faceless non-capture (no id at all), or a provisional visitor (a face but no
  body yet, `confirmed_at` NULL). Provisional rows are swept nightly if never confirmed.
- **Visitor (confirmed)** — a proper capture (clear face + body). Re-identifiable by face on any future
  day; kept as one id by best-of-set matching + deferred clustering.
- **Employee** — enrolled from a photo, or promoted from a visitor. Always "confirmed."

**Debugging surfaces (built for this).** Every decision prints one line —
`resolve: s1=… s2=… thr=… margin=… cands=…` then `→ ASSIGN id=…` or `New face → VISITOR …`. Live
calibration state is at `GET /admin/calibration` (impostor sample count, warmed-up flag, live match &
merge thresholds), and the consolidation loop logs a `calibration: {…}` line every cycle. If people
fragment or over-merge, read those together: they tell you the exact score and the exact live threshold
it was compared against.

**Duplicate suppression.** Part 1 emits one payload per crop per frame; a Redis `SET NX EX 30s` key
logs the first and suppresses the rest (keyed on identity+camera, or on `detection_id` for unknowns).
Presence still updates on every detection — only the ledger write and broadcast are deduped, and it
fails *open* if Redis is down.

**Presence & exit cameras.** A sighting on an `is_exit_camera` closes the open presence session and
evicts the Redis key; any other camera refreshes Redis and opens a session if none is open. At most
one open session per identity.

### API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/events` | — | Ingest one detection (resolve → track → dedup → log → broadcast). |
| `GET` | `/events` | — | Filtered event feed, newest-first. |
| `GET` | `/person/{id}` | — | Profile: history, sessions, photos, live presence. |
| `GET` | `/employees` | — | List enrolled employees. |
| `POST` | `/employees` | admin | Enroll a new employee (stores a Part-1 embedding). |
| `WS` | `/live` | — | Real-time event stream to Part 3. |
| `GET` | `/search?q=` | — | "Where is X now?" by name / EMP-ID / VIS-ID. |
| `POST` | `/identities/{id}/promote` | admin | Visitor → Employee (preserves id + history). |
| `POST` | `/identities/{id}/demote` | admin | Employee → Visitor. |
| `POST` | `/identities/{id}/name` | — | Set a friendly name (keeps id/label). |
| `GET` | `/logs/individual` | admin | Per-identity session log with durations. |
| `GET` | `/logs/facility` | admin | Facility-wide CSV export. |
| `POST` | `/admin/reset` | admin | Wipe people/events/sessions/vectors (keeps cameras). |
| `POST` | `/admin/clear-unknowns` | admin | Manual version of the nightly unknown sweep. |
| `POST` | `/admin/consolidate` | admin | Manually run the deferred best-of-set Visitor clustering. `?apply=true` to execute; dry-run otherwise. (Also runs automatically every 2 min.) |
| `POST` | `/identities/merge` | admin | Manually fold selected duplicate ids into a primary (human-decided, no similarity gate). |
| `POST` | `/identities/delete` | admin | Permanently delete selected identities (events, sessions, vectors, row). |
| `GET` | `/admin/calibration` | — | Live self-calibration state — impostor sample count, warmed-up flag, live match & merge thresholds. Read-only debug. |
| `GET` | `/health` | — | DB + Redis + Qdrant liveness. |

### Part 2 file reference

| File | What it does |
|---|---|
| `config.py` | pydantic-settings config; thresholds, DSNs, cron, retention. All env-overridable. |
| `api/main.py` | App factory + lifespan (init DB, ensure Qdrant collections, start schedulers), `/health`. |
| `api/schemas.py` | Pydantic request/response models — including the two contract shapes. |
| `api/auth.py` | HTTP Basic auth dependency for admin endpoints. |
| `api/routers/*.py` | events, person, employees, live, search, identities, logs, admin. |
| `services/ingestion_service.py` | Orchestrates `POST /events`: camera → resolve → track → dedup → log → broadcast. (Template learning now lives in the resolver.) |
| `services/identity_resolver.py` | The classification brain — recall-first best-of-set face matching at the self-calibrated threshold, confirm-on-capture, always-enroll templates. |
| `services/calibration_service.py` | Self-tuning thresholds: running impostor (2nd-best) similarity stats → live `match_threshold()` / `merge_threshold()`. No hand-tuning, adapts per site. |
| `services/session_tracker.py` | Opens/continues/closes presence sessions; exit-camera logic. |
| `services/dedup_service.py` | Redis NX-EX duplicate window; the admin `merge_identities`/`purge_identity`; and `consolidate_visitors` — the best-of-set deferred face clustering. |
| `services/presence_cache.py` | Redis "where is X now" — touch/get/evict, TTL from config. |
| `services/live_broadcaster.py` | In-process async pub/sub fan-out to `WS /live` subscribers. |
| `services/log_service.py` | Joined event feed, per-identity session log, facility CSV. |
| `services/search_service.py` | Live "find X" + full person profile assembly. |
| `services/conversion_service.py` | promote / demote / rename (id preserved); anonymize is a V2 stub. |
| `services/enrollment_service.py` | Enroll an employee + store their embedding in Qdrant. |
| `services/archive_service.py` | Incremental JSONL + per-person datasheet exports (Postgres untouched). |
| `repositories/*.py` | identity, embedding, event, camera, session — thin SQL / vector wrappers, no business logic. |
| `db/models.py` | SQLAlchemy 2.0 ORM — the six tables + enums. |
| `db/connection.py` | Async engine + session context managers. |
| `db/vector_store.py` | Qdrant wrapper — ensure/upsert/search/delete; embedded or server mode. |
| `workers/midnight_flush.py` | Scheduler: midnight flush + clear-unknowns, archive (30 min), retention (delete old events). |
| `workers/consolidation.py` | In-process loop (every 2 min) running the deferred best-of-set clustering at the self-calibrated merge threshold; folds duplicate Visitors. The source of truth for identity. |
| `scripts/seed.py` | Idempotent camera seed from the shared registry (no people by default). |
| `scripts/prune_events.py` | Postgres-only event retention for native mode (scheduler is off there). |
| `alembic/` | Migrations — initial 6-table schema + the visitor-confirmation columns. |

---

## Part 3 — Interface

`surveillance_UI/` is the "Sentinel" console — a plain single-page app, no framework, three script
files loaded in order: `data.js` declares the mutable data model (and demo/mock data), `api.js`
fills it from the Brain, `app.js` renders it. Its design principle is **opt-in with safe fallback**:
if the Brain is unreachable, the UI silently runs on mock data as a static prototype.

**How it wires to the Brain.** `login()` (client-side `admin/password123`, a prototype gate) reveals
the app, then `api.js` gates on `GET /health` returning `database:"ok"`. Only then does it hydrate
from `GET /events` + `GET /employees`, wiping the mock data in place, and open `WS /live` to fold in
new events. The Brain URL is resolved from, in order: a `?brain=` query param → `localStorage` →
`window.BRAIN_URL` → the same host on port 8000 → localhost.

**The camera grid & the "polled stills" trick.** Although the bridge offers a true MJPEG stream at
`/stream/<id>`, the browser deliberately uses **polled stills** instead: it fetches `/snapshot/<id>`
every ~350 ms for visible tiles only and hot-swaps the decoded image. Why: an open MJPEG connection
per tile would exhaust the browser's ~6-connections-per-origin limit and wedge the page. Tile DOM is
rebuilt only when the camera set changes; otherwise just the badges/counts update — so feeds never
flicker.

| File | What it does |
|---|---|
| `index.html` | The whole SPA shell — every view, modal, and sidebar; JS toggles visibility. |
| `data.js` | Mutable data model + mock fallback: `PEOPLE`, `DETECTIONS`, derived helpers (inside count, visits today). |
| `api.js` | The Brain integration layer — URL resolution, hydrate, live WS, event→person mapping, admin writes. |
| `app.js` | All behaviour — view routing, grid/feed rendering, person profiles, the "TRACK" auto-track animation. |
| `server.py` | The RTSP→MJPEG bridge + static server. Re-serves the pipeline's `/dev/shm` JPEGs; owns `/api/cameras`. |
| `styles.css` | Dark control-room glassmorphism; the green/gold/red identity-state color language. |

> **The color language.** The whole console speaks in three status colors that mean real domain
> states: **green = Employee/verified**, **gold = Visitor**, **red = Unknown/flagged**.

---

## The camera registry

`surveillance_Camera_config/` is the one source of truth for cameras, read by all three parts. It is
deliberately **split into two files** so credentials never reach git:

- `cameras.json` (committed) — metadata: `camera_uid`, name, zone, `is_exit_camera`, `is_active`,
  and the Part-1-only `role` and `match_threshold`.
- `cameras.secrets.json` (gitignored) — per-uid `ip`, `username`, `password`, `rtsp_path`.

`loader.py` joins them on `camera_uid` and builds the `stream_url` at runtime — **URL-encoding the
credentials** so an `@` in a password can't corrupt the RTSP URL. Three source types are supported:
`rtsp` (built from secrets), `url` (a ready-made HTTP/RTSP URL), and `pano` (one 360° camera fanned
into several flat views, each a normal camera). `to_brain_records()` projects cameras into exactly
the shape the Brain's seed script wants — dropping the Part-1-only fields.

> **Why UIDs must match.** Because Part 1, Part 2, and the UI bridge all read this same registry, a
> detection's `camera_id` lines up across the whole system — which is what lets a live event overlay
> on the correct dashboard tile.

---

## Running the system

Two orchestrators, for two very different situations:

**`run.sh` — real deployment.** The native GPU box. Boots the Brain (native + embedded Qdrant), the
UI bridge, the *real* perception pipeline against live cameras, and the two retention loops. Manages
long-lived processes via PID files. Postgres/Redis are expected as OS services.

```bash
./run.sh start | stop | status
./run.sh logs pipeline
```

**`run_all.sh` — dev/demo.** A laptop with no cameras. Auto-detects Docker (Compose) or falls back
to native for the Brain, serves the UI, and runs `tools/integration_sim.py` — a mock Part 1 that
streams the exact contract with a mix of Employee/Visitor/Unknown detections.

```bash
./run_all.sh
./run_all.sh --no-sim | down
```

The Brain itself can come up two ways: **Docker** (`docker compose up` — Postgres + Redis + Qdrant
server + app) or **native** (`run_native.sh` — OS Postgres + Redis, and Qdrant embedded in-process).
In native mode the background scheduler is disabled (embedded Qdrant is single-process), which is why
`run.sh` runs standalone retention pruners instead.

---

## Storage & retention

The shared `storage/` tree holds media written by Part 1 (`storage/img/<camera>/` snapshots) and log
exports written by Part 2 (`storage/logs`, `storage/datasheet`). On a 24/7 system these grow without
bound, so retention runs at several levels:

- **Write-time dedupe** — at most one body + one face crop per *track*, so an Unknown person
  lingering in view doesn't flood the disk.
- **Snapshot pruning** (`prune_storage.py`) — deletes images older than `RETENTION_DAYS` (7) and
  enforces a hard `STORAGE_MAX_GB` ceiling (5 GB), oldest-first.
- **Event pruning** — the Brain deletes `detection_events` older than the retention window (via the
  scheduler in Docker mode, or `prune_events.py` in native mode). Rows are archived to JSONL first.
- **Nightly flush** — closes dangling sessions, evicts Redis presence, and clears all unconfirmed
  Unknowns. Confirmed Visitors and Employees survive.

Qdrant vectors don't need ageing: they're keyed per-identity (bounded by confirmed people), and
unknowns are cleared nightly.

---

## Gotchas & teaching notes

- **`.env` OVERRIDES `config.py` — check it first.** The Brain uses pydantic `env_file=".env"`, so
  `surveillance_brain/.env` wins over the code defaults at runtime. This bit hard: `.env` was stuck at
  a stale **0.80 / 0.65 / 0.60** (detection / face / body) while the code intended **0.50 / 0.42 /
  0.85**, and the 0.65 face floor was *the* main cause of one person fragmenting into many Visitors.
  `.env` is now realigned. When tuning accuracy, confirm which value is actually *loaded*
  (`python -c "import config; print(config.FACE_SIMILARITY_THRESHOLD)"`), not just what `config.py`
  says.
- **Identity is face-only now.** Body ReID never creates *or* joins an identity (uniforms merge
  strangers); the body vector is stored only as the snapshot picture. The old constrained body re-link
  and `services/feature_matcher.py` are retired/unused. `anonymize_identity` (right-to-be-forgotten)
  remains a V2 stub that raises `NotImplementedError`.
- **There's no fixed match threshold — it self-calibrates.** Don't go looking for "the face threshold"
  to tune; the online floor is computed at runtime from the impostor-similarity stream
  (`calibration_service`). To see the *live* value, hit `GET /admin/calibration` or read the
  `resolve: … thr=…` log lines — not `config.py`. The cold-start defaults only apply before
  `CALIB_WARMUP` (150) samples accrue.
- **Duplicates are expected briefly, then auto-merged.** Recall-first matching can split one person
  into two Visitors on an online miss; the deferred clustering worker (every 2 min) folds them back.
  So judge "duplicates" over a few minutes, not instantaneously. If they *persist*, the merge threshold
  is too high (check `/admin/calibration`) or clustering isn't applying (`CONSOLIDATE_APPLY`).
- **Anti-fragmentation is four layers.** OC-SORT tracker (stable IDs) → best-shot tracklet resolution
  (highest-quality face) → recall-first best-of-set online matching (re-use the id when plausibly the
  same) → deferred best-of-set clustering every 2 min (fold any that still split). If "one person →
  many ids" reappears, debug in that order, reading the `resolve:` + `calibration:` logs together.
- **A worst-case escape hatch.** Auto-merge (`CONSOLIDATE_APPLY=True`) can in principle fold two
  different people together. If a remote tester ever sees that, set `CONSOLIDATE_APPLY=0` (falls back to
  log-only) and merge/split by hand via the dashboard.
- **"Detection" vs "identity" confidence.** Two different numbers. Detection confidence = the
  detector's "is this a person?" (gates whether we even try to identify). Identity confidence = the
  cosine similarity "is this Asha?" Don't conflate them when reading logs or tuning.
- **The grid is the pipeline's output.** If the dashboard tiles are laggy or low-res, the fix is in
  Part 1's pipeline (preview resolution, detection rate, GPU decode), *not* the UI — the UI only
  re-serves what the pipeline drew.

---

## Glossary

| Term | Meaning |
|---|---|
| embedding | A 512-number vector describing a face or body, so similarity is a cosine/dot product. |
| AdaFace | The face-recognition model (IR-101 / WebFace12M) — the primary identity signal. Not InsightFace. |
| MTCNN | Face *detector* + landmark finder (used only to locate/align a face for AdaFace). |
| OSNet | Body re-identification model (torchreid) — the fallback signal / stored picture. |
| RT-DETR | The transformer person *detector* (finds boxes). SAM 2 masks them (optional). |
| gallery | Part 1's *local* people database (`gallery.json`) — the standalone-mode counterpart to the Brain's Qdrant. |
| identify vs detect | A camera role. `identify` runs the full recognition pipeline; `detect` only finds people. |
| confirmed visitor | A visitor captured with a clear face + body — a proper capture, re-identifiable across days. |
| best-of-set matching | Scoring a probe against an identity by the MAX cosine over its several stored templates (not one averaged centroid) — tolerates pose/lighting variation on low-res faces. |
| self-calibrated threshold | The face-match floor, computed at runtime as `mean(s2) + K·std(s2)` over the impostor-similarity stream, so it adapts per deployment with no hand-tuning. See `calibration_service.py`. |
| deferred clustering | The background job (every 2 min) that re-clusters Visitor face templates and folds duplicates into one id — the source of truth for identity. |
| presence session | A Postgres record of one entry→exit; Redis holds the live "who's inside now" view. |
| `/dev/shm/sentinel` | Shared-memory folder where the pipeline writes annotated tile JPEGs for the UI bridge. |

---

*Surveillance Antz · Project RUNG01 · Cameras → Perception → Brain → Interface. Trust the code
constants over prose where they disagree.*
