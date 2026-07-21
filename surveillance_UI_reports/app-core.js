/* app-core.js — Reports & Logs site (trimmed from the main console's app-core /
   app-grid / app-track). Bootstrap, Brain wiring, login/logout, nav, clock,
   view routing and the shared helpers the Log/Report/person-modal code uses.
   Plain <script> (globals shared across files); loaded in order by index.html. */

/* ---------- Camera name registry ----------
   No live video on this site — /api/cameras only supplies id -> friendly name
   so event locations render with the same labels as the main console. When the
   server (or registry) is unavailable we fall back to the static CAMERAS list
   in data.js. */
let LIVE_CAMERAS = CAMERAS;
let BRAIN_ON = false;      // true once hydrated from the Brain (Part 2) API
let CAM_NAME_BY_ID = {};   // camera id/uid -> friendly name, for location labels

async function loadCameras() {
  try {
    const res = await fetch('/api/cameras', { cache: 'no-store' });
    if (res.ok) {
      const cams = await res.json();
      if (Array.isArray(cams) && cams.length) LIVE_CAMERAS = cams;
    }
  } catch (e) { /* server down — keep the static fallback list */ }
  CAM_NAME_BY_ID = {};
  LIVE_CAMERAS.forEach(c => { CAM_NAME_BY_ID[c.id] = c.name; });
}

/* Back the site with the Brain (Part 2). On success, PEOPLE is replaced with
   real data and api.js keeps it fresh by polling (no WS through the proxy).
   On any failure we leave the mock data.js content untouched. */
async function connectBrain() {
  if (typeof Brain === 'undefined') return;
  try {
    BRAIN_ON = await Brain.hydrate(CAM_NAME_BY_ID);
    if (BRAIN_ON) Brain.connectLive(onLiveRefresh);
  } catch (e) {
    console.warn('[Brain] connect failed, staying on mock data:', e.message);
    BRAIN_ON = false;
  }
}

/* api.js re-hydrated the roster — repaint whichever view is on screen. */
let liveRefreshQueued = false;
function onLiveRefresh() {
  if (liveRefreshQueued) return;
  liveRefreshQueued = true;
  requestAnimationFrame(() => {
    liveRefreshQueued = false;
    if (currentView === 'log') renderLog();
    if (currentView === 'report') renderReport();
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

/* ---------- Shared date/time helpers (same as the main console) ---------- */
const MONTH_NAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const WEEKDAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
let logYear, logMonth, logDay;   // selected month/year (0-indexed month) + selected day-of-month
let logInit = false;

/* "HH:MM" (24h) -> minutes since midnight, or null if blank/invalid. */
function hhmmToMin(t) {
  if (!t) return null;
  const [h, m] = t.split(':').map(Number);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
}

/* "HH:MM" (24h) -> "h:MM AM/PM" for display. */
function to12h(t) {
  const [h, m] = t.split(':').map(Number);
  const period = h < 12 ? 'AM' : 'PM';
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, '0')} ${period}`;
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
  renderLog();
  renderReport();
  renderDepartments();
  showView('report');
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
let currentView = 'report';
const SEARCH_PLACEHOLDER = {
  log:    'Search a person (name or ID)…',
  report: "Search a person, or 'visitor' / 'unknown'…",
};

function showView(name) {
  // Leaving the Report view drops merge/select mode so the floating bar can't
  // linger over another view.
  if (name !== 'report' && mergeMode) exitMergeMode();
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

  // Reset each view's own filter as we arrive, so search starts clean.
  if (name === 'log') renderLog();
  if (name === 'report') { reportSearch = ''; renderReport(); }
}

function onSearch(val) {
  const v = val.trim();
  if (currentView === 'report') { reportSearch = v; renderReport(); }
}
