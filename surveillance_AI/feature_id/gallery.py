# ─────────────────────────────────────────────
#  THE GALLERY  (ONE json file = our database of known people)
# ─────────────────────────────────────────────
# One record per known person. Each person holds TWO kinds of "views":
#   • face_views — AdaFace embeddings (PRIMARY identity signal)
#   • body_views — OSNet ReID embeddings (FALLBACK when the face isn't usable)
# Storing multiple views per signal is what lets confidence climb over time: the
# more angles we've seen, the more likely a new sighting matches one closely.
#
# The whole thing is a single human-readable JSON file (config.GALLERY_PATH).

import os
import json
from datetime import datetime

import numpy as np

from . import config
from .extractor import cosine_similarity


def _now():
    return datetime.now().isoformat(timespec="seconds")


class Person:
    """One known individual, holding face and/or body embedding 'views'."""
    def __init__(self, person_id, label, name, face_views=None, body_views=None,
                 created=None, updated=None):
        self.id = person_id
        self.label = label
        self.name = name
        self.face_views = face_views or []   # list[np.ndarray] (AdaFace, 512)
        self.body_views = body_views or []   # list[np.ndarray] (OSNet, 512)
        self.created = created or _now()
        self.updated = updated or _now()

    def face_similarity(self, embedding):
        return max((cosine_similarity(embedding, v) for v in self.face_views), default=0.0)

    def body_similarity(self, embedding):
        return max((cosine_similarity(embedding, v) for v in self.body_views), default=0.0)

    def to_dict(self):
        return {
            "id": self.id, "label": self.label, "name": self.name,
            "created": self.created, "updated": self.updated,
            "num_face_views": len(self.face_views),
            "num_body_views": len(self.body_views),
            "face_views": [v.tolist() for v in self.face_views],
            "body_views": [v.tolist() for v in self.body_views],
        }

    @staticmethod
    def from_dict(d):
        fv = [np.asarray(v, dtype="float32") for v in d.get("face_views", [])]
        bv = [np.asarray(v, dtype="float32") for v in d.get("body_views", [])]
        return Person(d["id"], d["label"], d["name"], fv, bv,
                      d.get("created"), d.get("updated"))


class Gallery:
    def __init__(self):
        self.people = []
        self._visitor_counter = 0
        self.load()

    # ── persistence (single JSON file) ───────
    def load(self):
        if os.path.exists(config.GALLERY_PATH):
            with open(config.GALLERY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.people = [Person.from_dict(p) for p in data.get("people", [])]
            self._visitor_counter = data.get("visitor_counter", 0)
            print(f"[gallery] loaded {len(self.people)} known people from json.")
        else:
            print("[gallery] no existing gallery.json — starting empty.")

    def save(self):
        os.makedirs(os.path.dirname(config.GALLERY_PATH), exist_ok=True)
        data = {
            "visitor_counter": self._visitor_counter,
            "people": [p.to_dict() for p in self.people],
        }
        tmp = config.GALLERY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, config.GALLERY_PATH)

    # ── adding people ────────────────────────
    def add(self, face_emb=None, body_emb=None, label=config.LABEL_VISITOR,
            name="", person_id=None):
        """Create a new person with an initial face and/or body view."""
        if person_id is None:
            self._visitor_counter += 1
            person_id = f"VIS-{self._visitor_counter:06d}"
        face_views = [face_emb] if face_emb is not None else []
        body_views = [body_emb] if body_emb is not None else []
        p = Person(person_id, label, name, face_views, body_views)
        self.people.append(p)
        self.save()
        return p

    def _add_view(self, views, embedding):
        """Append a view; when over the cap, drop the most redundant one."""
        views.append(embedding)
        if len(views) > config.MAX_VIEWS_PER_PERSON:
            redundancy = [
                sum(cosine_similarity(v, other) for other in views if other is not v)
                for v in views
            ]
            views.pop(int(np.argmax(redundancy)))

    def add_face_view(self, person, embedding):
        self._add_view(person.face_views, embedding)
        person.updated = _now()
        self.save()

    def add_body_view(self, person, embedding):
        self._add_view(person.body_views, embedding)
        person.updated = _now()
        self.save()

    # ── the core queries ─────────────────────
    def best_match_face(self, embedding):
        """Return (best_person, best_similarity) over people that have face views."""
        best_person, best_sim = None, 0.0
        for p in self.people:
            if not p.face_views:
                continue
            sim = p.face_similarity(embedding)
            if sim > best_sim:
                best_sim, best_person = sim, p
        return best_person, best_sim

    def best_match_body(self, embedding):
        """Return (best_person, best_similarity) over people that have body views."""
        best_person, best_sim = None, 0.0
        for p in self.people:
            if not p.body_views:
                continue
            sim = p.body_similarity(embedding)
            if sim > best_sim:
                best_sim, best_person = sim, p
        return best_person, best_sim
