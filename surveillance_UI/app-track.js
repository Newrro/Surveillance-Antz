/* app-track.js — split from the original app.js (TRACK inside list + auto-track animation).
   Plain <script> (globals shared across files); loaded in order by index.html. */

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