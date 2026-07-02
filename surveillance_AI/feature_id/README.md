# feature_id — face + body identity assignment

The identity brain of Part 1. Takes a **person crop** and answers *who is this?* →
`Employee` / `Visitor` / `Unknown`, with a **confidence %** — using two models,
face first and body as a fallback (the same design the Brain uses).

| Signal | Model | Role |
|---|---|---|
| **Face** (primary) | **AdaFace IR-101 / WebFace12M** + MTCNN detector | most discriminative → stable IDs |
| **Body** (fallback) | **OSNet** (`osnet_x1_0`, market1501) | used when no usable face (small / turned away) |

> Recognition model is **AdaFace, not InsightFace**. MTCNN (facenet-pytorch) is used
> only to find + align the face; the embedding is AdaFace. The AdaFace network code is
> vendored in [`adaface_net.py`](adaface_net.py); its weights live in `../models/`.

## Files

| File | Role |
|---|---|
| [`config.py`](config.py) | Every knob: models, `FACE_MATCH_THRESHOLD` (0.30), `BODY_MATCH_THRESHOLD` (0.75), `LEARN_CEILING`, auto-enroll, labels. |
| [`face_extractor.py`](face_extractor.py) | `FaceExtractor.embed(crop)` → 512-d AdaFace vector (detect → align 112×112 → embed), or `None`. |
| [`extractor.py`](extractor.py) | `Extractor.embed(crop)` → 512-d OSNet body vector. |
| [`adaface_net.py`](adaface_net.py) | Vendored AdaFace IR-network (from the AdaFace repo). |
| [`gallery.py`](gallery.py) | Known-people DB (`data/gallery.json`) — each person holds face **and** body views. |
| [`identify.py`](identify.py) | `Identifier` — extract face+body, match face→body, label + confidence, progressive learning, auto-enroll. |
| [`enroll.py`](enroll.py) | Register an employee from a photo (stores face + body). |
| [`demo.py`](demo.py) | Offline synthetic self-test (body path). |

## How identity is decided

```
crop → face_emb (AdaFace, maybe None) + body_emb (OSNet)
   face_emb matches a known face  >= FACE_MATCH_THRESHOLD  → recognised (matched_by=face)
   else body_emb matches a body   >= BODY_MATCH_THRESHOLD  → recognised (matched_by=body)
   else                                                    → new Visitor (auto-enrolled)
```

`Identifier.identify_features(face_emb, body_emb, face_threshold=...)` — the pipeline
computes the embeddings once (reused to emit to the Brain) and passes a per-camera
`face_threshold` override (`match_threshold` in `cameras.json`) so one camera can be
stricter without touching the others.

### Progressive confidence

When a known person matches above threshold but below `LEARN_CEILING` (0.92), the new
view is stored on their record, so their score climbs on future sightings. Views are
capped at `MAX_VIEWS_PER_PERSON`.

## Usage

```bash
# enroll staff once (face + body from a clear photo) so they're 'Employee':
python -m feature_id.enroll  EMP-001  "Asha R."  photo.jpg

# offline sanity check (synthetic, body path):
python -m feature_id.demo
```

In the live system, `../pipeline.py` calls `Identifier` on every person crop from an
`identify`-role camera. `data/gallery.json` + `data/snapshots/` are runtime state (gitignored).

> **Two confidences:** *detection* (FasterRCNN, "is this a person?") is separate from
> *identity* (AdaFace/OSNet cosine, "is this Asha?"). The % here is the identity one.
