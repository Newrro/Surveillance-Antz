#!/usr/bin/env bash
# ============================================================================
# run_all.sh — bring up all three parts of RUNG01 for an integrated test
# ============================================================================
#   Part 2 (Brain)  : docker compose (Postgres + Qdrant + Redis + FastAPI :8000)
#   Part 3 (UI)      : server.py static + MJPEG bridge (:8080), wired to the Brain
#   Part 1 (stand-in): tools/integration_sim.py streams mock detections -> Brain
#
# Real cameras + GPU are NOT required — the simulator produces the detection
# stream Part 1 would emit. Swap it for the real pipeline when it's ready.
#
# Usage:   ./run_all.sh            # start everything, stream events, tail logs
#          ./run_all.sh --no-sim   # start Brain + UI only (no mock stream)
#          ./run_all.sh down       # stop the Brain stack + background procs
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_URL="http://localhost:8000"
UI_PORT="${SENTINEL_PORT:-8080}"
PIDS=()

log()  { printf '\033[36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---- docker compose shim (v2 `docker compose` or legacy `docker-compose`) ----
compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@";
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@";
  else die "Docker Compose not found. Install Docker Desktop: https://docs.docker.com/get-docker/"; fi
}

teardown() {
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    log "Stopping UI + simulator…"
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  fi
}
trap teardown EXIT INT TERM

# ---- `down` subcommand ------------------------------------------------------
if [[ "${1:-}" == "down" ]]; then
  log "Stopping the Brain stack…"
  ( cd "$ROOT/surveillance_brain" && compose down )
  pkill -f "surveillance_UI/server.py" 2>/dev/null || true
  pkill -f "tools/integration_sim.py"  2>/dev/null || true
  log "Down."
  exit 0
fi

RUN_SIM=1
[[ "${1:-}" == "--no-sim" ]] && RUN_SIM=0

# ---- Part 2: the Brain ------------------------------------------------------
# Docker is only a convenience. If it's here we use compose; otherwise we fall
# back to native services (Postgres + Redis via Homebrew, embedded Qdrant).
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  log "Starting the Brain (Postgres + Qdrant + Redis + API) via docker compose…"
  ( cd "$ROOT/surveillance_brain"
    [[ -f .env ]] || cp .env.example .env
    compose up --build -d )
else
  warn "Docker not available — running the Brain natively (Homebrew + embedded Qdrant)."
  ( cd "$ROOT/surveillance_brain" && ./run_native.sh setup )
  log "Starting the Brain on :8000 (uvicorn, embedded Qdrant)…"
  ( cd "$ROOT/surveillance_brain" && ENABLE_MIDNIGHT_FLUSH=0 ./.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000 ) &
  PIDS+=($!)
fi

log "Waiting for the Brain to be healthy at $BRAIN_URL/health …"
for i in $(seq 1 60); do
  if curl -fsS "$BRAIN_URL/health" 2>/dev/null | grep -q '"database":"ok"'; then
    log "Brain is up."; break
  fi
  [[ $i -eq 60 ]] && die "Brain did not become healthy in time. Check its logs (docker: 'cd surveillance_brain && docker compose logs app'; native: the uvicorn output above)."
  sleep 2
done

# ---- Part 3: the UI bridge --------------------------------------------------
# LAN IP so the console is reachable from other computers on the network.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
LAN_IP="${LAN_IP:-localhost}"
log "Starting the Sentinel UI bridge on :$UI_PORT …"
if python3 -c "import cv2, numpy" 2>/dev/null; then
  SENTINEL_PORT="$UI_PORT" python3 "$ROOT/surveillance_UI/server.py" &
  PIDS+=($!)
else
  warn "opencv-python/numpy not installed — no live MJPEG feeds. Install with:"
  warn "    pip install opencv-python numpy"
  warn "Serving the UI as static files (full Brain integration, camera panels show placeholders)."
  ( cd "$ROOT/surveillance_UI" && python3 -m http.server "$UI_PORT" --bind 0.0.0.0 >/dev/null 2>&1 ) &
  PIDS+=($!)
fi
UI_URL="http://$LAN_IP:$UI_PORT"
sleep 1

# ---- Part 1 stand-in: the detection stream ---------------------------------
if [[ $RUN_SIM -eq 1 ]]; then
  if python3 -c "import requests" 2>/dev/null; then
    log "Streaming mock detections into the Brain (Part 1 stand-in)…"
    python3 "$ROOT/tools/integration_sim.py" --url "$BRAIN_URL" &
    PIDS+=($!)
  else
    warn "`requests` not installed — skipping the detection stream. Install it and run:"
    warn "    python3 tools/integration_sim.py"
  fi
fi

cat <<EOF

  ┌──────────────────────────────────────────────────────────────┐
   All parts up.
     • Sentinel UI (open on ANY computer on the LAN):
           $UI_URL
     • Brain API / docs   http://$LAN_IP:8000/docs
   Sign in:  admin / password123
   The UI auto-connects to the Brain on the same host — no ?brain=
   needed. Grid + logs + records are backed by Part 2.
   Ctrl-C to stop the UI + simulator (the Brain keeps running; use
   './run_all.sh down' to stop it too).
  └──────────────────────────────────────────────────────────────┘

EOF

# Keep the script alive so the background procs (and the trap) stay up.
wait
