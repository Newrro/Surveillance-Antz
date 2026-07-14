"""
tests/test_sightings_rework.py
==============================
Acceptance tests for the 2026-07 sighting/evidence rework:

  1.  A person with NO face creates ONE persistent Unknown case, with the
      body + full-frame evidence paths stored verbatim.
  2.  The same track (track_uuid) never creates a second case; a fragmented
      same-camera track re-joins its case ONLY via the constrained body link.
  3.  A high body match to a person on a DIFFERENT camera does NOT link
      (uniforms must never identify anyone across cameras).
  4.  The event feed and person profile expose the EXPLICIT evidence paths
      (face/body/full_frame/...) of exactly one sighting per row.
  5.  Hide (soft delete) removes a single sighting from every feed without
      touching the rest; unhide restores it.
  6.  Reassign moves chosen sightings; split-case detaches them to a new case.
  7.  Manual merge works for ALL type combinations; an employee can only
      survive as primary; merges are audited and unmerge restores the events.
  8.  Roster import: dry-run previews row errors; apply is idempotent on
      external_id; ZIP bundles parse rosters + photos.
  9.  Legacy rows (snapshot_path only) stay readable after the migration.

Tests run against the configured DB (skip when unreachable) and clean up
whatever they create.
"""
from __future__ import annotations

import base64
import io
import time
import uuid
import zipfile

import pytest


def _skip_unless_up(client):
    try:
        r = client.get("/health")
        if r.status_code != 200 or r.json().get("database") != "ok":
            pytest.skip("Brain dependencies not available")
    except Exception:
        pytest.skip("Brain app not reachable")


def _vec(seed: float, dims: int = 512):
    """Deterministic unit vector; different seeds → (near-)orthogonal."""
    import math
    v = [math.sin(seed * (i + 1) * 0.7) for i in range(dims)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


_CAM_NS = uuid.uuid4().hex[:6]     # per-run camera namespace: tests must not
                                    # be "co-present" with each other's events


def _obs(track, cam=None, conf=0.9, body=None, face=None, **media):
    if cam is None:
        cam = f"TESTCAM-{_CAM_NS}"
    payload = {
        "camera_id": cam,
        "timestamp": "2026-07-14T12:00:00Z",
        "detection_conf": conf,
        "detection_id": track,
        "track_uuid": track,
        "bbox": [10.0, 20.0, 110.0, 320.0],
        "frame_w": 2560, "frame_h": 1440,
    }
    if body is not None:
        payload["body_embedding"] = body
    if face is not None:
        payload["face_embedding"] = face
    payload.update(media)
    return payload


def _cleanup(client, admin_auth, identity_ids):
    ids = [i for i in identity_ids if i is not None]
    if ids:
        client.post("/identities/delete", auth=admin_auth, json={"identity_ids": ids})


# ---------------------------------------------------------------------------
# 1+4. Faceless person → ONE Unknown case with stored evidence
# ---------------------------------------------------------------------------
def test_faceless_observation_creates_case_with_evidence(client, admin_auth):
    _skip_unless_up(client)
    track = f"T-{uuid.uuid4().hex[:10]}"
    r = client.post("/events", json=_obs(
        track,
        body_path=f"storage/img/GATE-01/{track}_body.jpg",
        full_frame_path=f"storage/img/GATE-01/{track}_orig.jpg",
        full_frame_annotated_path=f"storage/img/GATE-01/{track}_annot.jpg",
    ))
    assert r.status_code == 200, r.text
    e = r.json()
    try:
        assert e["identity_id"] is not None, "faceless person must anchor to a case"
        assert (e["person_id"] or "").startswith("UNK-")
        # explicit evidence paths echoed verbatim — never derived
        assert e["body"] == f"storage/img/GATE-01/{track}_body.jpg"
        assert e["full_frame"] == f"storage/img/GATE-01/{track}_orig.jpg"
        assert e["face"] is None, "no face was captured — none may be invented"
        assert e["bbox"] == [10.0, 20.0, 110.0, 320.0]
        assert e["frame_w"] == 2560

        # the sighting appears in the feed with the same explicit paths
        feed = client.get(f"/events?limit=50&camera=TESTCAM-{_CAM_NS}").json()["events"]
        mine = [x for x in feed if x.get("track_uuid") == track]
        assert mine, "sighting missing from the feed"
        assert mine[0]["body"] == e["body"]
        assert mine[0]["full_frame"] == e["full_frame"]
        assert mine[0]["face"] is None
    finally:
        _cleanup(client, admin_auth, [e.get("identity_id")])


# ---------------------------------------------------------------------------
# 2. Track continuity + constrained body link group fragments into ONE case
# ---------------------------------------------------------------------------
def test_same_track_reuses_case_and_body_link_joins_fragments(client, admin_auth):
    _skip_unless_up(client)
    body = _vec(1.23)
    t1 = f"T-{uuid.uuid4().hex[:10]}"
    e1 = client.post("/events", json=_obs(t1, body=body)).json()
    ids = [e1.get("identity_id")]
    try:
        # (a) SAME track again → same case, no second identity
        e1b = client.post("/events", json=_obs(t1, body=body)).json()
        assert e1b["identity_id"] == e1["identity_id"]

        # (b) NEW track, SAME camera, seconds later, near-identical body
        #     → constrained body link reconnects to the SAME case
        t2 = f"T-{uuid.uuid4().hex[:10]}"
        e2 = client.post("/events", json=_obs(t2, body=body)).json()
        ids.append(e2.get("identity_id"))
        assert e2["identity_id"] == e1["identity_id"], \
            "fragmented same-camera track must re-join its case via body link"
        assert e2["matched_by"] == "body"
        assert e2["similarity"] is not None and e2["similarity"] >= 0.75, \
            "body links must be scored (auditable)"

        # (c) NEW track, same camera, DIFFERENT body → its own case
        t3 = f"T-{uuid.uuid4().hex[:10]}"
        e3 = client.post("/events", json=_obs(t3, body=_vec(9.87))).json()
        ids.append(e3.get("identity_id"))
        assert e3["identity_id"] != e1["identity_id"], \
            "a dissimilar body must NOT be pulled into the case"
    finally:
        _cleanup(client, admin_auth, set(ids))


# ---------------------------------------------------------------------------
# 3. Uniform guard: body similarity NEVER identifies across cameras
# ---------------------------------------------------------------------------
def test_body_similarity_never_links_across_cameras(client, admin_auth):
    _skip_unless_up(client)
    body = _vec(4.56)
    t1 = f"T-{uuid.uuid4().hex[:10]}"
    e1 = client.post("/events", json=_obs(t1, cam=f"TC1-{_CAM_NS}", body=body)).json()
    ids = [e1.get("identity_id")]
    try:
        t2 = f"T-{uuid.uuid4().hex[:10]}"
        e2 = client.post("/events", json=_obs(t2, cam=f"TC2-{_CAM_NS}", body=body)).json()
        ids.append(e2.get("identity_id"))
        assert e2["identity_id"] != e1["identity_id"], \
            "identical uniforms on DIFFERENT cameras must stay separate people"
    finally:
        _cleanup(client, admin_auth, set(ids))


# ---------------------------------------------------------------------------
# 5. Soft hide: one bad sighting leaves the feeds; nothing else is touched
# ---------------------------------------------------------------------------
def test_hide_one_sighting_keeps_history(client, admin_auth):
    _skip_unless_up(client)
    body = _vec(7.77)
    t = f"T-{uuid.uuid4().hex[:10]}"
    # Payloads with media are ALWAYS inserted (one evidence set per sighting);
    # media-less same-track repeats are dedup-suppressed — so send media, as
    # the real pipeline always does.
    e1 = client.post("/events", json=_obs(t, body=body, body_path=f"s/{t}_1.jpg")).json()
    e2 = client.post("/events", json=_obs(t, body=body, body_path=f"s/{t}_2.jpg")).json()
    iid = e1["identity_id"]
    try:
        assert e1["event_id"] and e2["event_id"] and e1["event_id"] != e2["event_id"]
        r = client.post("/events/hide", auth=admin_auth,
                        json={"event_ids": [e1["event_id"]], "reason": "test bad capture"})
        assert r.status_code == 200 and r.json()["hidden"] == 1

        prof = client.get(f"/person/{iid}").json()
        hist_ids = [h["event_id"] for h in prof["history"]]
        assert e1["event_id"] not in hist_ids, "hidden sighting must leave the profile"
        assert e2["event_id"] in hist_ids, "the OTHER sighting must survive"

        r = client.post("/events/unhide", auth=admin_auth,
                        json={"event_ids": [e1["event_id"]]})
        assert r.status_code == 200 and r.json()["restored"] == 1
        prof = client.get(f"/person/{iid}").json()
        assert e1["event_id"] in [h["event_id"] for h in prof["history"]]
    finally:
        _cleanup(client, admin_auth, [iid])


# ---------------------------------------------------------------------------
# 6. Reassign + split-case fix wrong associations without deleting anything
# ---------------------------------------------------------------------------
def test_reassign_and_split_case(client, admin_auth):
    _skip_unless_up(client)
    a = client.post("/events", json=_obs(f"T-{uuid.uuid4().hex[:10]}", body=_vec(2.2))).json()
    b = client.post("/events", json=_obs(f"T-{uuid.uuid4().hex[:10]}", body=_vec(3.3))).json()
    ids = [a["identity_id"], b["identity_id"]]
    try:
        # move A's sighting onto B
        r = client.post("/events/reassign", auth=admin_auth,
                        json={"event_ids": [a["event_id"]], "target_identity_id": b["identity_id"]})
        assert r.status_code == 200 and r.json()["moved"] == 1
        prof_b = client.get(f"/person/{b['identity_id']}").json()
        assert a["event_id"] in [h["event_id"] for h in prof_b["history"]]

        # split it back out into a brand-new case
        r = client.post("/events/split-case", auth=admin_auth,
                        json={"event_ids": [a["event_id"]]})
        assert r.status_code == 200
        new_case = r.json()["case_id"]
        ids.append(new_case)
        assert r.json()["label"].startswith("UNK-")
        prof_b = client.get(f"/person/{b['identity_id']}").json()
        assert a["event_id"] not in [h["event_id"] for h in prof_b["history"]]
    finally:
        _cleanup(client, admin_auth, set(ids))


# ---------------------------------------------------------------------------
# 7. Merge combinations + employee-primary rule + audit + unmerge
# ---------------------------------------------------------------------------
def _mk_case(client):
    e = client.post("/events", json=_obs(
        f"T-{uuid.uuid4().hex[:10]}", cam=f"TC-{uuid.uuid4().hex[:6]}",
        body=_vec(time.time() % 100))).json()
    return e["identity_id"], e["event_id"]


def _mk_visitor(client, seed):
    e = client.post("/events", json=_obs(f"T-{uuid.uuid4().hex[:10]}",
                                         face=_vec(seed), body=_vec(seed + 0.5))).json()
    assert (e["person_id"] or "").startswith("VIS-"), e
    return e["identity_id"], e["event_id"]


def _mk_employee(client, admin_auth, seed, name="Test Emp"):
    r = client.post("/employees", auth=admin_auth, json={
        "name": name, "department": "QA", "face_embedding": _vec(seed)})
    assert r.status_code in (200, 201), r.text
    return r.json()["identity_id"]


def test_merge_all_combinations_audited_and_undoable(client, admin_auth):
    _skip_unless_up(client)
    created = []
    try:
        # Unknown ↔ Unknown
        u1, _ = _mk_case(client); u2, ev2 = _mk_case(client)
        created += [u1, u2]
        r = client.post("/identities/merge", auth=admin_auth,
                        json={"primary_id": u1, "duplicate_ids": [u2]})
        assert r.status_code == 200, r.text
        audit_id = r.json()["audit_ids"][0]
        assert ev2 in [h["event_id"] for h in client.get(f"/person/{u1}").json()["history"]]

        # unmerge restores the fold
        r = client.post("/identities/unmerge", auth=admin_auth, json={"audit_id": audit_id})
        assert r.status_code == 200, r.text
        revived = r.json()["identity_id"]
        created.append(revived)
        assert ev2 in [h["event_id"] for h in client.get(f"/person/{revived}").json()["history"]]

        # Unknown → Visitor
        u3, _ = _mk_case(client); v1, _ = _mk_visitor(client, 11.1)
        created += [u3, v1]
        assert client.post("/identities/merge", auth=admin_auth,
                           json={"primary_id": v1, "duplicate_ids": [u3]}).status_code == 200

        # Visitor ↔ Visitor
        v2, _ = _mk_visitor(client, 22.2)
        created.append(v2)
        assert client.post("/identities/merge", auth=admin_auth,
                           json={"primary_id": v1, "duplicate_ids": [v2]}).status_code == 200

        # Visitor → Employee (employee must be primary)
        emp = _mk_employee(client, admin_auth, 33.3)
        created.append(emp)
        v3, _ = _mk_visitor(client, 44.4)
        created.append(v3)
        assert client.post("/identities/merge", auth=admin_auth,
                           json={"primary_id": emp, "duplicate_ids": [v3]}).status_code == 200
        # employee details survive
        prof = client.get(f"/person/{emp}").json()
        assert prof["type"] == "employee" and prof["name"] == "Test Emp"

        # employee folded INTO a visitor must be REJECTED
        emp2 = _mk_employee(client, admin_auth, 55.5, name="Keep Me")
        v4, _ = _mk_visitor(client, 66.6)
        created += [emp2, v4]
        r = client.post("/identities/merge", auth=admin_auth,
                        json={"primary_id": v4, "duplicate_ids": [emp2]})
        assert r.status_code == 400, "an employee may only survive as primary"

        # Employee ↔ Employee
        assert client.post("/identities/merge", auth=admin_auth,
                           json={"primary_id": emp, "duplicate_ids": [emp2]}).status_code == 200

        # audit trail recorded the merges
        trail = client.get("/identities/audit?action=merge&limit=10", auth=admin_auth).json()
        assert any(x["action"] == "merge" for x in trail)
    finally:
        _cleanup(client, admin_auth, set(created))


# ---------------------------------------------------------------------------
# 8. Roster import: preview, apply, idempotency, ZIP parsing
# ---------------------------------------------------------------------------
def test_import_csv_preview_apply_idempotent(client, admin_auth):
    _skip_unless_up(client)
    ext = f"XT-{uuid.uuid4().hex[:8]}"
    csv_body = (
        "external_id,name,department,email\n"
        f"{ext},Import Test,QA,imp@test.example\n"
        ",Missing Id,QA,\n"                      # row error: no external_id
    )
    b64 = base64.b64encode(csv_body.encode()).decode()

    # dry run: full row-level preview, nothing written
    r = client.post("/employees/import", auth=admin_auth,
                    json={"filename": "roster.csv", "content_b64": b64, "dry_run": True})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["total_rows"] == 2 and s["valid_rows"] == 1 and s["error_rows"] == 1
    assert "missing external_id" in " ".join(s["rows"][1]["errors"])

    created = []
    try:
        # apply → created
        r = client.post("/employees/import", auth=admin_auth,
                        json={"filename": "roster.csv", "content_b64": b64, "dry_run": False})
        s = r.json()
        assert s["created"] == 1 and s["updated"] == 0
        iid = next(x["identity_id"] for x in s["rows"] if not x["errors"])
        created.append(iid)

        # re-apply with a changed name → UPDATED, not duplicated
        csv2 = f"external_id,name,department\n{ext},Renamed Person,Ops\n"
        r = client.post("/employees/import", auth=admin_auth,
                        json={"filename": "roster.csv",
                              "content_b64": base64.b64encode(csv2.encode()).decode(),
                              "dry_run": False})
        s2 = r.json()
        assert s2["updated"] == 1 and s2["created"] == 0
        assert s2["rows"][0]["identity_id"] == iid
        prof = client.get(f"/person/{iid}").json()
        assert prof["name"] == "Renamed Person" and prof["department"] == "Ops"
    finally:
        _cleanup(client, admin_auth, created)


def test_import_zip_bundle_parses_roster_and_photos():
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from services.import_service import parse_bundle, validate_rows

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("roster.csv", "external_id,name\nE100,Zip Person\nE200,No Photo\n")
        zf.writestr("photos/E100_1.jpg", b"\xff\xd8fakejpegbytes")
        zf.writestr("photos/E100_2.jpg", b"\xff\xd8fakejpegbytes2")
    rows, photos = parse_bundle("bundle.zip", buf.getvalue())
    assert len(rows) == 2 and len(photos) == 2
    report = validate_rows(rows, photos)
    assert report[0]["photos"] == 2, "photos must auto-match by external_id"
    assert report[1]["photos"] == 0
    assert not report[0]["errors"] and not report[1]["errors"]


# ---------------------------------------------------------------------------
# 9. Legacy rows stay readable (pre-rework columns only)
# ---------------------------------------------------------------------------
def test_legacy_snapshot_only_payload_still_readable(client, admin_auth):
    _skip_unless_up(client)
    track = f"T-{uuid.uuid4().hex[:10]}"
    e = client.post("/events", json=_obs(
        track, body=_vec(0.42), snapshot_path="storage/img/GATE-01/legacy.jpg")).json()
    try:
        feed = client.get(f"/events?limit=50&camera=TESTCAM-{_CAM_NS}").json()["events"]
        mine = [x for x in feed if x.get("track_uuid") == track]
        assert mine and mine[0]["snapshot"] == "storage/img/GATE-01/legacy.jpg"
        # new explicit fields exist (None is fine) — the shape is uniform
        assert "face" in mine[0] and "full_frame" in mine[0]
    finally:
        _cleanup(client, admin_auth, [e.get("identity_id")])


# ---------------------------------------------------------------------------
# 10. Observation-first flow: faceless case folds into the Visitor once the
#     SAME track finally delivers a face (never traps people as Unknown).
# ---------------------------------------------------------------------------
def test_face_resolve_folds_unknown_case(client, admin_auth):
    _skip_unless_up(client)
    t = f"T-{uuid.uuid4().hex[:10]}"
    body = _vec(31.7)
    # 1. immediate faceless observation → Unknown case
    e1 = client.post("/events", json=_obs(t, body=body, body_path=f"s/{t}_o.jpg")).json()
    assert (e1["person_id"] or "").startswith("UNK-")
    case_id = e1["identity_id"]
    ids = [case_id]
    try:
        # 2. identity emit on the SAME track, now WITH a face → real Visitor,
        #    and the whole case folds onto it (audited case-attach)
        e2 = client.post("/events", json=_obs(t, body=body, face=_vec(77.9),
                                              body_path=f"s/{t}_i.jpg")).json()
        ids.append(e2["identity_id"])
        assert (e2["person_id"] or "").startswith("VIS-"), \
            "a good face must mint a Visitor, not stay stuck on the case"
        assert e2["identity_id"] != case_id
        # the case is gone; its sighting moved onto the visitor
        prof = client.get(f"/person/{e2['identity_id']}").json()
        assert e1["event_id"] in [h["event_id"] for h in prof["history"]], \
            "the case's earlier sighting must attach to the resolved person"
        assert client.get(f"/person/{case_id}").status_code == 404
    finally:
        _cleanup(client, admin_auth, set(ids))
