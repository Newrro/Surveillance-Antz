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
  renderPlogPromote(p);   // inline "Make employee" control (Visitors only)

  // Details of the person
  const rows = [];
  if (p.category === 'Employee') {
    rows.push(['Employee ID', p.employeeId], ['Department', p.department]);
  }
  rows.push(['Category', p.category]);
  if (p.gender) rows.push(['Gender', p.gender]);
  if (p.age)    rows.push(['Age', String(p.age)]);
  if (p.height) rows.push(['Height', p.height]);
  if (p.features) rows.push(['Features', p.features]);
  rows.push(['Total sightings', String(p.history.length)]);
  document.getElementById('plog-details').innerHTML =
    `<div class="plog-card-title">Details of the person</div>` +
    rows.map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');

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

/* Left column: weekly presence chart, hours-inside summary, and day tiles. */
function renderPlogSide() {
  const p = PEOPLE[plogPersonId];
  if (!p) return;

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

  const rows = p.history
    .filter(h => h.date === dateStr)
    .filter(h => !loc || h.location === loc)
    .filter(h => fromMin === null || hhmmToMin(h.time) >= fromMin)
    .filter(h => toMin   === null || hhmmToMin(h.time) <= toMin)
    .sort((a, b) => a.time.localeCompare(b.time));

  const body = document.getElementById('plog-body');
  body.innerHTML = rows.length ? rows.map(h => `
    <tr>
      <td class="mono">${to12h(h.time)}</td>
      <td>${h.location}</td>
      <td>
        <div class="plog-row-actions">
          <div class="plog-cap" title="Click to enlarge · ${h.location}" style="cursor:pointer"
               onclick="openPersonPhoto('${h.snapshot || ''}')">
            <div class="avatar" style="width:30px;height:30px;font-size:11px;${h.snapshot ? photoCssUrl(h.snapshot) : photoCss(p)}">${p.initials}</div>
            <i class="ti ti-camera"></i>
          </div>
          ${h.event_id != null ? `<button class="plog-del-btn" title="Delete this sighting"
                onclick="deleteSightingRow(${h.event_id})"><i class="ti ti-trash"></i></button>` : ''}
        </div>
      </td>
    </tr>`).join('')
    : `<tr><td colspan="3" style="color:var(--text-muted)">No sightings on this day for these filters.</td></tr>`;
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

/* Enlarged photo popup.
   CRITICAL invariant: the Face, Full body and Full scene shown are ALWAYS the
   same sighting — one filename stem, one frame, one person. Part 1 now writes the
   triple <stem>.jpg / <stem>_face.jpg / <stem>_full.jpg atomically from the same
   frame, so deriving the face/scene from the SHOWN body's stem can never mix two
   people. We never substitute a face from a different sighting (that was the old
   "face of A on the body of B" bug). When no specific sighting is clicked we pick
   the most recent sighting that actually HAS a face and show its whole triple. */
function _showTriple(bodyUrl) {
  const bodyFig = document.getElementById('photo-body-fig');
  const bodyImg = document.getElementById('photo-body-img');
  if (!bodyUrl) {
    bodyFig.style.display = 'none';
    _probeInto(document.getElementById('photo-face-img'), document.getElementById('photo-face-fig'), '');
    _probeInto(document.getElementById('photo-full-img'), document.getElementById('photo-full-fig'), '');
    return;
  }
  const stem = bodyUrl.replace(/\.jpg(\?.*)?$/i, '');
  bodyImg.src = bodyUrl; bodyFig.style.display = '';
  _probeInto(document.getElementById('photo-face-img'),
             document.getElementById('photo-face-fig'), stem + '_face.jpg');
  _probeInto(document.getElementById('photo-full-img'),
             document.getElementById('photo-full-fig'), stem + '_full.jpg');
}

function openPersonPhoto(snapshotOverride) {
  const p = PEOPLE[plogPersonId];
  if (!p) return;
  document.getElementById('photo-name').textContent = personName(p);
  document.getElementById('photo-sub').textContent =
    p.employeeId ? `${p.userId} · ${p.employeeId}` : p.userId;
  document.getElementById('photo-modal').classList.add('open');

  // A clicked sighting is honored EXACTLY (even if it has no face) — never mixed.
  if (snapshotOverride) { _showTriple(snapshotOverride); return; }

  // No specific sighting: show the most recent one that HAS a saved face, so the
  // face and body still belong to the same frame. Fall back to the newest body
  // (face hidden) when the person has no face on file at all.
  const bodies = [...new Set([
    ...p.history.map(h => h.snapshot).filter(Boolean).slice().reverse(),
    p.photo,
  ].filter(Boolean))];
  if (!bodies.length) { _showTriple(''); return; }
  let i = 0;
  const pick = () => {
    if (i >= bodies.length) { _showTriple(bodies[0]); return; }  // none has a face
    const bodyUrl = bodies[i++];
    const probe = new Image();
    probe.onload = () => _showTriple(bodyUrl);       // this sighting has a face
    probe.onerror = pick;                            // try an older sighting
    probe.src = bodyUrl.replace(/\.jpg(\?.*)?$/i, '_face.jpg');
  };
  pick();
}
function closePersonPhoto() { document.getElementById('photo-modal').classList.remove('open'); }
function closePersonPhotoIfBackdrop(e) { if (e.target.id === 'photo-modal') closePersonPhoto(); }

/* Rename the person currently open in the log modal. Keeps their id + VIS/EMP
   label — only sets a friendly name (e.g. Visitor VIS-2026-0001 → "Akash"). */
