# Identity Redesign — Face-Recognition Attendance Register

**Status:** proposed plan (2026-07-10). Supersedes the MTMCT direction in `ROADMAP.md`
Phase 3 for the *identity* concern. Written after the product spec was clarified.

---

## 1. What the system is actually for (product spec)

> "At one glance, a daily report of which **employees** and **visitors** were on campus.
> The same person must always get the **same id, any day**. **Employees** I enroll by
> feeding face photos. **Visitors** are people the AI can re-identify reliably (~99%)
> every time, and then I name them. **Unknowns** I don't care about — only surface them
> when I want to check who all came today."

Decoded into requirements:

| # | Requirement | Consequence for design |
|---|---|---|
| R1 | At-a-glance **daily attendance** of employees + visitors | Aggregate presence per identity per day; a report view is a first-class deliverable |
| R2 | **Same person → same id, across days** | Persistent gallery; visitors/employees are **never auto-deleted** (only unknowns are) |
| R3 | **Employees enrolled from face photos** | Photo-upload enrollment path (image → face embedding → identity) |
| R4 | **Visitors = reliably re-identifiable faces (~99%)**, then named | Open-set 1:N face matching tuned for **precision**; confirm-on-re-match before a visitor is "real"; human naming |
| R5 | **Unknowns are throwaway** | Anything not confidently a face-identity goes to a cheap Unknown bucket, cleared daily |
| P | **Priority = precision of employee/visitor identity** | When we name someone, we must be right. Missing a person (→ Unknown) is acceptable; a wrong merge is not |

**This is not multi-camera tracking.** A face gallery is inherently cross-camera and
cross-day: if the same face is seen on any camera on any day, it matches the same
gallery entry. We do **not** need camera geometry, homography, travel-time topology, or
body ReID to satisfy this spec. Those were solutions to a problem we don't have.

---

## 2. Why the current system produces "Frankenstein" identities

Observed: one "Visitor" whose face, body, and scene crops are three different people.
Root causes (all removed by this redesign):

1. **Greedy per-sighting matching with no abstain.** In the 0.45–0.60 similarity fog it
   force-picks match-or-mint; a coin-flip match welds a stranger onto an id, which then
   becomes a magnet.
2. **Body matched independently of face, then stapled on.** A sighting joins by face and
   backfills its body; if that frame's body crop was an occluding neighbour, the id now
   holds face-of-A + body-of-B.
3. **No tracklet purity / no quality gate.** Blurry, side-on, occluded crops enroll and
   match, and a track that jumps between two people poisons the template.
4. **No precision margin.** A single threshold can't separate "same person" from
   "look-alike" — there's no top-1-vs-top-2 check.

---

## 3. Design principles

1. **Face is the only identity key.** Body/clothing never creates or joins an identity
   (uniforms make it actively harmful). Body is optional metadata for the Unknown bucket
   at most.
2. **Precision over recall.** Better to leave someone Unknown than to name them wrong.
   Every knob is tuned toward low false-match.
3. **Quality-gate hard.** Only good, frontal, sharp, un-occluded faces enroll or match.
   A bad face produces *no identity signal* — it is not guessed.
4. **Abstain in the fog.** Open-set rejection: require a high top-1 score **and** a margin
   over top-2. The ambiguous middle stays Unknown/provisional, never auto-merges.
5. **Decide per tracklet, not per frame.** Aggregate a quality-weighted face template over
   a clean tracklet, then make one decision.
6. **Persistent, human-correctable gallery.** Employees/visitors live forever; humans name,
   merge, and delete. Only Unknowns are cleared daily.
7. **Measure everything.** Every threshold change is validated on `tools/mtmct_eval`
   (extended for cross-day precision), never eyeballed.

---

## 4. Target architecture

```
RTSP → decode → person detect → single-cam track ──► clean tracklet
                                                        │
                                    ┌───────────────────┘
                                    ▼
                         FACE: detect → align → quality gate ──(reject bad)──► drop
                                    │ (good faces only)
                                    ▼
                         per-tracklet quality-weighted face TEMPLATE
                                    │
                                    ▼
                   OPEN-SET 1:N match vs gallery (employees + visitors)
                      │                 │                    │
             top1≥T_hi & margin    T_lo<top1<T_hi or     top1<T_lo
              & consistent          low margin            │
                      ▼                 ▼                  ▼
                 ASSIGN to id      ABSTAIN → Unknown   NEW provisional
                 (attendance)      (throwaway)         (→ Visitor on
                                                        confident re-match)
```

### Stage 0 — Ingest (keep, simplified)
- RTSP decode at native res **capped ~1080p** (faces need pixels; 1440p was overkill and
  cost GPU — see `perf-storage-decisions`). Detection on a downscaled copy (already done).
- RT-DETR person detection, OcSort tracking — **keep**.
- **SAM2 OFF** (already done). Body ReID **removed from the identity path** entirely.

### Stage 1 — Face quality gate (the single biggest precision lever)
Before a face is used for anything, it must pass:
- **Detectable + alignable** frontal face (yaw within range).
- **Sharpness** above floor (variance-of-Laplacian) — already have.
- **Size** above floor (min inter-ocular / crop px).
- **Not occluded**: skip if the person box has high IoU overlap with another person box
  (prevents cropping a neighbour).
- **FIQA score** above floor. Start with the AdaFace feature-norm × sharpness proxy we
  already compute; upgrade to **CR-FIQA** (lightweight, CVPR'23) or **SER-FIQ** later.

A face that fails the gate contributes **nothing** — the tracklet may end up faceless →
Unknown. That is correct and intended.

### Stage 2 — Tracklet face template
- Pool only gate-passing face embeddings over the tracklet into one **quality-weighted
  L2-normalized template** (already implemented: `_pooled_face`).
- One template per tracklet = one decision unit.

### Stage 3 — Open-set 1:N resolution (the precision core)
Match the tracklet template against the gallery; let `s1,s2` be top-1/top-2 cosine to
*distinct* identities:
- **ASSIGN** to id1 iff `s1 ≥ T_high` **and** `s1 − s2 ≥ MARGIN`.
- **NEW provisional identity** iff `s1 < T_low`.
- **ABSTAIN → Unknown** otherwise (uncertain match, or ambiguous between two people).

`T_high`, `T_low`, `MARGIN` are **calibrated on labelled data** to hit target FPIR (false
positive identification rate), not guessed. This margin rule is what stops look-alikes and
kills the magnet effect.

**Visitor lifecycle (R4):**
- First confident, high-quality new face → **provisional visitor** (not yet shown as a
  confirmed Visitor).
- Seen again on a *different* tracklet and re-matches confidently → **confirmed Visitor**
  (this is the "AI can re-identify him reliably" bar). Now nameable by the user.
- One-off faces that never re-appear stay provisional/Unknown and age out — no junk cards.

### Stage 4 — Gallery management
- **Multi-template per identity**: store several high-quality embeddings per person
  (different angles), quality-weighted, capped (`GALLERY_MAX_VIEWS`, already added). Match
  = max over templates.
- **Employees**: templates from uploaded photos (Stage 6) + optionally auto-added
  high-quality live shots.
- **Persistent**: employees + visitors never auto-deleted. Only Unknowns cleared at
  midnight (existing job).
- **Periodic conservative re-clustering** (existing consolidator, log-only by default) to
  fix drift — high threshold + margin so it never merges different people.

### Stage 5 — Attendance aggregation & daily report (R1)
- Presence sessions per identity per day (entry/exit, cameras) — already exist.
- **Daily report view**: for a chosen day, list every **employee** and **confirmed
  visitor** seen, with first-seen, last-seen, cameras, total on-campus time, and a
  present/absent roll for employees. This is the "one glance" deliverable.
- Unknowns hidden by default; one toggle reveals "N unidentified people came today".

### Stage 6 — Employee enrollment from photos (R3)
- Admin UI: upload one or more face photos per employee + name/department.
- Endpoint: image → face detect + align + AdaFace embed (reuse the live face extractor) →
  quality-check → store as employee identity templates.
- Multiple photos → multiple templates → more robust recognition.

### Stage 7 — Unknowns (R5)
- Anything below the assign bar, faceless, or low-quality → Unknown, deduped loosely by
  track, cleared daily. Zero effort spent on them beyond "who came today" counting.

---

## 5. How we hit ~99% precision (open-set)

Ordered by impact:
1. **Quality gate** — reject bad faces; most false matches come from garbage crops.
2. **Threshold + top1/top2 margin** — open-set rejection of look-alikes and ambiguity.
3. **Multi-shot tracklet template** — cancels per-frame noise.
4. **Confirm-on-re-match** before a visitor is "real" — filters flukes.
5. **Multi-template gallery** — robust to angle/lighting.
6. **Threshold calibration on labelled data** — pick the operating point on the FPIR/FNIR
   curve for your cameras, don't guess (this is what the harness is for).
7. **Human naming + merge/delete** — the final correctness backstop.

Trade-off, stated plainly: tuning for 99% precision **increases** the number of people left
as Unknown (lower recall). Per your spec (P, R5) that's the correct trade — and the daily
report only needs the confident ones.

---

## 6. What we DELETE or demote (simplification)

- **SAM2 segmentation** — gone (done).
- **Body ReID in the identity path** — removed. `_constrained_body_relink` and body-match
  enrollment no longer influence employee/visitor identity. (Body may live on only as an
  Unknown-bucket hint, or be dropped entirely.)
- **Geometry / homography / camera topology / travel-time** — not built. Unnecessary for a
  face gallery.
- **The fragmentation-patching stack** simplifies: quality-gated templates + margin remove
  most fragmentation and over-merge at the source, so the patches matter less.

---

## 7. Data model changes

- `identities` (have): employee / visitor / unknown. Add a **provisional** flag on
  visitors (seen once, unconfirmed) distinct from confirmed.
- Face templates: Qdrant multi-vector per identity (have) + `quality` payload (added).
- **Employee photo enrollment**: endpoint + optional stored source photos.
- Attendance: `presence_sessions` (have) → drive the daily report.
- Drop/ignore body-collection usage in the identity path.

---

## 8. Metrics & targets (validate on `tools/mtmct_eval`, extended)

- **FPIR (false-positive identification rate)** — when the system names an employee/visitor,
  how often is it wrong. **Target ≤ 1%** (the "99%").
- **FNIR / recall** — how often a known person is missed (left Unknown). Track it, but it's
  allowed to be higher.
- **Cross-day stability** — same real person keeps one id across days. New harness label
  dimension: tag identities across multiple days.
- **Fragmentation & purity** — keep the existing two, watch purity (over-merge) hardest.

---

## 9. Phased implementation (each phase gated by the harness)

**Phase A — Precision core (fixes Frankenstein + fragmentation).**
Quality gate (incl. occlusion skip) → tracklet template (have) → open-set match with
`T_high`/`T_low`/`MARGIN` + abstain → remove body from identity path. Calibrate thresholds
on a labelled clip. *This is the phase that makes it "work".*

**Phase B — Employee photo enrollment.** UI upload + endpoint.

**Phase C — Daily attendance report.** The at-a-glance view (employees + confirmed visitors,
present/times, unknown count).

**Phase D — Gallery hygiene at scale.** Multi-template cap (have), conservative periodic
re-cluster, cross-day stability audit.

**Phase E — Optional quality upgrade.** CR-FIQA model for the gate; revisit only if
Phase-A numbers plateau.

---

## 10. Decisions & open questions

**Locked (2026-07-10):**
- **Operating point = MAX PRECISION.** Name a person only when nearly certain; accept that
  more people stay Unknown (lower recall). Calibrate `T_high`/`MARGIN` to the strict end of
  the FPIR curve.
- **Provisional visitors are HIDDEN until re-identified.** A new face is provisional and not
  shown as a Visitor card until the AI confidently re-matches it on a *second* tracklet.
  Only confirmed visitors appear in the report.

**Still open (don't block Phase A):**
- **Camera overlap** — do any cameras cover the same area? (Affects same-instant duplicate
  handling only.)
- **Employee scale** — how many employees to enroll? (Affects gallery size / calibration.)
```
