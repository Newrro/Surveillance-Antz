"""One-off feasibility + timing probe for the live perception stack on this Mac.
Grabs a real frame from a camera, runs detection + face embed + body embed, times each."""
import os, sys, time
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
import cv2, numpy as np, torch

from surveillance_Camera_config.loader import load_cameras

def t(): return time.time()

dev = "mps" if torch.backends.mps.is_available() else "cpu"
print(f"device={dev}")

# 1) grab a real frame
cams = [c for c in load_cameras(active_only=True, streamable_only=True)]
cam = cams[0]
print(f"opening {cam.camera_uid} ...")
cap = cv2.VideoCapture(cam.stream_url, cv2.CAP_FFMPEG)
frame = None
for _ in range(30):
    ok, f = cap.read()
    if ok and f is not None: frame = f; break
cap.release()
if frame is None: print("FAIL: no frame"); sys.exit(1)
print(f"frame {frame.shape}")

# 2) detector (fasterrcnn_mobilenet on mps)
os.environ["DETECTOR_MODEL"] = "fasterrcnn_mobilenet"
from detector import PersonDetector
s=t(); det = PersonDetector(model="fasterrcnn_mobilenet"); print(f"detector load {t()-s:.1f}s")
s=t(); boxes = det.detect(frame, conf=0.5); dt=t()-s
print(f"detect {dt*1000:.0f}ms -> {len(boxes)} person boxes")
for b in boxes[:5]: print("   box", [round(x,1) for x in b])

# 3) face extractor (AdaFace + MTCNN)
os.environ.setdefault("FEATURE_ID_DEVICE", "cpu")
s=t()
from feature_id.face_extractor import FaceExtractor
fe = FaceExtractor(); print(f"face load {t()-s:.1f}s")

# 4) body embedder: torchvision resnet18 penultimate (512-d), MPS
from torchvision.models import resnet18, ResNet18_Weights
s=t()
_rw = ResNet18_Weights.DEFAULT
_body = resnet18(weights=_rw); _body.fc = torch.nn.Identity(); _body.eval().to(dev)
_prep = _rw.transforms()
print(f"body(resnet18) load {t()-s:.1f}s")
def body_embed(crop_bgr):
    rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    from PIL import Image
    x = _prep(Image.fromarray(rgb)).unsqueeze(0).to(dev)
    with torch.no_grad(): v = _body(x)[0].cpu().numpy().astype("float32")
    n = np.linalg.norm(v); return v/n if n>0 else v

# run embeds on each detected person crop
H,W = frame.shape[:2]
for i,(x1,y1,x2,y2,sc) in enumerate(boxes[:3]):
    crop = frame[max(0,int(y1)):int(y2), max(0,int(x1)):int(x2)]
    if crop.size==0: continue
    s=t(); fv = fe.embed(crop); ft=t()-s
    s=t(); bv = body_embed(crop); bt=t()-s
    print(f"  person{i}: crop{crop.shape[:2]}  face={'None' if fv is None else fv.shape} {ft*1000:.0f}ms  body={bv.shape} {bt*1000:.0f}ms")

print("FEASIBLE" if boxes is not None else "??")
