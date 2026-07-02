# Shared Contracts

The three parts of Project RUNG01 build in parallel by agreeing on these JSON
shapes **on day one**. Each team can then develop and test against mock data
without waiting on the others.

## 1. Part 1 → Part 2 — detection payload

- Schema: [`part1_to_part2.event.schema.json`](part1_to_part2.event.schema.json)
- Transport: `POST /events` on the Brain (Part 2).
- Producer: Perception pipeline (Part 1, `surveillance_AI/`).
- Notes:
  - `detection_conf` is **0.0–1.0** (not a percentage). Below `0.80` → `Unknown`.
  - At least one of `face_embedding` / `body_embedding` (512-dim each) is required.
    Face is matched first; body is the cross-camera fallback.

## 2. Part 2 → Part 3 — event object

- Schema: [`part2_to_part3.event.schema.json`](part2_to_part3.event.schema.json)
- Transport: `POST /events` reply, `GET /events` list, and `WS /live` stream.
- Consumer: Interface (Part 3, `surveillance_UI/`).

## 3. Shared storage layout

Snapshots and clips are written by Part 1 and referenced by path in both
contracts. Agree on one root and keep it consistent:

```
storage/
├── img/        # snapshots (Part 1 writes, Brain stores the path)
├── vid/        # short clips (Part 1 writes, Brain stores the path)
├── logs/       # Brain: events-YYYY-MM-DD.jsonl  (JSON Lines export)
└── datasheet/  # Brain: person-{identity_id}.json (profile snapshots)
```

- **Part 1** writes media into `storage/img` and `storage/vid` and puts those
  paths in `snapshot_path` / `clip_path`.
- **Part 2 (Brain)** never moves media — it only stores the path string, and
  writes its own JSON/JSONL export under `storage/logs` and `storage/datasheet`.
- **Part 3** loads media by the path/URL returned in the event object.

## Validating a payload against a schema

```bash
pip install jsonschema
python - <<'PY'
import json, jsonschema
schema = json.load(open("contracts/part1_to_part2.event.schema.json"))
sample = schema["examples"][0]
jsonschema.validate(sample, schema)
print("valid")
PY
```
