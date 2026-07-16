from __future__ import annotations

from pydantic import BaseModel


class _StorageSettings(BaseModel):
    # ---- Local storage (JSON/JSONL archive) -----------------------------
    # Postgres is the permanent source of truth; these files are the
    # human-readable export/archive each feature writes for offline use.
    STORAGE_ROOT: str = "storage"          # snapshots/clips referenced by Part 1 live here too
    LOG_DIR: str = "storage/logs"          # append-only events.jsonl
    DATASHEET_DIR: str = "storage/datasheet"  # per-person datasheet snapshots (JSON)
