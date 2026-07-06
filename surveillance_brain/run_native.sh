#!/usr/bin/env bash
# ============================================================================
# run_native.sh — run the Brain (Part 2) WITHOUT Docker
# ============================================================================
# Docker is only a convenience. This script brings the Brain up on native
# services instead:
#   • PostgreSQL 16  — Homebrew (macOS) or apt/systemd (Linux)
#   • Redis          — Homebrew (macOS) or apt/systemd (Linux)
#   • Qdrant         — EMBEDDED in-process (qdrant-client local mode, no server),
#                      enabled by QDRANT_LOCAL_PATH in .env
#
# It creates a venv, installs requirements, migrates + seeds, then runs uvicorn.
# Idempotent — safe to re-run. Supports macOS (Homebrew) and Debian/Ubuntu (apt).
#
# Usage:  ./run_native.sh          # set up + run the API (foreground)
#         ./run_native.sh setup    # set up only (services + migrate + seed)
# ============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

log() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

OS="$(uname -s)"

# ============================================================================
#  SERVICE BRING-UP — Postgres + Redis (OS-specific)
# ============================================================================
if [[ "$OS" == "Darwin" ]]; then
  # ---- macOS / Homebrew -----------------------------------------------------
  PGBIN="/opt/homebrew/opt/postgresql@16/bin"
  PGDATA="/opt/homebrew/var/postgresql@16"
  command -v brew >/dev/null 2>&1 || die "Homebrew required: https://brew.sh"

  [[ -x "$PGBIN/pg_ctl" ]] || { log "Installing postgresql@16…"; brew install postgresql@16; }
  [[ -f "$PGDATA/PG_VERSION" ]] || { log "Initializing Postgres data dir…"; "$PGBIN/initdb" -D "$PGDATA" >/dev/null; }
  if ! "$PGBIN/pg_isready" -q 2>/dev/null; then
    log "Starting Postgres…"
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" start
    sleep 3
  fi
  log "Ensuring role + database (surveillance)…"
  "$PGBIN/psql" -d postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='surveillance'" | grep -q 1 \
    || "$PGBIN/psql" -d postgres -c "CREATE ROLE surveillance LOGIN PASSWORD 'surveillance' SUPERUSER;"
  "$PGBIN/psql" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='surveillance'" | grep -q 1 \
    || "$PGBIN/psql" -d postgres -c "CREATE DATABASE surveillance OWNER surveillance;"

  command -v redis-server >/dev/null 2>&1 || { log "Installing redis…"; brew install redis; }
  if ! redis-cli ping >/dev/null 2>&1; then
    log "Starting Redis…"
    redis-server --daemonize yes --port 6379
    sleep 1
  fi

elif [[ "$OS" == "Linux" ]]; then
  # ---- Debian / Ubuntu (apt + systemd) --------------------------------------
  if ! command -v psql >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
    die "Postgres and/or Redis not found. Install them first:
      sudo apt update && sudo apt install -y postgresql redis-server
    then re-run this script."
  fi

  log "Ensuring Postgres is running…"
  if ! pg_isready -q 2>/dev/null; then
    sudo systemctl start postgresql 2>/dev/null \
      || sudo pg_ctlcluster "$(pg_lsclusters -h | awk 'NR==1{print $1}')" main start 2>/dev/null \
      || die "Could not start Postgres. Start it manually: sudo systemctl start postgresql"
    sleep 2
  fi
  log "Ensuring role + database (surveillance)…"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='surveillance'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE ROLE surveillance LOGIN PASSWORD 'surveillance' SUPERUSER;"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='surveillance'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE surveillance OWNER surveillance;"

  log "Ensuring Redis is running…"
  if ! redis-cli ping >/dev/null 2>&1; then
    sudo systemctl start redis-server 2>/dev/null \
      || redis-server --daemonize yes --port 6379
    sleep 1
  fi

else
  die "Unsupported OS: $OS (this script handles macOS and Debian/Ubuntu Linux)."
fi

# ============================================================================
#  .env (embedded Qdrant)
# ============================================================================
if [[ ! -f .env ]]; then
  log "Writing .env (embedded Qdrant, local services)…"
  cp .env.example .env
  # Switch Qdrant to embedded mode + disable the scheduler (see note below).
  {
    echo ""
    echo "# --- native (Docker-free) overrides ---"
    echo "QDRANT_LOCAL_PATH=./runtime/qdrant_local"
    echo "ENABLE_MIDNIGHT_FLUSH=0"
  } >> .env
fi
mkdir -p runtime

# ============================================================================
#  venv + deps
# ============================================================================
[[ -d .venv ]] || { log "Creating venv…"; python3 -m venv .venv; }
log "Installing Python deps…"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/pip install --quiet -e .

# ============================================================================
#  migrate + seed
# ============================================================================
log "Migrating schema…"; ./.venv/bin/alembic upgrade head
log "Seeding cameras (from the shared registry)…"; ./.venv/bin/python scripts/seed.py

[[ "${1:-}" == "setup" ]] && { log "Setup complete."; exit 0; }

# ============================================================================
#  run
# ============================================================================
# Embedded Qdrant locks its on-disk path to ONE process; the scheduler would
# open a second client, so we keep it disabled here (ENABLE_MIDNIGHT_FLUSH=0).
log "Starting the Brain on :8000  (Ctrl-C to stop)…"
ENABLE_MIDNIGHT_FLUSH=0 exec ./.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
