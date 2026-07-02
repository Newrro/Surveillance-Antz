# ─────────────────────────────────────────────
#  STEP 1 — THE EXTRACTOR
#  Turns a picture of a person into 512 numbers (an "embedding").
# ─────────────────────────────────────────────
# This is the ONLY file that knows about the AI model. Everything else in your
# part just works with the 512-number vectors it produces. That means when you
# later switch to a face model or a GPU, you only change THIS file.

import numpy as np
import cv2
from torchreid.reid.utils import FeatureExtractor

from . import config


class Extractor:
    """Wraps the Re-ID model. Give it a person image, get back a 512-vector."""

    def __init__(self):
        # Load the model once (this is the slow part — do it a single time,
        # then reuse for every person). Downloads weights on first ever run.
        print(f"[extractor] loading {config.MODEL_NAME} on {config.DEVICE} ...")
        self._model = FeatureExtractor(
            model_name=config.MODEL_NAME,
            model_path=config.MODEL_PATH,
            device=config.DEVICE,
        )
        print("[extractor] ready.")

    def embed(self, person_bgr):
        """
        person_bgr : a cropped image of ONE person, in OpenCV's BGR format
                     (that's what your camera streams give you).
        returns    : a 512-length numpy vector, L2-normalized.

        We normalize the vector so that comparing two of them is just a dot
        product — that dot product IS the cosine similarity (0..1). Doing the
        normalization here means the gallery/matching code stays dead simple.
        """
        # torchreid expects RGB; OpenCV frames are BGR — convert.
        person_rgb = cv2.cvtColor(person_bgr, cv2.COLOR_BGR2RGB)

        # The model returns a torch tensor of shape (1, 512); pull out the row.
        vec = self._model([person_rgb]).cpu().numpy()[0].astype("float32")

        # L2-normalize (make its length 1). Guard against a zero vector.
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec


def cosine_similarity(a, b):
    """Similarity between two ALREADY-normalized vectors: 1.0 = identical look."""
    return float(np.dot(a, b))


# ── quick self-test: run `python -m feature_id.extractor` ──
if __name__ == "__main__":
    ex = Extractor()
    fake_person = (np.random.rand(256, 128, 3) * 255).astype("uint8")
    v = ex.embed(fake_person)
    print("embedding length:", v.shape, "| vector norm (should be ~1.0):",
          round(float(np.linalg.norm(v)), 4))
