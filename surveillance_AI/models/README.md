# Model weights (not committed)

Drop model checkpoints here. This folder is gitignored for `*.pt` / `*.pth`.

- **SAM 2** (segmentation): download `sam2.1_hiera_small.pt` and either place it
  here (the default path `segmenter.py` looks for) or point `SAM2_CHECKPOINT` at
  it. See the "Segmentation (SAM 2) setup" section in [`../README.md`](../README.md).
- **OSNet** (body ReID): auto-downloaded by torchreid on first run — nothing to
  place here unless you want market1501-trained weights (set `MODEL_PATH` in
  [`../feature_id/config.py`](../feature_id/config.py)).
