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
#  THE THRESHOLD  — the single most important number you own
# ─────────────────────────────────────────────
# Cosine similarity ranges 0..1 (1 = identical looking).
# If the best match in the gallery scores >= MATCH_THRESHOLD, we trust it.
# Otherwise the person is "Unknown".
#   - too HIGH  → real matches get missed (same person called Unknown)
#   - too LOW   → strangers get mislabeled as someone they aren't
# 0.75 is a sane starting point; you'll tune it with calibrate.py on real crops.
MATCH_THRESHOLD = 0.75

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
