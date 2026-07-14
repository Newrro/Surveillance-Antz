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
import shutil
import threading
import subprocess

import cv2
import numpy as np

# ── GPU (NVDEC) hardware decode ────────────────────────────────────────────
# Many 1440p HEVC streams saturate the CPU under OpenCV's software decoder. When
# a system ffmpeg with *_cuvid decoders is present, we decode on the GPU instead
# (a per-camera ffmpeg subprocess), dropping decode from ~1 core per camera to
# ~0.1. Auto-enabled for rtsp:// URLs; force off with GPU_DECODE=0.
#
# RESOLUTION: identity quality is bounded by the pixels on a face/body, so we
# decode at NATIVE resolution (probed per camera) up to GPU_DECODE_MAX_H, instead
# of hard-downscaling every stream to 720p. Detection is decoupled — the pipeline
# runs the detector on a cheap downscaled copy (RT-DETR resizes internally anyway)
# and crops faces/bodies from this full-res frame. NVDEC decode + a fixed-input
# detector means higher res costs GPU memory bandwidth, not inference time.
# Set GPU_DECODE_W and GPU_DECODE_H explicitly to force a fixed size (old behavior).
GPU_DECODE = os.environ.get("GPU_DECODE", "auto").lower()      # auto | 1 | 0
GPU_DECODE_CODEC = os.environ.get("GPU_DECODE_CODEC", "hevc_cuvid")
# Explicit fixed-size override (both must be set). Otherwise decode native, capped.
GPU_DECODE_W = int(os.environ["GPU_DECODE_W"]) if "GPU_DECODE_W" in os.environ else None
GPU_DECODE_H = int(os.environ["GPU_DECODE_H"]) if "GPU_DECODE_H" in os.environ else None
GPU_DECODE_MAX_H = int(os.environ.get("GPU_DECODE_MAX_H", "1440"))  # cap native height (VRAM/bandwidth guard)
GPU_DECODE_FPS = os.environ.get("GPU_DECODE_FPS", "6")         # decode fps cap (cooldown gates processing anyway)


def _probe_dims(url, codec=None):
    """Probe a stream's native (width, height) with ffprobe, or None on failure."""
    fp = shutil.which("ffprobe")
    if not fp or not url:
        return None
    cmd = [fp, "-v", "error", "-rtsp_transport", "tcp",
           "-select_streams", "v:0", "-show_entries", "stream=width,height",
           "-of", "csv=p=0:s=x", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        w, h = out.stdout.strip().split("x")[:2]
        w, h = int(w), int(h)
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _target_dims(native_w, native_h):
    """Decode target: explicit override if set, else native capped to MAX_H
    (aspect-preserved, even dimensions for the encoder)."""
    if GPU_DECODE_W and GPU_DECODE_H:
        return GPU_DECODE_W, GPU_DECODE_H
    if native_h > GPU_DECODE_MAX_H:
        scale = GPU_DECODE_MAX_H / native_h
        return (int(native_w * scale) // 2) * 2, (GPU_DECODE_MAX_H // 2) * 2
    return (native_w // 2) * 2, (native_h // 2) * 2

_gpu_ok_cache = None


def gpu_decode_available():
    """True if a system ffmpeg with the configured *_cuvid decoder exists."""
    global _gpu_ok_cache
    if _gpu_ok_cache is not None:
        return _gpu_ok_cache
    if GPU_DECODE == "0":
        _gpu_ok_cache = False
        return False
    ok = False
    ff = shutil.which("ffmpeg")
    if ff:
        try:
            out = subprocess.run([ff, "-hide_banner", "-decoders"],
                                 capture_output=True, text=True, timeout=10)
            ok = GPU_DECODE_CODEC in out.stdout
        except Exception:
            ok = False
    if GPU_DECODE == "1" and not ok:
        print("[nvr] GPU_DECODE=1 but ffmpeg/*_cuvid unavailable — using CPU decode.")
    _gpu_ok_cache = ok
    return ok

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
                self.frame_ts = time.time()

        cap.release()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def frame_age(self):
        """Seconds since the newest decoded frame (inf before the first)."""
        with self.lock:
            ts = getattr(self, "frame_ts", None)
        return (time.time() - ts) if ts else float("inf")

    def stop(self):
        self.running = False


class FFmpegCameraStream(threading.Thread):
    """Latest-frame reader that decodes on the GPU (NVDEC) via an ffmpeg
    subprocess, downscaled to WxH. Same interface as CameraStream so it is a
    drop-in replacement (camera_uid, label, get_frame(), stop())."""

    def __init__(self, camera, width=None, height=None, fps=GPU_DECODE_FPS):
        super().__init__(daemon=True)
        self.camera_uid = camera.camera_uid
        self.label = camera.label
        self.url = camera.stream_url
        self.fps = fps
        if width and height:                     # caller forced a size
            self.w, self.h = width, height
        else:
            native = _probe_dims(self.url) or (1280, 720)
            self.w, self.h = _target_dims(*native)
            print(f"[{self.camera_uid}] native {native[0]}x{native[1]} -> decode {self.w}x{self.h}")
        self.frame = None
        self.lock = threading.Lock()
        self.running = True

    def _spawn(self):
        # -fflags +discardcorrupt : drop corrupt HEVC packets instead of stalling
        # -timeout 10s (rtsp us)   : exit if the stream goes silent, so run() respawns
        #   (NOTE: rtsp uses -timeout, NOT -rw_timeout — the latter is "Option not
        #    found" for the rtsp demuxer and kills the stream instantly.)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-fflags", "+discardcorrupt", "-rtsp_transport", "tcp",
               "-timeout", "10000000", "-hwaccel", "cuda", "-c:v", GPU_DECODE_CODEC,
               "-i", self.url, "-an",
               "-vf", f"fps={self.fps},scale={self.w}:{self.h}",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=self.w * self.h * 3)

    def run(self):
        if not self.url:
            print(f"[{self.camera_uid}] no stream_url configured — skipping.")
            return
        fsz = self.w * self.h * 3
        print(f"[{self.camera_uid}] Connecting (GPU decode): {self.label}")
        proc = self._spawn()
        while self.running:
            buf = proc.stdout.read(fsz)
            if len(buf) != fsz:                      # ffmpeg exited / stream dropped
                if not self.running:
                    break
                print(f"[{self.camera_uid}] stream ended, reconnecting...")
                try:
                    proc.kill()
                except Exception:
                    pass
                time.sleep(2)
                proc = self._spawn()
                continue
            with self.lock:
                self.frame = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
                self.frame_ts = time.time()
        try:
            proc.kill()
        except Exception:
            pass

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def frame_age(self):
        """Seconds since the newest decoded frame (inf before the first)."""
        with self.lock:
            ts = getattr(self, "frame_ts", None)
        return (time.time() - ts) if ts else float("inf")

    def stop(self):
        self.running = False


def start_streams(cameras):
    """Start a frame-source per camera and return them. Each source exposes the
    same interface (camera_uid, label, get_frame(), stop()).

    - rtsp cameras (GPU)  → an FFmpegCameraStream (NVDEC decode) when a cuvid
                            ffmpeg is available; else a CameraStream (OpenCV/CPU).
    - url / file cameras  → a CameraStream (opens stream_url directly).
    - pano_view cameras   → a PanoView sharing one PanoStream per 360 source, so
                            the equirectangular feed is read only once.
    """
    sources = []
    pano_streams = {}   # pano_group -> PanoStream (shared across its views)
    use_gpu = gpu_decode_available()
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
            if use_gpu and (c.stream_url or "").startswith("rtsp://"):
                s = FFmpegCameraStream(c)
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
