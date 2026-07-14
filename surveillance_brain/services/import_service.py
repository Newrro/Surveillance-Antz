"""
services/import_service.py
==========================
Bulk employee roster import: XLSX / CSV, or a ZIP bundle (roster + photos).

Roster columns (header names, case-insensitive):
    external_id   REQUIRED — the idempotency key. Re-importing the same
                  external_id UPDATES that employee (name/department/email,
                  plus any new photos) instead of duplicating them.
    name          REQUIRED
    department    optional (default "General")
    email         optional
    photo         optional — filename(s) of photo(s) inside the ZIP,
                  ';'-separated. Additionally, any ZIP member named
                  `{external_id}.jpg` / `{external_id}_<n>.jpg` (or .png)
                  is auto-matched to that employee.

Every import runs a full VALIDATION pass first; `dry_run=True` returns the
row-level preview (valid / error per row) without writing anything. Photos
are embedded through the AI venv's face extractor (the Brain runs no ML);
photo failures degrade to a row warning — the roster row still applies.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
import re
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.connection import get_session
from db.models import IdentityType
from repositories import audit_repo, identity_repo
from services import enrollment_service

logger = logging.getLogger(__name__)

_PHOTO_EXT = (".jpg", ".jpeg", ".png")
_REQUIRED = ("external_id", "name")

# In-memory record of the last completed imports (job_id → summary). Imports
# run synchronously; this exists so GET /employees/import/{job_id} can re-fetch
# a result the UI lost (page reload) during this process lifetime.
_JOBS: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (h or "").strip().lower().replace(" ", "_"))


def _rows_from_csv(data: bytes) -> List[Dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [{_norm_header(k): (v or "").strip() for k, v in row.items() if k}
            for row in reader]


def _rows_from_xlsx(data: bytes) -> List[Dict[str, str]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        headers = [_norm_header(str(h)) if h is not None else "" for h in next(rows_iter)]
    except StopIteration:
        return []
    out = []
    for values in rows_iter:
        if values is None or all(v is None for v in values):
            continue
        row = {}
        for h, v in zip(headers, values):
            if h:
                row[h] = str(v).strip() if v is not None else ""
        out.append(row)
    return out


def parse_bundle(filename: str, content: bytes) -> tuple[List[Dict[str, str]], Dict[str, bytes]]:
    """Returns (roster_rows, photos{filename_lower: bytes})."""
    lower = filename.lower()
    if lower.endswith(".csv"):
        return _rows_from_csv(content), {}
    if lower.endswith(".xlsx"):
        return _rows_from_xlsx(content), {}
    if lower.endswith(".zip"):
        rows: List[Dict[str, str]] = []
        photos: Dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            roster_names = [n for n in names if n.lower().endswith((".csv", ".xlsx"))]
            if not roster_names:
                raise ValueError("ZIP contains no roster (.csv or .xlsx)")
            roster_name = sorted(roster_names)[0]
            data = zf.read(roster_name)
            rows = (_rows_from_csv(data) if roster_name.lower().endswith(".csv")
                    else _rows_from_xlsx(data))
            for n in names:
                if n.lower().endswith(_PHOTO_EXT):
                    base = n.rsplit("/", 1)[-1].lower()
                    photos[base] = zf.read(n)
        return rows, photos
    raise ValueError(f"unsupported file type: {filename!r} (need .csv, .xlsx or .zip)")


def _photos_for(row: Dict[str, str], photos: Dict[str, bytes]) -> List[bytes]:
    """Photos referenced by the row's `photo` column + auto-matched by external_id."""
    out: List[bytes] = []
    seen: set[str] = set()
    for ref in (row.get("photo") or "").split(";"):
        ref = ref.strip().lower()
        if ref and ref in photos and ref not in seen:
            out.append(photos[ref]); seen.add(ref)
    ext_id = (row.get("external_id") or "").strip().lower()
    if ext_id:
        pat = re.compile(re.escape(ext_id) + r"(_\d+)?\.(jpg|jpeg|png)$")
        for name, data in photos.items():
            if name not in seen and pat.fullmatch(name):
                out.append(data); seen.add(name)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_rows(rows: List[Dict[str, str]], photos: Dict[str, bytes]) -> List[Dict[str, Any]]:
    """Row-level validation preview: [{row, external_id, name, ..., photos, errors}]."""
    seen_ids: set[str] = set()
    report: List[Dict[str, Any]] = []
    for i, row in enumerate(rows, start=2):          # +2 = 1-based + header line
        errors: List[str] = []
        ext = (row.get("external_id") or "").strip()
        name = (row.get("name") or "").strip()
        for field in _REQUIRED:
            if not (row.get(field) or "").strip():
                errors.append(f"missing {field}")
        if ext:
            if len(ext) > 64:
                errors.append("external_id longer than 64 chars")
            if ext.lower() in seen_ids:
                errors.append(f"duplicate external_id {ext!r} in the file")
            seen_ids.add(ext.lower())
        email = (row.get("email") or "").strip()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append(f"invalid email {email!r}")
        n_photos = len(_photos_for(row, photos))
        report.append({
            "row": i, "external_id": ext, "name": name,
            "department": (row.get("department") or "").strip() or "General",
            "email": email or None, "photos": n_photos, "errors": errors,
        })
    return report


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
async def _upsert_row(entry: Dict[str, Any], photo_blobs: List[bytes]) -> Dict[str, Any]:
    """Create or update ONE employee by external_id. Returns the outcome dict."""
    warnings: List[str] = []
    async with get_session() as session:
        existing = await identity_repo.fetch_employee_by_external_id(
            session, entry["external_id"])
        if existing is not None:
            existing.name = entry["name"]
            existing.department = entry["department"]
            existing.email = entry["email"]
            identity_id = existing.identity_id
            outcome = "updated"
            await session.commit()
        else:
            year = datetime.utcnow().year
            seq = await identity_repo.next_employee_seq(session, year)
            label = f"EMP-{year}-{seq:04d}"
            identity = await identity_repo.create_identity(
                session, IdentityType.EMPLOYEE, label)
            await identity_repo.insert_employee(
                session, identity_id=identity.id, employee_seq=seq, year=year,
                name=entry["name"], department=entry["department"],
                email=entry["email"], external_id=entry["external_id"],
            )
            identity_id = identity.id
            outcome = "created"
            await session.commit()

    # Photos AFTER the row exists — a photo failure must not lose the roster row.
    photos_added = 0
    if photo_blobs:
        b64 = [base64.b64encode(b).decode() for b in photo_blobs]
        try:
            photos_added = await enrollment_service.add_employee_photos(identity_id, b64)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"photo enrollment failed: {e}")
    return {"identity_id": identity_id, "outcome": outcome,
            "photos_added": photos_added, "warnings": warnings}


async def run_import(
    filename: str,
    content_b64: str,
    dry_run: bool,
    actor: str,
) -> Dict[str, Any]:
    """Parse → validate → (optionally) apply. Returns the full summary."""
    try:
        content = base64.b64decode(content_b64)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"invalid base64 payload: {e}")
    rows, photos = parse_bundle(filename, content)
    if not rows:
        raise ValueError("roster contains no data rows")
    report = validate_rows(rows, photos)
    valid = [r for r in report if not r["errors"]]
    job_id = f"imp-{int(datetime.utcnow().timestamp() * 1000)}"
    summary: Dict[str, Any] = {
        "job_id": job_id, "filename": filename, "dry_run": dry_run,
        "total_rows": len(report), "valid_rows": len(valid),
        "error_rows": len(report) - len(valid),
        "rows": report, "created": 0, "updated": 0, "photos_added": 0,
    }
    if not dry_run:
        for entry, src_row in ((r, rows[r["row"] - 2]) for r in valid):
            blobs = _photos_for(src_row, photos)
            result = await _upsert_row(entry, blobs)
            entry["outcome"] = result["outcome"]
            entry["identity_id"] = result["identity_id"]
            entry["photos_added"] = result["photos_added"]
            if result["warnings"]:
                entry["errors"] = result["warnings"]      # surfaced as warnings
            summary[result["outcome"]] = summary.get(result["outcome"], 0) + 1
            summary["photos_added"] += result["photos_added"]
        async with get_session() as session:
            await audit_repo.record(
                session, actor=actor, action="import",
                subject_type="employees", subject_id=filename,
                details={k: summary[k] for k in
                         ("total_rows", "valid_rows", "error_rows",
                          "created", "updated", "photos_added")},
            )
            await session.commit()
    _JOBS[job_id] = summary
    logger.info("Employee import %s by %s: %d rows (%d valid) dry_run=%s created=%d updated=%d",
                filename, actor, summary["total_rows"], summary["valid_rows"],
                dry_run, summary["created"], summary["updated"])
    return summary


def job_status(job_id: str) -> Optional[Dict[str, Any]]:
    return _JOBS.get(job_id)
