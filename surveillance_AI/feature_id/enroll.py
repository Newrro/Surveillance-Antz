# ─────────────────────────────────────────────
#  ENROLL EMPLOYEES  — register known staff into the gallery
# ─────────────────────────────────────────────
# Run this once per employee to teach the system who your staff are. After this,
# identify() labels them "Employee" with their name instead of auto "Visitor".
#
# Extracts BOTH a face embedding (AdaFace, primary) and a body embedding (OSNet,
# fallback) from the photo, so the person can be recognised either way.
#
# Usage:
#     python -m feature_id.enroll  EMP-001  "Asha R."  path/to/photo.jpg
#
# Tip: a clear, front-facing photo works best (face drives recognition). A
# full-body shot also seeds the body-ReID fallback.

import sys
import cv2

from .extractor import Extractor
from .face_extractor import FaceExtractor
from .gallery import Gallery
from . import config


def enroll_employee(person_id, name, image_path):
    img = cv2.imread(image_path)
    if img is None:
        print(f"ERROR: could not read image: {image_path}")
        return

    face_ex = FaceExtractor()
    body_ex = Extractor()
    gal = Gallery()

    face_emb = face_ex.embed(img)
    body_emb = body_ex.embed(img)
    if face_emb is None and body_emb is None:
        print("ERROR: no face and no usable body found in the photo — nothing enrolled.")
        return

    person = gal.add(face_emb, body_emb, label=config.LABEL_EMPLOYEE,
                     name=name, person_id=person_id)
    got = []
    if face_emb is not None:
        got.append("face")
    if body_emb is not None:
        got.append("body")
    print(f"Enrolled {person.label}: {person.id} ({person.name})  [{', '.join(got)}]")
    if face_emb is None:
        print("  NOTE: no face detected — recognition will rely on body ReID only.")
    print(f"Gallery now has {len(gal.people)} people.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print('Usage: python -m feature_id.enroll  <id>  "<name>"  <image_path>')
        sys.exit(1)
    enroll_employee(sys.argv[1], sys.argv[2], sys.argv[3])
