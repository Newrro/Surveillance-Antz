#!/usr/bin/env python3
"""
Sentinel bridge server
──────────────────────
Browsers cannot play RTSP directly. This server reuses the CameraStream logic
from the standalone viewer, but instead of an OpenCV window it:

  • pulls each IP camera's RTSP feed on its own thread (as before),
  • re-encodes frames as JPEG and serves them as MJPEG over HTTP, so the web
    UI can show them with a plain <img> tag, and
  • serves the static UI files (index.html / styles.css / app.js / data.js).

Run:  python3 server.py           then open  http://localhost:8000
Deps: opencv-python, numpy   (same as the original viewer — no Flask needed)

Endpoints
  GET /                     -> index.html
  GET /api/cameras          -> [{id,name,location,status}, ...]  (live source of truth)
  GET /stream/<id>          -> multipart MJPEG live feed for one camera
"""

import os
# Force TCP transport + connection timeout so dead cameras fail fast instead of
# hanging the worker thread. Must be set BEFORE cv2 is imported.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

import cv2
import sys
import time
import json
import threading
import posixpath
import numpy as np
import subprocess
from urllib.parse import urlparse, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
HOST = "0.0.0.0"
# The Brain (Part 2) owns :8000. The UI bridge serves the web app + MJPEG feeds
# on :8080 by default so both can run on one host. Override with SENTINEL_PORT.
PORT = int(os.environ.get("SENTINEL_PORT", "8080"))
JPEG_QUALITY = 70          # 0-100; lower = less bandwidth/CPU
OFFLINE_AFTER = 8.0        # seconds without a real frame before status = offline
HERE = os.path.dirname(os.path.abspath(__file__))

# ── GPU (NVDEC) decode for the browser tiles ───────────────────────────────
# 1440p HEVC streams decoded on the CPU (OpenCV) saturate the machine when many
# run at once. If a system ffmpeg with *_cuvid decoders is present we decode on
# the GPU at tile resolution instead. Force off with GPU_DECODE=0.
import shutil as _shutil
GPU_DECODE = os.environ.get("GPU_DECODE", "auto").lower()      # auto | 1 | 0
GPU_DECODE_CODEC = os.environ.get("GPU_DECODE_CODEC", "hevc_cuvid")
UI_DECODE_W = int(os.environ.get("UI_DECODE_W", "640"))        # browser tile size
UI_DECODE_H = int(os.environ.get("UI_DECODE_H", "360"))
UI_DECODE_FPS = os.environ.get("UI_DECODE_FPS", "8")
_gpu_ok_cache = None


def gpu_decode_available():
    global _gpu_ok_cache
    if _gpu_ok_cache is not None:
        return _gpu_ok_cache
    ok = False
    if GPU_DECODE != "0":
        ff = _shutil.which("ffmpeg")
        if ff:
            try:
                out = subprocess.run([ff, "-hide_banner", "-decoders"],
                                     capture_output=True, text=True, timeout=10)
                ok = GPU_DECODE_CODEC in out.stdout
            except Exception:
                ok = False
    _gpu_ok_cache = ok
    return ok

# ─────────────────────────────────────────────
#  CAMERAS — loaded from the shared registry (surveillance_Camera_config)
# ─────────────────────────────────────────────
# The canonical camera UIDs + metadata live in surveillance_Camera_config/
# cameras.json; credentials in the gitignored cameras.secrets.json. Using the
# SAME registry the Brain seeds from means the UI's camera ids (GATE-RIGHT, …)
# match the Brain's — so live detections overlay on the right tiles. The loader
# builds each stream_url from the secrets, so no credentials live in this file.
sys.path.insert(0, os.path.dirname(HERE))  # repo root, to import the shared package
from surveillance_Camera_config.loader import load_cameras  # noqa: E402


def zone_label(zone_id: str) -> str:
    """'ZONE-SANJEEVAN' -> 'Sanjeevan' for a friendly sub-label under the tile."""
    return (zone_id or "").replace("ZONE-", "").replace("-", " ").title() or "—"


# (camera_uid, stream_url, name, location) — only streamable cameras (have creds).
CAMERAS = [
    (c.camera_uid, c.stream_url, c.name, zone_label(c.zone_id))
    for c in load_cameras(active_only=True, streamable_only=True)
]


# ─────────────────────────────────────────────
#  AI MODEL — plug yours in here
# ─────────────────────────────────────────────
def run_ai_model(frame, cam_id):
    """
    Drop your AI model logic here.
    `frame`  - BGR numpy array (standard OpenCV format)
    `cam_id` - which camera this frame is from (the stable UI id)
    Return the annotated frame.
    """
    return frame


def _placeholder(text, w=640, h=360):
    """A black frame with a status message, JPEG-encoded — shown until the real
    stream connects, so the UI's <img> always has something to display."""
    img = np.zeros((h, w, 3), dtype="uint8")
    cv2.putText(img, text, (int(w * 0.06), h // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (90, 90, 90), 2)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else b""


# ─────────────────────────────────────────────
#  PER-CAMERA THREAD  (grabs frames, keeps latest JPEG)
# ─────────────────────────────────────────────
class CameraStream(threading.Thread):
    def __init__(self, cam_id, stream_url, name, location):
        super().__init__(daemon=True)
        self.id       = cam_id
        self.name     = name
        self.location = location
        self.url      = stream_url

        self._cond      = threading.Condition()
        self._jpeg      = _placeholder(f"{name} — connecting...")
        self._seq       = 1            # bumped on every new frame; clients wait on it
                                       # (start at 1 so the initial placeholder is
                                       #  delivered immediately, even while the RTSP
                                       #  connection is still being established)
        self._last_real = 0.0          # time of last real (camera) frame
        self.running    = True
        self._encode    = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]

    # --- producer -------------------------------------------------------
    def _publish(self, jpeg_bytes, real):
        with self._cond:
            self._jpeg = jpeg_bytes
            self._seq += 1
            if real:
                self._last_real = time.time()
            self._cond.notify_all()

    def run(self):
        if self.url and self.url.startswith("rtsp://") and gpu_decode_available():
            self._run_gpu()
        else:
            self._run_cpu()

    def _run_gpu(self):
        """Decode on the GPU (NVDEC) via ffmpeg, at tile resolution — keeps the
        CPU free even with many 1440p HEVC cameras."""
        w, h = UI_DECODE_W, UI_DECODE_H
        fsz = w * h * 3
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-fflags", "+discardcorrupt", "-rtsp_transport", "tcp",
               "-rw_timeout", "10000000", "-hwaccel", "cuda", "-c:v", GPU_DECODE_CODEC,
               "-i", self.url, "-an",
               "-vf", f"fps={UI_DECODE_FPS},scale={w}:{h}",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        print(f"[{self.id}] Connecting (GPU decode): {self.name}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=fsz)
        while self.running:
            buf = proc.stdout.read(fsz)
            if len(buf) != fsz:
                if not self.running:
                    break
                self._publish(_placeholder(f"{self.name} — reconnecting..."), real=False)
                try:
                    proc.kill()
                except Exception:
                    pass
                time.sleep(2)
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=fsz)
                continue
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            frame = run_ai_model(frame, self.id)
            ok, jbuf = cv2.imencode(".jpg", frame, self._encode)
            if ok:
                self._publish(jbuf.tobytes(), real=True)
        try:
            proc.kill()
        except Exception:
            pass

    def _run_cpu(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        print(f"[{self.id}] Connecting: {self.name}")

        while self.running:
            if not cap.isOpened():
                self._publish(_placeholder(f"{self.name} — reconnecting..."), real=False)
                print(f"[{self.id}] Retrying in 3s...")
                time.sleep(3)
                cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                continue

            ret, frame = cap.read()
            if not ret:
                self._publish(_placeholder(f"{self.name} — signal lost"), real=False)
                print(f"[{self.id}] Frame grab failed, reconnecting...")
                cap.release()
                time.sleep(2)
                cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                continue

            frame = run_ai_model(frame, self.id)
            ok, buf = cv2.imencode(".jpg", frame, self._encode)
            if ok:
                self._publish(buf.tobytes(), real=True)

        cap.release()

    # --- consumer -------------------------------------------------------
    def wait_for_frame(self, last_seq, timeout=5.0):
        """Block until a frame newer than `last_seq` is available.
        Returns (jpeg_bytes, seq) or (None, last_seq) on timeout."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            if self._seq == last_seq:
                return None, last_seq
            return self._jpeg, self._seq

    @property
    def status(self):
        if self._last_real == 0.0:
            return "connecting"
        return "online" if (time.time() - self._last_real) < OFFLINE_AFTER else "offline"

    def stop(self):
        self.running = False


# Registry: id -> CameraStream
STREAMS = {}


# ─────────────────────────────────────────────
#  HTTP HANDLER
# ─────────────────────────────────────────────
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
    ".svg":  "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet; camera threads already log connection state

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "":
            path = "/index.html"

        if path == "/api/cameras":
            return self._send_cameras()
        if path.startswith("/stream/"):
            return self._send_stream(unquote(path[len("/stream/"):]))
        if path.startswith("/snapshot/"):
            return self._send_snapshot(unquote(path[len("/snapshot/"):]))
        return self._send_static(path)

    # --- /api/cameras ---------------------------------------------------
    def _send_cameras(self):
        payload = json.dumps([
            {"id": s.id, "name": s.name, "location": s.location, "status": s.status}
            for s in STREAMS.values()
        ]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    # --- /stream/<id> ---------------------------------------------------
    def _send_stream(self, cam_id):
        stream = STREAMS.get(cam_id)
        if stream is None:
            return self._send_error(404, "Unknown camera")

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        last_seq = 0
        try:
            while stream.running:
                jpeg, last_seq = stream.wait_for_frame(last_seq)
                if jpeg is None:
                    # No fresh frame within the timeout (camera down / still
                    # connecting). Push a status placeholder so the browser <img>
                    # shows the camera's state instead of going blank.
                    jpeg = _placeholder(f"{stream.name} — {stream.status}")
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client (browser) closed the tab / navigated away

    # --- /snapshot/<id> -------------------------------------------------
    # A single current JPEG still for one camera (not the MJPEG stream). The UI
    # uses this as a detected person's photo: the view from the camera they were
    # seen on. (A real Part 1 crops the person; this is the whole-frame stand-in.)
    def _send_snapshot(self, cam_id):
        stream = STREAMS.get(cam_id)
        if stream is None:
            return self._send_error(404, "Unknown camera")
        with stream._cond:
            jpeg = stream._jpeg
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(jpeg)

    # --- static files ---------------------------------------------------
    def _send_static(self, path):
        # Resolve safely inside HERE (no directory traversal).
        rel = posixpath.normpath(path).lstrip("/")
        full = os.path.join(HERE, rel)
        if not os.path.abspath(full).startswith(HERE) or not os.path.isfile(full):
            return self._send_error(404, "Not found")

        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    if not CAMERAS:
        print("No streamable cameras in the registry. Check "
              "surveillance_Camera_config/cameras.secrets.json (copy the .example "
              "and fill in credentials).")
    print(f"Starting {len(CAMERAS)} camera streams...")
    for cam_id, stream_url, name, location in CAMERAS:
        s = CameraStream(cam_id, stream_url, name, location)
        s.start()
        STREAMS[cam_id] = s

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n  Sentinel console →  {url}\n  (Ctrl-C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for s in STREAMS.values():
            s.stop()
        server.server_close()
        print("All streams closed.")


if __name__ == "__main__":
    main()
