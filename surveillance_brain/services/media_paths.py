"""
services/media_paths.py — durable per-identity profile photo path (Tier A).

The pipeline writes one durable photo per identity to storage/profiles/<id>.jpg
(outside storage/img, so the media pruner never deletes it). This helper returns
that repo-relative path IF the file exists, so the UI can prefer it over a
per-sighting snapshot (which may have aged out) and never show a blank face.

The Brain runs from surveillance_brain/, but the shared storage/ tree lives at
the REPO ROOT (where the pipeline writes and the UI bridge serves /storage from),
so we resolve existence against the repo root, not the Brain's cwd.
"""
from __future__ import annotations

import os
from typing import Optional

# services/ -> surveillance_brain/ -> repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def profile_rel(identity_id: Optional[int]) -> Optional[str]:
    """Repo-relative durable profile path ('storage/profiles/<id>.jpg') when it
    exists on disk, else None. The UI turns it into '/storage/...' via the bridge,
    exactly as it does for snapshot paths."""
    if identity_id is None:
        return None
    rel = f"storage/profiles/{identity_id}.jpg"
    return rel if os.path.exists(os.path.join(_REPO_ROOT, rel)) else None
