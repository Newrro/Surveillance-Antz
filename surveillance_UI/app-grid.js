/* app-grid.js — split from the original app.js (live grid, camera view, X-Ray, fullscreen).
   Plain <script> (globals shared across files); loaded in order by index.html. */

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
