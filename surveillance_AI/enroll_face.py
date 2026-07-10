#!/usr/bin/env python3
"""
enroll_face.py — embed face photo(s) for employee enrollment.

The Brain runs no ML model, so employee photo-enrollment shells out to THIS (it
lives in the AI venv with AdaFace). Reads one or more image paths, extracts the
AdaFace face embedding from the largest clear face in each, and prints a JSON
result to stdout. Enrollment is rare, so a per-request model load is fine.

Usage:
    python enroll_face.py IMG1 [IMG2 ...] [--face-out DIR]

Output (stdout): {"results": [{"path","ok","embedding":[...512],"quality",
                                "face_path"?,"error"?}, ...]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feature_id.face_extractor import FaceExtractor  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--face-out", default=None, help="dir to save aligned face crops")
    args = ap.parse_args()

    fe = FaceExtractor()
    results = []
    for path in args.images:
        rec = {"path": path, "ok": False}
        try:
            img = cv2.imread(path)
            if img is None:
                rec["error"] = "unreadable image"
                results.append(rec)
                continue
            emb, aligned = fe.embed_with_face(img)
            if emb is None:
                rec["error"] = "no clear face found (too small/blurry/none)"
                results.append(rec)
                continue
            rec["ok"] = True
            rec["embedding"] = [float(x) for x in emb]
            rec["quality"] = float(getattr(fe, "last_norm", 0.0))
            if args.face_out and aligned is not None:
                os.makedirs(args.face_out, exist_ok=True)
                fp = os.path.join(args.face_out, os.path.splitext(os.path.basename(path))[0] + "_face.jpg")
                cv2.imwrite(fp, aligned)
                rec["face_path"] = fp
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)
        results.append(rec)

    print(json.dumps({"results": results}))


if __name__ == "__main__":
    main()
