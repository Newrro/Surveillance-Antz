/* Fallback data — used only when no Brain (Part 2) is reachable. When the Brain
   is connected (see api.js), PEOPLE/DETECTIONS are replaced with real data.
   There is intentionally NO seeded person data here — real records come from
   the Brain; this file only provides the camera fallback list + the operator. */

/* Real "today" (local date, YYYY-MM-DD) so live events land on the default
   calendar day and the counters reflect the current day. */
const TODAY = (() => {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
})();

/* Fallback camera list — used only when the page is opened as a plain file
   (file://). When served by server.py the live list comes from /api/cameras,
   so keep the ids here in sync with CAMERAS in server.py. */
const CAMERAS = [
  { id: 'cam-front-gate-right',        name: 'Front Gate Right',        location: 'Perimeter — Front Gate', status: 'online' },
  { id: 'cam-front-gate-inside-left',  name: 'Front Gate Inside Left',  location: 'Perimeter — Front Gate', status: 'online' },
  { id: 'cam-front-gate-outside-left', name: 'Front Gate Outside Left', location: 'Perimeter — Front Gate', status: 'online' },
  { id: 'cam-building-front-pathway',  name: 'Building Front Pathway',  location: 'Building — Front',        status: 'online' },
  { id: 'cam-caviland-front',          name: 'Caviland Front',          location: 'Caviland — Front',        status: 'online' },
  { id: 'cam-sanjeevan-inside-front',  name: 'Sanjeevan Inside Front',  location: 'Sanjeevan — Inside',      status: 'online' },
  { id: 'cam-sanjeevan-inside',        name: 'Sanjeevan Inside',        location: 'Sanjeevan — Inside',      status: 'online' },
];

/* Departments (editable in Settings) */
let DEPARTMENTS = ['Facilities', 'Security', 'Logistics', 'Administration'];

/* ── People registry ──────────────────────────────────────────────
   Populated at runtime from the Brain (api.js). Empty by default — no seeded
   people. category: Employee | Visitor | Unknown. Each record carries a
   `history` array of {date, time, location}.                                */
let PEOPLE = {};

/* Live detection overlay boxes, keyed by camera id; personId references PEOPLE.
   Filled from the Brain's WS /live stream (api.js). Empty by default.        */
const DETECTIONS = {};

/* Operator accounts (Settings screen). The single real account — login is
   gated to these credentials in app.js (AUTH). No dummy seed operators. */
let OPERATORS = [
  { username: 'admin', role: 'Administrator', password: 'password123', lastLogin: '—' },
];

/* ── Derived helpers ──────────────────────────────────────────────── */

/* Display name for a person by category. */
function personName(p) {
  if (p.category === 'Employee') return p.name;
  if (p.category === 'Visitor')  return p.name || 'Visitor';
  return 'Unknown person';
}

/* Flat list of all movement entries across everyone: {personId, date, time, location}. */
function allLogEntries() {
  const out = [];
  Object.values(PEOPLE).forEach(p =>
    p.history.forEach(h => out.push({ personId: p.userId, date: h.date, time: h.time, location: h.location }))
  );
  return out;
}

/* People filtered by category. */
function peopleByCategory(cat) {
  return Object.values(PEOPLE).filter(p => p.category === cat);
}

/* Unique locations seen across everyone's history (for the Log location filter). */
function allLocations() {
  const set = new Set();
  Object.values(PEOPLE).forEach(p => p.history.forEach(h => set.add(h.location)));
  return Array.from(set).sort();
}

/* Count of people currently inside the premises = seen today and NOT last seen
   leaving through a gate. We treat a gate as an exit point: if someone's most
   recent sighting today is at a "Gate", they're considered to have exited. */
function countInside() {
  let n = 0;
  Object.values(PEOPLE).forEach(p => {
    const today = p.history
      .filter(h => h.date === TODAY)
      .sort((a, b) => a.time.localeCompare(b.time));
    if (!today.length) return;                 // not present today
    const last = today[today.length - 1];
    if (!/gate/i.test(last.location)) n++;     // last seen inside → still inside
  });
  return n;
}

/* Count of people who entered the premises today = anyone with at least one
   sighting today, regardless of whether they've since exited. */
function countVisitsToday() {
  let n = 0;
  Object.values(PEOPLE).forEach(p => {
    if (p.history.some(h => h.date === TODAY)) n++;
  });
  return n;
}
