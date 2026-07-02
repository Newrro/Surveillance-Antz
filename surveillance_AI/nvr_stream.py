"""
nvr_stream.py — RTSP ingest for Part 1 (Perception).

One background thread per camera keeps only the latest frame (drops stale ones),
reconnects on failure, and forces TCP transport so dead cameras fail fast instead
of hanging the worker.

Camera list + credentials are NOT defined here anymore — they live in the shared
registry under ../surveillance_Camera_config (metadata committed, secrets
gitignored). See surveillance_Camera_config/README.md.

Quick raw multi-camera view (no AI, just "are the cameras up?"):
    python nvr_stream.py

WHY WE STREAM CAMERAS DIRECTLY (not via the NVR): the site NVR (Impact / Hik-OEM
I-HNVR-1880 at 192.168.1.9) serves only ONE camera over RTSP regardless of the
channel requested — its live view is a proprietary encrypted websocket. So each
IP camera is streamed directly from its own address; the per-camera credentials
were read from the NVR's /ISAPI/ContentMgmt/InputProxy/channels config.
"""
import os

# Force TCP transport + connection timeout. Must be set BEFORE cv2 is imported.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

import sys
import time
import math
import threading

import cv2
import numpy as np

# ── import the shared camera registry (sibling folder in the monorepo) ──
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from surveillance_Camera_config import load_cameras  # noqa: E402


def open_capture(url):
    """Open a VideoCapture with FFmpeg options matched to the URL scheme:
      rtsp:// → force TCP transport + connection timeout (dead cams fail fast).
      http(s) → low-latency, no-buffer (MJPEG / Insta360-via-Jetson / local feeds).
    OPENCV_FFMPEG_CAPTURE_OPTIONS is process-global, so we set it just for this
    open and restore it — otherwise RTSP options leak onto HTTP streams (and
    break them) and vice-versa when several source types run together."""
    if url.startswith("rtsp://"):
        opts = "rtsp_transport;tcp|stimeout;5000000"
    else:
        opts = "fflags;nobuffer|flags;low_delay|reorder_queue_size;0"
    prev = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    finally:
        if prev is None:
            os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
        else:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = prev
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


# ─────────────────────────────────────────────
#  PER-CAMERA THREAD
# ─────────────────────────────────────────────
class CameraStream(threading.Thread):
    """Reads one camera in the background; get_frame() returns the latest frame."""

    def __init__(self, camera):
        """`camera` is a surveillance_Camera_config.Camera (has camera_uid,
        stream_url, label/name)."""
        super().__init__(daemon=True)
        self.camera_uid = camera.camera_uid
        self.label = camera.label
        self.url = camera.stream_url
        self.frame = None
        self.lock = threading.Lock()
        self.running = True

    def run(self):
        if not self.url:
            print(f"[{self.camera_uid}] no stream_url configured — skipping.")
            return
        cap = open_capture(self.url)
        print(f"[{self.camera_uid}] Connecting: {self.label}")

        while self.running:
            if not cap.isOpened():
                print(f"[{self.camera_uid}] Retrying in 3s...")
                time.sleep(3)
                cap = open_capture(self.url)
                continue

            ret, frame = cap.read()
            if not ret:
                print(f"[{self.camera_uid}] Frame grab failed, reconnecting...")
                cap.release()
                time.sleep(2)
                cap = open_capture(self.url)
                continue

            with self.lock:
                self.frame = frame

        cap.release()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.running = False


def start_streams(cameras):
    """Start a frame-source per camera and return them. Each source exposes the
    same interface (camera_uid, label, get_frame(), stop()).

    - rtsp / url cameras  → a CameraStream (opens stream_url directly).
    - pano_view cameras   → a PanoView sharing one PanoStream per 360 source, so
                            the equirectangular feed is read only once.
    """
    sources = []
    pano_streams = {}   # pano_group -> PanoStream (shared across its views)
    for c in cameras:
        if getattr(c, "source_type", "rtsp") == "pano_view":
            from pano import PanoStream, PanoView
            ps = pano_streams.get(c.pano_group)
            if ps is None:
                ps = PanoStream(c.stream_url, c.equi_w, c.equi_h)
                ps.start()
                pano_streams[c.pano_group] = ps
            sources.append(PanoView(c, ps))
        else:
            s = CameraStream(c)
            s.start()
            sources.append(s)
    return sources


# ─────────────────────────────────────────────
#  DISPLAY — grid layout in one window
# ─────────────────────────────────────────────
def make_grid(frames, labels, grid_cols=4, thumb_w=480, thumb_h=270):
    """Arrange frames into a single grid image."""
    grid_rows = math.ceil(len(frames) / grid_cols) if frames else 1
    grid_img = np.zeros((grid_rows * thumb_h, grid_cols * thumb_w, 3), dtype="uint8")

    for idx, (frame, label) in enumerate(zip(frames, labels)):
        row, col = idx // grid_cols, idx % grid_cols
        y1, y2 = row * thumb_h, (row + 1) * thumb_h
        x1, x2 = col * thumb_w, (col + 1) * thumb_w

        if frame is not None:
            thumb = cv2.resize(frame, (thumb_w, thumb_h))
        else:
            thumb = np.zeros((thumb_h, thumb_w, 3), dtype="uint8")
            cv2.putText(thumb, "Connecting...", (10, thumb_h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 100), 2)

        cv2.rectangle(thumb, (0, 0), (thumb_w, 28), (0, 0, 0), -1)
        cv2.putText(thumb, label, (6, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)
        grid_img[y1:y2, x1:x2] = thumb

    return grid_img


def main():
    cameras = load_cameras(streamable_only=True)
    print(f"Starting {len(cameras)} camera streams...")
    streams = start_streams(cameras)

    win = "NVR — All Cameras (raw)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cols = max(1, round(math.sqrt(len(streams))))
    fullscreen = False
    print("Press 'q' to quit, 'f' to toggle fullscreen.")

    while True:
        frames = [s.get_frame() for s in streams]
        labels = [s.label for s in streams]
        grid = make_grid(frames, labels, grid_cols=cols)
        cv2.imshow(win, grid)

        k = cv2.waitKey(1) & 0xFF
        if k == ord('q'):
            break
        elif k == ord('f'):
            fullscreen = not fullscreen
            cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN,
                                  cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL)

    for s in streams:
        s.stop()
    cv2.destroyAllWindows()
    print("All streams closed.")


if __name__ == "__main__":
    main()
