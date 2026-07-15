# Part 1 — Perception Pipeline (`surveillance_AI`)

> Owner: Prithvi + Tushar · Status: **implemented & validated**
> (detect → segment → **face (AdaFace) + body (OSNet)** → identity)

Turns raw camera input into a person identity: detect people, segment them, extract
a **face** embedding (primary) and a **body** embedding (fallback), and assign
**Employee / Visitor / Unknown + a confidence %** locally — with the option to also
POST the detection to the Brain (Part 2).

Validated on the live RTSP cameras (detection + role split) and on recorded video
(face-primary identity keeps the **same person on the same ID** across frames, where
body-only ReID used to churn a new ID every frame).

## The models (no YOLO)

| Stage | Model | Where |
|---|---|---|
| **Detection** — find people (boxes) | **RT-DETR** (default; swappable via `DETECTOR_MODEL`, not YOLO) | [`detector.py`](detector.py) |
| **Segmentation** — mask each person | **SAM 2** (optional) | [`segmenter.py`](segmenter.py) |
| **Face embedding — PRIMARY identity** | **AdaFace IR-101 / WebFace12M** + **SCRFD** detector (was MTCNN) | [`feature_id/face_extractor.py`](feature_id/face_extractor.py) |
| **Body ReID — FALLBACK identity** | **OSNet** (torchreid) | [`feature_id/extractor.py`](feature_id/extractor.py) |

> **Face first, body fallback** (matching the Brain's design): faces are far more
> discriminative than body ReID, so a visible face gives a stable ID; when no usable
> face is found (too small / turned away) we fall back to the body vector.
> AdaFace is the recognition model (**not** InsightFace); the face **detector** was
> **MTCNN** and is **now SCRFD** (`scrfd_500m_bnkps.onnx`, onnxruntime) — set
> `FACE_DETECTOR=mtcnn` to revert. SAM 2 is *segmentation* — the detector finds the box, SAM 2 masks it.
> The person detector is **swappable** via `DETECTOR_MODEL` (`rtdetr` default,
> `fasterrcnn_resnet50`, or the light `fasterrcnn_mobilenet`).

## Modules

| File | Role |
|---|---|
| [`nvr_stream.py`](nvr_stream.py) | Ingest — one thread per camera, latest-frame + reconnect, scheme-aware capture (RTSP vs HTTP). Reads the shared [`surveillance_Camera_config`](../surveillance_Camera_config/) registry. |
| [`pano.py`](pano.py) | 360° (Insta360) support — reads one equirectangular stream and carves it into flat perspective views (front/right/back/left), each a normal camera with its own role. |
| [`detector.py`](detector.py) | `PersonDetector` — person boxes from RT-DETR (default) or a FasterRCNN backend (`DETECTOR_MODEL`), plus false-positive shape filters (min height, upright aspect) tuned on the outdoor gate feed. |
| [`segmenter.py`](segmenter.py) | `SAM2Segmenter` — SAM 2 mask per person to blank the background before ReID. Heavy; opt-in (`--segment`). |
| [`feature_id/`](feature_id/) | Face (AdaFace) + body (OSNet) embeddings and the local identity gallery — face-primary/body-fallback, Employee/Visitor/Unknown + confidence, progressive learning. See [`feature_id/README.md`](feature_id/README.md). |
| [`pipeline.py`](pipeline.py) | **The producer** — role-aware: detect-only on path cams, full detect→segment→ReID→identity on identify cams; optional `POST /events` to the Brain. |
| [`live_view.py`](live_view.py) | Multi-camera detection viewer (grid + click-to-fullscreen). The "it works" demo. |
| [`emit_example.py`](emit_example.py) | Mock producer with synthetic embeddings — test the Brain before the models are set up. |

## Per-camera roles — run heavy models only where needed

Each camera's `role` (in [`../surveillance_Camera_config/cameras.json`](../surveillance_Camera_config/cameras.json))
decides how much runs on it:

- **`detect`** → person detection only. Cheap. For path / perimeter cameras.
- **`identify`** → detect + (SAM 2) + OSNet + identity. For high-res gate/back cameras
  where you actually recognise people.

So the path cameras run only the detection model; the gate/back cameras do the full
feature extraction. Adding cameras is two JSON lines (metadata + secret) — see
[`../surveillance_Camera_config/README.md`](../surveillance_Camera_config/README.md).

## Thresholds & confidence

- **Quality gate first → Unknown.** If the detection is weak (`detection_conf <
  DETECTION_CONF_THRESHOLD`, default **0.80**) or no usable face/body could be
  extracted, the sighting is **Unknown** — we don't guess. Mirrors the Brain's design.
- **Then search: face first, body fallback.** Above the gate we match the gallery:
  AdaFace face (`>= FACE_MATCH_THRESHOLD`, default **0.30**), else OSNet body
  (`>= BODY_MATCH_THRESHOLD`, default **0.75**) → the person's label (Employee /
  Visitor) + a real confidence %. No match → a **new Visitor**. A % shows only on a
  real match. All thresholds live in [`feature_id/config.py`](feature_id/config.py).
- **Per-camera threshold:** set `match_threshold` on a camera in `cameras.json` to
  override the **face** threshold for that camera only — raise it to make one camera
  stricter, no code change, others unaffected.
- **Progressive confidence:** when a known person matches above threshold but below
  `LEARN_CEILING` (0.92), the new view is learned, so their score climbs on future
  sightings (e.g. someone first seen at ~60% rises toward 90%+). Details in
  [`feature_id/README.md`](feature_id/README.md).

## Setup

```bash
cd surveillance_AI
python -m venv venv && source venv/bin/activate   # (Windows: venv\Scripts\activate)
pip install -r requirements.txt                    # torch/torchvision: pick your CPU/GPU build
# configure cameras — see ../surveillance_Camera_config/README.md
```

Install torch/torchvision matching your machine from <https://pytorch.org> (on the
RTX 4060 laptop install a CUDA build — the code auto-detects and uses the GPU).

### Weights

**Device** auto-detects: CUDA on the 4060 laptop, CPU otherwise. Force with
`FEATURE_ID_DEVICE=cpu|cuda`. All four models (FasterRCNN, SAM 2, AdaFace, OSNet) fit in 6 GB.

All weights live in [`models/`](models/), which is **gitignored** — so they travel in
a **zip** of the folder but not via `git clone`. If you clone, re-fetch them:

**AdaFace (face, primary)** — `models/adaface_ir101_webface12m.pt` (IR-101, WebFace12M).
The model *code* is vendored ([`feature_id/adaface_net.py`](feature_id/adaface_net.py));
only the weights download. To re-fetch and strip to model-only:
```bash
python -c "import gdown; gdown.download('https://drive.google.com/uc?id=1dswnavflETcnAuplZj1IOKKP0eM8ITgT','models/adaface.ckpt',quiet=False)"
python -c "import torch; sd=torch.load('models/adaface.ckpt',map_location='cpu')['state_dict']; torch.save({k[6:]:v for k,v in sd.items() if k.startswith('model.')},'models/adaface_ir101_webface12m.pt')"
```

**OSNet (body, fallback)** — picked up in order: `osnet_x1_0_market.pth` (market1501,
best; bundled) → `osnet_x1_0_imagenet.pth` (bundled fallback) → torchreid auto-download.
```bash
python -c "import gdown; gdown.download('https://drive.google.com/uc?id=1vduhq5DpN2q1g4fYEZfPI17MJeh9qyrA', 'models/osnet_x1_0_market.pth', quiet=False)"
```

### Segmentation (SAM 2) setup — only needed for `--segment`

SAM 2 is a heavy, optional dependency. Install it, then place the checkpoint under
[`models/`](models/) (or point `SAM2_CHECKPOINT` at it):

```bash
pip install "git+https://github.com/facebookresearch/sam2.git"
# download sam2.1_hiera_small.pt into surveillance_AI/models/
export SAM2_CHECKPOINT=surveillance_AI/models/sam2.1_hiera_small.pt   # optional; default path
export SAM2_CONFIG=configs/sam2.1/sam2.1_hiera_s.yaml
```

Detection + ReID work **without** SAM 2 — the pipeline just crops the bounding box.

## Run

```bash
python nvr_stream.py            # raw grid — are the cameras up?
python live_view.py             # live people detection, click a tile for fullscreen

# enroll staff once so they're 'Employee' (else everyone is an auto Visitor):
python -m feature_id.enroll  EMP-001  "Asha R."  photo.jpg

python pipeline.py              # all cameras: detect-only + local identity, prints results
python pipeline.py --segment    # + SAM 2 background removal before ReID
python pipeline.py --show       # + annotated window
python pipeline.py --cameras GATE-RIGHT        # one camera
python pipeline.py --emit --brain-url http://localhost:8000   # also POST to the Brain
```

### Testing without live cameras

No cameras on the LAN? Drive the models off a **recorded video** or the **laptop
webcam** — both are ordinary OpenCV sources (this is how identity was validated).

- **Detector filter gotcha:** the shape filters in [`detector.py`](detector.py)
  (`MIN_ASPECT`, `MIN_HEIGHT_FRAC`) are tuned for full-body gate views and will reject
  close-ups — a webcam head-and-shoulders box is *wider than tall*. For close-up
  testing, relax them at the top of your script:
  `import detector; detector.MIN_ASPECT = 0.3`.

## Deliverable — hand-off to Part 2 (optional `--emit`)

For every accepted person on an `identify` camera, `pipeline.py` can POST the payload
in [`../contracts/part1_to_part2.event.schema.json`](../contracts/part1_to_part2.event.schema.json)
to the Brain's `POST /events`:

- `camera_id` = the camera's `camera_uid`.
- `detection_conf` = the detector's score (`--conf`, default 0.50, gates person acceptance).
- `face_embedding` = 512-dim AdaFace vector when a face is visible (primary; else `null`).
- `body_embedding` = 512-dim OSNet ReID vector (fallback; `null` if the crop was too small).
- At least one of the two is always present (contract requirement).
- `snapshot_path` = crop saved under the shared `storage/img/<camera_uid>/`.

Detect-only cameras have no embedding, so they log detections but don't emit.

**Identity authority:** Part 1 assigns identity locally (its `feature_id` gallery) so
it works standalone. When integrated, the **Brain re-resolves identity** from the
embedding via its own vector search — so the contract stays embedding-only (no
`person_id`/`label` field), matching the Brain's spec. The two don't conflict: local
gallery = Part 1's self-contained mode; Brain = source of truth for the whole system.
