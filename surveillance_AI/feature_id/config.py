# ─────────────────────────────────────────────
#  CONFIG  — all the "knobs" for the feature-extraction + ID part
# ─────────────────────────────────────────────
# Keeping every setting in ONE place means you tune behaviour here without
# hunting through the code. This is the file you'll edit most while calibrating.

import os

# Where this package lives on disk (so paths work no matter where you run from)
HERE = os.path.dirname(os.path.abspath(__file__))

# The "gallery" = our database of known people's embeddings.
# ONE json file for everyone (updated in place — not one file per person).
# It's human-readable so you can open it and see exactly what's stored.
GALLERY_PATH = os.path.join(HERE, "data", "gallery.json")

# Folder where we save a cropped snapshot the first time we see someone
# (handy for the website later, and for you to eyeball who got which id).
SNAPSHOT_DIR = os.path.join(HERE, "data", "snapshots")

# ── The model that turns a person-image into 512 numbers ──
# osnet_x1_0 is a small, fast person Re-ID model.
MODEL_NAME = "osnet_x1_0"

# Weights, in order of preference:
#   1. ../models/osnet_x1_0_market.pth   — market1501-trained (BEST for person ReID)
#   2. ../models/osnet_x1_0_imagenet.pth — bundled ImageNet weights (offline-safe)
#   3. ""                                — torchreid auto-downloads ImageNet on first run
# See surveillance_AI/README.md ("Weights") for how to get the market1501 file.
_MODELS_DIR = os.path.abspath(os.path.join(HERE, "..", "models"))
_MARKET = os.path.join(_MODELS_DIR, "osnet_x1_0_market.pth")
_IMAGENET_LOCAL = os.path.join(_MODELS_DIR, "osnet_x1_0_imagenet.pth")
if os.path.exists(_MARKET):
    MODEL_PATH = _MARKET
elif os.path.exists(_IMAGENET_LOCAL):
    MODEL_PATH = _IMAGENET_LOCAL
else:
    MODEL_PATH = ""

# Device: auto-detect. The deployment laptop (RTX 4060) will use CUDA; CPU otherwise.
# Override with env FEATURE_ID_DEVICE=cpu|cuda if you need to force one.
DEVICE = os.environ.get("FEATURE_ID_DEVICE")
if not DEVICE:
    try:
        import torch as _torch
        DEVICE = "cuda" if _torch.cuda.is_available() else "cpu"
    except Exception:
        DEVICE = "cpu"

# ─────────────────────────────────────────────
#  FACE MODEL  — the PRIMARY identity signal (AdaFace, NOT InsightFace)
# ─────────────────────────────────────────────
# AdaFace IR-101 trained on WebFace12M: state-of-the-art face recognition, strong
# on the low-quality / off-angle faces typical of CCTV. Face DETECTION + 5-point
# landmarks come from facenet-pytorch's MTCNN (detector only); the embedding is
# AdaFace. Face is matched first; body ReID (below) is the cross-camera fallback.
FACE_BACKBONE = "ir_101"
FACE_WEIGHTS = os.path.join(_MODELS_DIR, "adaface_ir101_webface12m.pt")
FACE_MIN_SIZE = 40        # ignore faces smaller than this (px side) — too small to trust
FACE_DET_CONF = 0.90      # MTCNN face-detection confidence floor

# ─────────────────────────────────────────────
#  THE THRESHOLDS  — the numbers that most control identity accuracy
# ─────────────────────────────────────────────
# Cosine similarity; a gallery match >= threshold is trusted, else the person is new.
#   - too HIGH → real matches missed (same person keeps getting new ids)
#   - too LOW  → strangers merged into someone they aren't
# FACE (primary): AdaFace same-person cosine ~0.35+, different people usually < 0.2.
# BODY (fallback): OSNet ReID, less discriminative, so a higher bar.
FACE_MATCH_THRESHOLD = 0.30
BODY_MATCH_THRESHOLD = 0.75
MATCH_THRESHOLD = BODY_MATCH_THRESHOLD   # backward-compat alias (body)

# When we see an Unknown person, should we automatically remember them as a
# new Visitor so they're recognised next time? (Your gate→pathway design wants this.)
AUTO_ENROLL_UNKNOWN = True

# ─────────────────────────────────────────────
#  PROGRESSIVE CONFIDENCE  — "raise the score with new angles"
# ─────────────────────────────────────────────
# When we recognise someone but the match ISN'T near-perfect, that usually means
# we're seeing a new angle/pose. We SAVE that new view onto the person's record,
# so next time they show up we have more views to match against → higher confidence.
#
# Save a new view only when:  MATCH_THRESHOLD <= similarity < LEARN_CEILING
#   - below MATCH_THRESHOLD  → not confident it's them, do NOT contaminate them
#   - above LEARN_CEILING    → basically a duplicate we already have, no need
LEARN_CEILING = 0.92

# Cap how many views we keep per person (stops the json growing forever).
# When full, the lowest-value (most redundant) view is dropped.
MAX_VIEWS_PER_PERSON = 10

# ── "couldn't extract features" guard ──
# If the segmented person crop is smaller than this (too tiny/empty to be
# useful), we refuse to extract and report it instead of guessing an identity.
MIN_CROP_SIDE = 24   # pixels; a crop must be at least this wide AND tall

# The three labels this system can assign.
LABEL_EMPLOYEE = "Employee"
LABEL_VISITOR  = "Visitor"
LABEL_UNKNOWN  = "Unknown"
