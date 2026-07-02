# Camera Config (`surveillance_Camera_config`)

> Owner: Prithvi + Tushar · Status: **implemented**

The shared camera registry used by Part 1 (which RTSP stream to read) and Part 2
(which `camera_uid` is an exit, what zone it belongs to). It is **split into two
files so credentials never reach git**, joined at runtime on `camera_uid`:

| File | Committed? | Holds |
|---|---|---|
| `cameras.json` | ✅ yes | `camera_uid`, `name`, `zone_id`, `is_exit_camera`, `is_active`, `role`, `match_threshold` (no secrets) |
| `cameras.secrets.json` | 🚫 gitignored | per-uid `ip`, `username`, `password`, `rtsp_path`, `stream_type` |
| `cameras.secrets.example.json` | ✅ yes | template — copy it to `cameras.secrets.json` and fill in |
| `cameras.example.json` | ✅ yes | documents the **resolved** shape (metadata + built `stream_url`) the Brain seeds |

## Setup

```bash
cp cameras.secrets.example.json cameras.secrets.json
# edit cameras.secrets.json — keys MUST match camera_uid in cameras.json
python -m surveillance_Camera_config.loader     # prints the registry, verifies the join
```

## Using it (both parts import the same loader)

```python
from surveillance_Camera_config import load_cameras, to_brain_records

cams = load_cameras(streamable_only=True)   # Part 1: connect + tag detections
cams[0].camera_uid    # 'GATE-RIGHT'         → goes in the detection payload's camera_id
cams[0].stream_url    # 'rtsp://user:pw@192.168.1.210:554/Streaming/Channels/102'

to_brain_records(load_cameras(active_only=False))   # Part 2: seed the `cameras` table
```

`load_cameras()` builds the RTSP URL from the secrets (switching to the lighter
`…/102` sub-stream when `stream_type` is `"sub"`). A camera present in
`cameras.json` but missing from the secrets loads with `stream_url=None` — it
stays in the registry for the Brain but Part 1 can't stream it.

## Fields

Shared with the Brain's `cameras` table:

- `camera_uid` — stable string id used in detection payloads (e.g. `GATE-RIGHT`).
- `name` — human label.
- `zone_id` — logical zone the camera watches.
- `is_exit_camera` — `true` closes a presence session when a known person is seen.
- `stream_url` — RTSP URL Part 1 connects to (built from the secrets file).
- `is_active` — disable a camera without deleting it.

Part 1 (perception) only — not sent to the Brain:

- `role` — `"detect"` (person detection only — path/perimeter cams) or `"identify"`
  (detect + segment + OSNet ReID + identity — high-res gate/back cams). Controls how
  much of the pipeline runs on that camera, so you don't do feature extraction
  everywhere. Default `"detect"`.
- `match_threshold` — optional per-camera override of `feature_id`'s global
  `MATCH_THRESHOLD`. Raise it (e.g. `0.85`) to make one camera stricter without
  touching the others. Omit to use the global value.

> The 7 cameras in `cameras.json` were imported from the gate build. **Review
> `role`, `zone_id`, and `is_exit_camera` for your site** — I set the gate + Sanjeevan
> cameras to `identify` and the pathway/Caviland cameras to `detect` as a starting
> guess.

## Adding cameras (scaling)

Adding 10 more cameras is two JSON entries each — no code changes:

1. Append a metadata entry to `cameras.json`:
   ```json
   { "camera_uid": "PATH-07", "name": "North Path", "zone_id": "ZONE-PATH",
     "is_exit_camera": false, "is_active": true, "role": "detect" }
   ```
2. Add its credentials to `cameras.secrets.json` under the **same** `camera_uid`:
   ```json
   "PATH-07": { "ip": "192.168.1.55", "username": "admin", "password": "…",
                "rtsp_path": "Streaming/Channels/101" }
   ```
3. `python -m surveillance_Camera_config.loader` to verify the join.

Everything downstream (Part 1 pipeline, `to_brain_records()` seeding) picks it up
automatically. Set `role: "identify"` only on the cameras you want to run feature
extraction on.
