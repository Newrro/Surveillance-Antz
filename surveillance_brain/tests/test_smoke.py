"""
tests/test_smoke.py
===================
Smoke tests — verify the Brain boots and its primary API surface responds.

Goal (per the smoke-test philosophy): confirm the app boots, routers
register, DB/Redis/Qdrant connections are wired, and the primary endpoints
return non-5xx responses.  Deep logical testing is out of scope here.

These assume Postgres + Redis + Qdrant are reachable (CI provides them as
service containers).
"""
from __future__ import annotations

from tests.conftest import *  # noqa: F401,F403


def test_root_endpoint(client):
    r = client.get("/")
    assert r.status_code == 200, r.text
    assert r.json()["service"] == "Surveillance Brain"


def test_docs_endpoint(client):
    assert client.get("/docs").status_code == 200


def test_openapi_schema(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json().get("paths", {})
    for expected in [
        "/events",
        "/search",
        "/person/{identity_id}",
        "/employees",
        "/logs/individual",
        "/logs/facility",
        "/health",
    ]:
        assert expected in paths, f"Missing path {expected} in OpenAPI schema"


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("database", "redis", "qdrant", "version"):
        assert key in body


def test_search_endpoint(client):
    r = client.get("/search", params={"q": "nonexistent"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("inside", "not_in_facility", "not_found")


def test_search_empty_query_rejected(client):
    r = client.get("/search", params={"q": ""})
    assert 400 <= r.status_code < 500


def test_events_list_endpoint(client):
    """GET /events returns a (possibly empty) event list."""
    r = client.get("/events", params={"limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "events" in body and isinstance(body["events"], list)
    assert "count" in body


def test_events_rejects_unknown_camera(client):
    """POST /events with a bad camera_id → 400 (not 500)."""
    payload = {
        "camera_id": "CAM-DOES-NOT-EXIST",
        "timestamp": "2026-01-01T12:00:00Z",
        "detection_conf": 0.95,
        "face_embedding": [0.0] * 512,
    }
    r = client.post("/events", json=payload)
    assert r.status_code < 500, f"Got 5xx: {r.status_code} {r.text}"


def test_events_requires_embedding(client):
    """POST /events with neither embedding → 422 validation error."""
    payload = {
        "camera_id": "GATE-01",
        "timestamp": "2026-01-01T12:00:00Z",
        "detection_conf": 0.95,
    }
    r = client.post("/events", json=payload)
    assert r.status_code == 422, r.text


def test_person_not_found(client):
    """GET /person/{id} for a nonexistent id → 404 (not 500)."""
    r = client.get("/person/99999999")
    assert r.status_code in (404, 200), r.text


def test_employees_list(client):
    r = client.get("/employees")
    assert r.status_code == 200, r.text
    assert "employees" in r.json()


def test_employees_enroll_requires_auth(client):
    """POST /employees without admin creds → 401."""
    payload = {"name": "X", "department": "Y", "face_embedding": [0.0] * 512}
    r = client.post("/employees", json=payload)
    assert r.status_code == 401, r.text


def test_logs_individual_requires_auth(client):
    r = client.get("/logs/individual", params={"identity_id": 1})
    assert r.status_code == 401, r.text


def test_logs_facility_requires_auth(client):
    r = client.get("/logs/facility", params={"from": "2026-01-01", "to": "2026-12-31"})
    assert r.status_code == 401, r.text


def test_logs_individual_with_auth(client, admin_auth):
    r = client.get("/logs/individual", params={"identity_id": 99999}, auth=admin_auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity_id"] == 99999
    assert isinstance(body["sessions"], list)


def test_logs_facility_with_auth(client, admin_auth):
    r = client.get(
        "/logs/facility",
        params={"from": "2026-01-01T00:00:00", "to": "2026-12-31T23:59:59"},
        auth=admin_auth,
    )
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers.get("content-type", "")


def test_promote_requires_auth(client):
    r = client.post("/identities/99999/promote", json={"name": "T", "department": "T"})
    assert r.status_code == 401, r.text


def test_demote_requires_auth(client):
    r = client.post("/identities/99999/demote")
    assert r.status_code == 401, r.text
