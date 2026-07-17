#!/usr/bin/env bash
# ============================================================================
# reset.sh — wipe Surveillance-Antz back to a clean slate (native, no Docker)
# ============================================================================
# Clears ALL data while KEEPING cameras, code and config:
#   • PostgreSQL  — identities, visitors, employees, detection_events,
#                   presence_sessions, unknown_cases, audit_log
#                   (RESTART IDENTITY → next visitor is VIS-2026-0001 again)
#   • Qdrant      — face + body embedding vectors (embedded local store)
#   • Redis       — live presence + dedup guards
#   • Media       — storage/img (per-sighting JPEGs) + storage/profiles
#
# Self-contained: stops the stack, wipes directly (no running Brain needed),
# then restarts — so it works whether or not the system is up.
#
# Usage:
#   ./reset.sh                # confirm, full wipe, restart the stack
#   ./reset.sh -y             # skip the confirmation prompt
#   ./reset.sh --keep-media   # wipe DB/vectors/Redis only, leave JPEGs on disk
#   ./reset.sh --logs         # ALSO truncate runtime/logs/*
#   ./reset.sh --no-restart   # wipe but leave the stack stopped
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_DIR="$ROOT/surveillance_brain"
ENV_FILE="$BRAIN_DIR/.env"

log()  { printf '\033[36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }

ASSUME_YES=0; KEEP_MEDIA=0; WIPE_LOGS=0; RESTART=1
for a in "$@"; do
  case "$a" in
    -y|--yes)     ASSUME_YES=1 ;;
    --keep-media) KEEP_MEDIA=1 ;;
    --logs)       WIPE_LOGS=1 ;;
    --no-restart) RESTART=0 ;;
    *) err "unknown flag: $a"; exit 1 ;;
  esac
done

# ── read DB connection from the Brain .env (fallback to compose defaults) ──
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//; s/[[:space:]]*$//'; }
PGHOST="$(get_env POSTGRES_HOST)"; PGHOST="${PGHOST:-localhost}"
PGPORT="$(get_env POSTGRES_PORT)"; PGPORT="${PGPORT:-5432}"
PGUSER="$(get_env POSTGRES_USER)"; PGUSER="${PGUSER:-surveillance}"
PGDB="$(get_env POSTGRES_DB)";     PGDB="${PGDB:-surveillance}"
export PGPASSWORD="$(get_env POSTGRES_PASSWORD)"; PGPASSWORD="${PGPASSWORD:-surveillance}"
REDIS_DB_N="$(get_env REDIS_DB)";  REDIS_DB_N="${REDIS_DB_N:-0}"
QDRANT_DIR="$BRAIN_DIR/runtime/qdrant_local"

# ── confirm (destructive + irreversible) ──
echo
warn "This DELETES all people, events, embeddings, presence and — unless --keep-media —"
warn "every snapshot in storage/img and storage/profiles.  Cameras and code are KEPT."
if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "Type 'wipe' to confirm: " reply
  [[ "$reply" == "wipe" ]] || { err "aborted."; exit 1; }
fi

# ── 1. stop the stack so nothing repopulates mid-wipe ──
log "Stopping the stack…"
"$ROOT/run.sh" stop >/dev/null 2>&1 || true

# ── 2. PostgreSQL — truncate all DATA tables, keep cameras + alembic_version ──
log "Wiping PostgreSQL ($PGDB @ $PGHOST:$PGPORT)…"
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDB" -v ON_ERROR_STOP=1 -q <<'SQL' \
  && ok "Postgres cleared (cameras kept)" || err "Postgres wipe failed — is it running?"
TRUNCATE audit_log, detection_events, presence_sessions, unknown_cases,
         employees, visitors, identities
  RESTART IDENTITY CASCADE;
SQL

# ── 3. Qdrant — embedded local vector store (Brain recreates it on startup) ──
log "Wiping Qdrant vectors…"
rm -rf "$QDRANT_DIR" && ok "Qdrant vectors cleared" || warn "Qdrant dir not found (ok)"

# ── 4. Redis — live presence + dedup guards ──
log "Flushing Redis (db $REDIS_DB_N)…"
redis-cli -n "$REDIS_DB_N" flushdb >/dev/null 2>&1 && ok "Redis flushed" || warn "Redis flush failed (ok if down)"

# ── 5. Media on disk ──
if [[ $KEEP_MEDIA -eq 1 ]]; then
  warn "Keeping media (storage/img, storage/profiles) — new records won't reference old files."
else
  log "Deleting media (storage/img, storage/profiles)…"
  rm -rf "$ROOT"/storage/img/* "$ROOT"/storage/profiles/* 2>/dev/null
  mkdir -p "$ROOT/storage/img" "$ROOT/storage/profiles"
  ok "Media deleted"
fi

# ── 6. Logs (optional) ──
if [[ $WIPE_LOGS -eq 1 ]]; then
  log "Truncating runtime logs…"
  : > "$ROOT/runtime/logs/brain.log"    2>/dev/null || true
  : > "$ROOT/runtime/logs/ui.log"       2>/dev/null || true
  : > "$ROOT/runtime/logs/pipeline.log" 2>/dev/null || true
  ok "Logs truncated"
fi

# ── 7. restart ──
if [[ $RESTART -eq 1 ]]; then
  log "Restarting the stack…"
  "$ROOT/run.sh" start
else
  warn "Stack left stopped (--no-restart). Bring it back with: ./run.sh start"
fi
echo
ok "Reset complete — clean slate (cameras kept)."
