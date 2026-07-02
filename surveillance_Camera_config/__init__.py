# surveillance_Camera_config — the shared camera registry for Parts 1 & 2.
#
# Public entry point is loader.load_cameras(), which joins the committed
# non-secret metadata (cameras.json) with the gitignored credentials
# (cameras.secrets.json) into ready-to-use Camera objects.
from .loader import Camera, load_cameras, build_stream_url, to_brain_records

__all__ = ["Camera", "load_cameras", "build_stream_url", "to_brain_records"]
