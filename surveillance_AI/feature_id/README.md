# feature_id — body ReID + identity assignment

The second model of Part 1. Takes a **preprocessed / segmented person crop** and
answers: *who is this?* → `Employee`, `Visitor`, or `Unknown`, with a **confidence
percentage**. This is the identity brain that runs on `identify`-role cameras.

Model: **OSNet** (`osnet_x1_0`) via `torchreid` — a person Re-ID network that maps
a body crop to a 512-number embedding. Similar-looking people → similar vectors.

## Files

| File | Role |
|---|---|
| [`config.py`](config.py) | Every knob in one place: model, `MATCH_THRESHOLD` (0.75), `LEARN_CEILING`, auto-enroll, crop-size guard, labels. Tune here. |
| [`extractor.py`](extractor.py) | `Extractor.embed(crop)` → L2-normalized 512-vector. The only file that knows the model. |
| [`gallery.py`](gallery.py) | The known-people database — one human-readable `data/gallery.json`. Each person holds many "views" (angles); `best_match()` scores a new crop against all of them. |
| [`identify.py`](identify.py) | `Identifier` ties it together: guard → extract → match → threshold → label + confidence, with progressive learning + auto-enroll. |
| [`enroll.py`](enroll.py) | Register a known employee from a photo. |
| [`demo.py`](demo.py) | Offline self-test with synthetic people. |

## How identity is decided

```
crop → embed → best match in gallery (cosine similarity 0..1)
   sim >= threshold → recognised: return that person's id/label + confidence%
   sim <  threshold → Unknown → (if AUTO_ENROLL_UNKNOWN) enrolled as a new Visitor
```

`threshold` is the global `config.MATCH_THRESHOLD`, **unless** the camera sets a
`match_threshold` in `cameras.json` — then that per-camera value is used
(`Identifier.identify_embedding(emb, match_threshold=...)`). Raise it to make a
camera stricter.

### Progressive confidence — "raise the score with new angles"

When a known person matches **above threshold but below `LEARN_CEILING` (0.92)**,
that sighting is a fresh angle. `gallery.add_view()` stores it on the person's
record, so the next time they appear the best-of-all-views score is higher. Over a
few sightings, a person who first matched at ~60–75% climbs toward 90%+. Views are
capped at `MAX_VIEWS_PER_PERSON` (the most redundant one is dropped when full).

## Usage

```bash
# enroll staff once (so they're 'Employee', not auto-enrolled 'Visitor'):
python -m feature_id.enroll  EMP-001  "Asha R."  photo.jpg

# offline sanity check:
python -m feature_id.demo
```

In the live system, `../pipeline.py` calls `Identifier` on every person crop from
an `identify`-role camera. `data/gallery.json` and `data/snapshots/` are runtime
state (gitignored).

> **Note on the two "confidences":** *detection* confidence (FasterRCNN — "is this
> a person?") is separate from *identity* confidence (OSNet cosine — "is this
> Asha?"). The percentage this module reports is the identity one.
