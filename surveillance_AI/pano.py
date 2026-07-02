"""
pano.py — 360° (equirectangular) camera support for Part 1.

An Insta360 (via the Jetson) publishes ONE equirectangular stream. This module
opens that stream once and carves it into several flat perspective ("pinhole")
views — front / right / back / left — each of which behaves like a normal camera
the detector/identifier can consume.

    Insta360 X4 -> Jetson (encode+serve) -> PanoStream (this file) -> N PanoViews
                                                                      (front/right/...)

One PanoStream per physical 360 source; many PanoViews share it (the source is
read only once). Each PanoView exposes the same interface as nvr_stream's
CameraStream (`camera_uid`, `label`, `get_frame()`, `stop()`), so the pipeline
treats a 360 view exactly like any other camera — including its own role.

Reprojection math is the validated pano_views.py: precompute cv2.remap tables
once per view, then one remap per frame (fast enough for real time).
"""
import threading
import time

import cv2
import numpy as np

# Size of each carved perspective view. Detection downscales internally, so this
# only needs to be big enough to see people clearly.
OUT_W, OUT_H = 960, 540


def build_maps(equi_w, equi_h, out_w, out_h, fov_deg, yaw_deg, pitch_deg):
    """Precompute remap tables mapping perspective pixels -> equirect pixels."""
    f = 0.5 * out_w / np.tan(np.radians(fov_deg) / 2.0)

    xs = np.arange(out_w, dtype=np.float64) - (out_w - 1) / 2.0
    ys = (out_h - 1) / 2.0 - np.arange(out_h, dtype=np.float64)  # top -> sky
    xx, yy = np.meshgrid(xs, ys)
    zz = np.full_like(xx, f)

    dirs = np.stack([xx, yy, zz], axis=-1)
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)

    yaw, pitch = np.radians(yaw_deg), np.radians(pitch_deg)
    ry = np.array([[np.cos(yaw), 0.0, np.sin(yaw)],
                   [0.0, 1.0, 0.0],
                   [-np.sin(yaw), 0.0, np.cos(yaw)]])
    rx = np.array([[1.0, 0.0, 0.0],
                   [0.0, np.cos(pitch), -np.sin(pitch)],
                   [0.0, np.sin(pitch), np.cos(pitch)]])
    dirs = dirs @ (ry @ rx).T

    lon = np.arctan2(dirs[..., 0], dirs[..., 2])
    lat = np.arcsin(np.clip(dirs[..., 1], -1.0, 1.0))

    map_x = (lon / (2.0 * np.pi) + 0.5) * equi_w
    map_y = (0.5 - lat / np.pi) * equi_h
    return map_x.astype(np.float32), map_y.astype(np.float32)


class PanoStream(threading.Thread):
    """Reads one equirectangular source in the background; keeps the latest frame."""

    def __init__(self, url, equi_w, equi_h):
        super().__init__(daemon=True)
        self.url = url
        self.equi_w = equi_w
        self.equi_h = equi_h
        self.frame = None
        self.lock = threading.Lock()
        self.running = True

    def _open(self):
        # Reuse nvr_stream's scheme-aware opener (http → low-latency options).
        # Lazy import avoids an import cycle (nvr_stream imports pano on demand).
        from nvr_stream import open_capture
        return open_capture(self.url)

    def run(self):
        cap = self._open()
        print(f"[pano] connecting 360 source: {self.url}")
        while self.running:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("[pano] frame drop; reconnecting...")
                cap.release()
                time.sleep(1)
                cap = self._open()
                continue
            if frame.shape[1] != self.equi_w or frame.shape[0] != self.equi_h:
                frame = cv2.resize(frame, (self.equi_w, self.equi_h))
            with self.lock:
                self.frame = frame
        cap.release()

    def get_equi(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.running = False


class PanoView:
    """One flat perspective view carved from a shared PanoStream. Mimics the
    CameraStream interface so the pipeline/viewer treat it like any camera."""

    def __init__(self, camera, pano_stream):
        self.camera_uid = camera.camera_uid
        self.label = camera.label
        self._pano = pano_stream
        self._maps = build_maps(pano_stream.equi_w, pano_stream.equi_h,
                                OUT_W, OUT_H, camera.fov, camera.yaw, camera.pitch)

    def get_frame(self):
        equi = self._pano.get_equi()
        if equi is None:
            return None
        mx, my = self._maps
        return cv2.remap(equi, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)

    def stop(self):
        # shared; stopping is idempotent (multiple views may call it)
        self._pano.stop()
