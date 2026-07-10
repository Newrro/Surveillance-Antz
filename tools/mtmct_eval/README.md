# MTMCT evaluation harness

Measure identity accuracy with numbers instead of eyeballing. Built as the
prerequisite for the Phase 3 roadmap work (`docs/ROADMAP.md`): every re-ID model
swap or threshold change is a knob that can **fragment** (one person → many ids)
*or* **over-merge** (many people → one id — the uniform-clothing risk). This scores
both, so a change that looks better on one clip can be proven to not be silently
merging strangers elsewhere.

## Unit: the track

The Brain resolves identity once per **track** (`detection_id`, e.g.
`PATHWAY-FRONT-t73`). So the track is the atom of evaluation: it either got the
right identity or not. Metrics weight each track by its number of detection events
(a proxy for on-screen time — standard IDF1 weighting).

## Metrics

| Metric | Ideal | Meaning |
|---|---|---|
| **IDF1 / IDP / IDR** | 100% | Standard MOT identity F1 (optimal GT↔pred matching). One number punishing both failure modes. |
| **fragmentation** | 1.00 | Distinct predicted ids per real person. High = the "same guy is N Unknowns" problem. |
| **purity** | 100% | Fraction of predicted mass mapping to a single real person. Low = strangers merged (uniform risk). |

Read a change run-to-run: **IDF1 up** is good — *but* if IDF1 rose because
**purity fell**, the change bought accuracy by merging people. That's the
regression to reject for a uniformed site.

## Workflow

1. **Fix a clip set.** Record a few minutes of multi-camera footage once and replay
   it into the pipeline for every run, so results are comparable. (Live RTSP works
   too, but then runs aren't byte-identical.)
2. **Run** the pipeline + Brain over it so `detection_events` fills.
3. **Export** the tracks and an annotation sheet:
   ```bash
   cd surveillance_brain
   ./.venv/bin/python scripts/export_tracks.py --minutes 30 --out ../eval_run
   ```
4. **Label** `eval_run/labels_template.csv`: open each `snapshot` / `full_scene`
   image, put the **same name** in `true_person` for the same real person; use
   `ignore` to drop junk/duplicate tracks. Save as `eval_run/labels.csv`.
   *This is the only manual step, and the labels are reusable across every run on
   the same clip.*
5. **Score:**
   ```bash
   python3 tools/mtmct_eval/score.py --tracks eval_run/tracks.jsonl \
                                     --labels eval_run/labels.csv --json eval_run/report.json
   ```
6. **Change one thing** (a threshold, a model), re-run 2–3 and 5, and compare
   `report.json`. Roadmap rule: land Phase 3 items **one at a time**, each measured
   here against the same labeled clip.

## Files

- `metrics.py` — pure-stdlib metrics engine + self-test (`python3 metrics.py`).
- `score.py` — CLI: join tracks + labels, print/emit the report (`--selftest` to
  verify the engine).
- `../surveillance_brain/scripts/export_tracks.py` — DB → `tracks.jsonl` +
  `labels_template.csv`.

No third-party deps — `score.py`/`metrics.py` run under any `python3`. Only the
exporter needs the Brain venv (DB access).
