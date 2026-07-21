/* app-boot.js — page-load wiring for the Reports & Logs site. Loaded LAST so
   every function it references is defined. No camera feed polling here — this
   site has no live video. */

setInterval(tickClock, 1000);
tickClock();

/* ---------- Interactive background: brighten grid under the cursor ---------- */
(function initBgFx() {
  const root = document.documentElement;
  let x = -999, y = -999, queued = false;
  function apply() {
    queued = false;
    root.style.setProperty('--mx', x + 'px');
    root.style.setProperty('--my', y + 'px');
  }
  window.addEventListener('pointermove', e => {
    x = e.clientX; y = e.clientY;
    if (!queued) { queued = true; requestAnimationFrame(apply); }
  }, { passive: true });
  // hide the spotlight when the pointer leaves the window
  window.addEventListener('pointerout', e => {
    if (!e.relatedTarget) { x = -999; y = -999; apply(); }
  });
})();
