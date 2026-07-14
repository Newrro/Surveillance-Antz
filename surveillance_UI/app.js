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
    if (BRAIN_ON) Brain.connectLive(onLiveEvent);
  } catch (e) {
    console.warn('[Brain] connect failed, staying on mock data:', e.message);
    BRAIN_ON = false;
  }
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
// 110 ms ≈ 9 fps effective (each poll waits for the previous via the in-flight
// guard). The pipeline writes 12 fps previews to SHM; at the old 350 ms the grid
// showed ~3 fps of them — the "laggy video" was the POLL RATE, not the pipeline.
// Pure localhost HTTP + browser JPEG decode: zero GPU cost.
setInterval(pollFeeds, 110);

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
setInterval(tickClock, 1000);
tickClock();

/* ---------- View routing + context-aware search ---------- */
let currentView = 'grid';
const SEARCH_PLACEHOLDER = {
  grid:    'Search for a camera…',
  log:     'Search a person (name or ID)…',
  report:  "Search a person, or 'visitor' / 'unknown'…",
  records: 'Search by name or employee ID…',
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

  // TRACK belongs to the live grid only — hide it everywhere else.
  const topTrack = document.querySelector('.topbar-TRACK');
  if (topTrack) topTrack.style.display = (name === 'grid') ? '' : 'none';

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
function renderGrid() {
  const grid = document.getElementById('camera-grid');
  // Build tile DOM only when the camera set changes. Live events fire many times
  // a second; rebuilding innerHTML each time would destroy/recreate the polled
  // feed <img> → blank until the next poll → constant flicker. Build once, then
  // only refresh the badges. (No HTML detbox overlay: Part 1 draws the REAL boxes
  // on the video frame the feed already shows.)
  const sig = (SERVED ? 's:' : 'n:') + LIVE_CAMERAS.map(c => c.id).join(',');
  if (grid.dataset.sig !== sig) {
    grid.innerHTML = LIVE_CAMERAS.map(cam => `
      <div class="tile-wrap" data-name="${cam.name.toLowerCase()}" data-cam="${cam.id}">
        <div class="tile-head">
          <span class="tile-status"></span>
          <span class="tile-name">${cam.name}</span>
          <span class="tile-loc">${cam.location || ''}</span>
        </div>
        <div class="tile" onclick="openCamera('${cam.id}')">
          ${feedImg(cam.id, 'tile-feed')}
          <span class="bracket tl"></span><span class="bracket tr"></span>
          <span class="bracket bl"></span><span class="bracket br"></span>
          <span class="tile-count">No activity</span>
          <span class="tile-time"></span>
        </div>
      </div>`).join('');
    grid.dataset.sig = sig;
  }
  updateGridBadges();
}

/* Refresh only the dynamic per-tile text WITHOUT touching the feed <img> — safe
   to call on every live event. */
function updateGridBadges() {
  const ic = document.getElementById('inside-count');
  if (ic) ic.textContent = countInside();
  const vc = document.getElementById('visits-count');
  if (vc) vc.textContent = countVisitsToday();
  LIVE_CAMERAS.forEach(cam => {
    const wrap = document.querySelector(`#camera-grid .tile-wrap[data-cam="${cam.id}"]`);
    if (!wrap) return;
    const offline = cam.status === 'offline';
    const n = (DETECTIONS[cam.id] || []).length;
    wrap.querySelector('.tile-status').classList.toggle('off', offline);
    wrap.querySelector('.tile').classList.toggle('offline', offline);
    wrap.querySelector('.tile-count').textContent = n ? n + ' detected' : 'No activity';
    wrap.querySelector('.tile-time').textContent = offline ? 'signal lost' : nowTime();
  });
}

function filterGrid(q) {
  const query = (q || '').toLowerCase();
  document.querySelectorAll('#camera-grid .tile-wrap').forEach(w => {
    w.style.display = w.dataset.name.includes(query) ? '' : 'none';
  });
}

function nowTime() {
  return new Date().toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit' });
}

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

/* ---------- Individual camera ---------- */
function openCamera(camId) {
  const cam = LIVE_CAMERAS.find(c => c.id === camId);
  document.getElementById('camera-title').textContent = cam.name;
  document.getElementById('camera-sub').textContent = cam.location;

  const dets = DETECTIONS[camId] || [];
  const frame = document.getElementById('feed-frame');
  frame.innerHTML = `
    ${feedImg(camId, 'feed-video')}
    <span class="bracket tl"></span><span class="bracket tr"></span>
    <span class="bracket bl"></span><span class="bracket br"></span>
    <span class="feed-label">${cam.name}</span>
    <span class="feed-time">${nowTime()}</span>
    ${xrayHTML(dets)}
    <button class="feed-fs" title="Toggle fullscreen"
            onclick="event.stopPropagation(); toggleFeedFullscreen(document.getElementById('feed-frame'))">
      <i class="ti ti-maximize"></i>
    </button>
  `;

  showViewRaw('camera');
}

/* ---------- X-Ray: top-right panel of everyone detected on the feed ----------
   A top-right "X-Ray" box drops down a list of detected people, each expandable to
   full details. It lives inside the feed element so it also renders in fullscreen,
   where the normal person sidebar can't appear (native fullscreen paints only the
   feed + its descendants). Available in both the normal camera view and fullscreen. */
function xrayHTML(dets) {
  return `
    <button class="xray-btn" title="X-Ray — detected people"
            onclick="event.stopPropagation(); toggleXray()">
      <i class="ti ti-scan-eye"></i> X-Ray<span class="xray-count">${dets.length}</span>
    </button>
    <div class="xray-panel" id="xray-panel" onclick="event.stopPropagation()">
      <div class="xray-panel-head">Detected on feed <span>${dets.length}</span></div>
      <div class="xray-list">
        ${dets.length
          ? dets.map(d => xrayItemHTML(d.personId)).join('')
          : `<div class="xray-empty"><i class="ti ti-mood-empty"></i> No one detected on this feed.</div>`}
      </div>
    </div>`;
}

function xrayItemHTML(personId) {
  const p = PEOPLE[personId];
  const entries = todayEntries(p);
  const rows = [];
  rows.push(['Category', p.category]);
  if (p.employeeId) rows.push(['Employee ID', p.employeeId]);
  if (p.department) rows.push(['Department', p.department]);
  if (p.gender)     rows.push(['Gender', p.gender]);
  if (p.age)        rows.push(['Age', String(p.age)]);
  if (p.height)     rows.push(['Height', p.height]);
  if (p.features)   rows.push(['Features', p.features]);
  if (entries[0])   rows.push(['Entry time', to12h(entries[0].time)]);
  const last = entries[entries.length - 1];
  if (last)         rows.push(['Last seen', `${last.location} · ${to12h(last.time)}`]);

  return `
    <div class="xray-item" data-pid="${p.userId}">
      <button class="xray-item-head" onclick="event.stopPropagation(); toggleXrayItem(this)">
        <span class="avatar" style="${photoCss(p)}">${p.initials}</span>
        <span class="xray-item-who">
          <span class="xray-item-name">${personName(p)}</span>
          <span class="xray-item-id">${p.userId}</span>
        </span>
        <span class="badge badge-${p.category}">${p.category}</span>
        <i class="ti ti-chevron-down xray-caret"></i>
      </button>
      <div class="xray-item-body">
        ${rows.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}
        <button class="btn btn-block xray-log-btn"
                onclick="event.stopPropagation(); openXrayPersonLog('${p.userId}')">
          <i class="ti ti-route"></i> Full movement log
        </button>
      </div>
    </div>`;
}

/* Detection-box click: in fullscreen open the X-Ray panel on that person
   (the sidebar can't render over the fullscreen feed); otherwise open the sidebar. */
function onDetboxClick(e, personId) {
  e.stopPropagation();
  if (isFeedFullscreen()) openXrayForPerson(personId);
  else openPerson(personId);
}

function isFeedFullscreen() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement);
}

function toggleXray() {
  const panel = document.getElementById('xray-panel');
  if (!panel) return;
  const open = panel.classList.toggle('open');
  document.querySelector('.xray-btn')?.classList.toggle('active', open);
}

function toggleXrayItem(btn) {
  btn.closest('.xray-item')?.classList.toggle('open');
}

function openXrayForPerson(personId) {
  const panel = document.getElementById('xray-panel');
  if (!panel) return;
  panel.classList.add('open');
  document.querySelector('.xray-btn')?.classList.add('active');
  const item = panel.querySelector(`.xray-item[data-pid="${personId}"]`);
  if (item) { item.classList.add('open'); item.scrollIntoView({ block: 'nearest' }); }
}

/* Full log lives in a modal outside the feed, so leave fullscreen first. */
function openXrayPersonLog(personId) {
  if (isFeedFullscreen()) {
    (document.exitFullscreen || document.webkitExitFullscreen)?.call(document);
    setTimeout(() => openPersonLog(personId), 140);
  } else {
    openPersonLog(personId);
  }
}

/* Toggle a camera feed to/from fullscreen (native Fullscreen API). */
function toggleFeedFullscreen(el) {
  if (!el) return;
  const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  if (fsEl) {
    (document.exitFullscreen || document.webkitExitFullscreen)?.call(document);
  } else {
    (el.requestFullscreen || el.webkitRequestFullscreen)?.call(el);
  }
}

/* Keep every feed's maximize/minimize icon in sync with the fullscreen state,
   and close the X-Ray panel whenever we drop out of fullscreen. */
function onFeedFullscreenChange() {
  const on = !!(document.fullscreenElement || document.webkitFullscreenElement);
  document.querySelectorAll('.feed-fs i').forEach(i => {
    i.className = on ? 'ti ti-minimize' : 'ti ti-maximize';
  });
  if (!on) {
    document.getElementById('xray-panel')?.classList.remove('open');
    document.querySelectorAll('.xray-btn').forEach(b => b.classList.remove('active'));
  }
}
document.addEventListener('fullscreenchange', onFeedFullscreenChange);
document.addEventListener('webkitfullscreenchange', onFeedFullscreenChange);

/* like showView but without the search reset (camera has no search context) */
function showViewRaw(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + name).classList.remove('hidden');
  currentView = name;
  // TRACK belongs to the live grid only — the camera view isn't the grid.
  const topTrack = document.querySelector('.topbar-TRACK');
  if (topTrack) topTrack.style.display = (name === 'grid') ? '' : 'none';
}

/* ---------- Person sidebar (from a camera detection) ---------- */
let currentPersonId = null;

function todayEntries(p) {
  const t = p.history.filter(h => h.date === TODAY);
  return t.length ? t : p.history;
}

function openPerson(personId) {
  currentPersonId = personId;
  const p = PEOPLE[personId];
  const entries = todayEntries(p);
  setAvatar(document.getElementById('ps-avatar'), p);
  document.getElementById('ps-name').textContent = personName(p);
  document.getElementById('ps-id').textContent = p.userId;
  document.getElementById('ps-category').innerHTML = `<span class="badge badge-${p.category}">${p.category}</span>`;
  document.getElementById('ps-empid').textContent = p.employeeId ? `${p.employeeId} · ${p.department}` : '—';
  document.getElementById('ps-entry').textContent = entries[0] ? entries[0].time : '—';
  document.getElementById('ps-trail').innerHTML = entries.map(m =>
    `<li><span class="t">${m.time}</span><span>${m.location}</span></li>`
  ).join('');

  document.getElementById('overlay').classList.add('open');
  document.getElementById('person-sidebar').classList.add('open');
}

function closeSidebar() {
  document.getElementById('overlay').classList.remove('open');
  document.getElementById('person-sidebar').classList.remove('open');
}

function openPersonLogFromSidebar() {
  if (!currentPersonId) return;
  closeSidebar();
  openPersonLog(currentPersonId);
}

/* ---------- Person log modal (Report + sidebar) ---------- */
let plogPersonId = null;
let plogYear, plogMonth, plogDay;   // selected day inside the modal (month 0-indexed)

function openPersonLog(userId) {
  plogPersonId = userId;
  const p = PEOPLE[userId];
  setAvatar(document.getElementById('plog-avatar'), p);
  document.getElementById('plog-name').textContent = personName(p);
  document.getElementById('plog-id').textContent = p.employeeId ? `${p.userId} · ${p.employeeId}` : p.userId;
  document.getElementById('plog-badge').innerHTML = `<span class="badge badge-${p.category}">${p.category}</span>`;

  // Rename only Visitors/Employees (not Unknowns); "Make employee" only for Visitors.
  const renameBtn = document.getElementById('plog-rename');
  if (renameBtn) renameBtn.style.display = (p.category === 'Unknown') ? 'none' : '';
  renderPlogPromote(p);   // inline "Make employee" control (Visitors only)

  // Details of the person
  const rows = [];
  if (p.category === 'Employee') {
    rows.push(['Employee ID', p.employeeId], ['Department', p.department]);
  }
  rows.push(['Category', p.category]);
  if (p.gender) rows.push(['Gender', p.gender]);
  if (p.age)    rows.push(['Age', String(p.age)]);
  if (p.height) rows.push(['Height', p.height]);
  if (p.features) rows.push(['Features', p.features]);
  rows.push(['Total sightings', String(p.history.length)]);
  document.getElementById('plog-details').innerHTML =
    `<div class="plog-card-title">Details of the person</div>` +
    rows.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');

  // Default the selected day to the person's most recent sighting (else today).
  const dates = p.history.map(h => h.date).sort();
  const latest = dates.length ? dates[dates.length - 1] : TODAY;
  const [ly, lm, ld] = latest.split('-').map(Number);
  plogYear = ly; plogMonth = lm - 1; plogDay = ld;

  // Month / year selectors
  const mSel = document.getElementById('plog-month');
  mSel.innerHTML = MONTH_NAMES.map((m, i) => `<option value="${i}">${m}</option>`).join('');
  const ySel = document.getElementById('plog-year');
  ySel.innerHTML = [2024, 2025, 2026, 2027].map(y => `<option value="${y}">${y}</option>`).join('');
  mSel.value = plogMonth; ySel.value = plogYear;

  // Location filter — only places this person was actually seen.
  const locs = Array.from(new Set(p.history.map(h => h.location))).sort();
  document.getElementById('plog-loc').innerHTML =
    `<option value="">All locations</option>` + locs.map(l => `<option value="${l}">${l}</option>`).join('');

  clearPersonLogFilters();   // resets time/location and renders the table
  renderPlogSide();          // details column: chart, hours, day grid
  document.getElementById('plog-modal').classList.add('open');
}

function onPlogMonthYear() {
  plogMonth = parseInt(document.getElementById('plog-month').value, 10);
  plogYear  = parseInt(document.getElementById('plog-year').value, 10);
  const days = new Date(plogYear, plogMonth + 1, 0).getDate();
  if (plogDay > days) plogDay = 1;
  renderPlogSide();
  renderPersonLog();
}

function selectPlogDay(d) {
  plogDay = d;
  renderPlogSide();
  renderPersonLog();
}

/* Left column: weekly presence chart, hours-inside summary, and day tiles. */
function renderPlogSide() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;

  // Selected-day heading (shown on the right).
  const dateStr = isoDate(plogYear, plogMonth, plogDay);
  document.getElementById('plog-sel-date').textContent = dateStr;
  document.getElementById('plog-sel-day').textContent = WEEKDAYS[new Date(plogYear, plogMonth, plogDay).getDay()];

  // Day tiles for the selected month; days with sightings are marked.
  const daysInMonth = new Date(plogYear, plogMonth + 1, 0).getDate();
  const prefix = `${plogYear}-${pad2(plogMonth + 1)}-`;
  const marked = new Set(
    p.history.filter(h => h.date.startsWith(prefix)).map(h => parseInt(h.date.slice(-2), 10)));
  let tiles = '';
  for (let d = 1; d <= daysInMonth; d++) {
    const sel = d === plogDay ? 'selected' : '';
    const has = marked.has(d) ? 'has' : '';
    tiles += `<button class="plog-day ${sel} ${has}" onclick="selectPlogDay(${d})">${d}</button>`;
  }
  document.getElementById('plog-daygrid').innerHTML = tiles;

  renderPlogChart(p, dateStr);
}

/* Weekly report: sightings per day across the week (Sun–Sat) of the selected day. */
function renderPlogChart(p, dateStr) {
  const base = new Date(plogYear, plogMonth, plogDay);
  const sunday = new Date(base);
  sunday.setDate(base.getDate() - base.getDay());

  const week = [];
  for (let i = 0; i < 7; i++) {
    const dt = new Date(sunday);
    dt.setDate(sunday.getDate() + i);
    const iso = isoDate(dt.getFullYear(), dt.getMonth(), dt.getDate());
    week.push({ iso, dom: dt.getDate(), count: p.history.filter(h => h.date === iso).length });
  }

  const W = 300, H = 128, padL = 22, padR = 8, padT = 12, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxC = Math.max(1, ...week.map(d => d.count));
  const x = i => padL + plotW * (i / 6);
  const y = c => padT + plotH * (1 - c / maxC);

  const line = week.map((d, i) => `${x(i).toFixed(1)},${y(d.count).toFixed(1)}`).join(' ');
  const area = `${padL},${padT + plotH} ${line} ${padL + plotW},${padT + plotH}`;
  const dots = week.map((d, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(d.count).toFixed(1)}" r="3" class="plog-dot"/>`).join('');
  const xlabels = week.map((d, i) =>
    `<text x="${x(i).toFixed(1)}" y="${H - 6}" class="plog-axis" text-anchor="middle">${d.dom}</text>`).join('');
  const ymax = `<text x="${padL - 6}" y="${padT + 4}" class="plog-axis" text-anchor="end">${maxC}</text>`;
  const yzero = `<text x="${padL - 6}" y="${padT + plotH}" class="plog-axis" text-anchor="end">0</text>`;

  document.getElementById('plog-chart').innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" class="plog-svg" preserveAspectRatio="none">
      <polygon points="${area}" class="plog-area"/>
      <polyline points="${line}" class="plog-line"/>
      ${dots}${xlabels}${ymax}${yzero}
    </svg>
    <div class="plog-chart-cap">Sightings per day · week of ${dateStr}</div>`;

  // Hours of presence inside on the selected day (first → last sighting).
  const dayEntries = p.history.filter(h => h.date === dateStr).sort((a, b) => a.time.localeCompare(b.time));
  let hours;
  if (dayEntries.length >= 2) {
    const mins = hhmmToMin(dayEntries[dayEntries.length - 1].time) - hhmmToMin(dayEntries[0].time);
    hours = `${Math.floor(mins / 60)}h ${mins % 60}m`;
  } else if (dayEntries.length === 1) {
    hours = 'Single sighting';
  } else {
    hours = 'Not present';
  }
  document.getElementById('plog-hours').innerHTML =
    `<span class="plog-hours-label">Hours of presence inside</span><span class="plog-hours-val">${hours}</span>`;
}

/* Right column: this person's movements on the selected day, filtered. */
function renderPersonLog() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  const dateStr = isoDate(plogYear, plogMonth, plogDay);
  const fromMin = hhmmToMin(document.getElementById('plog-time-from').value);
  const toMin   = hhmmToMin(document.getElementById('plog-time-to').value);
  const loc     = document.getElementById('plog-loc').value;

  const rows = p.history
    .filter(h => h.date === dateStr)
    .filter(h => !loc || h.location === loc)
    .filter(h => fromMin === null || hhmmToMin(h.time) >= fromMin)
    .filter(h => toMin   === null || hhmmToMin(h.time) <= toMin)
    .sort((a, b) => a.time.localeCompare(b.time));

  const body = document.getElementById('plog-body');
  body.innerHTML = rows.length ? rows.map(h => `
    <tr>
      <td class="mono">${to12h(h.time)}</td>
      <td>${h.location}</td>
      <td>
        <div class="plog-row-actions">
          <div class="plog-cap" title="Click to enlarge · ${h.location}" style="cursor:pointer"
               onclick="openPersonPhoto('${h.snapshot || ''}')">
            <div class="avatar" style="width:30px;height:30px;font-size:11px;${h.snapshot ? photoCssUrl(h.snapshot) : photoCss(p)}">${p.initials}</div>
            <i class="ti ti-camera"></i>
          </div>
          ${h.event_id != null ? `<button class="plog-del-btn" title="Delete this sighting"
                onclick="deleteSightingRow(${h.event_id})"><i class="ti ti-trash"></i></button>` : ''}
        </div>
      </td>
    </tr>`).join('')
    : `<tr><td colspan="3" style="color:var(--text-muted)">No sightings on this day for these filters.</td></tr>`;
}

/* Delete one sighting (detection_event) from a person's log, then refresh. */
async function deleteSightingRow(eventId) {
  if (!confirm('Delete this sighting from the log? (the person is kept)')) return;
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.deleteSighting({ user: AUTH.username, pass: AUTH.password }, [eventId]);
    await connectBrain();                       // re-hydrate so the row is gone
    const p = PEOPLE[plogPersonId];
    if (p) {
      renderPlogSide();
      renderPlogChart(p, isoDate(plogYear, plogMonth, plogDay));
      renderPersonLog();
    } else {
      closePersonLog();                         // that was their last sighting
    }
    renderReport(); renderRecords();
    if (currentView === 'log') renderLog();
  } catch (e) {
    alert('Delete sighting failed: ' + e.message);
  }
}

function clearPersonLogFilters() {
  document.getElementById('plog-time-from').value = '';
  document.getElementById('plog-time-to').value = '';
  document.getElementById('plog-loc').value = '';
  renderPersonLog();
}
function closePersonLog() { document.getElementById('plog-modal').classList.remove('open'); }
function closePersonLogIfBackdrop(e) { if (e.target.id === 'plog-modal') closePersonLog(); }

/* Load <url> into <imgEl> only if it actually exists; otherwise hide <figEl>.
   Used for the face/scene siblings, which don't exist for a faceless sighting. */
function _probeInto(imgEl, figEl, url) {
  figEl.style.display = 'none';
  if (!url) return;
  const probe = new Image();
  probe.onload = () => { imgEl.src = url; figEl.style.display = ''; };
  probe.onerror = () => { figEl.style.display = 'none'; };
  probe.src = url;
}

/* Enlarged photo popup.
   CRITICAL invariant: the Face, Full body and Full scene shown are ALWAYS the
   same sighting — one filename stem, one frame, one person. Part 1 now writes the
   triple <stem>.jpg / <stem>_face.jpg / <stem>_full.jpg atomically from the same
   frame, so deriving the face/scene from the SHOWN body's stem can never mix two
   people. We never substitute a face from a different sighting (that was the old
   "face of A on the body of B" bug). When no specific sighting is clicked we pick
   the most recent sighting that actually HAS a face and show its whole triple. */
function _showTriple(bodyUrl) {
  const bodyFig = document.getElementById('photo-body-fig');
  const bodyImg = document.getElementById('photo-body-img');
  if (!bodyUrl) {
    bodyFig.style.display = 'none';
    _probeInto(document.getElementById('photo-face-img'), document.getElementById('photo-face-fig'), '');
    _probeInto(document.getElementById('photo-full-img'), document.getElementById('photo-full-fig'), '');
    return;
  }
  const stem = bodyUrl.replace(/\.jpg(\?.*)?$/i, '');
  bodyImg.src = bodyUrl; bodyFig.style.display = '';
  _probeInto(document.getElementById('photo-face-img'),
             document.getElementById('photo-face-fig'), stem + '_face.jpg');
  _probeInto(document.getElementById('photo-full-img'),
             document.getElementById('photo-full-fig'), stem + '_full.jpg');
}

function openPersonPhoto(snapshotOverride) {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  document.getElementById('photo-name').textContent = personName(p);
  document.getElementById('photo-sub').textContent =
    p.employeeId ? `${p.userId} · ${p.employeeId}` : p.userId;
  document.getElementById('photo-modal').classList.add('open');

  // A clicked sighting is honored EXACTLY (even if it has no face) — never mixed.
  if (snapshotOverride) { _showTriple(snapshotOverride); return; }

  // No specific sighting: show the most recent one that HAS a saved face, so the
  // face and body still belong to the same frame. Fall back to the newest body
  // (face hidden) when the person has no face on file at all.
  const bodies = [...new Set([
    ...p.history.map(h => h.snapshot).filter(Boolean).slice().reverse(),
    p.photo,
  ].filter(Boolean))];
  if (!bodies.length) { _showTriple(''); return; }
  let i = 0;
  const pick = () => {
    if (i >= bodies.length) { _showTriple(bodies[0]); return; }  // none has a face
    const bodyUrl = bodies[i++];
    const probe = new Image();
    probe.onload = () => _showTriple(bodyUrl);       // this sighting has a face
    probe.onerror = pick;                            // try an older sighting
    probe.src = bodyUrl.replace(/\.jpg(\?.*)?$/i, '_face.jpg');
  };
  pick();
}
function closePersonPhoto() { document.getElementById('photo-modal').classList.remove('open'); }
function closePersonPhotoIfBackdrop(e) { if (e.target.id === 'photo-modal') closePersonPhoto(); }

/* Rename the person currently open in the log modal. Keeps their id + VIS/EMP
   label — only sets a friendly name (e.g. Visitor VIS-2026-0001 → "Akash"). */
async function renameCurrentPerson() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  const entered = window.prompt(`Name for ${p.displayLabel || p.userId}:`, p.name || '');
  if (entered === null) return;                       // cancelled
  const name = entered.trim();
  if (BRAIN_ON && p.identityId != null) {
    try {
      await Brain.setName(p.identityId, name);
    } catch (e) {
      alert('Rename failed: ' + e.message);
      return;
    }
  }
  // Reflect locally (also covers the offline/mock case).
  p.name = name || null;
  // Naming someone means you recognise them → promote an Unknown to a Visitor.
  if (name && p.category === 'Unknown') p.category = 'Visitor';
  p.initials = name
    ? name.split(/\s+/).map(s => s[0]).slice(0, 2).join('').toUpperCase()
    : (p.displayLabel ? p.displayLabel.slice(-2) : '??');
  document.getElementById('plog-name').textContent = personName(p);
  setAvatar(document.getElementById('plog-avatar'), p);
  renderReport(); renderRecords();
  if (currentView === 'log') renderLog();
}

/* Promote control in the person-log modal header — shown for Visitors only.
   Clicking "Make employee" swaps the button for an inline name + department form. */
function renderPlogPromote(p) {
  const el = document.getElementById('plog-promote');
  if (!el) return;
  el.innerHTML = (p.category === 'Visitor')
    ? `<button class="btn btn-primary plog-promote-btn" onclick="showPromoteToEmployee()"><i class="ti ti-user-check"></i> Make employee</button>`
    : '';
}

/* Reveal the inline promote form: a name field, a department field, Confirm/Cancel. */
function showPromoteToEmployee() {
  const el = document.getElementById('plog-promote');
  const p = PEOPLE[plogPersonId];
  if (!el || !p) return;
  const esc = s => (s || '').replace(/"/g, '&quot;');
  el.innerHTML = `
    <span class="plog-promote-form">
      <input id="plog-promote-name" type="text" placeholder="Name" value="${esc(p.name)}">
      <input id="plog-dept-sel" type="text" placeholder="Department" value="${esc(p.department || 'General')}" list="plog-dept-options">
      <datalist id="plog-dept-options">${DEPARTMENTS.map(d => `<option value="${esc(d)}"></option>`).join('')}</datalist>
      <button class="btn btn-primary" onclick="promoteCurrentPerson()"><i class="ti ti-check"></i> Confirm</button>
      <button class="btn" title="Cancel" onclick="renderPlogPromote(PEOPLE[plogPersonId])"><i class="ti ti-x"></i></button>
    </span>`;
  document.getElementById('plog-promote-name')?.focus();
}

/* Promote the Visitor currently open in the modal to an Employee (keeps id + history).
   Reads the inline promote form when it's open, else falls back to prompts. */
async function promoteCurrentPerson() {
  const p = PEOPLE[plogPersonId];
  if (!p || p.identityId == null) return;
  const nameEl = document.getElementById('plog-promote-name');
  const deptEl = document.getElementById('plog-dept-sel');
  let name = nameEl ? nameEl.value.trim() : (window.prompt('Employee name:', p.name || '') || '').trim();
  if (!name) { if (nameEl) nameEl.focus(); return; }
  let department = deptEl ? deptEl.value.trim() : (window.prompt('Department:', p.department || 'General') || '').trim();
  if (!department) department = 'General';
  try {
    const r = await Brain.promoteToEmployee(
      p.identityId, { name, department },
      { user: AUTH.username, pass: AUTH.password });
    p.category = 'Employee';
    p.name = name;
    p.department = department;
    p.employeeId = (r && r.new_label) || p.employeeId;
    p.initials = name.split(/\s+/).map(s => s[0]).slice(0, 2).join('').toUpperCase();
    if (department && !DEPARTMENTS.includes(department)) DEPARTMENTS.push(department);
    openPersonLog(plogPersonId);       // refresh the modal (badge, buttons, details)
    renderReport(); renderRecords();
  } catch (e) {
    alert('Promote failed: ' + e.message);
  }
}

/* Settings → Daily reset: clear all Unknowns (unconfirmed people), keeping
   confirmed Visitors + Employees. Same as the automatic midnight sweep. */
async function clearUnknowns() {
  if (!confirm('Clear all Unknowns? Confirmed Visitors and Employees are kept.')) return;
  const btn = document.getElementById('clear-unknowns-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Clearing…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const r = await Brain.clearUnknowns({ user: AUTH.username, pass: AUTH.password });
    await connectBrain();
    renderGrid(); renderReport(); renderRecords();
    if (currentView === 'log') renderLog();
    alert(`Cleared ${r.removed ?? 0} unknown(s).`);
  } catch (e) {
    alert('Clear failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-eraser"></i> Clear all unknowns'; }
  }
}

/* Settings → Merge duplicate visitors: preview the face-centroid merge plan
   (dry-run, safe) then optionally apply. Two-step so a human always sees which
   Visitor cards will be folded together before anything changes. */
function _renderConsolidatePlan(r) {
  const box = document.getElementById('consolidate-result');
  const applyBtn = document.getElementById('consolidate-apply-btn');
  if (!r.duplicates_found) {
    box.textContent = 'No duplicate visitors found — nothing to merge.';
    if (applyBtn) applyBtn.style.display = 'none';
    return;
  }
  const lines = r.merges.map(m =>
    `• keep ${m.keep_label} ← merge ${m.merged_labels.join(', ')}  (face sim ${m.similarity})`);
  box.textContent = `Found ${r.duplicates_found} duplicate(s) in ${r.clusters} cluster(s):\n` + lines.join('\n');
  if (applyBtn) applyBtn.style.display = '';   // reveal Apply now that there's a plan
}
async function consolidatePreview() {
  const btn = document.getElementById('consolidate-preview-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    _renderConsolidatePlan(await Brain.consolidate({ user: AUTH.username, pass: AUTH.password }, false));
  } catch (e) {
    document.getElementById('consolidate-result').textContent = 'Preview failed: ' + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-users-group"></i> Preview duplicates'; }
  }
}
async function consolidateApply() {
  if (!confirm('Merge the previewed duplicate Visitors? This rewrites their sightings onto one id and cannot be undone.')) return;
  const btn = document.getElementById('consolidate-apply-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Merging…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const r = await Brain.consolidate({ user: AUTH.username, pass: AUTH.password }, true);
    await connectBrain();
    renderGrid(); renderReport(); renderRecords();
    if (currentView === 'log') renderLog();
    document.getElementById('consolidate-result').textContent =
      `Merged ${r.duplicates_found} duplicate(s) into ${r.clusters} identity(ies).`;
    btn.style.display = 'none';
  } catch (e) {
    document.getElementById('consolidate-result').textContent = 'Merge failed: ' + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-git-merge"></i> Apply merges'; }
  }
}

/* Records → Enroll employee: upload face photo(s) → Brain embeds + enrolls. */
let enrollFiles = [];
function openEnroll() {
  ['enroll-name', 'enroll-dept', 'enroll-email'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('enroll-files').value = '';
  document.getElementById('enroll-preview').innerHTML = '';
  document.getElementById('enroll-status').textContent = '';
  enrollFiles = [];
  const inp = document.getElementById('enroll-files');
  inp.onchange = () => {
    const prev = document.getElementById('enroll-preview'); prev.innerHTML = '';
    enrollFiles = [...inp.files];
    enrollFiles.forEach(f => { const im = new Image(); im.src = URL.createObjectURL(f); im.className = 'enroll-thumb'; prev.appendChild(im); });
  };
  document.getElementById('enroll-modal').classList.add('open');
}
function closeEnroll() { document.getElementById('enroll-modal').classList.remove('open'); }
function closeEnrollIfBackdrop(e) { if (e.target.id === 'enroll-modal') closeEnroll(); }
function _readAsDataURL(file) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
}
async function doEnrollEmployee() {
  const name = document.getElementById('enroll-name').value.trim();
  const dept = document.getElementById('enroll-dept').value.trim();
  const email = document.getElementById('enroll-email').value.trim();
  const status = document.getElementById('enroll-status');
  if (!name || !dept) { status.textContent = 'Name and department are required.'; return; }
  if (!enrollFiles.length) { status.textContent = 'Add at least one face photo.'; return; }
  const btn = document.getElementById('enroll-submit'); btn.disabled = true; btn.textContent = 'Enrolling…';
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const images = await Promise.all(enrollFiles.map(_readAsDataURL));
    const r = await Brain.enrollEmployeePhoto({ name, department: dept, email, images,
      auth: { user: AUTH.username, pass: AUTH.password } });
    status.textContent = `Enrolled ${r.name} (${r.label}) from ${r.photos_used || 1} photo(s).`;
    await connectBrain(); renderRecords(); renderReport();
    setTimeout(closeEnroll, 1400);
  } catch (e) {
    status.textContent = 'Enroll failed: ' + e.message;
  } finally {
    btn.disabled = false; btn.innerHTML = '<i class="ti ti-user-plus"></i> Enroll';
  }
}

/* Settings → Danger zone: wipe the whole database (people/events/snapshots),
   keeping cameras. Uses the single admin credentials. */
async function deleteDatabase() {
  if (!confirm('Delete the ENTIRE database — every person, event and snapshot?\nCameras are kept. This cannot be undone.')) return;
  const btn = document.getElementById('wipe-db-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.resetDatabase({ user: AUTH.username, pass: AUTH.password });
    await connectBrain();                 // re-hydrate the now-empty DB
    renderGrid(); renderReport(); renderRecords();
    if (currentView === 'log') renderLog();
    alert('Database deleted. Starting fresh.');
  } catch (e) {
    alert('Delete failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-trash"></i> Delete entire database'; }
  }
}

/* ---------- Track: people currently inside + auto-track simulation ---------- */

/* TRACK button (topbar): list everyone currently inside, newest sighting first. */
function openTrackInside() {
  const search = document.getElementById('track-search-input');
  if (search) search.value = '';                 // start each open with a clean search
  renderTrackInside();
  document.getElementById('track-modal').classList.add('open');
  if (search) search.focus();
}

/* Does this person's name / user id / employee id contain the query? */
function personMatchesQuery(p, q) {
  return (personName(p) + ' ' + p.userId + ' ' + (p.employeeId || '')).toLowerCase().includes(q);
}

function renderTrackInside() {
  const inside = peopleInside();
  document.getElementById('track-count').textContent = inside.length;

  const raw = (document.getElementById('track-search-input')?.value || '').trim();
  const q = raw.toLowerCase();
  const matches = q ? inside.filter(({ p }) => personMatchesQuery(p, q)) : inside;

  const body = document.getElementById('track-body');

  if (!matches.length) {
    let msg;
    if (!q) {
      msg = 'No one is currently inside the premises.';
    } else {
      // Distinguish "known person who has already left" from "no such person",
      // so the operator knows whether the search term was even valid.
      const known = Object.values(PEOPLE).find(p => personMatchesQuery(p, q));
      msg = known
        ? `${personName(known)} is not currently inside the premises.`
        : `User not found — no one matches “${raw}”.`;
    }
    body.innerHTML = `<tr class="track-empty"><td colspan="6">
        <div class="track-empty-msg"><i class="ti ti-user-off"></i> ${msg}</div>
      </td></tr>`;
    return;
  }

  body.innerHTML = matches.map(({ p, entry, last }) => `
    <tr onclick="startAutoTrack('${p.userId}')" title="Auto-track ${personName(p)}">
      <td><div class="avatar log-photo" style="width:44px;height:44px;font-size:14px;${photoCss(p)}">${p.initials}</div></td>
      <td>
        <div class="track-name">${personName(p)}</div>
        <div class="track-uid mono">${p.userId}${p.employeeId ? ' · ' + p.employeeId : ''}</div>
      </td>
      <td><span class="badge badge-${p.category}">${p.category}</span></td>
      <td class="mono">${to12h(entry.time)}</td>
      <td>${last.location} <span class="mono track-lasttime">${to12h(last.time)}</span></td>
      <td style="text-align:right">
        <button class="btn btn-primary track-go" onclick="event.stopPropagation(); startAutoTrack('${p.userId}')"><i class="ti ti-route"></i> Track</button>
      </td>
    </tr>`).join('');
}

function closeTrack() { document.getElementById('track-modal').classList.remove('open'); }
function closeTrackIfBackdrop(e) { if (e.target.id === 'track-modal') closeTrack(); }

/* --- Auto-track simulation: "follow" a person camera-to-camera along today's trail --- */
let atUserId = null;
let atTimers = [];

function clearAtTimers() { atTimers.forEach(t => clearTimeout(t)); atTimers = []; }

function atSetStatus(state, text) {
  const el = document.getElementById('at-status');
  el.className = 'at-status ' + state;                 // acquiring | tracking | located | exited
  document.getElementById('at-status-text').textContent = text;
}

/* Camera id backing a location name (so served MJPEG feeds can be shown). */
function camIdForLocation(loc) {
  const cam = LIVE_CAMERAS.find(c => c.name === loc);
  return cam ? cam.id : null;
}

/* Paint the feed frame for one waypoint. When opts.scanning is set we show the
   camera live (of wherever the target was last seen) with a full-feed scan sweep
   but no lock-on box yet — the "acquiring" state. Otherwise we lock on. */
function atRenderFeed(wp, p, opts = {}) {
  const scanning = !!opts.scanning;
  const frame = document.getElementById('at-feed');
  const camId = wp ? camIdForLocation(wp.location) : null;
  const label = wp
    ? (scanning ? `SCANNING · ${wp.location}` : wp.location)
    : 'SCANNING CAMERA NETWORK…';
  const time  = wp ? to12h(wp.time) : '';
  const camTag = camId ? camId.toUpperCase().replace(/^CAM-/, 'CAM ').replace(/-/g, ' ') : 'NO SIGNAL';

  // Randomised box placement per camera so each acquisition reads as a fresh lock-on.
  const top  = (16 + Math.random() * 30).toFixed(1);
  const left = (18 + Math.random() * 46).toFixed(1);
  const tagName = p.category === 'Employee' ? p.name : p.category;

  frame.innerHTML = `
    ${(SERVED && camId) ? feedImg(camId, 'feed-video') : ''}
    <span class="bracket tl"></span><span class="bracket tr"></span>
    <span class="bracket bl"></span><span class="bracket br"></span>
    <div class="at-scanline"></div>
    ${(wp && !scanning)
      ? `<div class="detbox-live at-detbox category-${p.category}" style="top:${top}%;left:${left}%;width:14%;height:38%">
           <span class="tag">${tagName} · TRACKING</span>
           <span class="at-crosshair"></span>
         </div>`
      : `<div class="at-searching"><i class="ti ti-viewfinder"></i> ${wp ? 'scanning last-seen camera…' : 'locating target…'}</div>`}
    <span class="feed-label">${label}</span>
    <span class="feed-time">${time}</span>
    <span class="at-camid">${camTag}</span>
    <button class="feed-fs" title="Toggle fullscreen"
            onclick="event.stopPropagation(); toggleFeedFullscreen(document.getElementById('at-feed'))">
      <i class="ti ti-maximize"></i>
    </button>`;
}

function atAddTrail(wp, isLast) {
  const li = document.createElement('li');
  li.className = 'at-trail-item' + (isLast ? ' current' : '');
  li.innerHTML = `<span class="t">${to12h(wp.time)}</span><span>${wp.location}</span>` +
    (isLast ? `<i class="ti ti-current-location at-here"></i>` : '');
  document.getElementById('at-trail').appendChild(li);
}

function atUpdateProgress(done, total) {
  document.getElementById('at-progress').innerHTML =
    `<div class="at-progress-label">Waypoint ${done} / ${total}</div>
     <div class="at-progress-bar"><span style="width:${(done / total * 100).toFixed(0)}%"></span></div>`;
}

function startAutoTrack(userId) {
  atUserId = userId;
  const p = PEOPLE[userId];
  const found = peopleInside().find(x => x.p.userId === userId);
  const trail = found ? found.trail : p.history.filter(h => h.date === TODAY).sort((a, b) => a.time.localeCompare(b.time));

  document.getElementById('at-avatar').textContent = p.initials;
  document.getElementById('at-name').textContent = personName(p);
  document.getElementById('at-id').textContent = p.employeeId ? `${p.userId} · ${p.employeeId}` : p.userId;

  document.getElementById('track-modal').classList.remove('open');
  document.getElementById('autotrack-modal').classList.add('open');

  runAutoTrack(p, trail);
}

function runAutoTrack(p, trail) {
  clearAtTimers();
  document.getElementById('at-trail').innerHTML = '';
  document.getElementById('at-progress').innerHTML = '';

  // No sightings today — there's no location to receive, so keep scanning the
  // network until we give up.
  if (!trail.length) {
    atSetStatus('acquiring', 'ACQUIRING TARGET…');
    atRenderFeed(null, p, { scanning: true });
    atTimers.push(setTimeout(() => atSetStatus('exited', 'NO SIGHTINGS TODAY'), 1100));
    return;
  }

  // The target's location is already known, so there's nothing to scan for —
  // stop the scanning UI immediately and lock on, then step camera to camera
  // along today's path.
  const step = (wp, i) => {
    const isLast = i === trail.length - 1;
    atRenderFeed(wp, p);
    atAddTrail(wp, isLast);
    atUpdateProgress(i + 1, trail.length);
    if (isLast) atSetStatus('located', `TARGET LOCATED · ${wp.location}`);
    else        atSetStatus('tracking', `TRACKING · ${wp.location}`);
  };

  step(trail[0], 0);                         // location received → lock on at once
  let delay = 1500;
  for (let i = 1; i < trail.length; i++) {
    const wp = trail[i];
    atTimers.push(setTimeout(() => step(wp, i), delay));
    delay += 1500;
  }
}

/* Back to the currently-inside list (stops the running simulation). */
function backToTrackList() {
  clearAtTimers();
  document.getElementById('autotrack-modal').classList.remove('open');
  openTrackInside();
}

function closeAutoTrack() {
  clearAtTimers();
  document.getElementById('autotrack-modal').classList.remove('open');
}
function closeAutoTrackIfBackdrop(e) { if (e.target.id === 'autotrack-modal') closeAutoTrack(); }

/* ---------- Log page (calendar + day datasheet) ---------- */
const MONTH_NAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const WEEKDAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
let logYear, logMonth, logDay;   // selected month/year (0-indexed month) + selected day-of-month
let logInit = false;

/* Log category toggle buttons — drive the SAME hidden #log-category filter that
   renderLogSheet()/clearLogFilters() read, so their logic stays unchanged.
   No button active === "" === all categories (the cleared state). */
function setLogCat(cat) {
  const el = document.getElementById('log-category');
  if (el) el.value = cat;
  syncLogCatButtons();
  renderLogSheet();
}
function syncLogCatButtons() {
  const cur = document.getElementById('log-category')?.value || '';
  ['Employee', 'Visitor', 'Unknown'].forEach(c => {
    const btn = document.getElementById('log-cat-' + c);
    if (btn) btn.classList.toggle('active', c === cur);
  });
}

const pad2 = n => String(n).padStart(2, '0');
const isoDate = (y, m, d) => `${y}-${pad2(m + 1)}-${pad2(d)}`;

/* Entry point: build controls (once), then render calendar + datasheet. */
function renderLog() {
  if (!logInit) {
    const [ty, tm, td] = TODAY.split('-').map(Number);
    logYear = ty; logMonth = tm - 1; logDay = td;   // default to today

    // Month dropdown
    const mSel = document.getElementById('log-month');
    mSel.innerHTML = MONTH_NAMES.map((m, i) => `<option value="${i}">${m}</option>`).join('');
    // Year dropdown — a small range around the data
    const ySel = document.getElementById('log-year');
    const years = [2024, 2025, 2026, 2027];
    ySel.innerHTML = years.map(y => `<option value="${y}">${y}</option>`).join('');
    // Location dropdown — every place seen across all histories
    const locSel = document.getElementById('log-loc');
    locSel.innerHTML = `<option value="">All locations</option>` +
      allLocations().map(l => `<option value="${l}">${l}</option>`).join('');
    logInit = true;
  }
  document.getElementById('log-month').value = logMonth;
  document.getElementById('log-year').value = logYear;
  renderCalendar();
  renderLogSheet();
}

function onLogMonthYear() {
  logMonth = parseInt(document.getElementById('log-month').value, 10);
  logYear  = parseInt(document.getElementById('log-year').value, 10);
  // Keep the selected day if it exists in the new month, else snap to the 1st.
  const days = new Date(logYear, logMonth + 1, 0).getDate();
  if (logDay > days) logDay = 1;
  renderCalendar();
  renderLogSheet();
}

function selectLogDay(d) {
  logDay = d;
  renderCalendar();
  renderLogSheet();
}

/* Reset every filter on the Log page (keeps the selected calendar day). */
function clearLogFilters() {
  document.getElementById('log-person').value = '';
  document.getElementById('log-loc').value = '';
  document.getElementById('log-time-from').value = '';
  document.getElementById('log-time-to').value = '';
  document.getElementById('log-category').value = '';
  renderLogSheet();
}

/* Days in the selected month that actually have log entries — marked with a dot. */
function daysWithEntries() {
  const prefix = `${logYear}-${pad2(logMonth + 1)}-`;
  const set = new Set();
  allLogEntries().forEach(e => { if (e.date.startsWith(prefix)) set.add(parseInt(e.date.slice(-2), 10)); });
  return set;
}

function renderCalendar() {
  const grid = document.getElementById('log-cal');
  const firstDow = new Date(logYear, logMonth, 1).getDay();      // 0=Sun
  const daysInMonth = new Date(logYear, logMonth + 1, 0).getDate();
  const marked = daysWithEntries();

  let cells = '';
  for (let i = 0; i < firstDow; i++) cells += `<span class="cal-cell empty"></span>`;
  for (let d = 1; d <= daysInMonth; d++) {
    const sel = d === logDay ? 'selected' : '';
    const dot = marked.has(d) ? '<span class="cal-dot"></span>' : '';
    cells += `<button class="cal-cell ${sel}" onclick="selectLogDay(${d})">${d}${dot}</button>`;
  }
  grid.innerHTML = cells;
}

/* Right-side datasheet for the currently-selected day. */
function renderLogSheet() {
  if (!logInit) return;
  const dateStr = isoDate(logYear, logMonth, logDay);
  const dayName = WEEKDAYS[new Date(logYear, logMonth, logDay).getDay()];
  document.getElementById('log-sheet-date').textContent = dateStr;
  document.getElementById('log-sheet-day').textContent = dayName;
  syncLogCatButtons();   // keep the category toggle in sync with the filter value

  const person = document.getElementById('log-person').value.trim().toLowerCase();
  const loc    = document.getElementById('log-loc').value;                 // exact (dropdown)
  const cat    = document.getElementById('log-category').value;            // exact (dropdown)
  const fromMin = hhmmToMin(document.getElementById('log-time-from').value); // minutes or null
  const toMin   = hhmmToMin(document.getElementById('log-time-to').value);   // minutes or null

  const rows = allLogEntries()
    .filter(e => e.date === dateStr)
    .filter(e => !loc  || e.location === loc)
    .filter(e => !cat  || PEOPLE[e.personId].category === cat)
    .filter(e => fromMin === null || hhmmToMin(e.time) >= fromMin)
    .filter(e => toMin   === null || hhmmToMin(e.time) <= toMin)
    .filter(e => {
      if (!person) return true;
      const p = PEOPLE[e.personId];
      return (personName(p) + ' ' + p.userId + ' ' + (p.employeeId || '')).toLowerCase().includes(person);
    })
    .sort((a, b) => a.time.localeCompare(b.time)); // earliest first

  const body = document.getElementById('log-body');
  body.innerHTML = rows.length ? rows.map(e => {
    const p = PEOPLE[e.personId];
    return `
      <tr onclick="openPersonLog('${p.userId}')">
        <td><div class="avatar log-photo" style="width:52px;height:52px;font-size:16px;${e.snapshot ? photoCssUrl(e.snapshot) : photoCss(p)}">${p.initials}</div></td>
        <td>${personName(p)}</td>
        <td><span class="badge badge-${p.category}">${p.category}</span></td>
        <td class="mono">${to12h(e.time)}</td>
        <td>${e.location}</td>
      </tr>`;
  }).join('')
    : `<tr><td colspan="5" style="color:var(--text-muted)">No entries match these filters.</td></tr>`;
}

/* ---------- Report page ---------- */
let reportSearch = '';
const REPORT_GROUPS = ['Employee', 'Visitor', 'Unknown'];

/* Most recent sighting across a person's whole history (for the "Last seen" line). */
function lastSeen(p) {
  if (!p.history || !p.history.length) return null;
  return p.history.reduce((a, b) => (a.date + a.time) >= (b.date + b.time) ? a : b);
}

let unknownOpen = false;   // the Unknown group is a dropdown — collapsed by default
function toggleUnknownGroup() { unknownOpen = !unknownOpen; renderReport(); }

function renderReport() {
  const q = reportSearch.toLowerCase();
  const container = document.getElementById('report-groups');
  container.innerHTML = REPORT_GROUPS.map(cat => {
    let people = peopleByCategory(cat);
    if (q) {
      people = people.filter(p =>
        (personName(p) + ' ' + p.userId + ' ' + (p.employeeId || '') + ' ' + p.category).toLowerCase().includes(q));
    }
    if (!people.length) return '';

    const cards = `<div class="people-grid">${people.map(p => personCard(p)).join('')}</div>`;

    // Unknowns are hidden behind a dropdown so they don't flood the page — a search
    // auto-expands them so matches are never hidden.
    if (cat === 'Unknown') {
      const open = unknownOpen || !!q;
      return `
        <div class="report-section">
          <div class="report-head">
            <span class="badge badge-${cat}">${cat}</span>
            <span class="report-count">${people.length}</span>
            <button class="report-caret-btn ${open ? 'open' : ''}" onclick="toggleUnknownGroup()"
                    title="${open ? 'Hide' : 'Show'} unknown detections" aria-label="Toggle unknown detections">
              <i class="ti ti-chevron-down"></i>
            </button>
          </div>
          ${open ? cards : ''}
        </div>`;
    }

    return `
      <div class="report-section">
        <div class="report-head">
          <span class="badge badge-${cat}">${cat}</span>
          <span class="report-count">${people.length}</span>
        </div>
        ${cards}
      </div>`;
  }).join('') || `<p style="color:var(--text-muted)">No people match this search.</p>`;
}

function personCard(p) {
  const sub = p.category === 'Employee' ? `${p.employeeId} · ${p.department}`
            : p.category === 'Visitor'  ? 'Visitor'
            : 'Unidentified';
  const last = lastSeen(p);
  const lastStr = last ? `${last.location} · ${to12h(last.time)}` : '—';
  const selected = mergeSelection.has(p.userId);
  const cls = 'person-card'
    + (mergeMode ? ' selectable' : '')
    + (selected ? ' selected' : '');
  // Keep the REAL snapshot avatar (photoCss) with the initials as fallback text.
  return `
    <div class="${cls}" onclick="onPersonCardClick(event, '${p.userId}')">
      <span class="person-card-check"><i class="ti ti-check"></i></span>
      <button class="person-card-merge" title="Merge duplicates"
              onclick="event.stopPropagation(); enterMergeMode('${p.userId}')">
        <i class="ti ti-git-merge"></i>
      </button>
      <button class="person-card-del" title="Delete record"
              onclick="event.stopPropagation(); deletePerson('${p.userId}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M4 7h16"></path>
          <path d="M10 11v6M14 11v6"></path>
          <path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12"></path>
          <path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"></path>
        </svg>
      </button>
      <div class="person-card-head">
        <div class="avatar person-card-photo" style="${photoCss(p)}">${p.initials}</div>
        <div>
          <div class="name">${personName(p)}</div>
          <div class="sub">${sub}</div>
        </div>
      </div>
      <div class="kv"><span class="k">User ID</span><span class="v">${p.userId}</span></div>
      <div class="kv"><span class="k">Last seen</span><span class="v">${lastStr}</span></div>
    </div>`;
}

/* ---------- Report: merge / delete identities (wired to the Brain) ----------
   A card's merge button starts selection mode; ticking 2+ cards then "Merge into
   one" folds them into a single identity server-side. A card's trash button
   deletes that one identity. Both re-hydrate from the Brain, then re-render. */
let mergeMode = false;
const mergeSelection = new Set();

/* Card click: toggles selection while merging, otherwise opens the person log. */
function onPersonCardClick(e, userId) {
  if (mergeMode) { toggleMergeSelect(userId); return; }
  openPersonLog(userId);
}

/* The merge button on a card starts selection mode with that card pre-selected. */
function enterMergeMode(userId) {
  // First card starts merge mode with a clean selection; clicking the merge
  // button on further cards ADDS them (don't reset — that was the "always 1
  // selected" bug). Once in merge mode, clicking card BODIES also toggles.
  if (!mergeMode) { mergeMode = true; mergeSelection.clear(); }
  if (userId && PEOPLE[userId]) mergeSelection.add(userId);
  renderReport();
  updateMergeBar();
}

function toggleMergeSelect(userId) {
  if (mergeSelection.has(userId)) mergeSelection.delete(userId);
  else mergeSelection.add(userId);
  renderReport();
  updateMergeBar();
}

function exitMergeMode() {
  mergeMode = false;
  mergeSelection.clear();
  updateMergeBar();
  renderReport();
}

/* Merge-only floating bar: show while selecting, update count, enable at 2+. */
function updateMergeBar() {
  const bar = document.getElementById('merge-bar');
  if (!bar) return;
  bar.classList.toggle('open', mergeMode);
  const count = document.getElementById('merge-bar-count');
  if (count) count.textContent = mergeSelection.size;
  const go = document.getElementById('merge-bar-go');
  if (go) go.disabled = mergeSelection.size < 2;
}

/* Combine every selected record into one identity via the Brain. The primary keeps
   the strongest identity (Employee > Visitor > Unknown, then a real name, then the
   richest history); the others are folded into it. */
async function mergePeople() {
  const ids = [...mergeSelection].map(id => PEOPLE[id]).filter(x => x && x.identityId != null);
  if (ids.length < 2) { alert('Select at least two records (with a real identity) to merge.'); return; }
  const rank = { Employee: 3, Visitor: 2, Unknown: 1 };
  const people = ids.slice().sort((a, b) =>
    (rank[b.category] - rank[a.category]) ||
    ((b.name ? 1 : 0) - (a.name ? 1 : 0)) ||
    (b.history.length - a.history.length));
  const primary = people[0];
  const dups = people.slice(1);
  if (!confirm(`Merge ${people.length} records into "${personName(primary)}" (${primary.userId})? Their movement logs will be combined and the other records removed. This can't be undone.`)) return;
  const go = document.getElementById('merge-bar-go');
  if (go) { go.disabled = true; go.textContent = 'Merging…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const r = await Brain.mergeIdentities({ user: AUTH.username, pass: AUTH.password },
      primary.identityId, dups.map(p => p.identityId));
    await connectBrain();
    exitMergeMode();
    renderReport(); renderRecords(); renderLog(); renderGrid();
    alert(`Merged ${(r && r.count != null) ? r.count : dups.length} record(s) into ${personName(primary)}.`);
  } catch (e) {
    alert('Merge failed: ' + e.message);
    updateMergeBar();
  }
}

/* Delete one person's identity via the Brain (per-card trash button). */
async function deletePerson(userId) {
  const p = PEOPLE[userId];
  if (!p) return;
  if (p.identityId == null) { alert('This record has no identity to delete.'); return; }
  if (!confirm(`Delete record for ${personName(p)} (${userId})? This removes their log and any live detections. This can't be undone.`)) return;
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.deleteIdentities({ user: AUTH.username, pass: AUTH.password }, [p.identityId]);
    mergeSelection.delete(userId);
    await connectBrain();
    renderReport(); renderRecords(); renderLog(); renderGrid();
    if (mergeMode) updateMergeBar();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

/* ---------- Records ---------- */
function renderRecords() {
  // Departments dropdown (kept in sync with DEPARTMENTS)
  const deptSel = document.getElementById('records-dept');
  const curDept = deptSel.value || '';
  deptSel.innerHTML = `<option value="">All departments</option>` +
    DEPARTMENTS.map(d => `<option ${d === curDept ? 'selected' : ''}>${d}</option>`).join('');

  const q = document.getElementById('records-search').value.trim().toLowerCase();
  const dept = deptSel.value;

  // Registered persons = employees + named visitors
  const people = Object.values(PEOPLE)
    .filter(p => p.category === 'Employee' || (p.category === 'Visitor' && p.name))
    .filter(p => !dept || p.department === dept)
    .filter(p => !q || (personName(p) + ' ' + (p.employeeId || '') + ' ' + p.userId).toLowerCase().includes(q));

  const grid = document.getElementById('records-grid');
  grid.innerHTML = people.length ? people.map(p => `
    <div class="record-card" onclick="openPersonLog('${p.userId}')">
      <div class="record-card-head">
        <div class="avatar" style="${photoCss(p)}">${p.initials}</div>
        <div>
          <div class="name">${personName(p)}</div>
          <div class="dept">${p.department || p.category}</div>
        </div>
      </div>
      <div class="kv"><span class="k">${p.employeeId ? 'Employee ID' : 'User ID'}</span><span class="v">${p.employeeId || p.userId}</span></div>
      <div class="kv"><span class="k">Height</span><span class="v">${p.height}</span></div>
      <div class="kv"><span class="k">Gender / age</span><span class="v">${p.gender} / ${p.age}</span></div>
      <div class="kv"><span class="k">Features</span><span class="v">${p.features || '—'}</span></div>
    </div>`).join('')
    : `<p style="color:var(--text-muted)">No records match.</p>`;
}

/* ---------- Settings: operators ---------- */
let editingOpIndex = null;

function renderOperators() {
  const body = document.getElementById('operators-body');
  if (!body) return;   // operator management removed — single admin login only
  body.innerHTML = OPERATORS.map((o, i) => `
    <tr>
      <td class="mono">${o.username}</td>
      <td>${o.role}</td>
      <td class="mono">${o.lastLogin}</td>
      <td style="text-align:right"><button class="btn" onclick="openOpEdit(${i})"><i class="ti ti-edit"></i> Edit</button></td>
    </tr>`).join('');
}

function openOpEdit(i) {
  editingOpIndex = i;
  const o = OPERATORS[i];
  document.getElementById('op-edit-user').value = o.username;
  document.getElementById('op-edit-role').value = o.role;
  document.getElementById('op-edit-pass').value = o.password;
  document.getElementById('op-modal').classList.add('open');
}
function saveOperator() {
  const o = OPERATORS[editingOpIndex];
  o.username = document.getElementById('op-edit-user').value.trim() || o.username;
  o.role     = document.getElementById('op-edit-role').value;
  o.password = document.getElementById('op-edit-pass').value;
  renderOperators();
  closeOpEdit();
}
function deleteOperator() {
  if (!confirm(`Delete operator "${OPERATORS[editingOpIndex].username}"?`)) return;
  OPERATORS.splice(editingOpIndex, 1);
  renderOperators();
  closeOpEdit();
}
function closeOpEdit() { document.getElementById('op-modal').classList.remove('open'); }
function closeOpEditIfBackdrop(e) { if (e.target.id === 'op-modal') closeOpEdit(); }

function addOperator() {
  const user = document.getElementById('op-new-user').value.trim();
  const role = document.getElementById('op-new-role').value;
  const pass = document.getElementById('op-new-pass').value;
  if (!user || !pass) { alert('Enter a username and a password.'); return; }
  OPERATORS.push({ username: user, role, password: pass, lastLogin: '—' });
  document.getElementById('op-new-user').value = '';
  document.getElementById('op-new-pass').value = '';
  renderOperators();
}

/* ---------- Settings: departments ---------- */
function renderDepartments() {
  const list = document.getElementById('dept-list');
  list.innerHTML = DEPARTMENTS.map((d, i) => `
    <span class="chip">${d}<button class="chip-x" onclick="removeDepartment(${i})" title="Remove"><i class="ti ti-x"></i></button></span>
  `).join('');
}
function addDepartment() {
  const input = document.getElementById('dept-new');
  const val = input.value.trim();
  if (!val) return;
  if (!DEPARTMENTS.includes(val)) DEPARTMENTS.push(val);
  input.value = '';
  renderDepartments();
  renderRecords(); // refresh department dropdown
}
function removeDepartment(i) {
  DEPARTMENTS.splice(i, 1);
  renderDepartments();
  renderRecords();
}

/* ---------- Interactive background: brighten grid under the cursor ---------- */
(function initBgFx() {
  const root = document.documentElement;
  let x = -999, y = -999, queued = false;
  function apply() {
    queued = false;
    root.style.setProperty('--mx', x + 'px');
    root.style.setProperty('--my', y + 'px');
  }
  window.addEventListener('pointermove', e => {
    x = e.clientX; y = e.clientY;
    if (!queued) { queued = true; requestAnimationFrame(apply); }
  }, { passive: true });
  // hide the spotlight when the pointer leaves the window
  window.addEventListener('pointerout', e => {
    if (!e.relatedTarget) { x = -999; y = -999; apply(); }
  });
})();
