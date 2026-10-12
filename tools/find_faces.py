#!/usr/bin/env python3
"""Find the faces in the photos and turn each into a vector, so that people can be
named later ("Laurence") and searched ("Laurence à vélo").

    tools/find_faces.py                             # all photos not done yet
    tools/find_faces.py --random --limit 30 --out /tmp/f --preview /tmp/f.jpg   # a sample

Detection: OpenCV's YuNet; recognition: OpenCV's SFace (128 numbers per face; two
faces of the same person have a cosine similarity above ~0.36). Both run on the
CPU and are installed by `make faces` (OpenCV in .venv/, used automatically).

Results: cache/faces.json (for each photo: its faces, with their box as fractions
of the upright photo and the detection score) and cache/faces.f32 (one row per
face, unit-length float32). Saved regularly: stop (Ctrl+C) and start again at
will; only new or changed photos are done, deleted ones are forgotten. A photo
whose tags only changed (e.g. a GPS position written by the site) keeps its
faces: same date and same size without metadata (photos.same_image()).
Face vectors are biometric data: they stay in cache/, like the photos.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    import cv2
except ImportError:  # use the project's .venv (make faces), where OpenCV is installed
    venv = os.path.join(ROOT, ".venv")
    # .venv/bin/python is a link to the system's python3: what differs is sys.prefix.
    if os.path.isdir(venv) and os.path.realpath(sys.prefix) != os.path.realpath(venv):
        python = os.path.join(venv, "bin", "python")
        os.execv(python, [python] + sys.argv)
    sys.exit("OpenCV is missing: run `make faces`")

import argparse
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, ROOT)
import photos  # noqa: E402

YUNET = os.path.join(ROOT, "models", "face_detection_yunet_2023mar.onnx")
SFACE = os.path.join(ROOT, "models", "face_recognition_sface_2021dec.onnx")
MODEL = "yunet-2023mar+sface-2021dec"
OUT = os.path.join(ROOT, "cache", "faces")  # + .json / .f32
DIM = 128

_local = threading.local()  # OpenCV's detector/recognizer: one per thread


def models():
    if not hasattr(_local, "det"):
        _local.det = cv2.FaceDetectorYN.create(YUNET, "", (320, 320), 0.8, 0.3, 5000)
        _local.rec = cv2.FaceRecognizerSF.create(SFACE, "")
    return _local.det, _local.rec


def find(path, size, min_score, min_face):
    """[(box as fractions (x, y, w, h), score, unit vector)] of the faces in an image."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((size, size))
    bgr = np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
    h, w = bgr.shape[:2]
    det, rec = models()
    det.setInputSize((w, h))
    det.setScoreThreshold(min_score)
    _, faces = det.detect(bgr)
    found = []
    for f in faces if faces is not None else []:
        x, y, fw, fh, score = *f[:4], f[14]
        if min(fw, fh) < min_face:  # too small to recognise anybody
            continue
        vec = rec.feature(rec.alignCrop(bgr, f)).flatten().astype(np.float32)
        vec /= np.linalg.norm(vec) or 1.0
        box = [max(0.0, x / w), max(0.0, y / h), min(fw / w, 1.0), min(fh / h, 1.0)]
        found.append(([round(float(v), 4) for v in box], round(float(score), 3), vec))
    return found


def load(out):
    try:
        with open(out + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        vecs = np.fromfile(out + ".f32", dtype="<f4").reshape(-1, meta["dim"])
        if meta.get("model") != MODEL:
            print(f"Previous faces were found with {meta.get('model')}: starting over.")
            return {}
        return {name: {**p, "faces": [{**fc, "vec": vecs[fc["row"]]} for fc in p["faces"]]}
                for name, p in meta["photos"].items()}
    except (OSError, ValueError, KeyError):
        return {}


def save(out, done):
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    rows, meta = [], {"model": MODEL, "dim": DIM, "photos": {}}
    for name in sorted(done):
        p = done[name]
        faces = []
        for fc in p["faces"]:
            faces.append({"box": fc["box"], "score": fc["score"], "row": len(rows)})
            rows.append(fc["vec"])
        meta["photos"][name] = {"mtime": p["mtime"], "size": p["size"], "dsize": p.get("dsize"),
                                "faces": faces}
    np.asarray(rows, dtype="<f4").reshape(-1, DIM).tofile(out + ".f32.tmp")
    with open(out + ".json.tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))
    os.replace(out + ".f32.tmp", out + ".f32")
    os.replace(out + ".json.tmp", out + ".json")


def preview(path, names, done, sources, cell=300):
    """A contact sheet of the photos with their faces boxed (score above each box)."""
    tiles = []
    for name in names:
        with Image.open(sources[name]) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((cell, cell))
        draw = ImageDraw.Draw(im)
        for fc in done[name]["faces"]:
            x, y, w, h = fc["box"]
            box = [x * im.width, y * im.height, (x + w) * im.width, (y + h) * im.height]
            draw.rectangle(box, outline=(255, 40, 40), width=3)
            draw.text((box[0] + 2, max(0, box[1] - 12)), f"{fc['score']:.2f}", fill=(255, 255, 0))
        tiles.append(im)
    cols = 6
    sheet = Image.new("RGB", (cols * (cell + 6), -(-len(tiles) // cols) * (cell + 6)), "white")
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % cols) * (cell + 6), (i // cols) * (cell + 6)))
    sheet.save(path, quality=85)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("--photos", default=os.path.join(ROOT, "data", "photos"))
    p.add_argument("--out", default=OUT, help="output files, without extension (default: %(default)s)")
    p.add_argument("--size", type=int, default=1600, help="longest side analysed, in pixels")
    p.add_argument("--min-score", type=float, default=0.8, help="detection confidence kept (0-1)")
    p.add_argument("--min-face", type=int, default=20,
                   help="smallest face kept, in pixels at --size (20 keeps group photos)")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--limit", type=int, help="process at most this many photos")
    p.add_argument("--random", action="store_true", help="pick the photos at random")
    p.add_argument("--force", action="store_true", help="process again photos already done")
    p.add_argument("--preview", help="write a contact sheet of the photos processed, faces boxed")
    args = p.parse_args()
    for f in (YUNET, SFACE):
        if not os.path.isfile(f):
            sys.exit(f"{f} is missing: run `make faces`")

    photo_dir = os.path.abspath(args.photos)
    shared = photo_dir == os.path.abspath(os.path.join(ROOT, "data", "photos"))
    index = photos.PhotoIndex(photo_dir, os.path.join(ROOT, "cache", "photos.json") if shared
                              else os.path.abspath(args.out) + ".index.json")
    thumbs = os.path.join(ROOT, "cache", "thumbs") if shared else os.path.abspath(args.out) + ".thumbs"

    done = load(args.out)
    current = {}
    for name in index.by_name:
        st = os.stat(os.path.join(photo_dir, name))
        current[name] = {"mtime": st.st_mtime, "size": st.st_size}
    for name in [n for n in done if n not in current]:
        del done[name]
    todo, retagged = [], 0
    for n in sorted(current):
        old = done.get(n)
        if args.force or not old or not photos.same_image(os.path.join(photo_dir, n), old, current[n]):
            todo.append(n)
        elif [old.get(k) for k in ("mtime", "size", "dsize")] != [current[n][k] for k in ("mtime", "size", "dsize")]:
            old.update(current[n])  # same image, new tags (or a first "dsize"): faces kept
            retagged += 1
    if args.random:
        random.shuffle(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(current)} photos, {len(done)} already done, {len(todo)} to do "
          f"(OpenCV {cv2.__version__}, {args.workers} threads)"
          + (f"; {retagged} with only their tags changed, kept" if retagged else ""))

    sources = {}

    def work(name):
        path = os.path.join(photo_dir, name)
        current[name].setdefault("dsize", photos.data_size(path))
        if index.by_name[name]["kind"] == "video":  # its preview frame
            path = photos.thumbnail(photo_dir, name, thumbs)
            if not path:
                raise RuntimeError("no preview frame (ffmpeg missing?)")
        sources[name] = path
        return find(path, args.size, args.min_score, args.min_face)

    lock, start, n_faces, n_err = threading.Lock(), time.time(), 0, 0
    futures = {}
    try:
        with ThreadPoolExecutor(args.workers) as pool:
            futures = {pool.submit(work, n): n for n in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                name = futures[fut]
                try:
                    faces = fut.result()
                    with lock:
                        done[name] = {**current[name],
                                      "faces": [{"box": b, "score": s, "vec": v} for b, s, v in faces]}
                    n_faces += len(faces)
                except Exception as e:  # one bad photo must not stop the others
                    n_err += 1
                    print(f"{name}: ERROR {e}", flush=True)
                if i % 50 == 0 or i == len(todo):
                    rate = (time.time() - start) / i
                    print(f"[{i}/{len(todo)}, {rate:.2f}s/photo, ~{(len(todo) - i) * rate / 60:.0f} min left] "
                          f"{n_faces} faces so far", flush=True)
                if i % 500 == 0:
                    with lock:
                        save(args.out, done)
    except KeyboardInterrupt:
        print("\nstopped: progress saved, run again to continue")
        for f in futures:
            f.cancel()
    finally:
        with lock:
            save(args.out, done)
    with_faces = sum(1 for p in done.values() if p["faces"])
    print(f"{len(done)} photos done, {with_faces} with faces, "
          f"{sum(len(p['faces']) for p in done.values())} faces; {n_err} errors")
    if args.preview:
        shown = [n for n in todo if n in done and n in sources]
        if shown:
            preview(args.preview, shown, done, sources)
            print(f"preview: {args.preview} ({len(shown)} photos)")
        else:
            print("preview: no photo processed, nothing to show")


if __name__ == "__main__":
    main()
