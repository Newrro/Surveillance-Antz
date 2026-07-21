/* app-log.js — split from the original app.js (Log page: calendar + datasheet).
   Plain <script> (globals shared across files); loaded in order by index.html. */

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
