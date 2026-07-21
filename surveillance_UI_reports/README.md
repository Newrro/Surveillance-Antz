# Reports & Logs — public website (`surveillance_UI_reports`)

A second, stand-alone website that hosts **only the Report and Log screens** of
the Sentinel console — same design (dark control-room theme), same functions
(calendar log, filters, category toggles, person-log modal with weekly chart,
sighting photos, rename/promote/merge/delete) — packaged to be shared online
through **one free ngrok tunnel**.

No live camera video is served here (that stays on the internal console); the
person photos come from the saved `/storage` snapshots.

## How it works

```
remote browser ──▶ https://xxxx.ngrok-free.app ──▶ server.py :8090
                                                     ├── static UI (this folder)
                                                     ├── /brain/*   ──▶ Brain API :8000  (reverse proxy)
                                                     ├── /storage/* ──▶ repo-root snapshots
                                                     └── /snapshot/<cam> ──▶ pipeline SHM still (fallback photo)
```

Everything is same-origin behind the single public URL, so the free ngrok plan
(one tunnel) is enough. Because the proxy is plain HTTP, live updates use
**polling** (the roster refreshes every 15 s) instead of the console's
WebSocket — same data, slight delay.

## Run it

```powershell
# 1. Start the Brain (Part 2) as usual on :8000

# 2. Start this site
cd surveillance_UI_reports
python server.py            # serves on http://localhost:8090

# 3. Expose it online (free ngrok account, one-time authtoken setup)
ngrok http 8090
```

Or do steps 2+3 in one go: `.\run_public.ps1` (Windows) / `./run_public.sh`.

Share the printed `https://xxxx.ngrok-free.app` URL. Sign in with the same
operator account as the console (**admin / password123**).

- `REPORTS_PORT` — change the local port (default 8090).
- `BRAIN_URL` — where the proxy finds the Brain (default `http://localhost:8000`).
- If the Brain isn't running, the site still works as a static prototype on the
  demo data in `data.js` (same as the main console).

> **Note:** the login is the prototype's client-side gate and the Brain admin
> endpoints use the same fixed credentials. Anyone with the URL + password can
> view and edit the logs — share the ngrok URL accordingly, and stop the tunnel
> (Ctrl-C) when you're done.

## Files

- `server.py` — static server + `/brain` reverse proxy (stdlib only, no deps)
- `index.html` — app shell: login + Log + Report views, person-log & photo modals
- `styles/` — same design system as the main console (copied)
- `data.js` — fallback demo data (copied unchanged)
- `api.js` — Brain client; defaults to the same-origin `/brain` proxy and polls
  instead of using WS `/live`
- `app-core.js` — login/nav/clock/routing + shared helpers (trimmed: no grid)
- `app-log.js` — Log page (copied unchanged)
- `app-report.js` — Report page + merge bar (trimmed: no Records/Settings)
- `app-person.js` — person-log modal + photo popup (trimmed: no camera sidebar)
- `app-admin.js` — rename + promote-to-employee (trimmed)
- `app-boot.js` — clock + background FX (no feed polling)
- `run_public.ps1` / `run_public.sh` — start the site + ngrok together
