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

import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# services/ -> surveillance_brain/ -> repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROFILES_DIR = os.path.join(_REPO_ROOT, "storage", "profiles")


def profile_rel(identity_id: Optional[int]) -> Optional[str]:
    """Repo-relative durable profile path ('storage/profiles/<id>.jpg') when it
    exists on disk, else None. The UI turns it into '/storage/...' via the bridge,
    exactly as it does for snapshot paths."""
    if identity_id is None:
        return None
    rel = f"storage/profiles/{identity_id}.jpg"
    return rel if os.path.exists(os.path.join(_REPO_ROOT, rel)) else None


def _abs(identity_id: int) -> str:
    return os.path.join(_PROFILES_DIR, f"{identity_id}.jpg")


def lock_path(identity_id: int) -> str:
    """Absolute path of the 'hands-off' marker for an identity's profile photo."""
    return os.path.join(_PROFILES_DIR, f"{identity_id}.lock")


def is_locked(identity_id: Optional[int]) -> bool:
    """True when a manual/enrolled profile photo is pinned for this identity — the
    pipeline must NOT overwrite it with a captured face. See
    surveillance_AI/snapshots.py:save_profile_photo, which honours the same marker."""
    if identity_id is None:
        return False
    return os.path.exists(lock_path(identity_id))


def _lock(identity_id: int) -> None:
    try:
        with open(lock_path(identity_id), "w") as f:
            f.write("manual")           # content is irrelevant — presence is the signal
    except OSError as e:
        logger.warning("could not write profile lock for id=%s: %s", identity_id, e)


def write_profile_from_bytes(identity_id: int, data: bytes) -> Optional[str]:
    """Write a MANUAL/enrolled profile photo to storage/profiles/<id>.jpg and pin it
    with a lock marker so the pipeline never overwrites it. Returns the repo-relative
    path, or None on failure. Best-effort; never raises."""
    if identity_id is None or not data:
        return None
    try:
        os.makedirs(_PROFILES_DIR, exist_ok=True)
        with open(_abs(identity_id), "wb") as f:
            f.write(data)
        _lock(identity_id)
        return f"storage/profiles/{identity_id}.jpg"
    except OSError as e:
        logger.warning("could not write profile photo for id=%s: %s", identity_id, e)
        return None


def delete_profile(identity_id: Optional[int]) -> None:
    """Remove an identity's durable profile photo AND its lock marker. Called when
    an identity is erased so no orphan avatar is left behind. Best-effort; never
    raises (a missing file is fine)."""
    if identity_id is None:
        return
    for p in (_abs(identity_id), lock_path(identity_id)):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("could not delete profile artefact %s: %s", p, e)


def write_profile_from_path(identity_id: int, src_abs: Optional[str]) -> Optional[str]:
    """Copy an already-saved image (e.g. the enrollment face crop) into the durable
    profile slot and pin it. Returns the repo-relative path, or None. Never raises."""
    if identity_id is None or not src_abs or not os.path.exists(src_abs):
        return None
    try:
        os.makedirs(_PROFILES_DIR, exist_ok=True)
        shutil.copyfile(src_abs, _abs(identity_id))
        _lock(identity_id)
        return f"storage/profiles/{identity_id}.jpg"
    except OSError as e:
        logger.warning("could not copy profile photo for id=%s: %s", identity_id, e)
        return None
