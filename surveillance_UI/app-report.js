/* app-report.js — split from the original app.js (Report + Records + merge bar + operators/departments).
   Plain <script> (globals shared across files); loaded in order by index.html. */

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
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
                   stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M6 9l6 6 6-6"></path>
              </svg>
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
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="6" cy="12" r="2.6"></circle>
          <circle cx="18" cy="6" r="2.6"></circle>
          <circle cx="18" cy="18" r="2.6"></circle>
          <path d="M8.4 10.8 15.6 7.2"></path>
          <path d="M8.4 13.2 15.6 16.8"></path>
        </svg>
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
  renderLogSheet();   // keep the Log datasheet's merge buttons/highlights in sync
  updateMergeBar();
}

function toggleMergeSelect(userId) {
  if (mergeSelection.has(userId)) mergeSelection.delete(userId);
  else mergeSelection.add(userId);
  renderReport();
  renderLogSheet();
  updateMergeBar();
}

function exitMergeMode() {
  mergeMode = false;
  mergeSelection.clear();
  updateMergeBar();
  renderReport();
  renderLogSheet();
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
  if (!DEPARTMENTS.length) {
    list.innerHTML = '<span class="desc">No departments yet — add one below.</span>';
    return;
  }
  list.innerHTML = DEPARTMENTS.map((d, i) => `
    <span class="chip dept-chip"><button class="chip-x" onclick="removeDepartment(${i})" title="Delete department"><i class="ti ti-x"></i></button><span class="dept-name">${d}</span></span>
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
  const name = DEPARTMENTS[i];
  if (name == null) return;
  if (!confirm(`Delete department "${name}"? Existing employees keep their department label; it just won't be offered for new ones.`)) return;
  DEPARTMENTS.splice(i, 1);
  renderDepartments();
  renderRecords();
}