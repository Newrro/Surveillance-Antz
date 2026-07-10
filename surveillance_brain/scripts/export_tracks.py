#!/usr/bin/env python3
"""
scripts/export_tracks.py — dump per-track Brain output for the MTMCT eval harness.

A "track" = one Part-1 detection_id (stable per person per camera pass). This
collapses the detection_events ledger to one row per track with the identity the
Brain finally assigned it, then writes:

  <out>/tracks.jsonl        machine input for tools/mtmct_eval/score.py
  <out>/labels_template.csv annotation sheet — open it, look at each snapshot, and
                            fill the `true_person` column (same name = same real
                            person; "ignore" to drop a bad/duplicate track).

Workflow:
  1. Record a fixed clip set and run the pipeline+Brain over it (so the same
     footage can be re-scored after each threshold/model change).
  2. python scripts/export_tracks.py --minutes 30 --out ../eval_run
  3. Label labels_template.csv → save as labels.csv
  4. python3 ../tools/mtmct_eval/score.py --tracks ../eval_run/tracks.jsonl \
                                          --labels ../eval_run/labels.csv

Usage:
  python scripts/export_tracks.py --out ../eval_run                # everything
  python scripts/export_tracks.py --minutes 30 --out ../eval_run   # last 30 min
  python scripts/export_tracks.py --since 2026-07-10T11:00:00 --until 2026-07-10T12:00:00 --out ../eval_run
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from db.connection import get_session  # noqa: E402
from sqlalchemy import text  # noqa: E402

STORAGE_ROOT = config.STORAGE_ROOT


def _full_scene(snapshot: str | None) -> str | None:
    """Derive the full-scene companion the pipeline saves beside the body crop."""
    if not snapshot or not snapshot.endswith(".jpg"):
        return None
    return snapshot[:-4] + "_full.jpg"


async def export(out_dir: str, since: datetime | None, until: datetime | None) -> int:
    where = ["detection_id IS NOT NULL"]
    params: dict = {}
    if since is not None:
        where.append("de.detected_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("de.detected_at <= :until")
        params["until"] = until
    sql = text(
        "SELECT de.detection_id, c.camera_uid, de.identity_id, i.display_label, "
        "       de.detected_at, de.snapshot_path, de.classification "
        "FROM detection_events de "
        "LEFT JOIN cameras c ON c.id = de.camera_id "
        "LEFT JOIN identities i ON i.id = de.identity_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY de.detection_id, de.detected_at"
    )
    async with get_session() as session:
        rows = (await session.execute(sql, params)).all()

    # aggregate per track
    tracks: dict[str, dict] = {}
    for det_id, cam, ident_id, label, at, snap, cls in rows:
        t = tracks.get(det_id)
        if t is None:
            t = tracks[det_id] = {
                "track_id": det_id, "camera": cam, "n_events": 0,
                "first_seen": at, "last_seen": at,
                "predicted_id": None, "predicted_label": None,
                "snapshot": None, "full_scene": None,
            }
        t["n_events"] += 1
        t["first_seen"] = min(t["first_seen"], at)
        t["last_seen"] = max(t["last_seen"], at)
        if ident_id is not None:                 # latest non-null identity wins
            t["predicted_id"] = ident_id
            t["predicted_label"] = label
        if t["predicted_label"] is None:
            t["predicted_label"] = str(cls.value if hasattr(cls, "value") else cls)
        if snap:
            t["snapshot"] = snap
            t["full_scene"] = _full_scene(snap)

    os.makedirs(out_dir, exist_ok=True)
    tracks_path = os.path.join(out_dir, "tracks.jsonl")
    labels_path = os.path.join(out_dir, "labels_template.csv")

    ordered = sorted(tracks.values(), key=lambda t: (t["first_seen"], t["camera"] or ""))
    with open(tracks_path, "w") as f:
        for t in ordered:
            o = dict(t)
            o["first_seen"] = t["first_seen"].isoformat()
            o["last_seen"] = t["last_seen"].isoformat()
            f.write(json.dumps(o) + "\n")

    with open(labels_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["track_id", "camera", "first_seen", "n_events",
                    "predicted_label", "snapshot", "full_scene", "true_person"])
        for t in ordered:
            w.writerow([t["track_id"], t["camera"] or "", t["first_seen"].isoformat(),
                        t["n_events"], t["predicted_label"] or "",
                        t["snapshot"] or "", t["full_scene"] or "", ""])

    print(f"[export_tracks] {len(ordered)} tracks from {len(rows)} events")
    print(f"  tracks   → {tracks_path}")
    print(f"  labels   → {labels_path}  (fill `true_person`, save as labels.csv)")
    if ordered:
        print(f"  snapshots are relative to repo root ({STORAGE_ROOT}/...); "
              f"open them to identify each track.")
    return len(ordered)


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../eval_run", help="output dir")
    ap.add_argument("--minutes", type=float, help="only tracks from the last N minutes")
    ap.add_argument("--since", help="ISO start (e.g. 2026-07-10T11:00:00)")
    ap.add_argument("--until", help="ISO end")
    args = ap.parse_args()

    since = until = None
    if args.minutes:
        since = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
    if args.since:
        since = _parse_dt(args.since)
    if args.until:
        until = _parse_dt(args.until)
    asyncio.run(export(args.out, since, until))


if __name__ == "__main__":
    main()
