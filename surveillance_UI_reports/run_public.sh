#!/usr/bin/env bash
# Start the Reports & Logs site and expose it online with a free ngrok tunnel.
# One-time setup:  ngrok config add-authtoken <YOUR_TOKEN>   (free account)
set -e
cd "$(dirname "$0")"
PORT="${REPORTS_PORT:-8090}"

python3 server.py &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT

# Free plan: one tunnel — the site proxies the Brain, so one URL is enough.
ngrok http "$PORT"
