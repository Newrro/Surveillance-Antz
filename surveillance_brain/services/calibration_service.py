"""
services/calibration_service.py
===============================
Self-calibrating decision thresholds for open-set face matching.

WHY THIS EXISTS
    On distant CCTV the ABSOLUTE cosine between two faces of the same person is
    low (~0.35-0.55) and varies by camera/lens/lighting, so no hand-picked global
    threshold ships well across deployments. Instead of guessing a number, we
    place the decision boundary RELATIVE to the similarity scale the cameras
    actually produce — specifically, just above the observed nearest-impostor
    similarity. That boundary is learned at runtime, per site, with no labels and
    no operator tuning.

THE SIGNAL (non-circular, always observable)
    For every face query with >=2 distinct gallery identities, the SECOND-best
    identity's similarity (`s2`) is — almost always — a DIFFERENT person than the
    query (at most one gallery identity is the true match). So the stream of `s2`
    values is a clean, decision-independent sample of the impostor distribution.
    We keep a running mean/std of it (Welford) and derive:

        online match threshold = clamp( mean(s2) + CALIB_MATCH_K · std(s2) , … )
        cluster-merge threshold = clamp( mean(s2) + CALIB_MERGE_K · std(s2) , … )

    Matching above `mean + 2·std` of impostors keeps false merges rare while
    letting genuine same-person pairs (which sit above the impostor mass, even at
    low absolute cosine) actually match. The merge threshold uses a larger K so
    unattended clustering is stricter than the online guess.

    Until CALIB_WARMUP samples accrue we fall back to the cold-start defaults in
    config, so behaviour is sane from the first event. State is in-memory (re-warms
    within minutes of a restart); nothing here blocks the ingest path.
"""

from __future__ import annotations

import logging
import math
import threading

import config

logger = logging.getLogger(__name__)


class _Running:
    """Welford online mean/variance — O(1) memory, numerically stable."""

    __slots__ = ("n", "mean", "_m2")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self._m2 = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self._m2 += delta * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self._m2 / self.n) if self.n > 1 else 0.0


_impostor = _Running()
_lock = threading.Lock()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def observe_impostor(s2: float | None) -> None:
    """Record one nearest-impostor similarity sample (the query's 2nd-best distinct
    identity). Call once per face query that had >=2 candidate identities."""
    if s2 is None or not config.IDENTITY_SELF_CALIBRATE:
        return
    try:
        v = float(s2)
    except (TypeError, ValueError):
        return
    if not math.isfinite(v):
        return
    with _lock:
        _impostor.add(v)


def _threshold(default: float, k: float, lo: float, hi: float) -> float:
    if not config.IDENTITY_SELF_CALIBRATE:
        return default
    with _lock:
        n, mean, std = _impostor.n, _impostor.mean, _impostor.std
    if n < config.CALIB_WARMUP:
        return default
    return _clamp(mean + k * std, lo, hi)


def match_threshold() -> float:
    """Online assign floor — recall-first, just above the impostor ceiling."""
    return _threshold(
        config.FACE_MATCH_THRESHOLD_DEFAULT, config.CALIB_MATCH_K,
        config.FACE_MATCH_MIN, config.FACE_MATCH_MAX,
    )


def merge_threshold() -> float:
    """Deferred cluster-merge floor — stricter than the online floor (precision)."""
    return _threshold(
        config.FACE_MERGE_THRESHOLD_DEFAULT, config.CALIB_MERGE_K,
        config.FACE_MERGE_MIN, config.FACE_MERGE_MAX,
    )


def stats() -> dict:
    """Snapshot for logging / a debug endpoint."""
    with _lock:
        n, mean, std = _impostor.n, _impostor.mean, _impostor.std
    return {
        "impostor_samples": n,
        "impostor_mean": round(mean, 4),
        "impostor_std": round(std, 4),
        "warmed_up": n >= config.CALIB_WARMUP,
        "match_threshold": round(match_threshold(), 4),
        "merge_threshold": round(merge_threshold(), 4),
    }
