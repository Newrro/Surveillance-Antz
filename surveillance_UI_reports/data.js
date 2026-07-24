/* Live-backed state for the Reports & Logs site.
   Everything here is filled from the Brain: PEOPLE/DEPARTMENTS by Brain.hydrate()
   (GET /identities/roster + /employees), DETECTIONS by live events. The registries
   start EMPTY — when the Brain is unreachable the site shows nothing rather than
   fabricated people. (No dummy seed data.) */

/* "Today" = the viewer's real local date (YYYY-MM-DD). en-CA formats as YYYY-MM-DD.
   Drives the Log calendar default and any today-based views. */
const TODAY = new Date().toLocaleDateString('en-CA');

/* Camera id/uid -> friendly name comes from GET /api/cameras at load time
   (see app-core.js loadCameras). This fallback stays empty: on this served site
   the live list always arrives, and an empty fallback avoids showing stale names. */
const CAMERAS = [];

/* Departments — seeded from the live employee/roster departments during hydrate,
   and editable in Settings. Starts empty (no dummy departments). */
let DEPARTMENTS = [];

/* People registry — one entry per identity. Rebuilt in place by Brain.hydrate();
   starts empty. category: Employee | Visitor | Unknown. `history` is the movement
   log (Date | Time | Location), lazy-loaded per person from GET /person/{id}. */
let PEOPLE = {};

/* Live detection activity per camera id (TTL'd badges). Populated by live events;
   starts empty. Kept as an object because api.js reads/writes it. */
let DETECTIONS = {};

/* ── Derived helpers ──────────────────────────────────────────────── */

/* Display name for a person by category. */
function personName(p) {
  if (p.name) return p.name;                 // a named person shows their name in any category
  if (p.category === 'Employee') return 'Employee';
  if (p.category === 'Visitor')  return 'Visitor';
  return 'Unknown person';
}

/* Flat list of all movement entries across everyone: {personId, date, time, location}. */
function allLogEntries() {
  const out = [];
  Object.values(PEOPLE).forEach(p =>
    p.history.forEach(h => out.push({ personId: p.userId, date: h.date, time: h.time, location: h.location, snapshot: h.snapshot || null }))
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
