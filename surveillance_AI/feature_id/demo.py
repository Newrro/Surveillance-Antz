# ─────────────────────────────────────────────
#  DEMO  — shows: single-json store, confidence %, and progressive learning.
# ─────────────────────────────────────────────
# Run:  python -m feature_id.demo   (delete data/gallery.json first for a clean run)

import json
import numpy as np

from .identify import Identifier
from . import config


def make_image(kind, noise=0):
    """Fake 'people'. `noise` simulates seeing the same person from a new angle."""
    rng = np.random.RandomState(0 if kind == "A" else 1)
    img = (rng.rand(256, 128, 3) * 60).astype("uint8")
    if kind == "A":
        img[:, :, 2] = np.clip(img[:, :, 2] + 180, 0, 255)
        img[60:180, 30:100] = (255, 255, 255)
    else:
        img[:, :, 1] = np.clip(img[:, :, 1] + 180, 0, 255)
        img[20:120, 40:110] = (0, 0, 0)
    if noise:
        img = np.clip(img.astype(int) + np.random.RandomState(9).randint(-noise, noise, img.shape), 0, 255).astype("uint8")
    return img


def show(tag, r):
    print(f"{tag:12} -> id={r['person_id']}  label={r['label']:9} "
          f"conf={r['confidence_pct']}%  new={r['is_new']}  "
          f"learned={r['learned']}  error={r['error']}")


def main():
    idf = Identifier()

    # Pre-enroll person A as an Employee (as enroll.py would from a photo).
    empA = idf.extractor.embed(make_image("A"))
    idf.gallery.add(empA, label=config.LABEL_EMPLOYEE, name="Test Person", person_id="EMP-TEST")
    print("\nEnrolled EMP-TEST with 1 view.\n")

    # See A again from a "new angle" (noisy) → recognised; may LEARN the new view.
    show("A new-angle", idf.identify(make_image("A", noise=40)))
    # A very different person → Unknown → auto-enrolled as a new Visitor.
    show("B stranger", idf.identify(make_image("B")))
    # An empty/failed segmentation → "couldn't extract features".
    show("empty crop", idf.identify(np.zeros((5, 5, 3), dtype="uint8")))

    # Show the ONE json file and how many views each person now has.
    with open(config.GALLERY_PATH, encoding="utf-8") as f:
        data = json.load(f)
    print(f"\nSINGLE FILE: {config.GALLERY_PATH}")
    for p in data["people"]:
        print(f"  {p['id']:12} {p['label']:9} views={p['num_views']}")
    print(f"\nMATCH_THRESHOLD={config.MATCH_THRESHOLD}  LEARN_CEILING={config.LEARN_CEILING}\n")


if __name__ == "__main__":
    main()
