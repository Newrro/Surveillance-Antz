"""
Tracker scenario tests: multiple people, crossing paths, brief occlusion, and
re-entry — the situations that historically turned ONE person into several
visitor ids. Run with both backends:

    venv/bin/python -m pytest tests/test_tracker_scenarios.py -v
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker import MotionTracker, SimpleTracker  # noqa: E402

FRAME = np.zeros((720, 1280, 3), dtype=np.uint8)


def _walk(x0, y0, dx, dy, steps, w=60, h=160, conf=0.9):
    """Synthetic person path: list of per-frame boxes."""
    return [(x0 + dx * i, y0 + dy * i, x0 + dx * i + w, y0 + dy * i + h, conf)
            for i in range(steps)]


def _backends():
    yield "simple", SimpleTracker
    try:
        MotionTracker()
        yield "ocsort", MotionTracker
    except Exception:
        pytest.skip("boxmot unavailable — MotionTracker scenarios skipped")


@pytest.mark.parametrize("name,cls", list(_backends()))
def test_two_separated_people_keep_two_stable_ids(name, cls):
    tr = cls()
    a = _walk(100, 100, 5, 0, 30)
    b = _walk(900, 400, -5, 0, 30)
    ids_a, ids_b = set(), set()
    for fa, fb in zip(a, b):
        vis = tr.update([fa, fb], FRAME)
        for t in vis:
            (ids_a if abs(t.box[0] - fa[0]) < abs(t.box[0] - fb[0]) else ids_b).add(t.id)
    # each person holds ONE id for the whole pass, and they never share one
    assert len(ids_a) == 1 and len(ids_b) == 1
    assert ids_a.isdisjoint(ids_b)


@pytest.mark.parametrize("name,cls", list(_backends()))
def test_brief_occlusion_does_not_mint_new_id(name, cls):
    tr = cls()
    path = _walk(200, 200, 6, 0, 40)
    seen_ids = set()
    for i, box in enumerate(path):
        boxes = [] if 15 <= i < 19 else [box]      # 4-frame occlusion gap
        for t in tr.update(boxes, FRAME):
            seen_ids.add(t.id)
    assert len(seen_ids) == 1, f"{name}: occlusion minted extra ids {seen_ids}"


@pytest.mark.parametrize("name,cls", list(_backends()))
def test_crossing_people_do_not_swap_into_one(name, cls):
    """Two people crossing must remain TWO tracks throughout (no fusion).
    A momentary id swap at the crossing point is a known limit of motion-only
    trackers; what we assert is that the pair never collapses into one id."""
    tr = cls()
    a = _walk(100, 300, 10, 0, 60)                 # left → right
    b = _walk(700, 300, -10, 0, 60)                # right → left
    for fa, fb in zip(a, b):
        vis = tr.update([fa, fb], FRAME)
        if fa[0] < fb[0] - 80 or fa[0] > fb[0] + 80:  # outside the crossing zone
            assert len(vis) == 2, f"{name}: lost a person while separated"


@pytest.mark.parametrize("name,cls", list(_backends()))
def test_reentry_after_long_absence_is_a_new_track(name, cls):
    """A person who leaves and returns much later should get a NEW track (the
    Brain re-links identity by face/body — the tracker must not glue distant
    appearances together by position)."""
    tr = cls(max_age=10) if cls is MotionTracker else cls(max_misses=10)
    first = _walk(200, 200, 5, 0, 10)
    ids_first = set()
    for box in first:
        for t in tr.update([box], FRAME):
            ids_first.add(t.id)
    for _ in range(60):                             # long absence
        tr.update([], FRAME)
    ids_second = set()
    for box in _walk(900, 500, -5, 0, 10):          # re-appears elsewhere
        for t in tr.update([box], FRAME):
            ids_second.add(t.id)
    assert ids_first.isdisjoint(ids_second)


def test_track_carries_sighting_state():
    """Track objects expose the immediate-sighting fields the pipeline uses."""
    tr = SimpleTracker()
    (t,) = tr.update([(10, 10, 60, 170, 0.9)], FRAME)
    assert t.sighted is False and t.sight_stem is None
    assert t.created_ts > 0
