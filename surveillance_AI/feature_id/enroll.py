# ─────────────────────────────────────────────
#  ENROLL EMPLOYEES  — register known staff into the gallery
# ─────────────────────────────────────────────
# Run this once per employee to teach the system who your staff are. After this,
# identify() will label them "Employee" with their name instead of "Visitor".
#
# Usage:
#     python -m feature_id.enroll  EMP-001  "Asha R."  path/to/photo.jpg
#
# Tip: a photo cropped tightly to the person (full body, clear) works best.

import sys
import cv2

from .extractor import Extractor
from .gallery import Gallery
from . import config


def enroll_employee(person_id, name, image_path):
    img = cv2.imread(image_path)
    if img is None:
        print(f"ERROR: could not read image: {image_path}")
        return

    ex = Extractor()
    gal = Gallery()

    emb = ex.embed(img)
    person = gal.add(emb, label=config.LABEL_EMPLOYEE, name=name, person_id=person_id)
    print(f"Enrolled {person.label}: {person.id} ({person.name})")
    print(f"Gallery now has {len(gal.people)} people.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print('Usage: python -m feature_id.enroll  <id>  "<name>"  <image_path>')
        sys.exit(1)
    enroll_employee(sys.argv[1], sys.argv[2], sys.argv[3])
