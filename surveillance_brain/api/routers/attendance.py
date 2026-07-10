"""
api/routers/attendance.py — daily attendance register (IDENTITY_REDESIGN.md Phase C).

GET /attendance?date=YYYY-MM-DD → who was on campus that day, at a glance:
  - employees: the full roster, each marked present/absent with first/last seen,
  - visitors: CONFIRMED visitors seen that day (provisional/unknown excluded),
  - unknown_count: distinct unidentified tracks that day (throwaway bucket).

Read-only, no auth (same as the other dashboard reads). Day boundaries use the DB
session's local date on detected_at.
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Query
from sqlalchemy import text

from db.connection import get_session

router = APIRouter(prefix="/attendance", tags=["attendance"])

# One aggregate row per identity seen on the day.
_SEEN_SQL = text("""
    SELECT de.identity_id,
           MIN(de.detected_at) AS first_seen,
           MAX(de.detected_at) AS last_seen,
           COUNT(*)            AS sightings,
           COUNT(DISTINCT de.camera_id) AS cameras
    FROM detection_events de
    WHERE de.identity_id IS NOT NULL AND de.detected_at::date = :d
    GROUP BY de.identity_id
""")

_UNKNOWN_SQL = text("""
    SELECT COUNT(DISTINCT COALESCE(detection_id, id::text)) AS n
    FROM detection_events
    WHERE detected_at::date = :d AND classification = 'unknown'
""")


@router.get("")
async def attendance(date: str | None = Query(None, description="YYYY-MM-DD; default today")):
    d = date_cls.fromisoformat(date) if date else date_cls.today()   # date object for asyncpg
    async with get_session() as session:
        seen_rows = (await session.execute(_SEEN_SQL, {"d": d})).mappings().all()
        seen = {r["identity_id"]: r for r in seen_rows}

        # Full employee roster (present iff seen today).
        emp_rows = (await session.execute(text(
            "SELECT e.identity_id, i.display_label, e.name, e.department "
            "FROM employees e JOIN identities i ON i.id = e.identity_id "
            "ORDER BY e.name"
        ))).mappings().all()
        employees = []
        for e in emp_rows:
            s = seen.get(e["identity_id"])
            employees.append({
                "identity_id": e["identity_id"], "label": e["display_label"],
                "name": e["name"], "department": e["department"],
                "present": s is not None,
                "first_seen": s["first_seen"].isoformat() if s else None,
                "last_seen": s["last_seen"].isoformat() if s else None,
                "sightings": s["sightings"] if s else 0,
                "cameras": s["cameras"] if s else 0,
            })

        # Confirmed visitors seen today.
        vis_rows = (await session.execute(text(
            "SELECT v.identity_id, i.display_label, v.name "
            "FROM visitors v JOIN identities i ON i.id = v.identity_id "
            "WHERE v.confirmed_at IS NOT NULL"
        ))).mappings().all()
        visitors = []
        for v in vis_rows:
            s = seen.get(v["identity_id"])
            if not s:
                continue
            visitors.append({
                "identity_id": v["identity_id"], "label": v["display_label"],
                "name": v["name"],
                "first_seen": s["first_seen"].isoformat(), "last_seen": s["last_seen"].isoformat(),
                "sightings": s["sightings"], "cameras": s["cameras"],
            })
        visitors.sort(key=lambda x: x["first_seen"])

        unknown_count = (await session.execute(_UNKNOWN_SQL, {"d": d})).scalar() or 0

    return {
        "date": d.isoformat(),
        "employees_present": sum(1 for e in employees if e["present"]),
        "employees_total": len(employees),
        "visitors_count": len(visitors),
        "unknown_count": int(unknown_count),
        "employees": employees,
        "visitors": visitors,
    }
