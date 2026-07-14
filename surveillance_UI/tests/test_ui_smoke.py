"""
UI smoke tests (no browser): static integrity of the dashboard assets +
the UI bridge's route table. Guards the security/no-fake-boxes rules:

    python3 -m pytest surveillance_UI/tests/test_ui_smoke.py -v
"""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def test_no_hardcoded_credentials_in_client():
    app = _read("app.js")
    assert "password123" not in app, "client must not ship hardcoded credentials"
    assert "sessionStorage" in app, "credentials must come from the session"


def test_no_fabricated_detection_boxes():
    app = _read("app.js")
    # the randomized fake box is gone; only real /tracks data drives boxes
    assert "Math.random() * 30" not in app
    assert "Math.random() * 46" not in app
    assert "/tracks/" in app, "real track metadata endpoint must be used"


def test_report_operations_present():
    app = _read("app.js")
    for fn in ("mergeCurrentPersonInto", "pickIdentity", "openSightCarousel",
               "hideSightingRow", "reassignSightingRow", "splitSightingRow",
               "eraseCurrentPersonPermanently", "importPreview", "importApply"):
        assert f"function {fn}" in app or f"async function {fn}" in app, f"missing {fn}"


def test_api_client_exposes_new_calls():
    api = _read("api.js")
    for fn in ("searchIdentities", "hideSightings", "reassignSightings",
               "splitCase", "unmerge", "importEmployees", "importStatus"):
        assert fn in api, f"api.js missing {fn}"


def test_index_has_import_section():
    html = _read("index.html")
    assert "import-file" in html and "importPreview()" in html


def test_server_routes():
    srv = _read("server.py")
    for route in ("/tracks/", "/diag", "/storage/", "/snapshot/", "/stream/"):
        assert route in srv, f"server.py missing route {route}"
    assert '".mp4"' in srv, "clip playback needs the mp4 mime type"


def test_js_syntax_with_node():
    """Real syntax check via `node --check` when node is available (a naive
    brace counter mis-fires on regex literals). Skips without node."""
    import shutil
    import subprocess
    node = shutil.which("node") or os.environ.get("NODE_BIN")
    if not node or not os.path.exists(node):
        import pytest
        pytest.skip("node not available for JS syntax check")
    for name in ("app.js", "api.js"):
        r = subprocess.run([node, "--check", os.path.join(HERE, name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr[:400]}"


def test_evidence_images_never_cropped():
    """Report evidence must show the WHOLE file: contain, never cover, and no
    canvas slicing. (.tile-feed cover is the live video wall, not evidence.)"""
    css = _read("styles.css")
    photo_img_rule = re.search(r"\.photo-img\s*{[^}]*}", css)
    assert photo_img_rule, ".photo-img rule missing"
    assert "object-fit: contain" in photo_img_rule.group(0)
    assert "object-fit: cover" not in photo_img_rule.group(0)
    for cls in (".ev-img", ".ev-clip", ".review-thumb"):
        rule = re.search(re.escape(cls) + r"\s*{[^}]*}", css)
        if rule:
            assert "object-fit: cover" not in rule.group(0), f"{cls} crops evidence"
    app = _read("app.js")
    assert "drawImage" not in app, "no canvas slicing of evidence"


def test_no_media_path_derivation():
    """Companion files come EXPLICITLY from the API — never from filename
    string surgery on another file's path."""
    app = _read("app.js")
    api = _read("api.js")
    for src in (app, api):
        assert not re.search(r"replace\([^)]*_face", src)
        assert not re.search(r"replace\([^)]*_full", src)
        assert not re.search(r"replace\([^)]*_orig", src)
        assert not re.search(r"replace\([^)]*_annot", src)


def test_carousel_contract():
    html = _read("index.html")
    for el in ("carousel-prev", "carousel-next", "carousel-actions", "photo-imgs"):
        assert el in html, f"carousel element {el} missing"
    app = _read("app.js")
    assert "Sighting ${carouselIdx + 1} of ${n}" in app, "position indicator missing"
    assert "ArrowLeft" in app and "ArrowRight" in app, "keyboard navigation missing"
    assert "No face captured" in app, "faceless sightings must say so, not borrow a face"
