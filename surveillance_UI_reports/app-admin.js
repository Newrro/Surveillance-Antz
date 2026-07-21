/* app-admin.js — Reports & Logs site (rename + promote from the person-log
   modal). Same code as the main console minus the Settings-page admin actions.
   Plain <script> (globals shared across files); loaded in order by index.html. */

async function renameCurrentPerson() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  const entered = window.prompt(`Name for ${p.displayLabel || p.userId}:`, p.name || '');
  if (entered === null) return;                       // cancelled
  const name = entered.trim();
  if (BRAIN_ON && p.identityId != null) {
    try {
      await Brain.setName(p.identityId, name);
    } catch (e) {
      alert('Rename failed: ' + e.message);
      return;
    }
  }
  // Reflect locally (also covers the offline/mock case).
  p.name = name || null;
  // Naming someone means you recognise them → promote an Unknown to a Visitor.
  if (name && p.category === 'Unknown') p.category = 'Visitor';
  p.initials = name
    ? name.split(/\s+/).map(s => s[0]).slice(0, 2).join('').toUpperCase()
    : (p.displayLabel ? p.displayLabel.slice(-2) : '??');
  document.getElementById('plog-name').textContent = personName(p);
  setAvatar(document.getElementById('plog-avatar'), p);
  renderReport();
  if (currentView === 'log') renderLog();
}

/* Promote control in the person-log modal header — shown for Visitors only.
   Clicking "Make employee" swaps the button for an inline name + department form. */
function renderPlogPromote(p) {
  const el = document.getElementById('plog-promote');
  if (!el) return;
  el.innerHTML = (p.category === 'Visitor')
    ? `<button class="btn btn-primary plog-promote-btn" onclick="showPromoteToEmployee()"><i class="ti ti-user-check"></i> Make employee</button>`
    : '';
}

/* Reveal the inline promote form: a name field, a department field, Confirm/Cancel. */
function showPromoteToEmployee() {
  const el = document.getElementById('plog-promote');
  const p = PEOPLE[plogPersonId];
  if (!el || !p) return;
  const esc = s => (s || '').replace(/"/g, '&quot;');
  el.innerHTML = `
    <span class="plog-promote-form">
      <input id="plog-promote-name" type="text" placeholder="Name" value="${esc(p.name)}">
      <input id="plog-dept-sel" type="text" placeholder="Department" value="${esc(p.department || 'General')}" list="plog-dept-options">
      <datalist id="plog-dept-options">${DEPARTMENTS.map(d => `<option value="${esc(d)}"></option>`).join('')}</datalist>
      <button class="btn btn-primary" onclick="promoteCurrentPerson()"><i class="ti ti-check"></i> Confirm</button>
      <button class="btn" title="Cancel" onclick="renderPlogPromote(PEOPLE[plogPersonId])"><i class="ti ti-x"></i></button>
    </span>`;
  document.getElementById('plog-promote-name')?.focus();
}

/* Promote the Visitor currently open in the modal to an Employee (keeps id + history).
   Reads the inline promote form when it's open, else falls back to prompts. */
async function promoteCurrentPerson() {
  const p = PEOPLE[plogPersonId];
  if (!p || p.identityId == null) return;
  const nameEl = document.getElementById('plog-promote-name');
  const deptEl = document.getElementById('plog-dept-sel');
  let name = nameEl ? nameEl.value.trim() : (window.prompt('Employee name:', p.name || '') || '').trim();
  if (!name) { if (nameEl) nameEl.focus(); return; }
  let department = deptEl ? deptEl.value.trim() : (window.prompt('Department:', p.department || 'General') || '').trim();
  if (!department) department = 'General';
  try {
    const r = await Brain.promoteToEmployee(
      p.identityId, { name, department },
      { user: AUTH.username, pass: AUTH.password });
    p.category = 'Employee';
    p.name = name;
    p.department = department;
    p.employeeId = (r && r.new_label) || p.employeeId;
    p.initials = name.split(/\s+/).map(s => s[0]).slice(0, 2).join('').toUpperCase();
    if (department && !DEPARTMENTS.includes(department)) DEPARTMENTS.push(department);
    openPersonLog(plogPersonId);       // refresh the modal (badge, buttons, details)
    renderReport();
  } catch (e) {
    alert('Promote failed: ' + e.message);
  }
}

/* ---------- Settings → Add employee: upload face photo(s) → Brain embeds + enrolls.
   The uploaded photo becomes the employee's fixed, LOCKED profile picture. ---------- */
let enrollFiles = [];
function _readAsDataURL(file) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
}
function openEnroll() {
  ['enroll-name', 'enroll-empid', 'enroll-dept', 'enroll-email'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('enroll-files').value = '';
  document.getElementById('enroll-preview').innerHTML = '';
  document.getElementById('enroll-status').textContent = '';
  const dl = document.getElementById('enroll-dept-options');
  if (dl) dl.innerHTML = DEPARTMENTS.map(d => `<option value="${d}"></option>`).join('');
  enrollFiles = [];
  const inp = document.getElementById('enroll-files');
  inp.onchange = () => {
    const prev = document.getElementById('enroll-preview'); prev.innerHTML = '';
    enrollFiles = [...inp.files];
    enrollFiles.forEach(f => { const im = new Image(); im.src = URL.createObjectURL(f); im.className = 'enroll-thumb'; prev.appendChild(im); });
  };
  document.getElementById('enroll-modal').classList.add('open');
}
function closeEnroll() { document.getElementById('enroll-modal').classList.remove('open'); }
function closeEnrollIfBackdrop(e) { if (e.target.id === 'enroll-modal') closeEnroll(); }
async function doEnrollEmployee() {
  const name = document.getElementById('enroll-name').value.trim();
  const empId = document.getElementById('enroll-empid').value.trim();
  const dept = document.getElementById('enroll-dept').value.trim();
  const email = document.getElementById('enroll-email').value.trim();
  const status = document.getElementById('enroll-status');
  if (!name || !empId || !dept) { status.textContent = 'Name, employee ID and department are required.'; return; }
  if (!enrollFiles.length) { status.textContent = 'Add at least one face photo.'; return; }
  const btn = document.getElementById('enroll-submit'); btn.disabled = true; btn.textContent = 'Adding…';
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const images = await Promise.all(enrollFiles.map(_readAsDataURL));
    const r = await Brain.enrollEmployeePhoto({ name, department: dept, email, external_id: empId, images,
      auth: { user: AUTH.username, pass: AUTH.password } });
    status.textContent = `Added ${r.name} (${r.label}) from ${r.photos_used || 1} photo(s).`;
    if (dept && !DEPARTMENTS.includes(dept)) DEPARTMENTS.push(dept);
    await connectBrain(); renderReport();          // auto-update the Report immediately
    if (currentView === 'log') renderLog();
    setTimeout(closeEnroll, 1400);
  } catch (e) {
    status.textContent = 'Add failed: ' + e.message;
  } finally {
    btn.disabled = false; btn.innerHTML = '<i class="ti ti-user-plus"></i> Add employee';
  }
}

/* ---------- Edit profile (Employees): photo / name / employee-id / department ----------
   The photo only changes when a new one is uploaded here; it is then LOCKED so the
   pipeline never overwrites it with a captured frame. */
let editpFile = null;
function openEditProfile() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  editpFile = null;
  document.getElementById('editp-name').value = p.name || '';
  document.getElementById('editp-empid').value = p.externalId || p.employeeId || '';
  document.getElementById('editp-dept').value = p.department || '';
  document.getElementById('editp-email').value = p.email || '';
  document.getElementById('editp-status').textContent = '';
  const dl = document.getElementById('editp-dept-options');
  if (dl) dl.innerHTML = DEPARTMENTS.map(d => `<option value="${d}"></option>`).join('');
  const prev = document.getElementById('editp-preview');
  prev.innerHTML = p.photo ? `<img class="enroll-thumb" src="${p.photo}" alt="current photo">` : '';
  const inp = document.getElementById('editp-file');
  inp.value = '';
  inp.onchange = () => {
    editpFile = inp.files && inp.files[0] ? inp.files[0] : null;
    prev.innerHTML = editpFile ? '' : (p.photo ? `<img class="enroll-thumb" src="${p.photo}" alt="current photo">` : '');
    if (editpFile) { const im = new Image(); im.src = URL.createObjectURL(editpFile); im.className = 'enroll-thumb'; prev.appendChild(im); }
  };
  document.getElementById('editp-modal').classList.add('open');
}
function closeEditProfile() { document.getElementById('editp-modal').classList.remove('open'); }
function closeEditProfileIfBackdrop(e) { if (e.target.id === 'editp-modal') closeEditProfile(); }
async function saveProfile() {
  const p = PEOPLE[plogPersonId];
  if (!p || p.identityId == null) return;
  const name = document.getElementById('editp-name').value.trim();
  const empId = document.getElementById('editp-empid').value.trim();
  const dept = document.getElementById('editp-dept').value.trim();
  const email = document.getElementById('editp-email').value.trim();
  const status = document.getElementById('editp-status');
  if (!name || !dept) { status.textContent = 'Name and department are required.'; return; }
  const btn = document.getElementById('editp-submit'); btn.disabled = true; btn.textContent = 'Saving…';
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const auth = { user: AUTH.username, pass: AUTH.password };
    if (editpFile) {
      const dataUrl = await _readAsDataURL(editpFile);
      await Brain.setProfilePhoto(auth, p.identityId, dataUrl);
      window.__photoBust = Date.now();             // force the browser to refetch the new photo
    }
    await Brain.updateEmployee(auth, p.identityId, { name, department: dept, external_id: empId || null, email: email || null });
    if (dept && !DEPARTMENTS.includes(dept)) DEPARTMENTS.push(dept);
    status.textContent = 'Saved.';
    await connectBrain();                          // re-hydrate so the new photo/details show
    renderReport();
    if (currentView === 'log') renderLog();
    if (document.getElementById('plog-modal').classList.contains('open')) openPersonLog(plogPersonId);
    setTimeout(closeEditProfile, 800);
  } catch (e) {
    status.textContent = 'Save failed: ' + e.message;
  } finally {
    btn.disabled = false; btn.innerHTML = '<i class="ti ti-device-floppy"></i> Save changes';
  }
}

/* ---------- Settings: departments ---------- */
function renderDepartments() {
  const list = document.getElementById('dept-list');
  if (!list) return;
  list.innerHTML = DEPARTMENTS.map((d, i) => `
    <span class="chip">${d}<button class="chip-x" onclick="removeDepartment(${i})" title="Remove"><i class="ti ti-x"></i></button></span>
  `).join('');
}
function addDepartment() {
  const input = document.getElementById('dept-new');
  const val = input.value.trim();
  if (!val) return;
  if (!DEPARTMENTS.includes(val)) DEPARTMENTS.push(val);
  input.value = '';
  renderDepartments();
}
function removeDepartment(i) {
  DEPARTMENTS.splice(i, 1);
  renderDepartments();
}

/* Settings → Daily reset: clear all Unknowns (unconfirmed people), keeping
   confirmed Visitors + Employees. Same as the automatic midnight sweep. */
async function clearUnknowns() {
  if (!confirm('Clear all Unknowns? Confirmed Visitors and Employees are kept.')) return;
  const btn = document.getElementById('clear-unknowns-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Clearing…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const r = await Brain.clearUnknowns({ user: AUTH.username, pass: AUTH.password });
    await connectBrain();
    renderReport();
    if (currentView === 'log') renderLog();
    alert(`Cleared ${r.removed ?? 0} unknown(s).`);
  } catch (e) {
    alert('Clear failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-eraser"></i> Clear all unknowns'; }
  }
}

/* Settings → Merge duplicate visitors: preview the face-centroid merge plan
   (dry-run, safe) then optionally apply. Two-step so a human always sees which
   Visitor cards will be folded together before anything changes. */
function _renderConsolidatePlan(r) {
  const box = document.getElementById('consolidate-result');
  const applyBtn = document.getElementById('consolidate-apply-btn');
  if (!r.duplicates_found) {
    box.textContent = 'No duplicate visitors found — nothing to merge.';
    if (applyBtn) applyBtn.style.display = 'none';
    return;
  }
  const lines = r.merges.map(m =>
    `• keep ${m.keep_label} ← merge ${m.merged_labels.join(', ')}  (face sim ${m.similarity})`);
  box.textContent = `Found ${r.duplicates_found} duplicate(s) in ${r.clusters} cluster(s):\n` + lines.join('\n');
  if (applyBtn) applyBtn.style.display = '';   // reveal Apply now that there's a plan
}
async function consolidatePreview() {
  const btn = document.getElementById('consolidate-preview-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    _renderConsolidatePlan(await Brain.consolidate({ user: AUTH.username, pass: AUTH.password }, false));
  } catch (e) {
    document.getElementById('consolidate-result').textContent = 'Preview failed: ' + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-users-group"></i> Preview duplicates'; }
  }
}
async function consolidateApply() {
  if (!confirm('Merge the previewed duplicate Visitors? This rewrites their sightings onto one id and cannot be undone.')) return;
  const btn = document.getElementById('consolidate-apply-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Merging…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    const r = await Brain.consolidate({ user: AUTH.username, pass: AUTH.password }, true);
    await connectBrain();
    renderReport();
    if (currentView === 'log') renderLog();
    document.getElementById('consolidate-result').textContent =
      `Merged ${r.duplicates_found} duplicate(s) into ${r.clusters} identity(ies).`;
    btn.style.display = 'none';
  } catch (e) {
    document.getElementById('consolidate-result').textContent = 'Merge failed: ' + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-git-merge"></i> Apply merges'; }
  }
}

/* Settings → Danger zone: wipe the whole database (people/events/snapshots),
   keeping cameras. Uses the single admin credentials. */
async function deleteDatabase() {
  if (!confirm('Delete the ENTIRE database — every person, event and snapshot?\nCameras are kept. This cannot be undone.')) return;
  const btn = document.getElementById('wipe-db-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.resetDatabase({ user: AUTH.username, pass: AUTH.password });
    await connectBrain();                 // re-hydrate the now-empty DB
    renderReport();
    if (currentView === 'log') renderLog();
    alert('Database deleted. Starting fresh.');
  } catch (e) {
    alert('Delete failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ti ti-trash"></i> Delete entire database'; }
  }
}
