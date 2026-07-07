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

# Pipeline options (override via env). Default: emit to Brain WITH SAM2 segment.
# --segment blanks the background before OSNet body ReID so the embedding
# describes the PERSON, not the (shared) camera scene. Without it, everyone on a
# camera looks alike to ReID and collapses onto one visitor id. It costs ~0.5GB
# VRAM (watch the 4GB ceiling); drop it with PIPELINE_ARGS="--emit" if you OOM.
PIPELINE_ARGS="${PIPELINE_ARGS:---emit --segment}"
CAMERAS="${CAMERAS:-}"          # empty = all cameras in cameras.json
export DET_MAX_SIDE="${DET_MAX_SIDE:-640}"   # smaller detector input = less lag
export DETECT_INTERVAL="${DETECT_INTERVAL:-0.2}"
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
  echo
  ok "All up.  Dashboard: http://localhost:8080   (admin / password123)   ·   API docs: http://localhost:8000/docs"
}

cmd_stop() { _stop_one pipeline; _stop_one ui; _stop_one brain; ok "All stopped (Postgres/Redis left running — they're OS services)."; }

cmd_status() {
  for n in brain ui pipeline; do
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
