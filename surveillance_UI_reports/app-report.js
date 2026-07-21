/* app-report.js — Reports & Logs site (Report page + merge bar). Same code as
   the main console minus Records / operators / departments (Settings-only).
   Plain <script> (globals shared across files); loaded in order by index.html. */

let reportSearch = '';
const REPORT_GROUPS = ['Employee', 'Visitor', 'Unknown'];

/* Most recent sighting across a person's whole history (for the "Last seen" line). */
function lastSeen(p) {
  if (!p.history || !p.history.length) return null;
  return p.history.reduce((a, b) => (a.date + a.time) >= (b.date + b.time) ? a : b);
}

// Every category is a collapsible dropdown so you can close one and open another
// (great on a phone). Employees/Visitors start open, Unknowns collapsed so they
// don't flood the page; a search force-expands all so matches are never hidden.
const groupOpen = { Employee: true, Visitor: true, Unknown: false };
function toggleGroup(cat) { groupOpen[cat] = !groupOpen[cat]; renderReport(); }
function toggleUnknownGroup() { toggleGroup('Unknown'); }   // back-compat alias

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
    const open = groupOpen[cat] || !!q;
    // The whole header is a big tap target; the caret also toggles (stop the
    // bubble so it doesn't double-fire).
    return `
      <div class="report-section">
        <div class="report-head" onclick="toggleGroup('${cat}')" role="button" aria-expanded="${open}">
          <span class="badge badge-${cat}">${cat}</span>
          <span class="report-count">${people.length}</span>
          <button class="report-caret-btn ${open ? 'open' : ''}"
                  onclick="event.stopPropagation();toggleGroup('${cat}')"
                  title="${open ? 'Hide' : 'Show'} ${cat}" aria-label="Toggle ${cat}">
            <i class="ti ti-chevron-down"></i>
          </button>
        </div>
        ${open ? cards : ''}
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
