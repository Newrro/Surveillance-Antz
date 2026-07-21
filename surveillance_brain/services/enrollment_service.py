"""
services/enrollment_service.py
==============================
Employee enrollment (backs POST /employees) + listing (GET /employees).

Boundary note:
    The Brain does NOT run any ML model.  Face/body embedding EXTRACTION is
    Part 1's responsibility.  So enrollment accepts an already-computed
    `face_embedding` (and optional `body_embedding`) in the request body —
    the UI uploads a photo to Part 1's extractor, gets the embedding back,
    then calls this endpoint.  A `photo_path` is stored for display only.

Creating an employee here mints a brand-new identity (EMP-YYYY-NNNN).  To
convert an existing visitor who is already in the system, use
conversion_service.promote_visitor_to_employee instead — that preserves
the person's movement history.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from db.connection import get_session
from db.models import IdentityType
from repositories import embedding_repo, identity_repo
from services import media_paths

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AI_PY = os.path.join(_REPO_ROOT, "surveillance_AI", "venv", "bin", "python")
_ENROLL_SCRIPT = os.path.join(_REPO_ROOT, "surveillance_AI", "enroll_face.py")


async def enroll_employee(
    name: str,
    department: str,
    face_embedding: Sequence[float],
    email: Optional[str] = None,
    body_embedding: Optional[Sequence[float]] = None,
    photo_path: Optional[str] = None,
    external_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Register a new employee and store their embeddings in Qdrant.
    Returns {identity_id, label, name, department, email, photo_path}.
    """
    async with get_session() as session:
        year = datetime.utcnow().year
        seq = await identity_repo.next_employee_seq(session, year)
        label = f"EMP-{year}-{seq:04d}"

        identity = await identity_repo.create_identity(session, IdentityType.EMPLOYEE, label)
        await identity_repo.insert_employee(
            session,
            identity_id=identity.id,
            employee_seq=seq,
            year=year,
            name=name,
            department=department,
            email=email,
            external_id=external_id,
        )
        # Store embeddings AFTER the identity row exists so the Qdrant
        # payload's identity_id is valid.  (Qdrant write happens inside the
        # DB transaction window; if the commit later fails the vectors are
        # orphaned but harmless — they only match a non-existent id and the
        # resolver defensively re-creates.)
        await embedding_repo.store_embeddings(
            identity.id,
            face_embedding=face_embedding,
            body_embedding=body_embedding,
            source="enroll",
        )
        identity_id = identity.id

    # Pin the uploaded photo as the durable, LOCKED profile avatar (storage/
    # profiles/<id>.jpg). This is the fixed thumbnail the Report shows and the
    # pipeline is forbidden from overwriting with a captured face.
    durable = None
    if photo_path:
        src_abs = photo_path if os.path.isabs(photo_path) else os.path.join(_REPO_ROOT, photo_path)
        durable = media_paths.write_profile_from_path(identity_id, src_abs)

    logger.info("Enrolled employee %s (%s) id=%d", name, label, identity_id)
    return {
        "identity_id": identity_id,
        "label": label,
        "name": name,
        "department": department,
        "email": email,
        "external_id": external_id,
        "photo_path": durable or photo_path,
    }


async def _embed_images_b64(images_b64: Sequence[str]) -> List[Dict[str, Any]]:
    """Decode base64 photos, shell out to the AI venv's SCRFD+AdaFace extractor
    (the Brain runs no ML model), and return the successful face results sorted
    best-first. Each result has {embedding, quality, face_path, ...}. Raises
    ValueError if no clear face is found in any image."""
    if not images_b64:
        raise ValueError("no images provided")
    stamp = int(time.time() * 1000)
    outdir = os.path.join(_REPO_ROOT, "storage", "enroll", str(stamp))
    os.makedirs(outdir, exist_ok=True)

    paths: List[str] = []
    for i, b64 in enumerate(images_b64):
        raw = base64.b64decode(b64.split(",", 1)[-1])       # tolerate data: URL prefix
        p = os.path.join(outdir, f"{i}.jpg")
        with open(p, "wb") as f:
            f.write(raw)
        paths.append(p)

    proc = await asyncio.create_subprocess_exec(
        _AI_PY, _ENROLL_SCRIPT, *paths, "--face-out", outdir,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    try:
        line = out.decode().strip().splitlines()[-1]
        results = json.loads(line)["results"]
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"face embedding failed: {(err.decode()[:300] or str(e))}")

    good = [r for r in results if r.get("ok")]
    if not good:
        errs = "; ".join(r.get("error", "") for r in results) or "no face detected"
        raise ValueError(f"No clear face found in the photo(s): {errs}")
    good.sort(key=lambda r: r.get("quality", 0.0), reverse=True)   # best face leads
    return good


async def enroll_employee_from_images(
    name: str,
    department: str,
    images_b64: Sequence[str],
    email: Optional[str] = None,
    external_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Enroll an employee from uploaded face PHOTO(S). Enrolls with the best face
    and stores the rest as extra gallery views. Raises ValueError if no clear face."""
    good = await _embed_images_b64(images_b64)
    best = good[0]
    face_path = best.get("face_path")
    rel_photo = os.path.relpath(face_path, _REPO_ROOT) if face_path else None
    result = await enroll_employee(
        name=name, department=department,
        face_embedding=best["embedding"], email=email, photo_path=rel_photo,
        external_id=external_id,
    )
    for r in good[1:]:                                   # extra photos → extra views
        await embedding_repo.store_embeddings(
            result["identity_id"], face_embedding=r["embedding"], source="enroll_photo",
        )
    result["photos_used"] = len(good)
    logger.info("Enrolled employee %s (id=%d) from %d photo(s)",
                result["label"], result["identity_id"], len(good))
    return result


async def import_employee_from_images(
    external_id: str,
    name: str,
    department: str,
    images_b64: Sequence[str],
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Bulk-import path — CREATE or UPDATE an employee keyed by external_id (the
    idempotency key). New id → enroll fresh. Existing id → append the new photos'
    embeddings as extra gallery views, refresh name/department/email, and update the
    durable locked profile from the best new face. Raises ValueError if no clear face.
    Returns {identity_id, action: 'created'|'updated', external_id, name, photos_used}."""
    good = await _embed_images_b64(images_b64)
    best = good[0]
    face_path = best.get("face_path")

    existing = None
    if external_id:
        async with get_session() as session:
            existing = await identity_repo.fetch_employee_by_external_id(session, external_id)

    if existing is None:
        rel_photo = os.path.relpath(face_path, _REPO_ROOT) if face_path else None
        created = await enroll_employee(
            name=name, department=department, face_embedding=best["embedding"],
            email=email, photo_path=rel_photo, external_id=external_id,
        )
        identity_id, action = created["identity_id"], "created"
        extras = good[1:]
    else:
        identity_id, action = existing.identity_id, "updated"
        # Add the best new face too (existing already has prior embeddings).
        await embedding_repo.store_embeddings(identity_id, face_embedding=best["embedding"], source="import")
        await update_employee(identity_id, name=name, department=department, email=email)
        media_paths.write_profile_from_path(identity_id, face_path)   # refresh locked avatar
        extras = good[1:]

    for r in extras:                                     # remaining photos → extra views
        await embedding_repo.store_embeddings(identity_id, face_embedding=r["embedding"], source="import")

    logger.info("Imported (%s) employee %s [%s] id=%d from %d photo(s)",
                action, name, external_id, identity_id, len(good))
    return {"identity_id": identity_id, "action": action, "external_id": external_id,
            "name": name, "photos_used": len(good)}


async def list_employees(limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
    """Return all employees for GET /employees."""
    async with get_session() as session:
        employees = await identity_repo.list_employees(session, limit=limit, offset=offset)
        # Grab the labels in the same session.
        out: List[Dict[str, Any]] = []
        for e in employees:
            identity = await identity_repo.fetch_identity_by_id(session, e.identity_id)
            out.append({
                "identity_id": e.identity_id,
                "label": identity.display_label if identity else None,
                "name": e.name,
                "department": e.department,
                "email": e.email,
                "external_id": e.external_id,
                # Durable uploaded avatar (storage/profiles/<id>.jpg) if it exists —
                # the fixed thumbnail the UI shows for this employee.
                "photo_path": media_paths.profile_rel(e.identity_id),
                "hired_at": e.hired_at.isoformat() if e.hired_at else None,
            })
    return out


async def update_employee(
    identity_id: int,
    *,
    name: Optional[str] = None,
    department: Optional[str] = None,
    external_id: Optional[str] = None,
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit an employee's profile fields. Returns the updated record (same shape as
    list_employees rows). Raises ValueError if the identity isn't an employee or the
    external_id clashes."""
    async with get_session() as session:
        emp = await identity_repo.update_employee(
            session, identity_id,
            name=name, department=department, external_id=external_id, email=email,
        )
        if emp is None:
            raise ValueError(f"identity {identity_id} is not an employee")
        identity = await identity_repo.fetch_identity_by_id(session, identity_id)
        label = identity.display_label if identity else None
    logger.info("Updated employee id=%d (%s)", identity_id, label)
    return {
        "identity_id": identity_id,
        "label": label,
        "name": emp.name,
        "department": emp.department,
        "email": emp.email,
        "external_id": emp.external_id,
        "photo_path": media_paths.profile_rel(identity_id),
    }
