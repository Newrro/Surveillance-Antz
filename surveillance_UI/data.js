/* Dummy data — replace with live API / AI pipeline output.
   Today (for the demo) is 2026-07-02; history spans 2026-07-01..02. */

const TODAY = '2026-07-02';

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
   One entry per detected identity. category: Employee | Visitor | Unknown.
   `history` is the full movement log (Date | Time | Location), chronological.
   Employees additionally carry employeeId / department / features.        */
let PEOPLE = {
  'U-10234': {
    userId: 'U-10234', initials: 'RK', category: 'Employee', name: 'Ravi Kumar',
    employeeId: 'EMP-1042', department: 'Facilities', features: 'Glasses, beard',
    height: '175 cm', gender: 'Male', age: 34, registeredDate: '2022-03-11',
    history: [
      { date: '2026-07-01', time: '09:05', location: 'Front Gate Right' },
      { date: '2026-07-01', time: '12:40', location: 'Sanjeevan Inside' },
      { date: '2026-07-02', time: '09:14', location: 'Front Gate Right' },
      { date: '2026-07-02', time: '09:17', location: 'Building Front Pathway' },
      { date: '2026-07-02', time: '09:32', location: 'Caviland Front' },
    ],
  },
  'U-10237': {
    userId: 'U-10237', initials: 'AK', category: 'Employee', name: 'Anita Krishnan',
    employeeId: 'EMP-0871', department: 'Security', features: 'None noted',
    height: '162 cm', gender: 'Female', age: 27, registeredDate: '2021-11-02',
    history: [
      { date: '2026-07-01', time: '08:10', location: 'Front Gate Right' },
      { date: '2026-07-02', time: '08:02', location: 'Front Gate Right' },
      { date: '2026-07-02', time: '08:05', location: 'Caviland Front' },
    ],
  },
  'U-10240': {
    userId: 'U-10240', initials: 'MJ', category: 'Employee', name: 'Manoj Joseph',
    employeeId: 'EMP-0555', department: 'Logistics', features: 'Tattoo, left arm',
    height: '180 cm', gender: 'Male', age: 41, registeredDate: '2019-06-20',
    history: [
      { date: '2026-07-01', time: '07:50', location: 'Front Gate Outside Left' },
      { date: '2026-07-02', time: '07:55', location: 'Front Gate Outside Left' },
      { date: '2026-07-02', time: '08:10', location: 'Building Front Pathway' },
    ],
  },
  'U-10241': {
    userId: 'U-10241', initials: 'SN', category: 'Employee', name: 'Sana Nair',
    employeeId: 'EMP-1190', department: 'Facilities', features: 'None noted',
    height: '158 cm', gender: 'Female', age: 25, registeredDate: '2023-01-09',
    history: [
      { date: '2026-07-02', time: '10:20', location: 'Front Gate Right' },
      { date: '2026-07-02', time: '10:25', location: 'Sanjeevan Inside Front' },
    ],
  },
  'U-10235': {
    userId: 'U-10235', initials: 'PS', category: 'Visitor', name: 'Priya Sharma',
    employeeId: null, department: null, features: 'Red bag',
    height: '168 cm', gender: 'Female', age: 29, registeredDate: null,
    history: [
      { date: '2026-07-02', time: '10:05', location: 'Front Gate Inside Left' },
      { date: '2026-07-02', time: '10:07', location: 'Building Front Pathway' },
      { date: '2026-07-02', time: '10:32', location: 'Front Gate Inside Left' },
    ],
  },
  'U-10242': {
    userId: 'U-10242', initials: 'KR', category: 'Visitor', name: 'Karan Rao',
    employeeId: null, department: null, features: 'Cap',
    height: '172 cm', gender: 'Male', age: 38, registeredDate: null,
    history: [
      { date: '2026-07-01', time: '14:10', location: 'Front Gate Right' },
      { date: '2026-07-01', time: '14:40', location: 'Caviland Front' },
    ],
  },
  'U-10236': {
    userId: 'U-10236', initials: '??', category: 'Unknown', name: null,
    employeeId: null, department: null, features: 'Dark jacket',
    height: '180 cm (est.)', gender: 'Male (est.)', age: 40, registeredDate: null,
    history: [
      { date: '2026-07-02', time: '10:41', location: 'Building Front Pathway' },
    ],
  },
  'U-10238': {
    userId: 'U-10238', initials: '??', category: 'Unknown', name: null,
    employeeId: null, department: null, features: 'Hood',
    height: '170 cm (est.)', gender: 'Male (est.)', age: 35, registeredDate: null,
    history: [
      { date: '2026-07-02', time: '11:20', location: 'Sanjeevan Inside' },
    ],
  },
};

/* Detected persons currently on camera, positioned as % coords for the overlay
   box. Keyed by camera id; personId references PEOPLE. */
const DETECTIONS = {
  'cam-front-gate-right': [
    { personId: 'U-10234', box: { top: 28, left: 40, w: 14, h: 38 } },
  ],
  'cam-building-front-pathway': [
    { personId: 'U-10235', box: { top: 20, left: 22, w: 13, h: 40 } },
    { personId: 'U-10236', box: { top: 30, left: 58, w: 13, h: 40 } },
  ],
  'cam-caviland-front': [
    { personId: 'U-10237', box: { top: 35, left: 45, w: 15, h: 40 } },
  ],
  'cam-sanjeevan-inside': [
    { personId: 'U-10238', box: { top: 25, left: 35, w: 13, h: 42 } },
  ],
};

/* Operator accounts (Settings screen) */
let OPERATORS = [
  { username: 'r.singh', role: 'Senior operator', password: 'sentinel1', lastLogin: '2026-07-02 08:45' },
  { username: 'p.desai', role: 'Operator',         password: 'sentinel2', lastLogin: '2026-07-02 06:10' },
  { username: 'admin',   role: 'Administrator',    password: 'admin',     lastLogin: '2026-07-01 22:03' },
];

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
   sighting dated today. */
function countVisitsToday() {
  return Object.values(PEOPLE).filter(p => p.history.some(h => h.date === TODAY)).length;
}

/* People currently inside the premises, with their movement trail for today.
   Same rule as countInside(): present today and last seen NOT at a gate.
   Returns [{ p, entry, last, trail }] sorted by entry time (earliest first),
   where `trail` is today's sightings in chronological order — the path the
   auto-track simulation follows across cameras. */
function peopleInside() {
  const out = [];
  Object.values(PEOPLE).forEach(p => {
    const trail = p.history
      .filter(h => h.date === TODAY)
      .sort((a, b) => a.time.localeCompare(b.time));
    if (!trail.length) return;                        // not present today
    const last = trail[trail.length - 1];
    if (/gate/i.test(last.location)) return;          // last seen at a gate → exited
    out.push({ p, entry: trail[0], last, trail });
  });
  out.sort((a, b) => a.entry.time.localeCompare(b.entry.time));
  return out;
}
