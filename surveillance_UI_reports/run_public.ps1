# Start the Reports & Logs site and expose it online with a free ngrok tunnel.
#
# One-time setup:
#   1. Install ngrok:  winget install ngrok.ngrok   (or download from https://ngrok.com/download)
#   2. Sign up (free) and connect your authtoken:
#        ngrok config add-authtoken <YOUR_TOKEN>
#
# Then just run:  .\run_public.ps1
# The public https://xxxx.ngrok-free.app URL is printed by ngrok (and shown at
# http://localhost:4040). Share that URL — login is the same admin account.

$port = if ($env:REPORTS_PORT) { $env:REPORTS_PORT } else { "8090" }

# 1) local site (static UI + Brain proxy) in its own window
Start-Process python -ArgumentList "server.py" -WorkingDirectory $PSScriptRoot

# 2) public tunnel (free plan: one tunnel — everything goes through this one URL)
ngrok http $port
