#!/usr/bin/env python3
"""
Reports & Logs public site — static server + Brain reverse proxy
────────────────────────────────────────────────────────────────
A second, stand-alone website that serves ONLY the Report + Log screens of the
Sentinel console (same design, same functions), packaged so ONE public URL —
e.g. a single free ngrok tunnel — exposes everything:

  • serves the static UI files in this folder (index.html / *.js / styles/),
  • reverse-proxies /brain/* to the Brain (Part 2) REST API, so remote
    browsers never need to reach :8000 themselves,
  • serves /storage/* person-crop snapshots from the repo root (same as the
    main console's bridge), and
  • serves /snapshot/<cam> single stills from the pipeline's shared-memory
    frames when the pipeline is running (photo fallback only — no video).

Run:  python server.py           then open  http://localhost:8090
Expose online:  ngrok http 8090  (see README.md)

No third-party deps — pure standard library.

Endpoints
  GET  /                    -> index.html
  GET  /api/cameras         -> [{id,name,location,status}, ...] from the shared registry
  ANY  /brain/<path>        -> proxied to BRAIN_URL/<path> (default http://localhost:8000)
  GET  /storage/<path>      -> person-crop snapshots written by Part 1
  GET  /snapshot/<cam>      -> latest pipeline frame for one camera (if available)
"""

import os
import sys
import json
import posixpath
import http.client
import tempfile
from urllib.parse import urlparse, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
HOST = "0.0.0.0"
# The Brain owns :8000 and the main console bridge owns :8080 — this public
# reports site defaults to :8090 so all three can run on one host.
PORT = int(os.environ.get("REPORTS_PORT", "8090"))
BRAIN_URL = os.environ.get("BRAIN_URL", "http://localhost:8000").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # repo root — storage/ lives here

# Pipeline shared-memory dir (same default as the main console's bridge) — used
# only for the /snapshot/<cam> photo fallback.
_SHM_DEFAULT = "/dev/shm/sentinel" if os.path.isdir("/dev/shm") else os.path.join(
    tempfile.gettempdir(), "sentinel")
SHM_DIR = os.environ.get("SENTINEL_SHM", _SHM_DEFAULT)

# ─────────────────────────────────────────────
#  CAMERAS — id -> friendly name from the shared registry (names only, no video)
# ─────────────────────────────────────────────
def zone_label(zone_id: str) -> str:
    """'ZONE-SANJEEVAN' -> 'Sanjeevan' for a friendly sub-label."""
    return (zone_id or "").replace("ZONE-", "").replace("-", " ").title() or "—"


def load_camera_list():
    """Same registry the Brain + main console use, so location labels match.
    Credentials are NOT needed — we never open a stream here."""
    try:
        sys.path.insert(0, ROOT)
        from surveillance_Camera_config.loader import load_cameras
        return [
            {"id": c.camera_uid, "name": c.name,
             "location": zone_label(c.zone_id), "status": "online"}
            for c in load_cameras(active_only=True, streamable_only=False)
        ]
    except Exception as e:
        print(f"[reports] camera registry unavailable ({e}) — UI will use its fallback list")
        return []


CAMERA_LIST = load_camera_list()

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
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4":  "video/mp4",
}

# Request headers worth forwarding to the Brain (auth + content negotiation).
PROXY_REQ_HEADERS = ("Content-Type", "Authorization", "Accept")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet

    # --- routing ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "":
            path = "/index.html"
        if path == "/api/cameras":
            return self._send_cameras()
        if path.startswith("/brain/") or path == "/brain":
            return self._proxy_brain("GET")
        if path.startswith("/storage/"):
            return self._send_media(path)
        if path.startswith("/snapshot/"):
            return self._send_snapshot(unquote(path[len("/snapshot/"):]))
        return self._send_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/brain/") or path == "/brain":
            return self._proxy_brain("POST")
        return self._send_error(404, "Not found")

    def do_PUT(self):
        if urlparse(self.path).path.startswith("/brain"):
            return self._proxy_brain("PUT")
        return self._send_error(404, "Not found")

    def do_DELETE(self):
        if urlparse(self.path).path.startswith("/brain"):
            return self._proxy_brain("DELETE")
        return self._send_error(404, "Not found")

    # --- /brain/* reverse proxy ------------------------------------------
    def _proxy_brain(self, method):
        # '/brain/events?limit=5' -> '/events?limit=5' on the Brain.
        target = self.path[len("/brain"):] or "/"
        u = urlparse(BRAIN_URL)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {h: self.headers[h] for h in PROXY_REQ_HEADERS if self.headers.get(h)}
        try:
            conn = conn_cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=30)
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except Exception as e:
            return self._send_error(502, f"Brain unreachable: {e}")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # --- /api/cameras ------------------------------------------------------
    def _send_cameras(self):
        payload = json.dumps(CAMERA_LIST).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    # --- /storage/... (person-crop snapshots written by Part 1) ------------
    def _send_media(self, path):
        rel = posixpath.normpath(path).lstrip("/")
        full = os.path.join(ROOT, rel)
        if not os.path.abspath(full).startswith(ROOT) or not os.path.isfile(full):
            return self._send_error(404, "Not found")
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    # --- /snapshot/<cam> — latest pipeline frame (photo fallback only) -----
    def _send_snapshot(self, cam_id):
        # cam ids come from the registry (e.g. 'GATE-RIGHT') — keep the read
        # inside SHM_DIR to block traversal.
        safe = os.path.basename(cam_id)
        fp = os.path.join(SHM_DIR, f"{safe}.jpg")
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            return self._send_error(404, "No snapshot available")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- static files -------------------------------------------------------
    def _send_static(self, path):
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
        # The HTML/JS/CSS change often (live edits) and are served over a mobile
        # reverse-proxy that otherwise caches them hard — which left phones running
        # STALE code (blank page after a fix). Force revalidation so every load
        # picks up the current files.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"\n  Reports & Logs site ->  http://localhost:{PORT}")
    print(f"  Brain proxy         ->  /brain/*  ->  {BRAIN_URL}")
    print(f"  Expose online       ->  ngrok http {PORT}\n  (Ctrl-C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
