# Surveillance Brain — Project RUNG01 · Part 2

> **Database + Tracking + Identity engine** for an AI-driven facility surveillance network.
> PostgreSQL 16 · Qdrant (vector DB) · Redis 7 · Python 3.12 · FastAPI · Alembic

This is **Part 2** of the three-part RUNG01 system. It sits between:

- **Part 1 — Perception** (`surveillance_AI/`): cameras → detect → segment → **face + body embeddings**. Sends detections here.
- **Part 3 — Interface** (`surveillance_UI/`): the dashboard. Reads events, profiles, and the live feed from here.

The AI edge is stateless — it sees a person, produces embeddings, and forgets. **This Brain remembers**: it decides *who* each person is, tracks *where they are*, and keeps the *log of everything that happened*.

---

## Table of Contents

1. [What changed vs. the old single-DB Brain](#1-what-changed)
2. [Quick Start (Docker Compose)](#2-quick-start-docker-compose)
3. [Architecture](#3-architecture)
4. [Data model & storage](#4-data-model--storage)
5. [Contracts — the JSON shapes that keep the 3 parts unblocked](#5-contracts)
6. [API reference](#6-api-reference)
7. [Configuration](#7-configuration)
8. [Project structure](#8-project-structure)
9. [Testing & CI](#9-testing--ci)
10. [Operational runbook](#10-operational-runbook)
11. [Scalability notes](#11-scalability-notes)
12. [Roadmap (V2)](#12-roadmap-v2)

---

## 1. What changed

Relative to the earlier single-Postgres Brain, this version implements the RUNG01 architecture:

| Area | Old | Now |
|---|---|---|
| Embeddings | Postgres + pgvector, one vector | **Qdrant**, two collections: `faces` + `bodies` |
| Input | single `embedding`, `confidence` 0–100 | `face_embedding` + `body_embedding`, `detection_conf` **0.0–1.0**, `detection_id`, `snapshot_path`, `clip_path` |
| Matching | one cosine search | **face-primary, body-fallback** |
| Duplicates | — | short-window **duplicate guard** (module 6) |
| Output | `/search`, `/logs` | adds **`GET /events`**, **`GET /person/{id}`**, **`GET/POST /employees`**, **`WS /live`** |
| Storage | Postgres only | Postgres permanent **+ JSON/JSONL archive** to local storage |
| TTL | Redis presence only | Redis presence only (Postgres stays permanent) |

---

## 2. Quick Start (Docker Compose)

**Prerequisite:** Docker Desktop running.

```bash
cd surveillance_brain
cp .env.example .env            # defaults match compose — no edits needed for first boot
docker compose up --build
```

First boot automatically: starts Postgres + Redis + Qdrant, runs `alembic upgrade head`, runs `python scripts/seed.py` (5 cameras + 1 sample employee enrolled into Qdrant), then starts uvicorn on **:8000**.

**Verify:**
- <http://localhost:8000/docs> — Swagger UI
- <http://localhost:8000/health> — `{database, redis, qdrant, version}` all `ok`
- <http://localhost:8000/events?limit=10> — event log (empty until you POST detections)
- <http://localhost:8000/search?q=Asha> — `not_in_facility` (seeded but not currently tracked)

**Admin creds** (for `POST /employees`, `/identities/*`, `/logs/*`): `admin` / `changeme` — **change in `.env` before any non-local deploy.**

---

## 3. Architecture

```
        Part 1 — Perception (surveillance_AI)
   cameras -> detect -> segment -> face+body embeddings
                     |  POST /events  (detection JSON)
                     v
+--------------------------------------------------------------+
|                    SURVEILLANCE BRAIN (this repo)             |
|                                                               |
|  api/routers/events.POST  -> ingestion_service.ingest         |
|      |                                                        |
|      |- identity_resolver.resolve(detection_conf, face, body) |
|      |     |- conf < 0.80 -> UNKNOWN (no vector search)       |
|      |     |- feature_matcher.find_match()                    |
|      |     |     |- Qdrant `faces`  search  (primary)         |
|      |     |     \- Qdrant `bodies` search  (fallback)        |
|      |     |- hit  -> EMPLOYEE | VISITOR                      |
|      |     \- miss -> new VISITOR (auto-enroll both vectors)  |
|      |                                                        |
|      |- session_tracker.on_detection()  (entry/exit + Redis)  |
|      |- dedup_service.is_duplicate()     (short-window guard)  |
|      |- event_repo.insert_detection_event()  (Postgres ledger)|
|      \- live_broadcaster.publish()       (-> WS /live)        |
|                                                               |
|  GET /events . GET /person/{id} . GET/POST /employees . /live |
+--------------------------------------------------------------+
     |              |                 |                  |
     v              v                 v                  v
 PostgreSQL 16   Qdrant           Redis 7          local storage
 (records+logs) (faces/bodies) (presence TTL +    (events.jsonl +
                                 dedup guard)      person-*.json)
                                                        ^
                                              archive worker (*/30m)
                     Part 3 — Interface (surveillance_UI) reads the API
```

**Read/write split:** high-frequency writes hit Postgres + Qdrant + Redis on ingest; live "where is X?" reads hit Redis only; the UI's log/profile reads hit Postgres.

---

## 4. Data model & storage

**PostgreSQL (permanent, structured):**
- `identities` — surrogate PK `id`, `identity_type`, `display_label` (`VIS-YYYY-NNNN` / `EMP-YYYY-NNNN`).
- `employees`, `visitors` — 1:1 extensions of `identities`.
- `cameras` — `camera_uid`, `zone_id`, `is_exit_camera`, `stream_url`.
- `presence_sessions` — one row per facility visit (`entry_at`/`exit_at`/`status`).
- `detection_events` — the ledger: `detection_id`, `identity_id`, `camera_id`, `classification`, `detection_conf`, `matched_by` (face/body/none), `similarity`, `snapshot_path`, `clip_path`.

**Qdrant (embeddings):** two cosine collections `faces` and `bodies`, dim = `EMBEDDING_DIMENSIONS`. Each point payload = `{identity_id, source}`, so vectors survive promote/demote (the surrogate `identity_id` never changes).

**Redis (hot, TTL):** `presence:current:{identity_id}` hash (camera/zone/last_seen, TTL 300s) for live tracking, and `dedup:{identity_id}:{camera_id}` keys for the duplicate guard. **This is the only TTL layer** — per the Part 2 data-lifetime decision, Postgres records are permanent.

**Local storage (export/archive, JSON/JSONL):** the archive worker (`ARCHIVE_CRON`, every 30 min) writes:
- `storage/logs/events-YYYY-MM-DD.jsonl` — append-only event lines (incremental via a cursor file).
- `storage/datasheet/person-{id}.json` — per-person profile snapshots.

Postgres is the source of truth; these files are the human-readable offline copy each feature keeps.

---

## 5. Contracts

The three teams stay unblocked by agreeing on two JSON contracts. They also live as machine-readable schemas in the repo root [`../contracts/`](../contracts/).

### 5.1 Part 1 -> Part 2 — `POST /events`

```json
{
  "detection_id": "det-abc-123",
  "camera_id": "GATE-01",
  "timestamp": "2026-07-01T09:14:22.501Z",
  "detection_conf": 0.93,
  "face_embedding": [0.12, -0.44, "... 512 floats ..."],
  "body_embedding": [0.02,  0.31, "... 512 floats ..."],
  "snapshot_path": "storage/img/abc.jpg",
  "clip_path": "storage/vid/abc.mp4"
}
```
- `detection_conf` is **0.0–1.0**. Below `DETECTION_CONF_THRESHOLD` (0.80) → `Unknown`, no vector search.
- **At least one** of `face_embedding` / `body_embedding` is required. Face is matched first; body is the fallback.

### 5.2 Part 2 -> Part 3 — event object (returned by `POST /events`, `GET /events`, `WS /live`)

```json
{
  "event_id": 42,
  "detection_id": "det-abc-123",
  "time": "2026-07-01T09:14:22.501000+00:00",
  "camera": "GATE-01",
  "camera_id": 1,
  "person_id": "EMP-2026-0001",
  "identity_id": 1,
  "label": "Employee",
  "name": "Asha R.",
  "confidence": 0.93,
  "matched_by": "face",
  "similarity": 0.94,
  "snapshot": "storage/img/abc.jpg",
  "clip": "storage/vid/abc.mp4",
  "duplicate": false
}
```

---

## 6. API reference

Interactive docs at `/docs`.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | – | Service info |
| GET | `/health` | – | DB + Redis + Qdrant status |
| POST | `/events` | – | Ingest one detection (Part 1) |
| GET | `/events?from=&to=&label=&camera=` | – | Event log list (Part 3) |
| GET | `/person/{identity_id}` | – | Profile + history + photos |
| GET | `/employees` | – | List enrolled employees |
| POST | `/employees` | Basic | Enroll employee (embedding from Part 1) |
| WS | `/live` | – | Real-time event stream |
| GET | `/search?q=` | – | Live "where is X right now?" |
| POST | `/identities/{id}/promote` | Basic | Visitor → Employee (keeps history) |
| POST | `/identities/{id}/demote` | Basic | Employee → Visitor (keeps history) |
| GET | `/logs/individual?identity_id=&from=&to=` | Basic | Per-person sessions (JSON) |
| GET | `/logs/facility?from=&to=` | Basic | Facility event log (CSV) |

**Enrollment note:** the Brain runs no ML model. `POST /employees` accepts an already-computed `face_embedding` (Part 1 extracts it from the uploaded photo); `photo_path` is stored for display only.

---

## 7. Configuration

All via env (`.env`). Highlights (see [`.env.example`](.env.example) for the full list):

| Variable | Default | Meaning |
|---|---|---|
| `DETECTION_CONF_THRESHOLD` | `0.80` | Below this (0–1) → Unknown, no vector search |
| `FACE_SIMILARITY_THRESHOLD` | `0.65` | Cosine floor for a face match (primary) |
| `BODY_SIMILARITY_THRESHOLD` | `0.60` | Cosine floor for a body ReID match (fallback) |
| `EMBEDDING_DIMENSIONS` | `512` | Must match Part 1's model output |
| `SESSION_TIMEOUT_SECONDS` | `300` | Redis presence TTL (the only TTL) |
| `DUPLICATE_WINDOW_SECONDS` | `30` | Suppress repeat logs of same person+camera |
| `ARCHIVE_CRON` | `*/30 * * * *` | JSON/JSONL export cadence |
| `MIDNIGHT_FLUSH_CRON` | `0 0 * * *` | Close dangling sessions daily |
| `QDRANT_HOST` / `QDRANT_PORT` | `localhost` / `6333` | Qdrant location |

---

## 8. Project structure

```
surveillance_brain/
├── config.py                  # env-driven settings + constants
├── db/
│   ├── connection.py          # async SQLAlchemy engine + session
│   ├── models.py              # ORM models (no embeddings — those are in Qdrant)
│   └── vector_store.py        # Qdrant client wrapper (faces + bodies)
├── repositories/              # thin CRUD: identity, camera, session, event, embedding->Qdrant
├── services/
│   ├── feature_matcher.py     # face-primary, body-fallback Qdrant search
│   ├── identity_resolver.py   # Unknown gate + match + auto-enroll visitor
│   ├── dedup_service.py       # duplicate guard + identity merge
│   ├── session_tracker.py     # entry/exit sessions + Redis presence
│   ├── presence_cache.py      # Redis live-presence layer
│   ├── ingestion_service.py   # ingest orchestrator (returns event object)
│   ├── live_broadcaster.py    # in-process pub/sub for WS /live
│   ├── search_service.py      # find_live + person profile
│   ├── log_service.py         # event list + individual log + facility CSV
│   ├── enrollment_service.py  # POST /employees
│   ├── conversion_service.py  # promote / demote
│   └── archive_service.py     # JSON/JSONL export to local storage
├── api/
│   ├── main.py                # app + lifespan (Qdrant bootstrap, schedulers)
│   ├── auth.py                # HTTP Basic Auth
│   ├── schemas.py             # Pydantic contracts
│   └── routers/               # events, person, employees, live, search, identities, logs
├── workers/midnight_flush.py  # APScheduler: flush + archive jobs
├── alembic/                   # migrations (0001 initial schema)
├── scripts/seed.py            # cameras + sample employee (-> Qdrant)
├── tests/                     # smoke tests
├── docker-compose.yml         # postgres + redis + qdrant + app
├── Dockerfile · requirements.txt · pyproject.toml · .env.example
```

---

## 9. Testing & CI

```bash
# with the stack up (docker compose up -d postgres redis qdrant), from the host:
pip install -r requirements.txt && pip install -e .
alembic upgrade head && python scripts/seed.py
pytest tests/ -v
```

CI (`.github/workflows/ci.yml`) spins up Postgres + Redis + Qdrant service containers, migrates, seeds, and runs the smoke suite on every push/PR.

---

## 10. Operational runbook

```bash
docker compose up --build -d           # boot
docker compose logs -f app             # follow app logs
docker compose exec postgres psql -U surveillance -d surveillance   # inspect DB
docker compose exec redis redis-cli    # inspect presence/dedup keys
curl http://localhost:6333/collections # Qdrant collections (faces, bodies)
docker compose exec app python -c "import asyncio; from workers.midnight_flush import run_archive; asyncio.run(run_archive())"  # manual archive
docker compose down                    # stop (keep volumes)
docker compose down -v                 # stop + delete data
```

---

## 11. Scalability notes

- **Ingest throughput** — one transaction per detection (identity + session + event); asyncpg pool 20+10. Scale app workers via `docker compose up --scale app=N` (Postgres/Redis/Qdrant all handle concurrent clients). The duplicate guard sheds per-frame spam so the ledger only grows on genuinely new presence.
- **Vector search** — Qdrant scales to millions of vectors and can be sharded/replicated independently of Postgres. Tune HNSW params per corpus size.
- **`WS /live`** — currently in-process pub/sub (single worker sees only its own events). To scale horizontally, back `live_broadcaster` with Redis Pub/Sub — the `publish()`/`subscribe()` API stays identical.
- **Archive** — incremental via a cursor; safe to run frequently. For very high event volume, move the JSONL export to object storage.

---

## 12. Roadmap (V2)

- **Right to be Forgotten** — `conversion_service.anonymize_identity` + `DELETE /identities/{id}`: drop Qdrant vectors, anonymize events/sessions, delete rows.
- **Redis-backed `WS /live`** for multi-worker fan-out.
- **Per-camera API keys** on `POST /events`.
- **Admin action audit log** (who promoted/enrolled whom).
- **Multi-facility** (`facility_id` on all tables).

---

## License

MIT — see [LICENSE](LICENSE).

Spec & architecture: **Saaketh** · Part 2 (Brain) v2.0.0
