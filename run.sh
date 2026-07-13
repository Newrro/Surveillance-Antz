#!/usr/bin/env bash
# ============================================================================
# run.sh — start / stop the whole Surveillance-Antz system (native, no Docker)
# ============================================================================
# Manages the 3 app processes:
#   1. Brain   — FastAPI + Postgres + embedded Qdrant + Redis   (:8000)
#   2. UI      — Sentinel dashboard + RTSP->MJPEG bridge         (:8080)
#   3. Pipeline— perception: detect + SAM2 segment + emit to Brain
#
# Postgres + Redis are OS services (systemd) and start on boot — this script
# only checks them. Qdrant runs embedded inside the Brain (no server).
#
# Usage:
#   ./run.sh start      # start Brain + UI + pipeline (background)
#   ./run.sh stop       # stop all three
#   ./run.sh restart    # stop then start
#   ./run.sh status      # what's running + health
#   ./run.sh logs [brain|ui|pipeline]   # tail a log (Ctrl-C to exit)
#
# Choose which cameras the pipeline runs (default: all in cameras.json):
#   CAMERAS="LOCAL-STREAM"  ./run.sh start      # one camera
#   PIPELINE_ARGS="--emit"  ./run.sh start      # e.g. drop --segment (no SAM2)
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$ROOT/runtime"; LOGS="$RUNTIME/logs"; PIDS="$RUNTIME/pids"
mkdir -p "$LOGS" "$PIDS"

AI_PY="$ROOT/surveillance_AI/venv/bin/python"
BRAIN_DIR="$ROOT/surveillance_brain"
BRAIN_PY="$BRAIN_DIR/.venv/bin/uvicorn"
BRAIN_PYEXE="$BRAIN_DIR/.venv/bin/python"

# Pipeline options (override via env). Default: emit to Brain WITH SAM2 segment.
# --segment blanks the background before OSNet body ReID so the body vector
# describes the PERSON, not the shared scene — this is what keeps the SAME person
# matching across cameras. It costs GPU; if boxes lag too much on the 6GB card,
# drop it with PIPELINE_ARGS="--emit" (accuracy trade — see notes below).
PIPELINE_ARGS="${PIPELINE_ARGS:---emit --segment}"
CAMERAS="${CAMERAS:-}"          # empty = all cameras in cameras.json

# ── GPU decode (NVDEC) — offload 1440p HEVC decode from CPU to the GPU ────────
# The CPU can't decode 7×1440p HEVC streams at once; NVDEC (ffmpeg *_cuvid) does
# it on the GPU, freeing the CPU that was starving detection + preview. Requires a
# system ffmpeg with hevc_cuvid (present on this box). Force off with GPU_DECODE=0.
export GPU_DECODE="${GPU_DECODE:-1}"

# ── Detector + preview tuning ────────────────────────────────────────────────
export DET_MAX_SIDE="${DET_MAX_SIDE:-640}"    # detector input longest side (speed)
export DETECT_INTERVAL="${DETECT_INTERVAL:-0.2}"
export DET_FP16="${DET_FP16:-1}"              # half-precision detection on the GPU
export PREVIEW_W="${PREVIEW_W:-1280}"         # grid tile resolution (was 640×360)
export PREVIEW_H="${PREVIEW_H:-720}"
export PREVIEW_FPS="${PREVIEW_FPS:-12}"
export PREVIEW_QUALITY="${PREVIEW_QUALITY:-85}"  # grid JPEG quality (was 70)

# ── Identity as a lagged background service ───────────────────────────────────
# The grid shows LIVE detection boxes (foreground, high-priority GPU stream); the
# heavy face/body/SAM2 identity runs in the background (low-priority stream). It is
# still allowed to lag, but the cadence below is tuned to SAMPLE MANY FRAMES while a
# person is in view (a 3s appearance = ~15 detector frames) and to capture the photo
# EARLY — so the snapshot is a sharp, well-framed shot, not a late "already walked
# off" frame. Turn these down if the [perf] log shows the detector (grid) starving.
export IDENTITY_MIN_HITS="${IDENTITY_MIN_HITS:-1}"        # start identity almost immediately (was 3 → ~1s late)
export TRACK_MIN_HITS="${TRACK_MIN_HITS:-1}"              # OcSort confirms a track fast (was 3)
export IDENTITY_MAX_RATE="${IDENTITY_MAX_RATE:-14}"       # heavy resolves/sec across ALL cams (was 4 → the bottleneck)
export RESOLVE_INTERVAL="${RESOLVE_INTERVAL:-0.25}"       # re-probe an unresolved track this often (was 1.0 → few frames)
export IDENTITY_MAX_PROBES="${IDENTITY_MAX_PROBES:-8}"    # frames pooled (upper bound; was 3)
export IDENTITY_MIN_EMIT_PROBES="${IDENTITY_MIN_EMIT_PROBES:-3}"  # show a first label after this many face frames
export IDENTITY_LATENCY_BUDGET="${IDENTITY_LATENCY_BUDGET:-1.5}"  # re-probe an emitted track this often (was 4.0)

# ── Storage retention (bound storage/img growth on a 24/7 system) ────────────
export RETENTION_DAYS="${RETENTION_DAYS:-7}"   # delete snapshots older than this
export STORAGE_MAX_GB="${STORAGE_MAX_GB:-5}"   # hard ceiling; oldest deleted first
export PRUNE_INTERVAL_S="${PRUNE_INTERVAL_S:-3600}"
export PYTHONUNBUFFERED=1                     # live logs (no block-buffering to the log file)

log()  { printf '\033[36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }

_alive() { local pf="$PIDS/$1.pid"; [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null; }

_start_one() {  # name  workdir  logfile  cmd...
  local name="$1" wd="$2" lf="$3"; shift 3
  if _alive "$name"; then warn "$name already running (pid $(cat "$PIDS/$name.pid"))"; return 0; fi
  ( cd "$wd" && exec "$@" ) >"$lf" 2>&1 &
  echo $! > "$PIDS/$name.pid"
  ok "$name started (pid $!) → $lf"
}

_stop_one() {
  local name="$1"; local pf="$PIDS/$name.pid"
  if _alive "$name"; then
    local pid; pid="$(cat "$pf")"
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
    ok "$name stopped"
  else
    warn "$name not running"
  fi
  rm -f "$pf"
}

check_services() {
  systemctl is-active --quiet postgresql   || { err "Postgres not running: sudo systemctl start postgresql"; return 1; }
  redis-cli ping >/dev/null 2>&1           || { err "Redis not running: sudo systemctl start redis-server"; return 1; }
  ok "Postgres + Redis up"
}

cmd_start() {
  check_services || exit 1
  log "Starting Brain (:8000)…"
  ENABLE_MIDNIGHT_FLUSH=0 _start_one brain "$BRAIN_DIR" "$LOGS/brain.log" \
    "$BRAIN_PY" api.main:app --host 0.0.0.0 --port 8000
  # wait for health before starting the producer
  log "Waiting for Brain /health…"
  for i in $(seq 1 30); do
    curl -fsS http://localhost:8000/health 2>/dev/null | grep -q '"database":"ok"' && { ok "Brain healthy"; break; }
    [[ $i -eq 30 ]] && { err "Brain never became healthy — see $LOGS/brain.log"; exit 1; }
    sleep 1
  done
  log "Starting UI bridge (:8080)…"
  SENTINEL_PORT=8080 _start_one ui "$ROOT" "$LOGS/ui.log" \
    "$AI_PY" surveillance_UI/server.py
  log "Starting perception pipeline…"
  local cam_arg=(); [[ -n "$CAMERAS" ]] && cam_arg=(--cameras "$CAMERAS")
  _start_one pipeline "$ROOT/surveillance_AI" "$LOGS/pipeline.log" \
    "$AI_PY" pipeline.py "${cam_arg[@]}" $PIPELINE_ARGS --brain-url http://localhost:8000
  log "Starting storage pruner (keep ${RETENTION_DAYS}d, cap ${STORAGE_MAX_GB}GB)…"
  _start_one prune "$ROOT/surveillance_AI" "$LOGS/prune.log" \
    "$AI_PY" prune_storage.py --loop
  # DB retention (Postgres-only; the Brain's own scheduler is off in native mode).
  _start_one dbprune "$BRAIN_DIR" "$LOGS/dbprune.log" \
    "$BRAIN_PYEXE" scripts/prune_events.py --loop
  echo
  ok "All up.  Dashboard: http://localhost:8080   (admin / password123)   ·   API docs: http://localhost:8000/docs"
}

cmd_stop() { _stop_one dbprune; _stop_one prune; _stop_one pipeline; _stop_one ui; _stop_one brain; ok "All stopped (Postgres/Redis left running — they're OS services)."; }

cmd_status() {
  for n in brain ui pipeline prune dbprune; do
    if _alive "$n"; then ok "$n running (pid $(cat "$PIDS/$n.pid"))"; else warn "$n stopped"; fi
  done
  echo "--- Brain health ---"; curl -s http://localhost:8000/health 2>/dev/null || echo "(unreachable)"; echo
  echo "--- events in ledger ---"; curl -s "http://localhost:8000/events?limit=1" 2>/dev/null | head -c 120; echo
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  status)  cmd_status ;;
  logs)    tail -f "$LOGS/${2:-brain}.log" ;;
  *) echo "Usage: ./run.sh {start|stop|restart|status|logs [brain|ui|pipeline]}"; exit 1 ;;
esac
