"""People in the photos: the faces found by tools/find_faces.py, matched to the
names given in the site.

The user names a few faces ("Laurence") and corrects mistakes ("pas Laurence");
those examples and refusals are kept in data/people.json, each with its face
vector, so they survive a new run of tools/find_faces.py. Every face found is
then given to the closest person, if it is close enough (THRESHOLD, cosine of
the SFace vectors) and not closer to one of that person's refused faces.
Needs NumPy; without it, or without cache/faces.*, there are simply no people.
"""

import hashlib
import json
import os
import re
import threading
import unicodedata

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

ROOT = os.path.dirname(os.path.abspath(__file__))
FACES = os.path.join(ROOT, "cache", "faces")  # + .json / .f32 (tools/find_faces.py)
STORE = os.path.join(ROOT, "data", "people.json")  # the user's names and corrections
CROPS = os.path.join(ROOT, "cache", "face_crops")  # small images of faces, made once
THRESHOLD = (
    0.5  # same person above this (SFace's own suggestion, 0.363, is too loose here)
)
# Words that only link names in a query ("Laurence et Paul", "les photos de Laurence").
FILLER = {
    "et",
    "avec",
    "and",
    "with",
    "de",
    "du",
    "des",
    "d",
    "la",
    "le",
    "les",
    "l",
    "photo",
    "photos",
    "image",
    "images",
    "&",
    ",",
    "+",
}


def _plain(word):
    """Lower case, no accents: how names are compared in a query."""
    return "".join(
        c
        for c in unicodedata.normalize("NFD", word.casefold())
        if not unicodedata.combining(c)
    )


class People:
    def __init__(self, faces=FACES, store=STORE):
        self.faces_base, self.store_path = faces, store
        self.lock = threading.RLock()
        self.stamp = None
        self.faces = []  # [(photo, index, box, score)] in matrix row order
        self.by_photo = {}  # photo -> [row, …]
        self.matrix = None  # rows: unit face vectors
        self.store = {"threshold": THRESHOLD, "people": {}}
        self.assigned = {}  # row -> (name, similarity, "example" | "auto")

    # ---------- loading ----------

    def _refresh(self):
        """Reload the faces / the names when their files changed; re-match if so."""
        if np is None:
            return
        stamps = []
        for path in (
            self.faces_base + ".f32",
            self.faces_base + ".json",
            self.store_path,
        ):
            try:
                st = os.stat(path)
                stamps.append((st.st_mtime, st.st_size))
            except OSError:
                stamps.append(None)
        with self.lock:
            if stamps == self.stamp:
                return
            if stamps[0] and stamps[1]:
                with open(self.faces_base + ".json", encoding="utf-8") as f:
                    meta = json.load(f)
                matrix = np.fromfile(self.faces_base + ".f32", dtype="<f4").reshape(
                    -1, meta["dim"]
                )
                faces, by_photo = [None] * len(matrix), {}
                for photo, p in meta["photos"].items():
                    for i, fc in enumerate(p["faces"]):
                        faces[fc["row"]] = (photo, i, fc["box"], fc["score"])
                        by_photo.setdefault(photo, []).append(fc["row"])
                if (
                    None in faces
                ):  # files being written by find_faces.py: keep the old ones
                    return
                self.faces, self.by_photo, self.matrix = faces, by_photo, matrix
            if stamps[2]:
                with open(self.store_path, encoding="utf-8") as f:
                    self.store = json.load(f)
            self._match()
            self.stamp = stamps

    def _match(self):
        """Give each face to the closest person, if close enough and not refused."""
        self.assigned = {}
        if self.matrix is None or not len(self.matrix):
            return
        threshold = self.store.get("threshold", THRESHOLD)
        best = np.full(len(self.matrix), -1.0, dtype=np.float32)
        who = np.full(len(self.matrix), -1)
        names = sorted(self.store["people"])
        for k, name in enumerate(names):
            person = self.store["people"][name]
            if not person["examples"]:
                continue
            pos = (
                self.matrix
                @ np.asarray([e["vec"] for e in person["examples"]], dtype=np.float32).T
            )
            s = pos.max(axis=1)
            if person.get("refused"):
                neg = (
                    self.matrix
                    @ np.asarray(
                        [e["vec"] for e in person["refused"]], dtype=np.float32
                    ).T
                )
                s = np.where(
                    neg.max(axis=1) >= s, -1.0, s
                )  # closer to a refused face: not them
            take = (s >= threshold) & (s > best)
            best[take], who[take] = s[take], k
        for row in np.flatnonzero(who >= 0):
            self.assigned[int(row)] = (
                names[who[row]],
                round(float(best[row]), 3),
                "auto",
            )
        # The faces the user named are theirs, whatever the matching says.
        for name, person in self.store["people"].items():
            for e in person["examples"]:
                row = self._row(e["photo"], e["face"], e["vec"])
                if row is not None:
                    self.assigned[row] = (name, 1.0, "example")

    def _row(self, photo, index, vec=None):
        """Row of a face, checked by its vector when given (find_faces.py may have re-run)."""
        rows = self.by_photo.get(photo, [])
        if not 0 <= index < len(rows):
            return None
        row = rows[index]
        if (
            vec is not None
            and float(self.matrix[row] @ np.asarray(vec, dtype=np.float32)) < 0.95
        ):
            return None
        return row

    def _save(self):
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.store, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.store_path)
        self.stamp = None  # re-match on the next request

    # ---------- reading ----------

    def available(self):
        self._refresh()
        return self.matrix is not None

    def faces_of(self, photo):
        """[{face, box, score, name, similarity, kind}] of a photo."""
        self._refresh()
        with self.lock:
            out = []
            for i, row in enumerate(self.by_photo.get(photo, [])):
                name, sim, kind = self.assigned.get(row, (None, None, None))
                _, _, box, score = self.faces[row]
                out.append(
                    {
                        "face": i,
                        "box": box,
                        "score": score,
                        "name": name,
                        "similarity": sim,
                        "kind": kind,
                    }
                )
            return out

    def names_in(self, photo):
        """Names of the people recognised in a photo (sorted, no duplicates)."""
        self._refresh()
        with self.lock:
            return sorted(
                {
                    self.assigned[r][0]
                    for r in self.by_photo.get(photo, [])
                    if r in self.assigned
                }
            )

    def people(self, among=None):
        """[{name, aliases, photos, total, examples, cover: {photo, face}}], most photographed
        first. total: all their photos; photos: those in `among` (e.g. the photos of the
        selected days), or all of them if `among` is None."""
        self._refresh()
        with self.lock:
            counts, cover = {}, {}
            for row, (name, sim, kind) in self.assigned.items():
                photo, i = self.faces[row][:2]
                counts.setdefault(name, set()).add(photo)
                if kind == "example" and name not in cover:
                    cover[name] = {"photo": photo, "face": i}
            out = []
            for n, p in self.store["people"].items():
                theirs = counts.get(n, set())
                out.append(
                    {
                        "name": n,
                        "aliases": p.get("aliases", []),
                        "total": len(theirs),
                        "photos": len(theirs if among is None else theirs & among),
                        "examples": len(p["examples"]),
                        "cover": cover.get(n),
                    }
                )
            return sorted(
                out, key=lambda p: (-p["photos"], -p["total"], p["name"].lower())
            )

    def split_query(self, text):
        """(names, rest) of a search: the known names it contains (as stored, in
        order) and the rest of the text, e.g. "laurence et Paul à vélo" ->
        (["Laurence", "Paul"], "à vélo"). Case and accents of names do not matter."""
        self._refresh()
        with self.lock:
            known = sorted(
                (
                    (tuple(_plain(w) for w in re.findall(r"\w+", alias)), name)
                    for name, p in self.store["people"].items()
                    for alias in [name] + p.get("aliases", [])
                ),
                key=lambda k: -len(k[0]),
            )
        tokens = list(re.finditer(r"\w+|[^\w\s]", text))
        plain = [_plain(t.group()) for t in tokens]
        names, used, i = [], set(), 0
        while i < len(tokens):
            for (
                words,
                name,
            ) in known:  # longest names first: "Marie Claire" before "Marie"
                if words and tuple(plain[i : i + len(words)]) == words:
                    if name not in names:
                        names.append(name)
                    used.update(range(i, i + len(words)))
                    i += len(words)
                    break
            else:
                i += 1
        if not names:
            return [], text.strip()
        rest = [t.group() for k, t in enumerate(tokens) if k not in used]
        if all(_plain(w) in FILLER for w in rest):  # "les photos de Laurence"
            return names, ""
        while rest and _plain(rest[0]) in FILLER - {"de", "la", "le", "les", "l", "d"}:
            rest.pop(0)  # "Laurence et Paul à la plage": drop the leading "et"
        return names, " ".join(rest).replace(" '", "'").replace("' ", "'").strip()

    def photos_of(self, name):
        """{photo: similarity of its best face} of the photos where `name` was recognised
        (the name as typed: case and spaces do not matter)."""
        self._refresh()
        with self.lock:
            name = self.canonical(name)
            found = {}
            for row, (n, sim, _) in self.assigned.items():
                if n == name:
                    photo = self.faces[row][0]
                    found[photo] = max(found.get(photo, 0), sim)
            return found

    # ---------- the user's answers ----------

    def _face(self, photo, index):
        row = self._row(photo, index)
        if row is None:
            raise KeyError("unknown face")
        return {
            "photo": photo,
            "face": index,
            "box": self.faces[row][2],
            "vec": [round(float(v), 5) for v in self.matrix[row]],
        }, row

    def canonical(self, name):
        """The person a name or alias designates, as their main name ("petite nièce"
        -> "Laurence"); case, accents and spaces ignored. An unknown name is returned
        tidied up (it will be a new person)."""
        name = " ".join(name.split())
        key = _plain(name)
        for existing, person in self.store["people"].items():
            if key in {_plain(n) for n in [existing] + person.get("aliases", [])}:
                return existing
        return name

    def set_aliases(self, name, aliases):
        """Other names of a person ("petite nièce"), for naming faces
        and searching. Refused if one is already another person's name or alias."""
        self._refresh()
        with self.lock:
            name = self.canonical(name)
            person = self.store["people"].get(name)
            if person is None:
                raise KeyError(f"unknown person {name}")
            clean = []
            for alias in aliases:
                alias = " ".join(str(alias).split())
                if (
                    not alias
                    or _plain(alias) == _plain(name)
                    or _plain(alias) in map(_plain, clean)
                ):
                    continue
                if len(alias) > 100 or not re.search(r"\w", alias):
                    raise ValueError(f"invalid alias {alias!r}")
                other = self.canonical(alias)
                if other in self.store["people"] and other != name:
                    raise ValueError(f"« {alias} » is already {other}")
                clean.append(alias)
            person["aliases"] = clean
            self._save()
            return name, clean

    def rename(self, name, new, merge=False):
        """Correct a person's name ("Laurance" -> "Laurence"); returns the name kept.
        If `new` is already someone else's name or alias, they are one person: merged
        (faces, refusals and aliases) when merge is True, else LookupError(their name)."""
        self._refresh()
        with self.lock:
            name = self.canonical(name)
            person = self.store["people"].get(name)
            if person is None:
                raise KeyError(f"unknown person {name}")
            new = " ".join(str(new).split())
            if not new or len(new) > 100 or not re.search(r"\w", new):
                raise ValueError("invalid name")
            other = self.canonical(new)
            if other in self.store["people"] and other != name:
                if not merge:
                    raise LookupError(other)
                target = self.store["people"][other]
                key = lambda e: (e["photo"], e["face"])  # noqa: E731
                named = {key(e) for e in target["examples"]}
                target["examples"] += [e for e in person["examples"] if key(e) not in named]
                named |= {key(e) for e in person["examples"]}
                refused = {}
                for e in target.get("refused", []) + person.get("refused", []):
                    if key(e) not in named:  # named as one of them: not a refusal any more
                        refused.setdefault(key(e), e)
                target["refused"] = list(refused.values())
                target["aliases"] = [a for a in dict.fromkeys(target.get("aliases", []) + person.get("aliases", []))
                                     if _plain(a) != _plain(other)]
                del self.store["people"][name]
                kept = other
            else:  # only the spelling changes (or one of their aliases becomes the name)
                person["aliases"] = [a for a in person.get("aliases", []) if _plain(a) != _plain(new)]
                del self.store["people"][name]
                self.store["people"][new] = person
                kept = new
            self._save()
            return kept

    @staticmethod
    def _drop(entries, photo, index):
        return [e for e in entries if not (e["photo"] == photo and e["face"] == index)]

    def name_face(self, photo, index, name):
        """This face is `name` (a new person, a confirmation or a correction)."""
        self._refresh()
        with self.lock:
            face, _ = self._face(photo, index)
            name = self.canonical(name)
            if not name:
                raise ValueError("empty name")
            for person in self.store["people"].values():  # it is nobody else
                person["examples"] = self._drop(person["examples"], photo, index)
            person = self.store["people"].setdefault(
                name, {"examples": [], "refused": []}
            )
            person["refused"] = self._drop(person.get("refused", []), photo, index)
            person["examples"].append(face)
            self._save()
            return name

    def refuse_face(self, photo, index, name):
        """This face is NOT `name`."""
        self._refresh()
        with self.lock:
            face, _ = self._face(photo, index)
            name = self.canonical(name)
            person = self.store["people"].get(name)
            if person is None:
                raise KeyError(f"unknown person {name}")
            person["examples"] = self._drop(person["examples"], photo, index)
            person.setdefault("refused", [])
            person["refused"] = self._drop(person["refused"], photo, index) + [face]
            if not person["examples"]:  # nobody left to recognise: forget the name
                del self.store["people"][name]
            self._save()

    # ---------- face images ----------

    def crop(self, photo, index, source, size=160):
        """Path of a small square image of a face (made once, from `source`)."""
        from PIL import Image, ImageOps

        self._refresh()
        with self.lock:
            row = self._row(photo, index)
            if row is None:
                raise KeyError("unknown face")
            box = self.faces[row][2]
        key = hashlib.sha1(f"{photo}#{index}#{box}".encode()).hexdigest()
        dst = os.path.join(CROPS, key[:2], key + ".jpg")
        if os.path.isfile(dst):
            return dst
        with Image.open(source) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            x, y, w, h = box
            cx, cy, side = (
                (x + w / 2) * im.width,
                (y + h / 2) * im.height,
                max(w * im.width, h * im.height) * 1.5,
            )
            im = im.crop(
                (
                    int(cx - side / 2),
                    int(cy - side / 2),
                    int(cx + side / 2),
                    int(cy + side / 2),
                )
            )
            im.thumbnail((size, size))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            im.save(dst + ".tmp", "JPEG", quality=85)
        os.replace(dst + ".tmp", dst)
        return dst
