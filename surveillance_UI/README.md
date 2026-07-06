# Part 3 — Interface / Logs Website (`surveillance_UI`)

> Owner: Ammarath · Status: **implemented** (operator console prototype)

**Sentinel** — the human-facing surveillance console. A vanilla HTML/CSS/JS app
served by a small Python bridge, with a dark control-room theme.

## Screens

| Screen | What it does | Brain API (target) |
|---|---|---|
| Live grid | Real-time camera tiles; click a tile to open a camera | `WS /live` |
| Individual camera | Live feed + detected-person list; click a person for their sidebar | — |
| Person sidebar | User ID, employee ID/name or Unknown/Visitor, entry time, places visited → **Track movement** | `GET /person/{identity_id}` |
| Reports & logs | Searchable/filterable table of classified detections (Photo, Category, Time, Location, Action) | `GET /events?from=&to=&label=&camera=` |
| Records | Datasheet cards for registered employees/visitors (face embedding hidden — backend-only) | `POST /employees` |
| Settings | Operator account management + reclassify an Unknown/Visitor into a verified Employee | derived from event stream |

## Running with live feeds (recommended)
Browsers can't play RTSP, so `server.py` bridges it: it pulls each camera's
RTSP feed (OpenCV, TCP transport) and re-serves it as MJPEG that the UI shows
with a plain `<img>`. It also serves the web app, so everything is same-origin.

```
python3 server.py        # needs: opencv-python, numpy — serves on :8080
```
Then open **http://localhost:8080** and sign in with **admin / password123**. The Brain
(Part 2) owns :8000, so the UI bridge defaults to **:8080** (`SENTINEL_PORT` to
change).

- `STREAM_TYPE = "sub"` in `server.py` uses the lighter sub-stream (good for the
  grid). Switch to `"main"` for full quality at higher CPU/bandwidth.
- The camera list lives in `CAMERAS` in `server.py` (the source of truth). The
  UI fetches it from `/api/cameras`; each tile streams from `/stream/<id>`.
- Plug detection/recognition into `run_ai_model(frame, cam_id)` — it runs on
  every frame before encoding, so annotations are baked into the feed.

## Running as a static prototype (no video)
Open `index.html` directly in a browser. With no backend, `/api/cameras` fails
and the UI falls back to the static `CAMERAS` list in `data.js`; feeds show the
placeholder panels. Useful for design work without the cameras present.

## Wiring to the Brain (Part 2) — live data
`api.js` connects the UI to the Brain's REST + WebSocket API. On login it pings
`GET /health`; if the Brain is up it **replaces** the mock `PEOPLE` + history
with real data from `GET /events` and `GET /employees`, then subscribes to
`WS /live` to fold in new detections as they happen. Every render function in
`app.js` (`renderGrid`, `openPerson`, `renderLog`, `renderRecords`, …) keeps
working unchanged — only the data behind it is now real.

**If the Brain is unreachable, the UI silently falls back to `data.js`** — so it
still runs as a static prototype with no backend.

Point the UI at a Brain (first match wins):
1. `?brain=http://host:8000` query param (also saved to `localStorage`)
2. `localStorage.brainUrl`
3. `window.BRAIN_URL`
4. `http://localhost:8000` (default — the Brain's compose port)

```
# UI bridge on :8080, Brain on :8000
SENTINEL_PORT=8080 python3 server.py
open "http://localhost:8080/?brain=http://localhost:8000"
```

To bring up **all three parts together**, see the repo-root
[`../INTEGRATION.md`](../INTEGRATION.md) and `../run_all.sh`.

### Contract mapping (Brain event → UI)
The Brain's [event object](../contracts/part2_to_part3.event.schema.json) maps
into the UI's person/history model in `api.js`:

| Brain field | UI use |
|---|---|
| `identity_id` | stable person key (survives promote/demote) |
| `person_id` / `label` / `name` | display id · category (Employee/Visitor/Unknown) · name |
| `time` | split into `{date, time}` for the log/calendar |
| `camera` / `zone_id` | location label (falls back to the RTSP bridge's camera name) |
| `snapshot` / `confidence` | photo + detection confidence |

## Swapping in mock vs real data
Everything renders from the arrays in `data.js`. `api.js` overwrites them from
the Brain when one is reachable; otherwise they stay as the built-in demo data.

- Detection boxes: live events carry no pixel box, so `api.js` places a centered
  marker per person on the camera overlay. Part 1 can later send real boxes
  through the same `DETECTIONS[camId] = [{personId, box:{top,left,w,h}}]` shape,
  or draw them into the frame in `run_ai_model`.
- Colors carry meaning throughout: teal = employee/verified, amber = visitor,
  red = unknown/flagged.

## Files
- `server.py` — RTSP→MJPEG bridge + static server (serves the app on :8080)
- `index.html` — app shell + all views
- `styles.css` — design system (dark control-room theme)
- `data.js` — fallback camera list + demo data (used when no Brain is reachable)
- `api.js` — Brain (Part 2) client: hydrate from REST + stream over WS /live
- `app.js` — view routing, interactions, live-feed + Brain wiring

## Contracts to build against
- Event object (live feed + log table + profile history):
  [`../contracts/part2_to_part3.event.schema.json`](../contracts/part2_to_part3.event.schema.json)
- Full API surface: run the Brain and open `http://localhost:8000/docs`.
