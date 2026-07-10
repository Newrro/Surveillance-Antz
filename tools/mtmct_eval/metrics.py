"""
tools/mtmct_eval/metrics.py — identity-clustering metrics for the MTMCT harness.

Pure stdlib (no numpy/scipy) so it runs anywhere with `python3`. The unit of
evaluation is a TRACK (one Part-1 detection_id, e.g. "PATHWAY-FRONT-t73"): the
Brain resolves identity once per track, so a track is the smallest thing that can
be "the same person" or "a different person". Each track carries:

    pred   — the predicted cluster key (identity_id, or a unique key per Unknown)
    gt     — the ground-truth person label a human assigned (None/"ignore" → skipped)
    weight — how much this track counts (default: its number of detection_events,
             a proxy for on-screen time — the standard IDF1 weighting)

We report the two failure modes that matter, separately, because they trade off:

  • FRAGMENTATION — one real person split across many predicted ids. High = the
    "same guy is 4 different Unknowns" problem. Ideal 1.0.
  • PURITY / OVER-MERGE — one predicted id covering several real people. Low purity
    = the uniform-clothing merge risk. Ideal 1.0.

  • IDF1 / IDP / IDR — the standard MOT identity F1 (Ristani et al. 2016): optimal
    1-to-1 matching between GT and predicted identities, then TP/FP/FN over weighted
    detections. One number that punishes BOTH failure modes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass
class Track:
    track_id: str
    pred: str                     # predicted cluster key
    gt: Optional[str]             # ground-truth person, or None to skip
    weight: float = 1.0


# ── optimal assignment (Hungarian / Kuhn-Munkres, O(n^3), minimization) ──────
def _hungarian_min(cost: List[List[float]]):
    """Minimum-cost perfect assignment on a (padded-to-square) matrix. Returns
    assignment[row] = col. Standard 1-indexed potentials implementation."""
    n = len(cost)
    m = len(cost[0]) if n else 0
    size = max(n, m, 1)
    a = [[cost[i][j] if i < n and j < m else 0.0 for j in range(size)] for i in range(size)]
    INF = float("inf")
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, size + 1):
                if not used[j]:
                    cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = [-1] * size
    for j in range(1, size + 1):
        if p[j] != 0:
            assignment[p[j] - 1] = j - 1
    return assignment


def _max_overlap_match(gt_ids, pred_ids, overlap):
    """Max total weighted overlap over 1-to-1 (gt ↔ pred) matchings."""
    if not gt_ids or not pred_ids:
        return 0.0
    cost = [[-overlap.get((g, p), 0.0) for p in pred_ids] for g in gt_ids]
    assign = _hungarian_min(cost)
    total = 0.0
    for gi, g in enumerate(gt_ids):
        pj = assign[gi]
        if 0 <= pj < len(pred_ids):
            total += overlap.get((g, pred_ids[pj]), 0.0)
    return total


# ── metrics ──────────────────────────────────────────────────────────────
def _labeled(tracks: Iterable[Track]) -> List[Track]:
    return [t for t in tracks if t.gt not in (None, "", "ignore")]


def idf1(tracks: Iterable[Track]) -> Dict[str, float]:
    """IDF1/IDP/IDR over weighted detections with optimal id matching."""
    ts = _labeled(tracks)
    if not ts:
        return {"idf1": 0.0, "idp": 0.0, "idr": 0.0, "idtp": 0.0, "idfp": 0.0, "idfn": 0.0}
    overlap = defaultdict(float)
    gt_w = defaultdict(float)
    pred_w = defaultdict(float)
    for t in ts:
        overlap[(t.gt, t.pred)] += t.weight
        gt_w[t.gt] += t.weight
        pred_w[t.pred] += t.weight
    idtp = _max_overlap_match(list(gt_w), list(pred_w), overlap)
    total_gt = sum(gt_w.values())
    total_pred = sum(pred_w.values())
    idfn = total_gt - idtp
    idfp = total_pred - idtp
    idp = idtp / total_pred if total_pred else 0.0
    idr = idtp / total_gt if total_gt else 0.0
    f1 = (2 * idtp / (2 * idtp + idfp + idfn)) if (2 * idtp + idfp + idfn) else 0.0
    return {"idf1": f1, "idp": idp, "idr": idr, "idtp": idtp, "idfp": idfp, "idfn": idfn}


def fragmentation(tracks: Iterable[Track]) -> Dict[str, float]:
    """Per real person, how many distinct predicted ids they're split across.
    weighted_avg weights each person by their total on-screen time (weight)."""
    ts = _labeled(tracks)
    by_gt: Dict[str, set] = defaultdict(set)
    gt_w: Dict[str, float] = defaultdict(float)
    for t in ts:
        by_gt[t.gt].add(t.pred)
        gt_w[t.gt] += t.weight
    if not by_gt:
        return {"mean": 0.0, "weighted": 0.0, "worst": 0.0, "n_persons": 0}
    counts = {g: len(preds) for g, preds in by_gt.items()}
    total_w = sum(gt_w.values()) or 1.0
    return {
        "mean": sum(counts.values()) / len(counts),
        "weighted": sum(counts[g] * gt_w[g] for g in counts) / total_w,
        "worst": max(counts.values()),
        "n_persons": len(counts),
    }


def purity(tracks: Iterable[Track]) -> Dict[str, float]:
    """Per predicted id, how many distinct real people it merges. <1 person means
    over-merge (the uniform-clothing risk). Reported as mean people-per-id and the
    fraction of predicted mass that is 'pure' (id maps to a single real person)."""
    ts = _labeled(tracks)
    by_pred: Dict[str, set] = defaultdict(set)
    pred_w: Dict[str, float] = defaultdict(float)
    pure_w = 0.0
    # weight of each (pred) that belongs to its majority gt counts as pure mass
    pred_gt_w: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in ts:
        by_pred[t.pred].add(t.gt)
        pred_w[t.pred] += t.weight
        pred_gt_w[t.pred][t.gt] += t.weight
    if not by_pred:
        return {"mean_people_per_id": 0.0, "pure_fraction": 0.0, "worst": 0.0, "n_ids": 0}
    for pred, gts in pred_gt_w.items():
        pure_w += max(gts.values())              # majority-person mass is "pure"
    counts = {p: len(g) for p, g in by_pred.items()}
    total_w = sum(pred_w.values()) or 1.0
    return {
        "mean_people_per_id": sum(counts.values()) / len(counts),
        "pure_fraction": pure_w / total_w,
        "worst": max(counts.values()),
        "n_ids": len(counts),
    }


def summarize(tracks: Iterable[Track]) -> Dict[str, object]:
    ts = list(tracks)
    lab = _labeled(ts)
    return {
        "n_tracks_total": len(ts),
        "n_tracks_scored": len(lab),
        "n_tracks_ignored": len(ts) - len(lab),
        "idf1": idf1(ts),
        "fragmentation": fragmentation(ts),
        "purity": purity(ts),
    }


# ── self-test: hand-verifiable cases ────────────────────────────────────────
def _selftest() -> None:
    def approx(a, b, tol=1e-9):
        assert abs(a - b) <= tol, f"{a} != {b}"

    # 1) perfect: one person, one id, weight 2 → IDF1 1.0, frag 1, purity 1
    perfect = [Track("t1", "id:1", "A", 1), Track("t2", "id:1", "A", 1)]
    r = summarize(perfect)
    approx(r["idf1"]["idf1"], 1.0)
    approx(r["fragmentation"]["weighted"], 1.0)
    approx(r["purity"]["pure_fraction"], 1.0)

    # 2) fragmentation: one person, two different ids → IDF1 0.5, frag 2, purity ok
    frag = [Track("t1", "id:1", "A", 1), Track("t2", "id:2", "A", 1)]
    r = summarize(frag)
    approx(r["idf1"]["idf1"], 0.5)
    approx(r["fragmentation"]["weighted"], 2.0)
    approx(r["purity"]["pure_fraction"], 1.0)         # no strangers merged
    approx(r["purity"]["mean_people_per_id"], 1.0)

    # 3) over-merge: two people, one id → IDF1 0.5, frag ok, purity 0.5 (BAD)
    merge = [Track("t1", "id:1", "A", 1), Track("t2", "id:1", "B", 1)]
    r = summarize(merge)
    approx(r["idf1"]["idf1"], 0.5)
    approx(r["fragmentation"]["weighted"], 1.0)       # neither person split
    approx(r["purity"]["pure_fraction"], 0.5)         # id is half-wrong
    approx(r["purity"]["mean_people_per_id"], 2.0)

    # 4) 'ignore' tracks are excluded
    ign = [Track("t1", "id:1", "A", 1), Track("t2", "unk:t2", "ignore", 5)]
    r = summarize(ign)
    assert r["n_tracks_scored"] == 1 and r["n_tracks_ignored"] == 1
    approx(r["idf1"]["idf1"], 1.0)

    # 5) Hungarian picks the optimal (not greedy) assignment
    #    GT A(w3) all in P1; GT B(w2) split P1(1)+P2(1). Greedy might match B→P1.
    mix = [Track("a1", "P1", "A", 3), Track("b1", "P1", "B", 1), Track("b2", "P2", "B", 1)]
    r = idf1(mix)
    # optimal: A↔P1 (overlap 3), B↔P2 (overlap 1) → IDTP=4; totals gt=5 pred=5
    approx(r["idtp"], 4.0)
    approx(r["idf1"], 2 * 4 / (2 * 4 + 1 + 1))

    print("metrics self-test: PASS (5 cases)")


if __name__ == "__main__":
    _selftest()
