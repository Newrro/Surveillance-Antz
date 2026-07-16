/* app-boot.js — page-load wiring. Loaded LAST so every function it
   references (pollFeeds, tickClock, onFeedFullscreenChange) is defined.
   Extracted verbatim from the original app.js top-level statements. */

// 110 ms ≈ 9 fps effective (each poll waits for the previous via the in-flight
// guard). The pipeline writes 12 fps previews to SHM; at the old 350 ms the grid
// showed ~3 fps of them — the "laggy video" was the POLL RATE, not the pipeline.
// Pure localhost HTTP + browser JPEG decode: zero GPU cost.
setInterval(pollFeeds, 110);

setInterval(tickClock, 1000);
tickClock();

document.addEventListener('fullscreenchange', onFeedFullscreenChange);
document.addEventListener('webkitfullscreenchange', onFeedFullscreenChange);

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
