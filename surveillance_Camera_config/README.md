# Camera Config (`surveillance_Camera_config`)

> Owner: Prithvi + Tushar · Status: **skeleton**

The shared camera registry used by Part 1 (which RTSP stream to read) and
Part 2 (which `camera_uid` is an exit, what zone it belongs to). Part 2 seeds
a baseline set in `surveillance_brain/scripts/seed.py`; this folder is where
the authoritative camera list / provisioning config lives.

See [`cameras.example.json`](cameras.example.json) for the shape. Fields map
1:1 to the Brain's `cameras` table:

- `camera_uid` — stable string id used in detection payloads (e.g. `GATE-01`).
- `name` — human label.
- `zone_id` — logical zone the camera watches.
- `is_exit_camera` — `true` closes a presence session when a known person is seen.
- `stream_url` — RTSP URL Part 1 connects to (also returned by the Brain's `/search`).
- `is_active` — disable a camera without deleting it.
