/* ============================================================================
   api.js — Part 3 → Part 2 (Brain) integration layer
   ============================================================================
   The Sentinel UI renders from the in-memory structures in data.js (PEOPLE,
   DETECTIONS, log entries). This module hydrates those SAME structures from the
   Brain's REST API and keeps them live over WS /live — so every render function
   in app.js keeps working unchanged, just backed by real data.

   Design: OPT-IN + SAFE FALLBACK.
     - No Brain reachable → we touch nothing; the UI runs on data.js mock data.
     - Brain reachable    → we replace PEOPLE + live DETECTIONS with real data
                            and stream new events in as they happen.

   Configuring the Brain URL (first match wins):
     1. ?brain=http://host:8000   query param  (also persisted to localStorage)
     2. localStorage 'brainUrl'
     3. window.BRAIN_URL           (set inline in index.html if you like)
     4. http://localhost:8000      default (matches the Brain's compose port)

   Contracts consumed (see ../contracts/ and surveillance_brain/api/schemas.py):
     GET  /health                     -> liveness gate before we switch over
     GET  /events?limit=&label=&...   -> EventsResponse { count, events:[EventOut] }
     GET  /employees                  -> EmployeeListResponse { employees:[...] }
     GET  /person/{identity_id}       -> PersonProfile (history + sessions)
     GET  /search?q=                  -> SearchResponse (live "where is X")
     POST /employees                  -> enroll (Basic auth)
     WS   /live                       -> { type:'connected' } then { type:'event', ...EventOut }
   ========================================================================== */

const Brain = (() => {
  'use strict';

  /* ---------- config ---------- */
  function resolveBaseUrl() {
    try {
      const q = new URLSearchParams(location.search).get('brain');
      if (q) { localStorage.setItem('brainUrl', q); return q.replace(/\/$/, ''); }
      const stored = localStorage.getItem('brainUrl');
      if (stored) return stored.replace(/\/$/, '');
    } catch (_) { /* file:// with blocked storage — fall through */ }
    if (typeof window !== 'undefined' && window.BRAIN_URL) {
      return String(window.BRAIN_URL).replace(/\/$/, '');
    }
    // Default: the Brain is on the SAME host that served this page, on :8000.
    // This makes LAN access work out of the box — open http://<host>:8080 from
    // any computer and it talks to http://<host>:8000, no ?brain= needed.
    // (Only falls back to localhost for file:// where there's no host.)
    try {
      if (typeof location !== 'undefined' && location.hostname) {
        const proto = location.protocol === 'https:' ? 'https' : 'http';
        return `${proto}://${location.hostname}:8000`;
      }
    } catch (_) { /* no location — fall through */ }
    return 'http://localhost:8000';
  }

  const BASE = resolveBaseUrl();
  const WS_BASE = BASE.replace(/^http/, 'ws');

  const state = {
    base: BASE,
    connected: false,   // /health passed and hydrate() succeeded
    ws: null,
    onLiveEvent: null,   // set by app.js
  };

  /* ---------- low-level fetch helpers ---------- */
  async function getJSON(path, { timeout = 6000 } = {}) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(BASE + path, { cache: 'no-store', signal: ctrl.signal });
      if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
      return await res.json();
    } finally {
      clearTimeout(t);
    }
  }

  async function health() {
    try {
      const h = await getJSON('/health', { timeout: 3000 });
      return !!h && h.database === 'ok';
    } catch (_) {
      return false;
    }
  }

  /* ---------- shape mappers: Brain -> UI ---------- */

  // "EMP-2026-0001" / "VIS-2026-0007" -> initials-ish token for the avatar.
  function initialsFor(evt) {
    if (evt.name) {
      return evt.name.split(/\s+/).map(s => s[0]).slice(0, 2).join('').toUpperCase();
    }
    return evt.label === 'Unknown' ? '??' : (evt.person_id ? evt.person_id.slice(-2) : '??');
  }

  // A stable UI key for a person. Prefer the internal identity_id (survives
  // promote/demote); fall back to the display label; last resort a per-event id.
  function personKey(evt) {
    if (evt.identity_id != null) return `ID-${evt.identity_id}`;
    if (evt.person_id) return evt.person_id;
    return `UNK-${evt.detection_id || evt.event_id || Math.abs(hashStr(String(evt.time)))}`;
  }

  function hashStr(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; }
    return h;
  }

  // ISO time -> { date:'YYYY-MM-DD', time:'HH:MM' } in local time.
  function splitTime(iso) {
    const d = iso ? new Date(iso) : new Date();
    const pad = n => String(n).padStart(2, '0');
    return {
      date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
      time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
    };
  }

  // Human-readable location for an event. The Brain event carries a camera UID
  // (+ zone_id); the RTSP bridge's /api/cameras gives friendly names. Prefer the
  // bridge name when we can resolve it, else the UID/zone.
  function locationFor(evt, camNameById) {
    const uid = evt.camera || (evt.camera_id != null ? String(evt.camera_id) : null);
    if (uid && camNameById && camNameById[uid]) return camNameById[uid];
    return evt.camera || evt.zone_id || (evt.camera_id != null ? `Camera ${evt.camera_id}` : 'Unknown location');
  }

  // A photo URL for a detected person. A real Part 1 attaches a cropped image
  // (absolute URL) — use it when present. Otherwise fall back to a live still
  // from the camera they were seen on (served by server.py's /snapshot/<uid>,
  // same origin as this page). Returns null when neither is available.
  function photoFor(evt) {
    const s = evt.snapshot;
    if (s) {
      if (/^https?:\/\//i.test(s)) return s;          // already a full URL
      return '/' + String(s).replace(/^\/+/, '');     // 'storage/img/..' -> '/storage/img/..' (server.py serves it)
    }
    const camUid = evt.camera || null;                // fallback: live camera still
    if (camUid) return `/snapshot/${encodeURIComponent(camUid)}`;
    return null;
  }

  // Build/return a PEOPLE record for an event, creating it if new.
  function upsertPerson(people, evt, camNameById) {
    const key = personKey(evt);
    let p = people[key];
    if (!p) {
      p = people[key] = {
        userId: key,
        identityId: evt.identity_id ?? null,
        displayLabel: evt.person_id ?? null,
        initials: initialsFor(evt),
        category: evt.label || 'Unknown',
        name: evt.name || null,
        employeeId: evt.label === 'Employee' ? (evt.person_id || null) : null,
        department: null,
        features: '—',
        height: '—', gender: '—', age: '—',
        registeredDate: null,
        history: [],
      };
    } else {
      // Category NEVER downgrades: a person is Unknown only until they're first
      // recognised — once any sighting matches (Visitor) or they're an Employee,
      // they stay that. (Rank Employee > Visitor > Unknown.)
      const RANK = { Unknown: 1, Visitor: 2, Employee: 3 };
      if (evt.label && (RANK[evt.label] || 0) >= (RANK[p.category] || 0)) p.category = evt.label;
      if (evt.name) p.name = evt.name;
      if (evt.label === 'Employee' && evt.person_id) p.employeeId = evt.person_id;
    }
    // Photo = the person's NEWEST snapshot (works for both the initial hydrate,
    // which arrives newest-first, and live events that arrive later). Keeping the
    // newest also makes the derived <stem>_face.jpg most likely to exist.
    const photo = photoFor(evt);
    if (photo && (!p._photoTime || (evt.time && evt.time > p._photoTime))) {
      p.photo = photo;
      p._photoTime = evt.time || p._photoTime;
    }
    const st = splitTime(evt.time);
    const loc = locationFor(evt, camNameById);
    // De-dupe identical (date,time,location) rows so repeated pings don't pile up.
    if (!p.history.some(h => h.date === st.date && h.time === st.time && h.location === loc)) {
      p.history.push({ date: st.date, time: st.time, location: loc,
                       snapshot: evt.snapshot || null, event_id: evt.event_id ?? null });
      p.history.sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time));
    }
    return { key, person: p, loc, when: st };
  }

  /* ---------- hydrate: fill PEOPLE / DETECTIONS from the Brain ---------- */
  async function hydrate(camNameById) {
    if (!(await health())) return false;

    // Pull a generous window of recent events (newest-first per the Brain).
    let events = [];
    try {
      const resp = await getJSON('/events?limit=2000');
      events = (resp && resp.events) || [];
    } catch (e) {
      console.warn('[Brain] /events failed:', e.message);
      return false;
    }

    // Enrich with the enrolled-employee roster (names/departments) so records
    // show real employee details even before that person is seen on camera.
    let employees = [];
    try {
      const er = await getJSON('/employees');
      employees = (er && er.employees) || [];
    } catch (_) { /* non-fatal */ }

    // Rebuild PEOPLE in place (data.js declares it mutable).
    Object.keys(PEOPLE).forEach(k => delete PEOPLE[k]);

    // Seed known employees first so their details exist even with zero sightings.
    employees.forEach(emp => {
      const key = `ID-${emp.identity_id}`;
      PEOPLE[key] = {
        userId: key, identityId: emp.identity_id, displayLabel: emp.label || null,
        initials: (emp.name || '').split(/\s+/).map(s => s[0]).slice(0, 2).join('').toUpperCase() || 'EM',
        category: 'Employee', name: emp.name, employeeId: emp.label || null,
        department: emp.department || null, features: '—', height: '—', gender: '—', age: '—',
        registeredDate: emp.hired_at ? emp.hired_at.slice(0, 10) : null, history: [],
      };
      if (emp.department && !DEPARTMENTS.includes(emp.department)) DEPARTMENTS.push(emp.department);
    });

    // Fold every event into the people registry + history.
    events.forEach(evt => {
      const { person } = upsertPerson(PEOPLE, evt, camNameById);
      if (person.department && !DEPARTMENTS.includes(person.department)) DEPARTMENTS.push(person.department);
    });

    // Clear demo detection boxes — real live boxes arrive over WS /live.
    Object.keys(DETECTIONS).forEach(k => delete DETECTIONS[k]);

    state.connected = true;
    console.info(`[Brain] hydrated ${Object.keys(PEOPLE).length} people from ${events.length} events @ ${BASE}`);
    return true;
  }

  /* ---------- live WS stream ---------- */
  function connectLive(onEvent) {
    state.onLiveEvent = onEvent;
    try {
      const ws = new WebSocket(`${WS_BASE}/live`);
      state.ws = ws;
      ws.onmessage = (msg) => {
        let data;
        try { data = JSON.parse(msg.data); } catch (_) { return; }
        if (!data || data.type !== 'event') return;      // ignore the {type:'connected'} greeting
        if (data.duplicate) return;                       // suppressed by the Brain's dedup guard
        if (typeof state.onLiveEvent === 'function') state.onLiveEvent(data);
      };
      ws.onclose = () => {
        // Reconnect after a short delay; the Brain restarts / network blips.
        if (state.connected) setTimeout(() => connectLive(state.onLiveEvent), 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch (_) { /* noop */ } };
    } catch (e) {
      console.warn('[Brain] live WS unavailable:', e.message);
    }
  }

  /* Fold one live event into PEOPLE + DETECTIONS and hand the caller the person key. */
  function applyLiveEvent(evt, camNameById) {
    const { key, person, loc } = upsertPerson(PEOPLE, evt, camNameById);
    // Surface the person on the camera's live overlay. The Brain event has no
    // pixel box, so we place a centered marker; Part 1 can later send real boxes.
    const camId = evt.camera || (evt.camera_id != null ? String(evt.camera_id) : null);
    if (camId) {
      // Record recent activity for the tile's "N detected" badge only — NO pixel
      // box. The real detection boxes are drawn on the video by Part 1; entries
      // here are TTL'd so the badge shows current activity, not a running total.
      const now = Date.now();
      const TTL = 8000;
      const arr = (DETECTIONS[camId] || [])
        .filter(d => now - (d.t || 0) < TTL && d.personId !== key);
      arr.push({ personId: key, t: now });
      DETECTIONS[camId] = arr;
    }
    return { key, person, loc, camId };
  }

  /* ---------- writes ---------- */
  // Enroll an employee. Needs a Part-1-computed face_embedding + Basic auth.
  async function enrollEmployee({ name, department, email, face_embedding, body_embedding, photo_path, auth }) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/employees', {
      method: 'POST', headers,
      body: JSON.stringify({ name, department, email, face_embedding, body_embedding, photo_path }),
    });
    if (!res.ok) throw new Error(`POST /employees -> HTTP ${res.status}`);
    return res.json();
  }

  async function findLive(q) {
    return getJSON('/search?q=' + encodeURIComponent(q));
  }

  // Daily attendance register for a given day (YYYY-MM-DD, default today).
  async function attendance(date) {
    return getJSON('/attendance' + (date ? '?date=' + encodeURIComponent(date) : ''));
  }

  // Delete individual sightings (detection_events) by id.
  async function deleteSighting(auth, eventIds) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/events/delete', {
      method: 'POST', headers, body: JSON.stringify({ event_ids: eventIds }),
    });
    if (!res.ok) throw new Error(`POST /events/delete -> HTTP ${res.status}`);
    return res.json();
  }

  // Enroll an employee from uploaded face photo(s). images = array of base64.
  async function enrollEmployeePhoto({ name, department, email, images, auth }) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/employees/enroll-photo', {
      method: 'POST', headers,
      body: JSON.stringify({ name, department, email: email || null, images }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      throw new Error(detail);
    }
    return res.json();
  }

  // Wipe the whole database (people/events/sessions/embeddings). Admin Basic auth.
  async function resetDatabase(auth) {
    const headers = {};
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/admin/reset', { method: 'POST', headers });
    if (!res.ok) throw new Error(`POST /admin/reset -> HTTP ${res.status}`);
    return res.json();
  }

  // Promote a visitor to an employee (keeps identity_id + history). Admin auth.
  async function promoteToEmployee(identityId, { name, department, email }, auth) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + `/identities/${identityId}/promote`, {
      method: 'POST', headers, body: JSON.stringify({ name, department, email: email || null }),
    });
    if (!res.ok) throw new Error(`promote -> HTTP ${res.status}`);
    return res.json();
  }

  // Clear all Unknowns (unconfirmed people) — keeps confirmed Visitors/Employees.
  async function clearUnknowns(auth) {
    const headers = {};
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/admin/clear-unknowns', { method: 'POST', headers });
    if (!res.ok) throw new Error(`POST /admin/clear-unknowns -> HTTP ${res.status}`);
    return res.json();
  }

  // Manually merge duplicate identities: fold duplicateIds into primaryId.
  async function mergeIdentities(auth, primaryId, duplicateIds) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/identities/merge', {
      method: 'POST', headers,
      body: JSON.stringify({ primary_id: primaryId, duplicate_ids: duplicateIds }),
    });
    if (!res.ok) throw new Error(`POST /identities/merge -> HTTP ${res.status}`);
    return res.json();
  }

  // Permanently delete identities (sightings, sessions, vectors, row).
  async function deleteIdentities(auth, ids) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/identities/delete', {
      method: 'POST', headers, body: JSON.stringify({ identity_ids: ids }),
    });
    if (!res.ok) throw new Error(`POST /identities/delete -> HTTP ${res.status}`);
    return res.json();
  }

  // Gallery consolidation (Phase 2): fold duplicate Visitors (same face) into one.
  // apply=false → dry-run preview (safe); apply=true → perform the merges.
  async function consolidate(auth, apply = false) {
    const headers = {};
    if (auth) headers.Authorization = 'Basic ' + btoa(`${auth.user}:${auth.pass}`);
    const res = await fetch(BASE + '/admin/consolidate?apply=' + (apply ? 'true' : 'false'),
      { method: 'POST', headers });
    if (!res.ok) throw new Error(`POST /admin/consolidate -> HTTP ${res.status}`);
    return res.json();
  }

  // Give a person a friendly name, keeping their id + VIS/EMP label.
  async function setName(identityId, name) {
    const res = await fetch(BASE + `/identities/${identityId}/name`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) throw new Error(`POST /identities/${identityId}/name -> HTTP ${res.status}`);
    return res.json();
  }

  return {
    get base() { return state.base; },
    get connected() { return state.connected; },
    health, hydrate, connectLive, applyLiveEvent, enrollEmployee, findLive, resetDatabase, setName, promoteToEmployee, clearUnknowns, consolidate, mergeIdentities, deleteIdentities, enrollEmployeePhoto, attendance, deleteSighting,
    // exposed for reuse/testing
    _map: { splitTime, locationFor, personKey, upsertPerson },
  };
})();

if (typeof window !== 'undefined') window.Brain = Brain;
