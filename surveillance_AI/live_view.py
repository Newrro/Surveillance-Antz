"""
live_view.py — all cameras on one screen with live people detection; click a
tile to open that camera fullscreen. This is the "detection working perfectly"
demo from the gate build, ported onto the shared camera registry + detector.

  • Grid view: every camera live, person boxes drawn. Detection runs round-robin
    (one camera per loop) with boxes cached per camera, so the grid stays smooth.
  • Click any tile  → that camera opens FULLSCREEN with detection every frame.
  • In fullscreen: click again or press 'g' → back to the grid.

Run (inside the venv that has SAM 2 if you want masks):
    python live_view.py

Keys:
    g        back to grid (from fullscreen)
    m        toggle SAM 2 masks (fullscreen only; slow — needs SAM 2 installed)
    + / -    raise / lower detection confidence
    q        quit
"""
import os
import math

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import cv2
import numpy as np

from detector import PersonDetector, draw_boxes
import nvr_stream as nvr
from surveillance_Camera_config import load_cameras

TW, TH = 480, 270   # tile size in the grid


def main():
    cameras = load_cameras(streamable_only=True)
    if not cameras:
        print("No streamable cameras. Check cameras.json / cameras.secrets.json.")
        return

    detector = PersonDetector()
    segmenter = None   # lazily built the first time masks are toggled on

    n = len(cameras)
    cols = max(1, round(math.sqrt(n)))
    rows = math.ceil(n / cols)

    print("Starting all camera streams...")
    streams = nvr.start_streams(cameras)

    boxes_cache = [[] for _ in range(n)]
    rr = 0
    conf = 0.50
    show_masks = False
    state = {"mode": "grid", "selected": None, "click": None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            state["click"] = (x, y)

    win = "Part 1 — multi-camera detection"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.setMouseCallback(win, on_mouse)

    def set_fullscreen(on):
        cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN if on else cv2.WINDOW_NORMAL)

    def ensure_segmenter():
        nonlocal segmenter
        if segmenter is None:
            try:
                from segmenter import SAM2Segmenter
                segmenter = SAM2Segmenter()
            except Exception as e:  # noqa: BLE001
                print(f"[live_view] SAM 2 unavailable, masks disabled: {e}")
                return False
        return True

    while True:
        if state["mode"] == "grid":
            f_rr = streams[rr].get_frame()
            if f_rr is not None:
                boxes_cache[rr] = detector.detect(f_rr, conf=conf, normalized=True)
            rr = (rr + 1) % n

            grid = np.zeros((rows * TH, cols * TW, 3), np.uint8)
            for i, s in enumerate(streams):
                r, c = i // cols, i % cols
                frame = s.get_frame()
                tile = (cv2.resize(frame, (TW, TH)) if frame is not None
                        else np.zeros((TH, TW, 3), np.uint8))
                draw_boxes(tile, boxes_cache[i], normalized=True, thickness=2, with_score=False)
                cv2.rectangle(tile, (0, 0), (TW, 20), (0, 0, 0), -1)
                cv2.putText(tile, f"[{i}] {s.label[:24]}  ({len(boxes_cache[i])})",
                            (4, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
                grid[r*TH:(r+1)*TH, c*TW:(c+1)*TW] = tile

            cv2.putText(grid, "click a camera to open fullscreen   |   q quit",
                        (6, rows*TH - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            cv2.imshow(win, grid)

            if state["click"] is not None:
                x, y = state["click"]; state["click"] = None
                c, r = x // TW, y // TH
                idx = r * cols + c
                if 0 <= c < cols and 0 <= r < rows and 0 <= idx < n:
                    state["mode"] = "single"; state["selected"] = idx
                    set_fullscreen(True)
                    print(f"opening [{idx}] {cameras[idx].camera_uid} fullscreen")

        else:  # single / fullscreen
            i = state["selected"]
            frame = streams[i].get_frame()
            if frame is None:
                blank = np.zeros((TH, TW, 3), np.uint8)
                cv2.putText(blank, "connecting...", (20, 140),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (120, 120, 120), 2)
                cv2.imshow(win, blank)
            else:
                nb = detector.detect(frame, conf=conf, normalized=True)
                vis = frame.copy()
                if show_masks and nb and ensure_segmenter():
                    from segmenter import apply_mask_overlay
                    H, W = frame.shape[:2]
                    segmenter.set_frame(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    for (nx1, ny1, nx2, ny2, _s) in nb:
                        mask = segmenter.mask_for_box((nx1*W, ny1*H, nx2*W, ny2*H))
                        apply_mask_overlay(vis, mask)
                draw_boxes(vis, nb, normalized=True, thickness=2, with_score=True)
                cv2.rectangle(vis, (0, 0), (vis.shape[1], 28), (0, 0, 0), -1)
                cv2.putText(vis, f"[{i}] {streams[i].label}  |  {len(nb)} ppl  |  "
                                 f"conf {conf:.2f}  |  masks {'ON' if show_masks else 'off'}"
                                 f"  |  click/g=grid  m=mask  +/- conf  q=quit",
                            (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)
                cv2.imshow(win, vis)

            if state["click"] is not None:
                state["click"] = None
                state["mode"] = "grid"; state["selected"] = None
                set_fullscreen(False)

        k = cv2.waitKey(1) & 0xFF
        if k == ord('q'):
            break
        elif k == ord('g') and state["mode"] == "single":
            state["mode"] = "grid"; state["selected"] = None
            set_fullscreen(False)
        elif k == ord('m'):
            show_masks = not show_masks
        elif k in (ord('+'), ord('=')):
            conf = min(0.95, conf + 0.05)
        elif k in (ord('-'), ord('_')):
            conf = max(0.05, conf - 0.05)

    for s in streams:
        s.stop()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
