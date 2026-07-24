/* app-core.js — split from the original app.js (bootstrap, feeds helpers, login/logout, nav, clock, view routing).
   Plain <script> (globals shared across files); loaded in order by index.html. */

/* ---------- Live camera source ----------
   When served by server.py, the camera list + live status come from
   /api/cameras. When opened as a plain file, we fall back to the static
   CAMERAS array in data.js. */
let LIVE_CAMERAS = CAMERAS;
let SERVED = false; // true once we know a backend is present (enables MJPEG feeds)
let BRAIN_ON = false; // true once hydrated from the Brain (Part 2) API
let CAM_NAME_BY_ID = {}; // camera id/uid -> friendly name, for location labels

async function loadCameras() {
  try {
    const res = await fetch('/api/cameras', { cache: 'no-store' });
    if (res.ok) {
      const cams = await res.json();
      if (Array.isArray(cams) && cams.length) {
        LIVE_CAMERAS = cams;
        SERVED = true;
      }
    }
  } catch (e) {
    /* file:// or server down — keep the static fallback list, no live video */
  }
  CAM_NAME_BY_ID = {};
  LIVE_CAMERAS.forEach(c => { CAM_NAME_BY_ID[c.id] = c.name; });
}

/* Try to back the whole UI with the Brain (Part 2). On success, PEOPLE +
   DETECTIONS are replaced with real data and we stream new events over WS /live.
   On any failure we leave the mock data.js content untouched. */
async function connectBrain() {
  if (typeof Brain === 'undefined') return;
  try {
    BRAIN_ON = await Brain.hydrate(CAM_NAME_BY_ID);
    if (BRAIN_ON) {
      Brain.connectLive(onLiveEvent);
      refreshOccupancy();                                   // seed the grid counts
      if (!window._occTimer) window._occTimer = setInterval(refreshOccupancy, 15000);
    }
  } catch (e) {
    console.warn('[Brain] connect failed, staying on mock data:', e.message);
    BRAIN_ON = false;
  }
}

/* Pull authoritative visit/inside counts from the Brain and repaint the badges.
   Cheap; polled every 15s and after live bursts. Falls back to client counts if
   the endpoint is unavailable (old Brain not yet restarted). */
async function refreshOccupancy() {
  if (typeof Brain === 'undefined' || !Brain.occupancy) return;
  const occ = await Brain.occupancy();
  if (occ) { OCCUPANCY = occ; if (typeof updateGridBadges === 'function') updateGridBadges(); }
}

/* A new detection arrived over WS /live — fold it into PEOPLE + DETECTIONS and
   refresh the views that show people/activity. */
let liveRefreshQueued = false;
function onLiveEvent(evt) {
  Brain.applyLiveEvent(evt, CAM_NAME_BY_ID);
  // Coalesce bursts into one repaint per animation frame.
  if (liveRefreshQueued) return;
  liveRefreshQueued = true;
  requestAnimationFrame(() => {
    liveRefreshQueued = false;
    updateGridBadges();   // NOT renderGrid — never recreate the feed <img> (flicker)
    if (currentView === 'log') renderLog();
    if (currentView === 'report') renderReport();
    if (currentView === 'records') renderRecords();
  });
}

/* Live camera feed as a POLLED STILL (not an infinite MJPEG stream).
   An MJPEG <img src="/stream/..."> holds one HTTP connection open forever; with
   many cameras that exhausts the browser's ~6-per-origin limit and the page
   (on reload) or its photos can never fetch — the tab "reloads forever". We poll
   a single JPEG per tile instead: short, reused connections that always release.
   The pipeline writes an annotated frame per camera to shared memory; server.py
   serves the latest at /snapshot/<id>. pollFeeds() refreshes only VISIBLE feeds. */
function feedImg(camId, cls) {
  if (!SERVED) return '';
  return `<img class="${cls}" alt="" data-cam="${camId}" data-feed>`;
}

function pollFeeds() {
  document.querySelectorAll('img[data-feed]').forEach(img => {
    if (!img.isConnected || img.offsetParent === null) return;  // hidden view — skip
    if (img.dataset.loading === '1') return;                    // fetch in flight — don't pile up
    img.dataset.loading = '1';
    fetch(`/snapshot/${encodeURIComponent(img.dataset.cam)}?t=${Date.now()}`, { cache: 'no-store' })
      .then(r => r.ok ? r.blob() : Promise.reject(new Error(r.status)))
      .then(blob => {
        // Swap to a decoded local blob only AFTER it loads, so a failed poll
        // keeps the last good frame (no blank flash). Revoke the previous blob.
        const url = URL.createObjectURL(blob);
        const prev = img.dataset.blob;
        img.onload = () => { if (prev) URL.revokeObjectURL(prev); };
        img.src = url;
        img.dataset.blob = url;
      })
      .catch(() => { /* keep last good frame */ })
      .finally(() => { img.dataset.loading = '0'; });
  });
}

/* ---------- Person photo helpers ----------
   The Brain gives each person a snapshot crop (api.js sets p.photo). Paint it as
   the avatar background with the initials text as the fallback shown until (or if)
   the image loads. Returns '' when there's no photo. */
function photoCss(p) {
  return (p && p.photo)
    ? `background-image:url('${p.photo}');background-size:cover;background-position:center;`
    : '';
}
/* Background-image CSS for a specific snapshot URL — used to show the crop from
   THAT sighting (per-row in logs), not the person's single representative photo. */
function photoCssUrl(url) {
  return url
    ? `background-image:url('${url}');background-size:cover;background-position:center;`
    : '';
}
function setAvatar(el, p) {
  if (!el) return;
  el.textContent = (p && p.initials) || '?';
  el.style.backgroundImage = (p && p.photo) ? `url('${p.photo}')` : '';
  el.style.backgroundSize = 'cover';
  el.style.backgroundPosition = 'center';
}

/* ---------- Auth ---------- */
/* The single valid operator. Real deployments should verify against the Brain,
   not a client-side constant — this gates the prototype only. */
const AUTH = { username: 'admin', password: 'password123' };

async function login() {
  const u = document.getElementById('login-user').value.trim();
  const p = document.getElementById('login-pass').value;
  const err = document.getElementById('login-error');
  if (u !== AUTH.username || p !== AUTH.password) {
    if (err) err.classList.remove('hidden');
    document.getElementById('login-pass').value = '';
    return;
  }
  if (err) err.classList.add('hidden');

  document.getElementById('view-login').classList.add('hidden');
  document.getElementById('app-shell').classList.remove('hidden');
  document.getElementById('operator-name').textContent = u;
  document.querySelector('#app-shell .avatar').textContent = initialsFromUsername(u);
  await loadCameras();
  await connectBrain();
  renderGrid();
  renderLog();
  renderReport();
  renderRecords();
  renderDepartments();
  showView('grid');
  playNavAnimation();
}

/* Nav entrance: the pill drops in first, then each icon flies up one by one.
   Toggling the class (with a reflow between) restarts it on every sign-in. */
function playNavAnimation() {
  const nav = document.querySelector('.nav');
  if (!nav) return;
  nav.classList.remove('nav-animate');
  void nav.offsetWidth; // force reflow so the animation replays
  nav.classList.add('nav-animate');
}

function logout() {
  document.getElementById('app-shell').classList.add('hidden');
  document.getElementById('view-login').classList.remove('hidden');
  const s = document.getElementById('global-search');
  if (s) s.value = '';
}

function initialsFromUsername(u) {
  const part = u.split('.')[0] || u;
  return (part[0] || 'U').toUpperCase() + (u.split('.')[1] ? u.split('.')[1][0].toUpperCase() : '');
}

/* ---------- Clock ---------- */
function tickClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  el.textContent = new Date().toLocaleTimeString('en-GB', { hour12: false });
}

/* ---------- View routing + context-aware search ---------- */
let currentView = 'grid';
const SEARCH_PLACEHOLDER = {
  grid:    'Search for a camera…',
  log:     'Search a person (name or ID)…',
  report:  "Search a person, or 'visitor' / 'unknown'…",
  records: 'Search by name or employee ID…',
};

function showView(name) {
  // Merge/select mode is shared by Report and Log (both offer a merge action);
  // leaving BOTH of those drops it so the floating bar can't linger elsewhere.
  if (name !== 'report' && name !== 'log' && mergeMode) exitMergeMode();
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + name).classList.remove('hidden');
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  const navBtn = document.querySelector(`.nav-item[data-view="${name}"]`);
  if (navBtn) navBtn.classList.add('active');

  currentView = name;
  const search = document.getElementById('global-search');
  if (search) {
    search.value = '';
    search.placeholder = SEARCH_PLACEHOLDER[name] || 'Search…';
  }
  // The Log page has its own person/location search, so hide the top search there.
  const topSearch = document.querySelector('.topbar-search');
  if (topSearch) topSearch.style.visibility = (name === 'log') ? 'hidden' : 'visible';

  // Log is a Report-only action — its top-bar button shows only on the Report view.
  const topActions = document.querySelector('.topbar-actions');
  if (topActions) topActions.style.display = (name === 'report') ? '' : 'none';

  // Reset each view's own filter as we arrive, so search starts clean.
  if (name === 'grid') filterGrid('');
  if (name === 'log') renderLog();
  if (name === 'report') { reportSearch = ''; renderReport(); }
  if (name === 'records') { const r = document.getElementById('records-search'); if (r) r.value = ''; renderRecords(); }
}

function onSearch(val) {
  const v = val.trim();
  if (currentView === 'grid') filterGrid(v);
  else if (currentView === 'report') { reportSearch = v; renderReport(); }
  else if (currentView === 'records') { document.getElementById('records-search').value = v; renderRecords(); }
}

/* ---------- Live grid ---------- */