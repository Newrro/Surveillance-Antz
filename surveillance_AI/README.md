# Part 1 — Perception Pipeline (`surveillance_AI`)

> Owner: Prithvi + Tushar · Status: **skeleton** (not yet implemented)

Turns raw camera input into a clean feature vector per person, then POSTs a
detection to the Brain (Part 2).

## Pipeline

| Stage | What to build |
|---|---|
| Ingest / Server | Connect to each camera by `camera_id` via RTSP. Read frames + attach timestamp. |
| Preprocessing | Resize, denoise (Gaussian), color-normalize. |
| Detection | Detect people (YOLO / PeopleNet) → bounding boxes. |
| Segmentation | Mask the person (YOLO-seg / Mask R-CNN); apply the **>80% confidence** threshold — discard low-quality masks. |
| Zoom + record | Digital-crop around the person; save snapshot + short clip to shared `storage/`. |
| Feature extraction | Produce a **face embedding** + **body ReID embedding** per clean crop. |

**Tech:** Python, OpenCV, PyTorch, YOLOv8-seg, InsightFace/ArcFace (face), OSNet/ReIdentificationNet (body).

## Deliverable — hand-off to Part 2

For every accepted person, POST the payload defined in
[`../contracts/part1_to_part2.event.schema.json`](../contracts/part1_to_part2.event.schema.json)
to the Brain's `POST /events`. See [`emit_example.py`](emit_example.py) for a
runnable mock producer to test against the Brain before the real model exists.
