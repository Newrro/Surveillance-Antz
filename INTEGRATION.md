# Running RUNG01 — all three parts, integrated

This guide brings the whole system up on one machine so you can watch a
detection flow end-to-end:

```
 Part 1 (Perception)          Part 2 (Brain)                 Part 3 (Interface)
 real cameras + GPU   ─POST /events─▶  identity + tracking + DB  ─WS /live─▶  Sentinel UI
   ─or─ integration_sim.py            REST: /events /person /employees        (surveillance_UI)
```

On a dev laptop the real Part 1 (RTSP cameras + GPU embeddings) isn't
available, so **`tools/integration_sim.py` stands in for it** — it streams
detection payloads (the exact Part 1 → Part 2 contract) into the Brain, which
the UI then shows live. Swap it for the real pipeline when it's ready; nothing
else changes.

## Two ways to run Part 2 (the Brain)

Docker is only a **convenience**, not a requirement. Pick one:

| | Docker | Native (Docker-free) |
|---|---|---|
| Postgres + Redis + Qdrant | in containers | Homebrew Postgres + Redis, **embedded Qdrant** (in-process, no server) |
| Bring-up | `docker compose up` | `surveillance_brain/run_native.sh` |
| Prereqs | Docker Desktop | Homebrew, Python 3.11+ |

`run_all.sh` **auto-detects**: if Docker is running it uses Compose, otherwise it
falls back to the native path automatically.

### Prerequisites

| Need | Why | Install |
|---|---|---|
| **Python 3.11+** | Brain venv, UI bridge, the detection simulator | already on most machines |
| *Either* Docker | run the Brain's datastores in containers | <https://docs.docker.com/get-docker/> |
| *or* Homebrew | run Postgres + Redis natively (Qdrant is embedded) | <https://brew.sh> — `run_native.sh` installs `postgresql@16` + `redis` for you |
| `pip install requests` | the simulator POSTs to the Brain | into any Python |
| `pip install opencv-python numpy` | *optional* — live MJPEG camera feeds in the UI | only if you have RTSP cameras |

## One command

```bash
./run_all.sh
```

This will: boot the Brain (Docker **or** native, auto-migrating + seeding), wait
for `/health`, start the Sentinel UI on **:8080**, and stream mock detections
into the Brain. Then open:

- **UI** — <http://localhost:8080/?brain=http://localhost:8000>
- **Brain docs** — <http://localhost:8000/docs>

Sign in with **admin / password123**. The grid, logs, and records are now backed by the
Brain; new detections appear live as the simulator emits them.

Stop the UI + simulator with `Ctrl-C`; stop the Brain too with `./run_all.sh down`.
Start without the mock stream: `./run_all.sh --no-sim`.

## Manual steps (what `run_all.sh` automates)

```bash
# 1) Part 2 — the Brain     (pick ONE)
#   a) Docker:
cd surveillance_brain && cp .env.example .env && docker compose up --build -d
#   b) Native (Docker-free): Homebrew Postgres+Redis + embedded Qdrant, migrate+seed+run
cd surveillance_brain && ./run_native.sh
curl -s localhost:8000/health          # {"database":"ok","redis":"ok","qdrant":"ok"}

# 2) Part 3 — the UI (new terminal, repo root)
SENTINEL_PORT=8080 python3 surveillance_UI/server.py     # or: python3 -m http.server 8080 -d surveillance_UI
open "http://localhost:8080/?brain=http://localhost:8000"

# 3) Part 1 stand-in — stream detections (new terminal)
pip install requests
python3 tools/integration_sim.py                          # loops ~1 event / 2s
#   python3 tools/integration_sim.py --once               # single round
```

### Native mode notes
- `run_native.sh` installs `postgresql@16` + `redis` via Homebrew, initializes +
  starts them, creates the `surveillance` role/db, writes a `.env` with
  `QDRANT_LOCAL_PATH` (embedded Qdrant), then migrates, seeds, and runs uvicorn.
- **Embedded Qdrant locks its on-disk path to one process**, so the archive /
  midnight-flush scheduler is disabled in native mode (`ENABLE_MIDNIGHT_FLUSH=0`)
  — it would open a second Qdrant client. For the scheduler, run a real Qdrant
  server (Docker, or a standalone `qdrant` binary) and drop `QDRANT_LOCAL_PATH`.
- Stop native services: `redis-cli shutdown` and
  `/opt/homebrew/opt/postgresql@16/bin/pg_ctl -D /opt/homebrew/var/postgresql@16 stop`.

## How Part 3 connects to Part 2

The UI ([`surveillance_UI/api.js`](surveillance_UI/api.js)) calls the Brain
directly from the browser (the Brain enables CORS `*`). It reads the Brain URL
from, in order: the `?brain=` query param → `localStorage.brainUrl` →
`window.BRAIN_URL` → `http://localhost:8000`.

On login it pings `/health`; if healthy it hydrates `PEOPLE` + history from
`GET /events` and `GET /employees`, then subscribes to `WS /live` to fold in new
events as they arrive. **If the Brain is unreachable, the UI silently falls back
to the mock data in `data.js`** — so it still runs as a static prototype.

Ports: the Brain owns **:8000**; the UI bridge serves on **:8080**
(`SENTINEL_PORT` to change). Camera *feeds* still come from the UI's own RTSP
bridge; camera *identity/location* labels come from the Brain event's
`camera` / `zone_id`.

## What each detection exercises

`integration_sim.py` sends a representative mix every round:

| Detection | Confidence | Expected Brain classification |
|---|---|---|
| The seeded employee (Asha R., `EMP-2026-0001`) | 0.95 | **Employee** (face match against the seed) |
| A rotating visitor | 0.90 | **Visitor** (auto-enrolled on first sight, matched after) |
| A blurry crop (~35% of rounds) | 0.55 | **Unknown** (below the 0.80 gate, no vector search) |

## What is *not* runnable on a dev box

- **Real Part 1** — needs RTSP cameras + a GPU for AdaFace/OSNet embeddings. See
  [`surveillance_AI/README.md`](surveillance_AI/README.md). The simulator replaces it.
- **Live MJPEG feeds in the UI** — need reachable cameras on the LAN and
  `opencv-python`. Without them the UI shows placeholder panels but is otherwise
  fully integrated with the Brain.

## Troubleshooting

- `docker: command not found` / daemon not running → install/start Docker Desktop.
- Brain `/health` shows `error:` for redis/qdrant → containers still warming up;
  re-check after a few seconds, or `(cd surveillance_brain && docker compose logs app)`.
- UI shows mock names (Ravi Kumar, etc.) instead of Brain data → the Brain wasn't
  reachable at load. Confirm `/health`, then reload with the `?brain=` param.
- Port clash on 8000/8080 → set `APP_PORT` (Brain `.env`) / `SENTINEL_PORT` (UI).
