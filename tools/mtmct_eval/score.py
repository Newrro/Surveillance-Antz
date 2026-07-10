#!/usr/bin/env python3
"""
tools/mtmct_eval/score.py — score a Brain run against ground-truth labels.

Inputs (produced by scripts/export_tracks.py + a human):
  tracks.jsonl      one JSON object per track:
                    {"track_id","camera","n_events","predicted_id","predicted_label","snapshot"}
  labels.csv        annotation file: columns track_id,true_person  (blank/"ignore" → skipped)

Usage:
  python3 tools/mtmct_eval/score.py --tracks eval_run/tracks.jsonl --labels eval_run/labels.csv
  python3 tools/mtmct_eval/score.py --selftest      # verify the metrics engine

Report both the run's numbers AND how to read them, so a threshold change can be
compared run-to-run: IDF1 up is good; fragmentation toward 1.0 is good; purity
toward 1.0 is good — and a change that lifts IDF1 by MERGING people will show up
as purity dropping, which is exactly the uniform-clothing regression to catch.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import Track, summarize  # noqa: E402


def _load_tracks(path):
    rows = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            rows[o["track_id"]] = o
    return rows


def _load_labels(path):
    labels = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if "track_id" not in (reader.fieldnames or []):
            raise SystemExit(f"labels file {path} needs a 'track_id' column; got {reader.fieldnames}")
        # accept true_person / person / label as the ground-truth column
        gt_col = next((c for c in ("true_person", "person", "label") if c in reader.fieldnames), None)
        if gt_col is None:
            raise SystemExit(f"labels file {path} needs a 'true_person' column; got {reader.fieldnames}")
        for row in reader:
            labels[row["track_id"].strip()] = (row[gt_col] or "").strip()
    return labels


def build_tracks(track_rows, labels):
    """Join exported tracks with human labels → Track list. A predicted_id of null
    (an ephemeral Unknown) becomes a unique singleton cluster per track, so it
    can't accidentally match another Unknown."""
    out = []
    unlabeled = 0
    for tid, o in track_rows.items():
        gt = labels.get(tid, "")
        if gt == "":
            unlabeled += 1
        pid = o.get("predicted_id")
        pred = f"id:{pid}" if pid not in (None, "") else f"unk:{tid}"
        out.append(Track(track_id=tid, pred=pred, gt=(gt or None),
                         weight=float(o.get("n_events", 1) or 1)))
    return out, unlabeled


def _fmt(report):
    idf = report["idf1"]
    fr = report["fragmentation"]
    pu = report["purity"]
    L = []
    L.append("── MTMCT evaluation ─────────────────────────────────────────")
    L.append(f"tracks: {report['n_tracks_scored']} scored, "
             f"{report['n_tracks_ignored']} ignored, {report['n_tracks_total']} total")
    L.append("")
    L.append(f"  IDF1            {idf['idf1']*100:5.1f}%   (IDP {idf['idp']*100:.1f} / IDR {idf['idr']*100:.1f})")
    L.append(f"                  IDTP {idf['idtp']:.0f}  IDFP {idf['idfp']:.0f}  IDFN {idf['idfn']:.0f}")
    L.append("")
    L.append(f"  fragmentation   {fr['weighted']:.2f} ids/person  (mean {fr['mean']:.2f}, "
             f"worst {fr['worst']:.0f}, over {fr['n_persons']} people)   ← 1.00 ideal")
    L.append(f"  purity          {pu['pure_fraction']*100:5.1f}% pure   "
             f"({pu['mean_people_per_id']:.2f} people/id, worst {pu['worst']:.0f}, "
             f"over {pu['n_ids']} ids)   ← 100% ideal")
    L.append("─────────────────────────────────────────────────────────────")
    L.append("  fragmentation ↑ = one person split into many (the Unknown-flood).")
    L.append("  purity ↓        = strangers merged into one id (the uniform risk).")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", help="tracks.jsonl from export_tracks.py")
    ap.add_argument("--labels", help="labels.csv (track_id,true_person)")
    ap.add_argument("--json", help="write the full report to this JSON path")
    ap.add_argument("--selftest", action="store_true", help="verify the metrics engine and exit")
    args = ap.parse_args()

    if args.selftest:
        from metrics import _selftest
        _selftest()
        return
    if not args.tracks or not args.labels:
        ap.error("--tracks and --labels are required (or use --selftest)")

    track_rows = _load_tracks(args.tracks)
    labels = _load_labels(args.labels)
    tracks, unlabeled = build_tracks(track_rows, labels)
    report = summarize(tracks)

    print(_fmt(report))
    if unlabeled:
        print(f"\n  note: {unlabeled} exported track(s) had no label row and were skipped.")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  full report → {args.json}")


if __name__ == "__main__":
    main()
