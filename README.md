# Surveillance Antz — Project RUNG01

An AI-driven facility surveillance system, split into **three independently
buildable parts** that communicate over two agreed JSON contracts. Cameras
see people; the system remembers who they are, where they are, and keeps the
log of everything that happened.

```
  Cameras (RTSP) ─▶ Part 1: Perception ─▶ Part 2: Brain ─▶ Part 3: Interface
                    (detect/segment/       (identity +       (dashboard,
                     embeddings)            tracking + DB)     live feed, logs)
```

> **New to the codebase?** Read [`ARCHITECTURE.md`](ARCHITECTURE.md) — a full teaching
> walkthrough of every part and file (rich HTML version at [`docs/architecture.html`](docs/architecture.html)).

## The three parts

| Part | Folder | Owner | Responsibility | Status |
|---|---|---|---|---|
| **1 — Perception** | [`surveillance_AI/`](surveillance_AI/) | Prithvi + Tushar | RTSP ingest → detect → segment → **face (AdaFace) + body (OSNet) embeddings** → identity → emits detection JSON | ✅ implemented |
| **1b — Camera config** | [`surveillance_Camera_config/`](surveillance_Camera_config/) | Prithvi + Tushar | Camera registry (uid, RTSP url, zone, is_exit) shared by Parts 1 & 2 | ✅ implemented |
| **2 — Brain** | [`surveillance_brain/`](surveillance_brain/) | **Saaketh** | Identity resolution, tracking, dedup, DB (Postgres + **Qdrant** + Redis), logs, REST + WS API | ✅ implemented |
| **3 — Interface** | [`surveillance_UI/`](surveillance_UI/) | Ammarath | Live feed, log table, person profiles, employee enrollment — reads Part 2's API only | 🟡 skeleton |

> **Parts 1, 1b and 2 are implemented; Part 3 (Interface) is scaffolded**
> (README + contract) so its team starts from the same base. Part 1's
> perception pipeline is built on the working gate-camera detector.

## How the parts connect — the two contracts

Everything hinges on two JSON shapes, defined once in [`contracts/`](contracts/)
so each team can build and test against mock data without waiting on the others.

1. **Part 1 → Part 2** — the detection payload (`POST /events`):
   `detection_id`, `camera_id`, `timestamp`, `detection_conf` (0–1),
   `face_embedding` (512), `body_embedding` (512), `snapshot_path`, `clip_path`.
   → [`contracts/part1_to_part2.event.schema.json`](contracts/part1_to_part2.event.schema.json)

2. **Part 2 → Part 3** — the event object (`GET /events`, `WS /live`,
   `GET /person/{id}`, `GET/POST /employees`).
   → [`contracts/part2_to_part3.event.schema.json`](contracts/part2_to_part3.event.schema.json)

Shared media (snapshots/clips) live under a common `storage/` path referenced
by those paths — see [`contracts/README.md`](contracts/README.md).

## Run the Brain (Part 2)

```bash
cd surveillance_brain
cp .env.example .env
docker compose up --build      # Postgres + Redis + Qdrant + FastAPI on :8000
```

Then open <http://localhost:8000/docs>. Full details in
[`surveillance_brain/README.md`](surveillance_brain/README.md).

## Suggested milestones

| Week | Part 1 (Perception) | Part 2 (Brain) | Part 3 (Interface) |
|---|---|---|---|
| 1 | Read 1 camera, show frames | DB schema + mock ingest | Static UI on mock data |
| 2 | Detect + segment person | Embeddings + ID assignment | Live table + filters |
| 3 | Extract embeddings, emit JSON | Tracking + duplicate merge | Wire to real API + WebSocket |
| 4 | End-to-end integration on real gate footage | | |

## License

MIT — see [LICENSE](LICENSE).

## 2026-07 upgrade: sightings, evidence groups, and report operations

**Every detected human is logged immediately** — as soon as a track is stable
(`IDENTITY_MIN_HITS` detections, default 3), the pipeline posts an observation
to `POST /events` (embeddings optional) and captures **one immutable evidence
set** written atomically (temp file → rename), all from the same moment:

    {stem}_body.jpg    body crop exactly as captured
    {stem}_face.jpg    face crop (only when a face was captured)
    {stem}_orig.jpg    ORIGINAL full frame — untouched, native resolution
    {stem}_annot.jpg   separate annotated copy (box drawn, may be downscaled)
    storage/clips/…    pre/post-roll MP4 (per-camera in-memory ring buffer)

Evidence sets are never overwritten — a later/better frame is a NEW sighting.
The Brain stores every path explicitly (`face_path`, `body_path`,
`full_frame_path`, `full_frame_annotated_path`, `clip_path` on
`detection_events`); no consumer derives one file from another's name.
Detect-only cameras emit sightings too (they skip identity). Faceless people
become persistent **Unknown cases** (`UNK-YYYY-NNNN`): one mergeable record per
person, grouped by track continuity plus a constrained body link (same camera,
≤`BODY_MERGE_WINDOW_SECONDS`, cosine ≥`BODY_MERGE_THRESHOLD`; body similarity
NEVER identifies anyone across cameras/days). When a face later resolves on the
same track, the whole case folds onto the person automatically (audited).

**Report UI**: the person photo popup is a **sighting carousel** — Previous/Next
arrows + keyboard ←/→ move the WHOLE evidence set together, with
`Sighting N of M`, timestamp, camera, confidence and track id. All evidence
renders `object-fit: contain` (never cropped/sliced), clicks through to the
original file, and a faceless sighting says "No face captured for this
sighting" instead of borrowing one. Per-sighting actions: **Hide** (the default
delete — soft, reasoned, audited, reversible), **Reassign to…**, and **Not this
person** (split to a new Unknown case). `Merge into…` (search by EMP-/VIS-/UNK-
id or name) supports every category combination — an employee only survives as
primary. Merges audit to `audit_log` and `POST /identities/unmerge` reverses
them (vectors folded into a capped gallery are not split back — see
limitations). The old identity-wide delete is an explicit type-ERASE action.
The client ships **no hardcoded credentials** — login verifies against the
Brain and admin calls use per-session Basic auth (the Brain records the actor
in the audit log).

**Unknown-case review**: Settings → *Unknown-case link review* lists suggested
body-similarity links (score in [`BODY_REVIEW_THRESHOLD`, auto bar), same
camera, recent) with both parties' evidence thumbnails; approval merges
(audited, undoable), nothing is auto-applied.

**Bulk employee import**: Settings → *Bulk employee import* — XLSX/CSV roster
or ZIP (roster + photos `EXTID.jpg` / `EXTID_1.jpg`). `external_id` is the
idempotency key (re-import updates, never duplicates); always previews with
row-level errors before applying; multiple photos per employee supported.

**Detection & live**: per-camera geometry gates in `cameras.json`
(`roi`, `exclude`, `min_height_frac`, `min_aspect`; global defaults
`MIN_HEIGHT_FRAC`/`MIN_ASPECT`), stale-frame dropping (`STALE_FRAME_MAX_S`),
explicit tracker backend (boxmot/OC-SORT pinned; startup FAILS rather than
silently degrading — `TRACKER=simple` or `TRACKER_ALLOW_FALLBACK=1` to opt in,
flagged in metrics). Live boxes come only from real tracker output
(`GET :8080/tracks/<CAM>`); diagnostics at `GET :8080/diag` (per-camera
detections/tracks/observations/emits, frame age, detect-ms EMA, identity queue
depth, GPU memory, tracker backend).

### Migration
```bash
cd surveillance_brain && .venv/bin/python -m alembic upgrade head
# 0003: sightings columns + unknown_cases + audit_log + employees.external_id
# 0004: explicit evidence media paths + one-time legacy backfill
```
Existing rows stay readable: 0004 backfills `body/face/full_frame` paths from
the legacy stem convention once, so old evidence keeps displaying.

### New configuration (env; defaults in code)
| Variable | Default | Meaning |
|---|---|---|
| `CLIP_ENABLE` / `CLIP_FPS` | `1` / `6` | short-clip recorder on/off + frame rate |
| `CLIP_PRE_S` / `CLIP_POST_S` | `4` / `4` | seconds of pre/post-roll around a sighting |
| `CLIP_MAX_CONCURRENT` | `3` | parallel clip writers (excess sightings skip clips) |
| `FULL_FRAME_ORIG_QUALITY` | `92` | JPEG quality of the untouched original frame |
| `STALE_FRAME_MAX_S` | `2.0` | skip detection when the newest decode is older |
| `MIN_HEIGHT_FRAC` / `MIN_ASPECT` | `0` / `0` | global shape gates (per-camera overrides win) |
| `TRACKER` / `TRACKER_ALLOW_FALLBACK` | `ocsort` / `0` | backend + explicit degraded opt-in |
| `METRICS_INTERVAL` | `2.0` | seconds between metrics.json dumps |
| `BODY_REVIEW_THRESHOLD` (Brain) | `0.65` | suggested-links floor for the review screen |
| `BODY_REVIEW_WINDOW_MINUTES` (Brain) | `240` | how far back suggestions look |

### Tests
```bash
surveillance_brain/.venv/bin/python -m pytest surveillance_brain/tests -q  # 27: API + acceptance (live DB; isolated Qdrant)
surveillance_AI/venv/bin/python -m pytest surveillance_AI/tests -q         # 9: tracker scenarios (both backends)
python3 -m pytest surveillance_UI/tests -q                                  # 9: UI evidence rules + contracts
```

### Diagnostics / benchmarks
```bash
curl -s localhost:8080/diag | python3 -m json.tool        # live pipeline metrics
curl -s localhost:8000/admin/calibration                  # identity thresholds
grep -E 'ASSIGN|MINT-AVERT|BODY-RELINK|UNKNOWN CASE' runtime/logs/brain.log  # decision mix
# identity-accuracy harness (labelled clips): tools/mtmcteval/ — see its README
```

### Known accuracy limitations (honest)
- Distant-CCTV faces of the same person can score 0.25–0.45 cosine across
  tracklets — below any safe threshold. Residual duplicate visitors still occur
  and are folded later by consolidation, the review screen, or manual merge.
- Body similarity sits in an irreducible overlap zone around 0.70–0.75
  (measured same-person 0.735/0.742 vs different-person 0.731). The 0.75
  auto-link bar deliberately misses some same-person pairs rather than merge
  strangers; uniformed staff should be enrolled as employees.
- Unmerge cannot split face templates back out of the primary's capped gallery;
  the revived identity re-accumulates templates on its next sightings.
- Clips are best-effort: saturated writers (> `CLIP_MAX_CONCURRENT`) skip the
  clip rather than block ingest; the clip file appears seconds after the row.
- The crossing-people scenario can still momentarily swap OC-SORT ids (motion-
  only tracker limit); the pair never fuses into one id (tested).
