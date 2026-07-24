/* app-report.js — Reports & Logs site (Report page + merge bar). Same code as
   the main console minus Records / operators / departments (Settings-only).
   Plain <script> (globals shared across files); loaded in order by index.html. */

let reportSearch = '';
/* Most recent sighting across a person's whole history (for the "Last seen" line). */
function lastSeen(p) {
  if (!p.history || !p.history.length) return null;
  return p.history.reduce((a, b) => (a.date + a.time) >= (b.date + b.time) ? a : b);
}

/* The Report shows ONE category at a time, chosen by the toggle below the search
   (Employees | Visitors | Unknowns — no "All"), just like the Log's category tabs.
   Employees is the default. */
let reportCat = 'Employee';
function setReportCat(cat) {
  reportCat = cat;
  syncReportCatButtons();
  renderReport();
}
function syncReportCatButtons() {
  ['Employee', 'Visitor', 'Unknown'].forEach(c => {
    const btn = document.getElementById('report-cat-' + c);
    if (btn) btn.classList.toggle('active', c === reportCat);
  });
}

function renderReport() {
  syncReportCatButtons();
  const q = reportSearch.toLowerCase();
  const cat = reportCat;
  const container = document.getElementById('report-groups');

  let people = peopleByCategory(cat);
  if (q) {
    people = people.filter(p =>
      (personName(p) + ' ' + p.userId + ' ' + (p.employeeId || '') + ' ' + p.category).toLowerCase().includes(q));
  }

  // Employees alphabetically by name; Visitors & Unknowns by their id number
  // ascending (VIS-2026-0007 before -0042, etc.).
  const numId = p => {
    const m = String(p.displayLabel || p.userId || '').match(/(\d+)\s*$/);
    return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER;
  };
  people = people.slice().sort(cat === 'Employee'
    ? (a, b) => personName(a).localeCompare(personName(b), undefined, { sensitivity: 'base' })
    : (a, b) => (numId(a) - numId(b)) || String(a.userId).localeCompare(String(b.userId)));

  if (!people.length) {
    const kind = cat.toLowerCase() + 's';
    container.innerHTML = `<p style="color:var(--text-muted)">${q ? `No ${kind} match this search.` : `No ${kind} yet.`}</p>`;
    return;
  }
  container.innerHTML = `<div class="people-grid">${people.map(p => personCard(p)).join('')}</div>`;
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
      <button class="person-card-merge" title="Merge this person into an employee"
              onclick="event.stopPropagation(); openMergeInto('${p.userId}')">
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
  // button on further cards ADDS them. Once in merge mode, clicking card
  // BODIES also toggles.
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
    renderReport(); renderLog();
    alert(`Merged ${(r && r.count != null) ? r.count : dups.length} record(s) into ${personName(primary)}.`);
  } catch (e) {
    alert('Merge failed: ' + e.message);
    updateMergeBar();
  }
}

/* ---------- Merge INTO an employee (searchable picker) ----------
   A card's merge button opens a searchable list of ALL employees; picking one
   folds THIS person (and their whole movement log) into that employee's identity
   server-side. This is the fragmentation-cleanup path: "this stray visitor id is
   really employee X → merge it into X". */
let mergeIntoSource = null;   // userId of the record being merged away

function openMergeInto(userId) {
  const p = PEOPLE[userId];
  if (!p) return;
  if (p.identityId == null) { alert('This record has no identity to merge.'); return; }
  mergeIntoSource = userId;
  document.getElementById('mergeinto-title').textContent = `Merge ${personName(p)} into…`;
  const search = document.getElementById('mergeinto-search');
  if (search) search.value = '';
  renderMergeIntoList('');
  document.getElementById('mergeinto-modal').classList.add('open');
  if (search) search.focus();
}

/* Render the employee list filtered by the search box (name / employee id / id). */
function renderMergeIntoList(query) {
  const q = (query || '').toLowerCase();
  const src = PEOPLE[mergeIntoSource];
  let emps = peopleByCategory('Employee')
    .filter(e => e.identityId != null && (!src || e.userId !== src.userId));
  if (q) emps = emps.filter(e =>
    (personName(e) + ' ' + (e.employeeId || '') + ' ' + e.userId).toLowerCase().includes(q));
  emps.sort((a, b) => personName(a).localeCompare(personName(b), undefined, { sensitivity: 'base' }));
  const list = document.getElementById('mergeinto-list');
  if (!list) return;
  list.innerHTML = emps.length ? emps.map(e => `
    <button class="mergeinto-item" type="button" onclick="doMergeInto('${e.userId}')">
      <span class="avatar mergeinto-photo" style="${photoCss(e)}">${e.initials}</span>
      <span class="mergeinto-who">
        <span class="mergeinto-name">${personName(e)}</span>
        <span class="mergeinto-sub">${e.employeeId || e.userId}</span>
      </span>
      <i class="ti ti-git-merge"></i>
    </button>`).join('')
    : `<p style="color:var(--text-muted);padding:12px">No employees match.</p>`;
}

function closeMergeInto() {
  document.getElementById('mergeinto-modal').classList.remove('open');
  mergeIntoSource = null;
}
function closeMergeIntoIfBackdrop(e) { if (e.target.id === 'mergeinto-modal') closeMergeInto(); }

/* Fold the source record into the chosen employee (employee is the primary that
   keeps its id, label, name and photo). */
async function doMergeInto(targetUserId) {
  const src = PEOPLE[mergeIntoSource];
  const tgt = PEOPLE[targetUserId];
  if (!src || !tgt || src.identityId == null || tgt.identityId == null) return;
  if (!confirm(`Merge ${personName(src)} (${src.userId}) into ${personName(tgt)} (${tgt.employeeId || tgt.userId})?\n${personName(src)}'s sightings fold into ${personName(tgt)} and the ${src.userId} record is removed. This can't be undone.`)) return;
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.mergeIdentities({ user: AUTH.username, pass: AUTH.password },
      tgt.identityId, [src.identityId]);
    closeMergeInto();
    await connectBrain();
    renderReport();
    if (currentView === 'log') renderLog();
    alert(`Merged into ${personName(tgt)}.`);
  } catch (e) {
    alert('Merge failed: ' + e.message);
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
    renderReport(); renderLog();
    if (mergeMode) updateMergeBar();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}
