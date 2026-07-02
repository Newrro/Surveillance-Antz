# ─────────────────────────────────────────────
#  STEP 2 — THE GALLERY  (ONE json file = our database of known people)
# ─────────────────────────────────────────────
# One record per known person, and each person can hold MANY embeddings
# ("views" = different angles/poses). Storing multiple views is what lets
# confidence climb over time: the more angles we've seen, the more likely a
# new sighting matches one of them closely.
#
# The whole thing is a single human-readable JSON file (config.GALLERY_PATH).
# Structure:
# {
#   "visitor_counter": 7,
#   "people": [
#     {"id": "EMP-001", "label": "Employee", "name": "Asha R.",
#      "created": "...", "updated": "...",
#      "views": [[512 floats], [512 floats], ...]}
#   ]
# }

import os
import json
from datetime import datetime

import numpy as np

from . import config
from .extractor import cosine_similarity


def _now():
    return datetime.now().isoformat(timespec="seconds")


class Person:
    """One known individual, holding one or more embedding 'views'."""
    def __init__(self, person_id, label, name, views, created=None, updated=None):
        self.id      = person_id
        self.label   = label
        self.name    = name
        self.views   = views          # list[np.ndarray], each a 512-vector
        self.created = created or _now()
        self.updated = updated or _now()

    # how well does `embedding` match THIS person? = best of all their views
    def similarity_to(self, embedding):
        return max((cosine_similarity(embedding, v) for v in self.views), default=0.0)

    def to_dict(self):
        return {
            "id": self.id, "label": self.label, "name": self.name,
            "created": self.created, "updated": self.updated,
            "num_views": len(self.views),
            "views": [v.tolist() for v in self.views],
        }

    @staticmethod
    def from_dict(d):
        views = [np.asarray(v, dtype="float32") for v in d["views"]]
        return Person(d["id"], d["label"], d["name"], views,
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
        # write to a temp file then replace, so a crash can't corrupt the json
        tmp = config.GALLERY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, config.GALLERY_PATH)

    # ── adding people ────────────────────────
    def add(self, embedding, label, name="", person_id=None):
        """Create a brand-new person with one initial view."""
        if person_id is None:
            self._visitor_counter += 1
            person_id = f"VIS-{self._visitor_counter:06d}"
        p = Person(person_id, label, name, [embedding])
        self.people.append(p)
        self.save()
        return p

    def add_view(self, person, embedding):
        """
        Attach a NEW angle/view to an existing person → raises future confidence.
        Keeps at most MAX_VIEWS_PER_PERSON; when full, drops the most redundant
        view (the one most similar to the others, i.e. adds least new info).
        """
        person.views.append(embedding)
        if len(person.views) > config.MAX_VIEWS_PER_PERSON:
            # redundancy score = how similar each view is to all the others.
            # Drop the highest — it's the least unique, so we lose the least.
            redundancy = [
                sum(cosine_similarity(v, other) for other in person.views if other is not v)
                for v in person.views
            ]
            drop_idx = int(np.argmax(redundancy))
            person.views.pop(drop_idx)
        person.updated = _now()
        self.save()

    # ── the core query ───────────────────────
    def best_match(self, embedding):
        """Return (best_person, best_similarity). Empty gallery → (None, 0.0)."""
        best_person, best_sim = None, 0.0
        for p in self.people:
            sim = p.similarity_to(embedding)
            if sim > best_sim:
                best_sim, best_person = sim, p
        return best_person, best_sim
