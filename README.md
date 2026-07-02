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

## The three parts

| Part | Folder | Owner | Responsibility | Status |
|---|---|---|---|---|
| **1 — Perception** | [`surveillance_AI/`](surveillance_AI/) | Prithvi + Tushar | RTSP ingest → detect → segment (>80% conf) → **body ReID embedding** (face: TODO) → emits detection JSON | ✅ implemented |
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
