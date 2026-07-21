/* app-person.js — split from the original app.js (person sidebar, person-log modal, chart, photo popup).
   Plain <script> (globals shared across files); loaded in order by index.html. */

function openPerson(personId) {
  currentPersonId = personId;
  const p = PEOPLE[personId];
  const entries = todayEntries(p);
  setAvatar(document.getElementById('ps-avatar'), p);
  document.getElementById('ps-name').textContent = personName(p);
  document.getElementById('ps-id').textContent = p.userId;
  document.getElementById('ps-category').innerHTML = `<span class="badge badge-${p.category}">${p.category}</span>`;
  document.getElementById('ps-empid').textContent = p.employeeId ? `${p.employeeId} · ${p.department}` : '—';
  document.getElementById('ps-entry').textContent = entries[0] ? entries[0].time : '—';
  document.getElementById('ps-trail').innerHTML = entries.map(m =>
    `<li><span class="t">${m.time}</span><span>${m.location}</span></li>`
  ).join('');

  document.getElementById('overlay').classList.add('open');
  document.getElementById('person-sidebar').classList.add('open');
}

function closeSidebar() {
  document.getElementById('overlay').classList.remove('open');
  document.getElementById('person-sidebar').classList.remove('open');
}

function openPersonLogFromSidebar() {
  if (!currentPersonId) return;
  closeSidebar();
  openPersonLog(currentPersonId);
}

/* ---------- Person log modal (Report + sidebar) ---------- */
let plogPersonId = null;
let plogYear, plogMonth, plogDay;   // selected day inside the modal (month 0-indexed)
let plogRows = [];                  // the currently-shown sighting rows (index → evidence)
let photoRowIndex = -1;             // which plogRows entry the evidence popup is showing

function openPersonLog(userId) {
  plogPersonId = userId;
  const p = PEOPLE[userId];
  setAvatar(document.getElementById('plog-avatar'), p);
  document.getElementById('plog-name').textContent = personName(p);
  document.getElementById('plog-id').textContent = p.employeeId ? `${p.userId} · ${p.employeeId}` : p.userId;
  document.getElementById('plog-badge').innerHTML = `<span class="badge badge-${p.category}">${p.category}</span>`;

  // Rename only Visitors/Employees (not Unknowns); "Make employee" only for Visitors.
  const renameBtn = document.getElementById('plog-rename');
  if (renameBtn) renameBtn.style.display = (p.category === 'Unknown') ? 'none' : '';
  // "Edit profile" (photo / name / employee-id / department) — Employees only.
  const editBtn = document.getElementById('plog-editprofile');
  if (editBtn) editBtn.style.display = (p.category === 'Employee') ? '' : 'none';
  renderPlogPromote(p);   // inline "Make employee" control (Visitors only)

  // Default the selected day to the person's most recent sighting (else today).
  const dates = p.history.map(h => h.date).sort();
  const latest = dates.length ? dates[dates.length - 1] : TODAY;
  const [ly, lm, ld] = latest.split('-').map(Number);
  plogYear = ly; plogMonth = lm - 1; plogDay = ld;

  // Month / year selectors
  const mSel = document.getElementById('plog-month');
  mSel.innerHTML = MONTH_NAMES.map((m, i) => `<option value="${i}">${m}</option>`).join('');
  const ySel = document.getElementById('plog-year');
  ySel.innerHTML = [2024, 2025, 2026, 2027].map(y => `<option value="${y}">${y}</option>`).join('');
  mSel.value = plogMonth; ySel.value = plogYear;

  // Location filter — only places this person was actually seen.
  const locs = Array.from(new Set(p.history.map(h => h.location))).sort();
  document.getElementById('plog-loc').innerHTML =
    `<option value="">All locations</option>` + locs.map(l => `<option value="${l}">${l}</option>`).join('');

  clearPersonLogFilters();   // resets time/location and renders the table
  renderPlogSide();          // details column: chart, hours, day grid
  document.getElementById('plog-modal').classList.add('open');
}

function onPlogMonthYear() {
  plogMonth = parseInt(document.getElementById('plog-month').value, 10);
  plogYear  = parseInt(document.getElementById('plog-year').value, 10);
  const days = new Date(plogYear, plogMonth + 1, 0).getDate();
  if (plogDay > days) plogDay = 1;
  renderPlogSide();
  renderPersonLog();
}

function selectPlogDay(d) {
  plogDay = d;
  renderPlogSide();
  renderPersonLog();
}

/* Person details card — Employee id, department, category, gender, and the
   number of sightings on the CURRENTLY SELECTED day (updates as the day changes). */
function renderPlogDetails(p) {
  const dateStr = isoDate(plogYear, plogMonth, plogDay);
  const dayCount = p.history.filter(h => h.date === dateStr).length;
  const rows = [];
  if (p.category === 'Employee') {
    rows.push(['Employee ID', p.employeeId || '—'], ['Department', p.department || '—']);
  }
  rows.push(['Category', p.category]);
  rows.push(['Gender', p.gender || '—']);
  rows.push(['Sightings this day', String(dayCount)]);
  document.getElementById('plog-details').innerHTML =
    `<div class="plog-card-title">Details of the person</div>` +
    rows.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}

/* Left column: weekly presence chart, hours-inside summary, and day tiles. */
function renderPlogSide() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;

  renderPlogDetails(p);   // details reflect the selected day's sighting count

  // Selected-day heading (shown on the right).
  const dateStr = isoDate(plogYear, plogMonth, plogDay);
  document.getElementById('plog-sel-date').textContent = dateStr;
  document.getElementById('plog-sel-day').textContent = WEEKDAYS[new Date(plogYear, plogMonth, plogDay).getDay()];

  // Day tiles for the selected month; days with sightings are marked.
  const daysInMonth = new Date(plogYear, plogMonth + 1, 0).getDate();
  const prefix = `${plogYear}-${pad2(plogMonth + 1)}-`;
  const marked = new Set(
    p.history.filter(h => h.date.startsWith(prefix)).map(h => parseInt(h.date.slice(-2), 10)));
  let tiles = '';
  for (let d = 1; d <= daysInMonth; d++) {
    const sel = d === plogDay ? 'selected' : '';
    const has = marked.has(d) ? 'has' : '';
    tiles += `<button class="plog-day ${sel} ${has}" onclick="selectPlogDay(${d})">${d}</button>`;
  }
  document.getElementById('plog-daygrid').innerHTML = tiles;

  renderPlogChart(p, dateStr);
}

/* Weekly report: sightings per day across the week (Sun–Sat) of the selected day. */
function renderPlogChart(p, dateStr) {
  const base = new Date(plogYear, plogMonth, plogDay);
  const sunday = new Date(base);
  sunday.setDate(base.getDate() - base.getDay());

  const week = [];
  for (let i = 0; i < 7; i++) {
    const dt = new Date(sunday);
    dt.setDate(sunday.getDate() + i);
    const iso = isoDate(dt.getFullYear(), dt.getMonth(), dt.getDate());
    week.push({ iso, dom: dt.getDate(), count: p.history.filter(h => h.date === iso).length });
  }

  const W = 300, H = 128, padL = 22, padR = 8, padT = 12, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxC = Math.max(1, ...week.map(d => d.count));
  const x = i => padL + plotW * (i / 6);
  const y = c => padT + plotH * (1 - c / maxC);

  const line = week.map((d, i) => `${x(i).toFixed(1)},${y(d.count).toFixed(1)}`).join(' ');
  const area = `${padL},${padT + plotH} ${line} ${padL + plotW},${padT + plotH}`;
  const dots = week.map((d, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(d.count).toFixed(1)}" r="3" class="plog-dot"/>`).join('');
  const xlabels = week.map((d, i) =>
    `<text x="${x(i).toFixed(1)}" y="${H - 6}" class="plog-axis" text-anchor="middle">${d.dom}</text>`).join('');
  const ymax = `<text x="${padL - 6}" y="${padT + 4}" class="plog-axis" text-anchor="end">${maxC}</text>`;
  const yzero = `<text x="${padL - 6}" y="${padT + plotH}" class="plog-axis" text-anchor="end">0</text>`;

  document.getElementById('plog-chart').innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" class="plog-svg" preserveAspectRatio="none">
      <polygon points="${area}" class="plog-area"/>
      <polyline points="${line}" class="plog-line"/>
      ${dots}${xlabels}${ymax}${yzero}
    </svg>
    <div class="plog-chart-cap">Sightings per day · week of ${dateStr}</div>`;

  // Hours of presence inside on the selected day (first → last sighting).
  const dayEntries = p.history.filter(h => h.date === dateStr).sort((a, b) => a.time.localeCompare(b.time));
  let hours;
  if (dayEntries.length >= 2) {
    const mins = hhmmToMin(dayEntries[dayEntries.length - 1].time) - hhmmToMin(dayEntries[0].time);
    hours = `${Math.floor(mins / 60)}h ${mins % 60}m`;
  } else if (dayEntries.length === 1) {
    hours = 'Single sighting';
  } else {
    hours = 'Not present';
  }
  document.getElementById('plog-hours').innerHTML =
    `<span class="plog-hours-label">Hours of presence inside</span><span class="plog-hours-val">${hours}</span>`;
}

/* Right column: this person's movements on the selected day, filtered. */
function renderPersonLog() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  const dateStr = isoDate(plogYear, plogMonth, plogDay);
  const fromMin = hhmmToMin(document.getElementById('plog-time-from').value);
  const toMin   = hhmmToMin(document.getElementById('plog-time-to').value);
  const loc     = document.getElementById('plog-loc').value;

  plogRows = p.history
    .filter(h => h.date === dateStr)
    .filter(h => !loc || h.location === loc)
    .filter(h => fromMin === null || hhmmToMin(h.time) >= fromMin)
    .filter(h => toMin   === null || hhmmToMin(h.time) <= toMin)
    .sort((a, b) => a.time.localeCompare(b.time));

  const body = document.getElementById('plog-body');
  body.innerHTML = plogRows.length ? plogRows.map((h, i) => {
    // The row thumbnail shows the captured FACE crop (falls back to the person's
    // photo, then initials). Clicking it opens the full evidence for THIS sighting.
    const faceUrl = h.face || (h.snapshot ? h.snapshot.replace(/\.jpg(\?.*)?$/i, '_face.jpg') : '');
    const thumbCss = faceUrl ? photoCssUrl(faceUrl) : photoCss(p);
    const canAct = h.event_id != null;
    return `
    <tr>
      <td class="mono">${to12h(h.time)}</td>
      <td>${h.location}</td>
      <td>
        <div class="avatar plog-face-thumb" title="Click to view evidence · ${h.location}"
             onclick="openPersonPhoto(${i})" style="${thumbCss}">${p.initials}</div>
      </td>
      <td>
        <div class="plog-row-actions">
          <button class="plog-act-btn plog-act-del" title="Delete this sighting" ${canAct ? '' : 'disabled'}
                  onclick="deleteSightingRow(${h.event_id})"><i class="ti ti-trash"></i> Delete</button>
          <button class="plog-act-btn plog-act-unknown" title="Detach this sighting into a new Unknown case" ${canAct ? '' : 'disabled'}
                  onclick="makeSightingUnknown(${h.event_id})"><i class="ti ti-help-circle"></i> Make unknown</button>
        </div>
      </td>
    </tr>`;
  }).join('')
    : `<tr><td colspan="4" style="color:var(--text-muted)">No sightings on this day for these filters.</td></tr>`;
}

/* Detach one sighting into a brand-new Unknown case (removes it from this person). */
async function makeSightingUnknown(eventId) {
  if (!confirm('Make this sighting an Unknown? It is detached from this person into a new Unknown case. This can be undone by re-merging.')) return;
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.splitCase({ user: AUTH.username, pass: AUTH.password }, [eventId]);
    await connectBrain();                       // re-hydrate so the row moves out
    const p = PEOPLE[plogPersonId];
    if (p) { renderPlogSide(); renderPersonLog(); }
    else closePersonLog();                       // that was their last sighting
    renderReport(); renderRecords();
    if (currentView === 'log') renderLog();
  } catch (e) {
    alert('Make unknown failed: ' + e.message);
  }
}

/* Delete one sighting (detection_event) from a person's log, then refresh. */
async function deleteSightingRow(eventId) {
  if (!confirm('Delete this sighting from the log? (the person is kept)')) return;
  try {
    if (!BRAIN_ON) throw new Error('Brain not connected');
    await Brain.deleteSighting({ user: AUTH.username, pass: AUTH.password }, [eventId]);
    await connectBrain();                       // re-hydrate so the row is gone
    const p = PEOPLE[plogPersonId];
    if (p) {
      renderPlogSide();
      renderPlogChart(p, isoDate(plogYear, plogMonth, plogDay));
      renderPersonLog();
    } else {
      closePersonLog();                         // that was their last sighting
    }
    renderReport(); renderRecords();
    if (currentView === 'log') renderLog();
  } catch (e) {
    alert('Delete sighting failed: ' + e.message);
  }
}

function clearPersonLogFilters() {
  document.getElementById('plog-time-from').value = '';
  document.getElementById('plog-time-to').value = '';
  document.getElementById('plog-loc').value = '';
  renderPersonLog();
}
function closePersonLog() { document.getElementById('plog-modal').classList.remove('open'); }
function closePersonLogIfBackdrop(e) { if (e.target.id === 'plog-modal') closePersonLog(); }

/* Load <url> into <imgEl> only if it actually exists; otherwise hide <figEl>.
   Used for the face/scene siblings, which don't exist for a faceless sighting. */
function _probeInto(imgEl, figEl, url) {
  figEl.style.display = 'none';
  if (!url) return;
  const probe = new Image();
  probe.onload = () => { imgEl.src = url; figEl.style.display = ''; };
  probe.onerror = () => { figEl.style.display = 'none'; };
  probe.src = url;
}

/* Evidence popup for ONE sighting: the captured Face and Full body share a single
   frame (same stem — the face can never belong to a different person than the body),
   and below them the recorded Video (clip) of that moment spans both. When a clip is
   absent we fall back to the still full-scene frame. Each panel opens full-size on
   click / the video plays inline — so they can be inspected individually. */
function _showEvidence(sighting) {
  const faceImg = document.getElementById('photo-face-img');
  const faceFig = document.getElementById('photo-face-fig');
  const bodyImg = document.getElementById('photo-body-img');
  const bodyFig = document.getElementById('photo-body-fig');
  const vFig = document.getElementById('photo-video-fig');
  const vid = document.getElementById('photo-video');
  const vImg = document.getElementById('photo-video-img');
  const vCap = document.getElementById('photo-video-cap');

  const body  = sighting && (sighting.body || sighting.snapshot);
  const clip  = sighting && sighting.clip;
  // Prefer the EXPLICIT evidence paths from the API. For legacy sightings that
  // predate those columns (face/full_frame NULL) the pipeline still wrote the
  // companion crops next to the body with a shared stem
  // (<stem>.jpg / _face.jpg / _full.jpg), so derive them as a fallback. Any
  // derived file that doesn't exist is hidden by _probeInto's onerror.
  const stem  = body ? body.replace(/\.jpg(\?.*)?$/i, '') : null;
  const face  = (sighting && sighting.face) || (stem ? stem + '_face.jpg' : null);
  const scene = (sighting && (sighting.full_frame_annotated || sighting.full_frame))
              || (stem ? stem + '_full.jpg' : null);

  _probeInto(faceImg, faceFig, face);
  _probeInto(bodyImg, bodyFig, body);

  // Bottom frame: prefer the recorded clip (a real video), else the still scene.
  try { vid.pause(); } catch (e) {}
  if (clip) {
    vImg.style.display = 'none';
    vid.src = clip; vid.style.display = '';
    vCap.textContent = 'Video';
    vFig.style.display = '';
  } else if (scene) {
    vid.removeAttribute('src'); vid.style.display = 'none';
    vImg.style.display = '';
    _probeInto(vImg, vFig, scene);
    vCap.textContent = 'Scene';
  } else {
    vid.removeAttribute('src'); vid.style.display = 'none';
    vImg.style.display = 'none'; vFig.style.display = 'none';
  }
}

/* Open one evidence frame full-size in its own tab (individual access). */
function openRawFrame(el) {
  const src = (el && (el.currentSrc || el.src)) || '';
  if (src) window.open(src, '_blank');
}

function openPersonPhoto(rowIndex) {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  document.getElementById('photo-modal').classList.add('open');

  let sighting = null;
  if (typeof rowIndex === 'number' && plogRows[rowIndex]) {
    sighting = plogRows[rowIndex];                       // the exact clicked sighting
    photoRowIndex = rowIndex;
  } else {
    // Header avatar: newest sighting that HAS a face, else the newest sighting.
    const rev = [...p.history].reverse();
    sighting = rev.find(h => h.face) || rev[0] || null;
    // If that sighting is one of the currently-listed rows, arrow keys can walk
    // from it; otherwise navigation starts from the top of the list.
    photoRowIndex = sighting ? plogRows.indexOf(sighting) : -1;
  }

  document.getElementById('photo-name').textContent = personName(p);
  const idSub = p.employeeId ? `${p.userId} · ${p.employeeId}` : p.userId;
  document.getElementById('photo-sub').textContent = (sighting && sighting.time)
    ? `${idSub} · ${to12h(sighting.time)}${sighting.location ? ' · ' + sighting.location : ''}`
    : idSub;

  // Show the keyboard hint only when there's more than one sighting to move between.
  const hint = document.getElementById('photo-nav-hint');
  if (hint) hint.style.display = (photoRowIndex >= 0 && plogRows.length > 1) ? '' : 'none';

  _showEvidence(sighting);
}

/* Walk to the previous/next sighting in the currently-shown log with the arrow keys
   (Down = next/later, Up = previous/earlier). Clamps at both ends of the list. */
function navigatePhoto(delta) {
  if (photoRowIndex < 0 || !plogRows.length) return;
  const next = Math.min(plogRows.length - 1, Math.max(0, photoRowIndex + delta));
  if (next !== photoRowIndex) openPersonPhoto(next);
}

/* While the evidence popup is open, Up/Down move between this person's sightings. */
document.addEventListener('keydown', (e) => {
  const modal = document.getElementById('photo-modal');
  if (!modal || !modal.classList.contains('open')) return;
  if (e.key === 'ArrowDown')      { e.preventDefault(); navigatePhoto(1); }
  else if (e.key === 'ArrowUp')   { e.preventDefault(); navigatePhoto(-1); }
});

function closePersonPhoto() {
  const vid = document.getElementById('photo-video');
  try { vid.pause(); } catch (e) {}
  document.getElementById('photo-modal').classList.remove('open');
}
function closePersonPhotoIfBackdrop(e) { if (e.target.id === 'photo-modal') closePersonPhoto(); }

/* Rename the person currently open in the log modal. Keeps their id + VIS/EMP
   label — only sets a friendly name (e.g. Visitor VIS-2026-0001 → "Akash"). */