"""
segmenter.py — SAM 2 segmentation (Part 1, Perception).

Given a frame and person boxes (from detector.py), produce a boolean mask per
person. Used to (a) draw clean mask overlays in the live viewer and (b) blank the
background out of a person crop before ReID embedding, so the body vector
describes the person and not the wall behind them.

SAM 2 is an OPTIONAL heavy dependency — masking every frame is slow on CPU/MPS.
Detection alone stays smooth; segmentation is opt-in. Install SAM 2 and point
these env vars at the checkpoint (see surveillance_AI/README.md):

    SAM2_CHECKPOINT=/path/to/sam2.1_hiera_small.pt
    SAM2_CONFIG=configs/sam2.1/sam2.1_hiera_s.yaml     # config name in the sam2 package
"""
import os

import numpy as np

# Defaults match the checkpoint the gate feed was validated on.
DEFAULT_CHECKPOINT = os.environ.get(
    "SAM2_CHECKPOINT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "sam2.1_hiera_small.pt"),
)
DEFAULT_CONFIG = os.environ.get("SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_s.yaml")


class SAM2Segmenter:
    """Lazy SAM 2 wrapper. Import + model load only happen when first constructed,
    so the rest of Part 1 runs without SAM 2 installed."""

    def __init__(self, checkpoint=None, config=None, device=None):
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        checkpoint = checkpoint or DEFAULT_CHECKPOINT
        config = config or DEFAULT_CONFIG
        if not os.path.exists(checkpoint):
            raise FileNotFoundError(
                f"SAM 2 checkpoint not found: {checkpoint}\n"
                "Download it and set SAM2_CHECKPOINT (see surveillance_AI/README.md)."
            )
        if device is None:
            try:
                from detector import pick_device      # flat: `python pipeline.py`
            except ImportError:
                from .detector import pick_device      # package: `python -m surveillance_AI...`
            device = pick_device()

        print(f"[segmenter] loading SAM 2 ({os.path.basename(checkpoint)}) on {device} ...")
        self._torch = torch
        self.predictor = SAM2ImagePredictor(build_sam2(config, checkpoint, device=device))
        print("[segmenter] ready.")

    def set_frame(self, frame_rgb):
        """Give SAM 2 the frame ONCE, then call mask_for_box() per person box."""
        self.predictor.set_image(frame_rgb)

    def mask_for_box(self, box_xyxy):
        """Return a boolean HxW mask for one box (absolute pixel xyxy).
        Call set_frame() first."""
        box = np.asarray(box_xyxy[:4], dtype=np.float32)
        with self._torch.inference_mode():
            masks, _, _ = self.predictor.predict(box=box, multimask_output=False)
        return masks[0].astype(bool)


def apply_mask_overlay(vis_bgr, mask, color=(0, 255, 0), alpha=0.6):
    """Blend a translucent colored overlay onto vis_bgr where mask is True."""
    vis_bgr[mask] = (vis_bgr[mask] * (1 - alpha) + np.array(color) * alpha).astype(np.uint8)
    return vis_bgr
