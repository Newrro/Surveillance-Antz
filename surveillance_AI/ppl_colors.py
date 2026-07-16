"""ppl_colors.py — box colours (BGR) by identity outcome. Shared by pipeline.py
   and its extracted helper modules (geometry / snapshots / payload)."""

_COL_EMP = (0, 200, 0)        # Employee → green
_COL_VIS = (0, 170, 255)      # Visitor → orange
_COL_UNKNOWN = (60, 60, 220)  # Unknown / below gate → red
_COL_PERSON = (200, 200, 200) # detected, not yet identified → grey
