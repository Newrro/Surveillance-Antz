#!/usr/bin/env python3
"""
tools/mtmct_eval/label_server.py — click-to-label ground truth for the eval harness.

Turns the manual step into: look at a person's crops, type a name, next. Same name
= same real person; that's the whole task. Names autocomplete so re-using one is a
keystroke. Progress saves to labels.csv (resumable — re-open and it reloads).

  python3 tools/mtmct_eval/label_server.py --tracks eval_run/tracks.jsonl
  # open http://localhost:8900 , label, click Save → eval_run/labels.csv

Then score:
  python3 tools/mtmct_eval/score.py --tracks eval_run/tracks.jsonl --labels eval_run/labels.csv

Serves snapshot images straight from the repo (read-only, path-guarded). Run it
from the repo root, or pass --repo-root.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE = {"tracks": [], "labels_path": "", "repo_root": ""}

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>MTMCT labeling</title><style>
 body{font:14px system-ui,sans-serif;margin:0;background:#111;color:#eee}
 header{position:sticky;top:0;background:#1b1b1b;padding:12px 16px;border-bottom:1px solid #333;
   display:flex;gap:16px;align-items:center;z-index:10}
 header b{font-size:16px} .muted{color:#999}
 button{background:#2d6;border:0;color:#042;font-weight:600;padding:8px 16px;border-radius:6px;cursor:pointer}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;padding:16px}
 .card{background:#1b1b1b;border:1px solid #333;border-radius:10px;padding:10px}
 .imgs{display:flex;gap:8px}.imgs img{height:150px;border-radius:6px;background:#000;object-fit:contain;border:1px solid #333}
 .imgs .body{width:110px;object-fit:cover}
 .meta{font-size:12px;color:#aaa;margin:6px 0}
 .card input{width:100%;padding:8px;border-radius:6px;border:1px solid #444;background:#222;color:#fff;font-size:14px}
 .card input.done{border-color:#2d6}
 .tid{font-family:monospace;font-size:11px;color:#78f}
 .ign{background:#444;color:#ccc;font-weight:400;padding:4px 8px;font-size:12px;margin-top:6px}
</style></head><body>
<header>
  <b>Ground-truth labeling</b>
  <span class=muted id=prog></span>
  <span class=muted>Type the SAME name for the same real person. Tab to next.</span>
  <button onclick=save()>Save labels.csv</button>
  <span id=status class=muted></span>
</header>
<div class=grid id=grid></div>
<datalist id=names></datalist>
<script>
let tracks=[], labels={};
function img(p){return p?('/file?path='+encodeURIComponent(p)):''}
function refreshNames(){
  const s=new Set(Object.values(labels).filter(v=>v&&v!=='ignore'));
  document.getElementById('names').innerHTML=[...s].map(n=>`<option value="${n}">`).join('');
}
function prog(){
  const n=tracks.length, d=Object.values(labels).filter(v=>v).length;
  document.getElementById('prog').textContent=`${d}/${n} labeled`;
}
function set(tid,v){ if(v)labels[tid]=v; else delete labels[tid]; refreshNames(); prog(); }
function card(t){
  const d=document.createElement('div');d.className='card';
  const full=t.full_scene?`<img src="${img(t.full_scene)}" title="full scene">`:'';
  d.innerHTML=`<div class=imgs><img class=body src="${img(t.snapshot)}" title="body">${full}</div>
    <div class=meta><span class=tid>${t.track_id}</span> · ${t.camera||'?'} · ${t.n_events} ev · pred: ${t.predicted_label||'—'}</div>
    <input list=names placeholder="who is this? (name)" value="${labels[t.track_id]||''}">
    <button class=ign>ignore (bad/duplicate track)</button>`;
  const inp=d.querySelector('input');
  const mark=()=>{inp.classList.toggle('done',!!inp.value)};
  inp.addEventListener('input',()=>{set(t.track_id,inp.value.trim());mark()});
  d.querySelector('.ign').onclick=()=>{inp.value='ignore';set(t.track_id,'ignore');mark()};
  mark();
  return d;
}
async function save(){
  const st=document.getElementById('status');st.textContent='saving…';
  const r=await fetch('/save',{method:'POST',body:JSON.stringify(labels)});
  st.textContent=r.ok?('saved '+(await r.json()).n+' → labels.csv'):'save FAILED';
}
async function load(){
  tracks=await (await fetch('/tracks.json')).json();
  labels=await (await fetch('/labels.json')).json();
  const g=document.getElementById('grid');
  tracks.forEach(t=>g.appendChild(card(t)));
  refreshNames();prog();
}
load();
</script></body></html>"""


def _safe_path(rel: str) -> str | None:
    root = os.path.realpath(STATE["repo_root"])
    full = os.path.realpath(os.path.join(root, rel))
    if full == root or full.startswith(root + os.sep):
        return full if os.path.isfile(full) else None
    return None


def _load_existing_labels() -> dict:
    p = STATE["labels_path"]
    out = {}
    if os.path.isfile(p):
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("track_id"):
                    out[row["track_id"]] = (row.get("true_person") or "").strip()
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode())
        elif u.path == "/tracks.json":
            self._send(200, json.dumps(STATE["tracks"]).encode(), "application/json")
        elif u.path == "/labels.json":
            self._send(200, json.dumps(_load_existing_labels()).encode(), "application/json")
        elif u.path == "/file":
            rel = (parse_qs(u.query).get("path") or [""])[0]
            fp = _safe_path(rel)
            if not fp:
                self._send(404, b"not found")
                return
            with open(fp, "rb") as f:
                data = f.read()
            ext = os.path.splitext(fp)[1].lower()
            ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext.lstrip("."), "application/octet-stream")
            self._send(200, data, ctype)
        else:
            self._send(404, b"not found")

    def do_POST(self):
        if urlparse(self.path).path != "/save":
            self._send(404, b"not found")
            return
        n = int(self.headers.get("Content-Length", 0))
        labels = json.loads(self.rfile.read(n) or b"{}")
        with open(STATE["labels_path"], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["track_id", "true_person"])
            for t in STATE["tracks"]:
                tid = t["track_id"]
                w.writerow([tid, labels.get(tid, "")])
        self._send(200, json.dumps({"n": sum(1 for v in labels.values() if v)}).encode(), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", required=True, help="tracks.jsonl from export_tracks.py")
    ap.add_argument("--out", help="labels.csv path (default: labels.csv beside tracks)")
    ap.add_argument("--repo-root", default=os.getcwd(), help="root the snapshot paths are relative to")
    ap.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()

    with open(args.tracks) as f:
        STATE["tracks"] = [json.loads(l) for l in f if l.strip()]
    STATE["labels_path"] = args.out or os.path.join(os.path.dirname(os.path.abspath(args.tracks)), "labels.csv")
    STATE["repo_root"] = args.repo_root

    print(f"[label] {len(STATE['tracks'])} tracks | labels → {STATE['labels_path']}")
    print(f"[label] open http://localhost:{args.port}   (Ctrl-C to stop)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
