# Part 3 — Interface / Logs Website (`surveillance_UI`)

> Owner: Ammarath · Status: **skeleton** (not yet implemented)

The human-facing dashboard. Talks **only** to the Brain's REST + WebSocket API
(Part 2) — it never touches the AI models or the databases directly.

## Screens

| Screen | What to build | Brain API used |
|---|---|---|
| Live feed | Real-time events: "09:14 · Gate 1 · Employee Asha R." | `WS /live` |
| Log table | Searchable/filterable by date, camera, label | `GET /events?from=&to=&label=&camera=` |
| Person profile | Click an event → photos, features, full visit history | `GET /person/{identity_id}` |
| Employee enrollment | Upload photo → register employee | `POST /employees` (embedding from Part 1) |
| Alerts (optional) | Highlight Unknown / after-hours entries | derived from the event stream |

**Tech:** React (or your framework of choice), talking to the Brain on
`http://<brain-host>:8000`.

## Contracts to build against

- Event object (live feed + log table + profile history):
  [`../contracts/part2_to_part3.event.schema.json`](../contracts/part2_to_part3.event.schema.json)
- Full API surface: run the Brain and open `http://localhost:8000/docs`.

You can build the entire UI against mock data matching the event schema before
the Brain is even running — then point it at the real API.
