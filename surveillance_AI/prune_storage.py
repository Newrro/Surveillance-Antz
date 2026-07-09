#!/usr/bin/env python3
"""
prune_storage.py — bound the on-disk size of storage/img/ (Part 1 snapshots).

Part 1 writes a person crop (and face crop) per track to storage/img/<cam>/.
On a 24/7 system that grows without limit, so this prunes it two ways:

  1. AGE   — delete snapshots older than RETENTION_DAYS (default 7).
  2. SIZE  — if the total still exceeds STORAGE_MAX_GB (default 5), delete the
             oldest files until it fits (a hard ceiling regardless of write rate).

Run once:      python prune_storage.py
Run as a loop: python prune_storage.py --loop        (prunes every PRUNE_INTERVAL_S, default 1h)

run.sh starts the --loop form alongside the Brain/UI/pipeline.
"""
import argparse
import os
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_IMG = os.path.join(REPO_ROOT, "storage", "img")

RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "7"))
STORAGE_MAX_GB = float(os.environ.get("STORAGE_MAX_GB", "5"))
PRUNE_INTERVAL_S = float(os.environ.get("PRUNE_INTERVAL_S", "3600"))


def _iter_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            yield fp, st.st_mtime, st.st_size


def prune_once():
    if not os.path.isdir(STORAGE_IMG):
        return 0, 0
    now = time.time()
    cutoff = now - RETENTION_DAYS * 86400
    max_bytes = int(STORAGE_MAX_GB * 1024**3)

    files = list(_iter_files(STORAGE_IMG))
    removed = 0
    freed = 0

    # 1) age-based pruning
    survivors = []
    for fp, mtime, size in files:
        if mtime < cutoff:
            try:
                os.remove(fp)
                removed += 1
                freed += size
            except OSError:
                pass
        else:
            survivors.append((fp, mtime, size))

    # 2) size-cap pruning — oldest first until under the ceiling
    total = sum(s for _, _, s in survivors)
    if total > max_bytes:
        for fp, _mtime, size in sorted(survivors, key=lambda x: x[1]):
            if total <= max_bytes:
                break
            try:
                os.remove(fp)
                removed += 1
                freed += size
                total -= size
            except OSError:
                pass

    # tidy now-empty camera dirs
    for dirpath, dirs, files_ in os.walk(STORAGE_IMG, topdown=False):
        if dirpath != STORAGE_IMG and not dirs and not files_:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    if removed:
        print(f"[prune] removed {removed} file(s), freed {freed/1024**2:.1f} MB "
              f"(keep {RETENTION_DAYS:g}d, cap {STORAGE_MAX_GB:g}GB)", flush=True)
    return removed, freed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true",
                    help=f"prune every PRUNE_INTERVAL_S ({PRUNE_INTERVAL_S:g}s) instead of once")
    args = ap.parse_args()
    if args.loop:
        print(f"[prune] loop every {PRUNE_INTERVAL_S:g}s "
              f"(keep {RETENTION_DAYS:g}d, cap {STORAGE_MAX_GB:g}GB) → {STORAGE_IMG}", flush=True)
        while True:
            try:
                prune_once()
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                print(f"[prune] error: {e}", flush=True)
            time.sleep(PRUNE_INTERVAL_S)
    else:
        prune_once()


if __name__ == "__main__":
    main()
