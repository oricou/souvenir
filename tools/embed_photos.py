#!/usr/bin/env python3
"""Turn each photo into a vector, for searching photos by meaning ("un bateau",
"plage au coucher du soleil"…) in the site.

The vectors come from a multimodal embedding model served locally with an
OpenAI-compatible API, e.g. on a computer with an NVIDIA card:

    vllm serve Qwen/Qwen3-VL-Embedding-2B --runner pooling --max-model-len 8192

then:

    tools/embed_photos.py                  # encode the photos not done yet
    tools/embed_photos.py --limit 50       # try on 50 photos first

Each photo is reduced (--size, 1024 px) and sent only to that API. Videos are
encoded from their preview frame. The site sends a search sentence to the same
model and compares its vector with the photos' vectors.

Results: cache/embeddings.json (model, vector size, and which photo is on which
row) and cache/embeddings.f16 (the vectors, unit length, as little-endian
half-precision floats, one row per photo). Saved regularly: the tool can be
stopped (Ctrl+C) and started again; it only encodes new or changed photos,
forgets deleted ones, and starts over if the model changes. A photo whose tags
only changed (e.g. a GPS position written by the site) keeps its vector: same
date and same size without metadata (photos.same_image()).
Only the standard library and Pillow are needed.
"""

import argparse
import base64
import io
import json
import math
import os
import random
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import photos  # noqa: E402

OUT = os.path.join(ROOT, "cache", "embeddings")  # + .json / .f16
# Instruction for the photos; the site uses its own for search queries (search.py).
INSTRUCTION = "Represent the user's input."


class Api:
    def __init__(self, url, key, timeout):
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def _open(self, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def models(self):
        return [m["id"] for m in self._open("/models")["data"]]

    def embed_image(self, model, data_url):
        body = {"model": model, "encoding_format": "float", "messages": [
            {"role": "system", "content": [{"type": "text", "text": INSTRUCTION}]},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url}}]},
        ]}
        return self._open("/embeddings", body)["data"][0]["embedding"]


def image_data_url(path, size):
    """The photo as a JPEG data URL, at most `size` pixels on its longest side."""
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def pack(vector):
    """Unit-length vector as half-precision bytes (the site compares by dot product)."""
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return struct.pack(f"<{len(vector)}e", *(x / norm for x in vector))


def load(out):
    """(meta, {name: row bytes}) of a previous run, or empty."""
    try:
        with open(out + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        with open(out + ".f16", "rb") as f:
            data = f.read()
        step = meta["dim"] * 2
        rows = {r["name"]: (r, data[i * step:(i + 1) * step]) for i, r in enumerate(meta["rows"])}
        return meta, rows
    except (OSError, ValueError, KeyError):
        return None, {}


def save(out, model, dim, rows):
    """Write both files (vectors first, so the .json never points past the data)."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    names = sorted(rows)
    with open(out + ".f16.tmp", "wb") as f:
        for n in names:
            f.write(rows[n][1])
    meta = {"model": model, "dim": dim, "instruction": INSTRUCTION,
            "rows": [rows[n][0] for n in names]}
    with open(out + ".json.tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(out + ".f16.tmp", out + ".f16")
    os.replace(out + ".json.tmp", out + ".json")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("--api", default="http://localhost:8000/v1",
                   help="OpenAI-compatible API of the embedding model (default: %(default)s)")
    p.add_argument("--api-key", default=os.environ.get("SOUVENIR_API_KEY"),
                   help="key of the API if it needs one (vllm serve --api-key …); "
                        "default: $SOUVENIR_API_KEY")
    p.add_argument("--model", help="model name (default: the one the API serves)")
    p.add_argument("--photos", default=os.path.join(ROOT, "data", "photos"))
    p.add_argument("--out", default=OUT, help="output files, without extension (default: %(default)s)")
    p.add_argument("--size", type=int, default=1024, help="longest side sent, in pixels")
    p.add_argument("--workers", type=int, default=4, help="photos sent at the same time")
    p.add_argument("--limit", type=int, help="encode at most this many photos")
    p.add_argument("--random", action="store_true", help="pick the photos at random")
    p.add_argument("--force", action="store_true", help="encode again photos already done")
    p.add_argument("--timeout", type=int, default=120, help="seconds per photo")
    args = p.parse_args()

    api = Api(args.api, args.api_key, args.timeout)
    try:
        model = args.model or api.models()[0]
    except urllib.error.HTTPError as e:
        sys.exit(f"The API at {api.url} refused the request ({e.code} {e.reason})"
                 + (": wrong or missing --api-key?" if e.code == 401 else ""))
    except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError) as e:
        sys.exit(f"No model API at {api.url} ({e}). Start it first, e.g.: "
                 "vllm serve Qwen/Qwen3-VL-Embedding-2B --runner pooling --max-model-len 8192")
    print(f"Model: {model} at {api.url}")

    meta, rows = load(args.out)
    if meta and meta.get("model") != model:
        print(f"Previous vectors were made by {meta.get('model')}: starting over "
              "(vectors of different models cannot be compared).")
        meta, rows = None, {}
    dim = meta["dim"] if meta else None

    # The site's photo index (and preview cache) are only shared for the site's own photo
    # folder: another folder gets its own, so a test never replaces the site's files.
    photo_dir = os.path.abspath(args.photos)
    shared = photo_dir == os.path.abspath(os.path.join(ROOT, "data", "photos"))
    index = photos.PhotoIndex(photo_dir, os.path.join(ROOT, "cache", "photos.json") if shared
                              else os.path.abspath(args.out) + ".index.json")
    thumbs = os.path.join(ROOT, "cache", "thumbs") if shared else os.path.abspath(args.out) + ".thumbs"

    current = {}
    for name in index.by_name:
        st = os.stat(os.path.join(photo_dir, name))
        current[name] = {"name": name, "mtime": st.st_mtime, "size": st.st_size}
    gone = [n for n in rows if n not in current]
    for n in gone:
        del rows[n]
    todo, retagged = [], 0
    for n, r in sorted(current.items()):
        old = rows[n][0] if n in rows else None
        if args.force or not old or not photos.same_image(os.path.join(photo_dir, n), old, r):
            todo.append(n)
        elif [old.get(k) for k in ("mtime", "size", "dsize")] != [r[k] for k in ("mtime", "size", "dsize")]:
            rows[n] = ({**old, **r}, rows[n][1])  # same image, new tags (or a first "dsize"): vector kept
            retagged += 1
    if args.random:
        random.shuffle(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(current)} photos, {len(rows)} already encoded"
          + (f", {len(gone)} deleted ones forgotten" if gone else "") + f", {len(todo)} to do"
          + (f"; {retagged} with only their tags changed, kept" if retagged else ""))
    if not todo:
        if gone or retagged:
            save(args.out, model, dim, rows)
        return

    lock = threading.Lock()

    def work(name):
        path = os.path.join(photo_dir, name)
        current[name].setdefault("dsize", photos.data_size(path))
        if index.by_name[name]["kind"] == "video":  # encode its preview frame
            path = photos.thumbnail(photo_dir, name, thumbs)
            if not path:
                raise RuntimeError("no preview frame (ffmpeg missing?)")
        return api.embed_image(model, image_data_url(path, args.size))

    start, n_ok, n_err = time.time(), 0, 0
    futures = {}
    try:
        with ThreadPoolExecutor(args.workers) as pool:
            futures = {pool.submit(work, name): name for name in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                name = futures[fut]
                try:
                    vector = fut.result()
                    with lock:
                        if dim is None:
                            dim = len(vector)
                        if len(vector) != dim:
                            raise RuntimeError(f"vector of {len(vector)} numbers, expected {dim}")
                        rows[name] = (current[name], pack(vector))
                    n_ok += 1
                    status = "ok"
                except Exception as e:  # one bad photo must not stop the others
                    n_err += 1
                    status = f"ERROR {e}"
                if status != "ok" or i % 25 == 0 or i == len(todo):
                    rate = (time.time() - start) / i
                    print(f"[{i}/{len(todo)}, {rate:.2f}s/photo, ~{(len(todo) - i) * rate / 60:.0f} min left] "
                          + (f"{name}: {status}" if status != "ok" else "…"), flush=True)
                if i % 200 == 0:
                    with lock:
                        save(args.out, model, dim, rows)
    except KeyboardInterrupt:
        print("\nstopped: progress saved, run again to continue")
        for f in futures:
            f.cancel()
    finally:
        if dim is not None:
            with lock:
                save(args.out, model, dim, rows)
    print(f"{n_ok} encoded, {n_err} errors; {len(rows)} photos in {args.out}.json/.f16")


if __name__ == "__main__":
    main()
