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
) -> Dict[str, Any]:
    """
    Register a new employee and store their embeddings in Qdrant.
    Returns {identity_id, label, name, department, email}.
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

    logger.info("Enrolled employee %s (%s) id=%d", name, label, identity_id)
    return {
        "identity_id": identity_id,
        "label": label,
        "name": name,
        "department": department,
        "email": email,
        "photo_path": photo_path,
    }


async def enroll_employee_from_images(
    name: str,
    department: str,
    images_b64: Sequence[str],
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Enroll an employee from uploaded face PHOTO(S). Decodes the base64 images,
    shells out to the AI venv's face extractor (the Brain has no ML model) to get
    AdaFace embeddings, enrolls with the best face, and stores the rest as extra
    gallery views. Raises ValueError if no clear face is found."""
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

    best = good[0]
    face_path = best.get("face_path")
    rel_photo = os.path.relpath(face_path, _REPO_ROOT) if face_path else None
    result = await enroll_employee(
        name=name, department=department,
        face_embedding=best["embedding"], email=email, photo_path=rel_photo,
    )
    for r in good[1:]:                                   # extra photos → extra views
        await embedding_repo.store_embeddings(
            result["identity_id"], face_embedding=r["embedding"], source="enroll_photo",
        )
    result["photos_used"] = len(good)
    logger.info("Enrolled employee %s (id=%d) from %d photo(s)",
                result["label"], result["identity_id"], len(good))
    return result


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
                "hired_at": e.hired_at.isoformat() if e.hired_at else None,
            })
    return out
