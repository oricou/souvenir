#!/usr/bin/env python3
"""Build tiles/timezones.bin: which time zone each place of the world is in.

Downloads the time-zone boundaries of timezone-boundary-builder (the data the
operating systems use; one download, nothing of yours is sent) and draws them
on a grid of RES degrees (0.05° ≈ 5 km). The server then finds a position's
zone with one array lookup, and Python's zoneinfo gives its offset at any date
(summer time included). Run it again to update after a new release.

The "with-oceans-1970" variant is used: oceans have their Etc/GMT±N zones, and
regions whose clocks differed at any time since 1970 stay separate, so old
traces get the offset they had then.

Needs NumPy (python3-numpy) and ~3 GB of free memory while it runs (~1 min).

File format: b"TZG1", uint16 width, uint16 height, float32 resolution,
uint16 number of zones, then the zone names (uint8 length + UTF-8), then the
zlib-compressed grid of uint16 zone numbers (0 = none), row 0 = southmost,
column 0 = westmost (−180°).
"""

import argparse
import hashlib
import io
import json
import os
import struct
import sys
import time
import urllib.request
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tiles", "timezones.bin")
API = "https://api.github.com/repos/evansiroky/timezone-boundary-builder/releases/latest"
ASSET = "timezones-with-oceans-1970.geojson.zip"
RES = 0.05


def download():
    with urllib.request.urlopen(API, timeout=30) as r:
        release = json.load(r)
    asset = next(a for a in release["assets"] if a["name"] == ASSET)
    print(f"release {release['tag_name']}: {ASSET}, {asset['size'] / 1e6:.1f} MB")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=300) as r:
        data = r.read()
    expected = (asset.get("digest") or "").removeprefix("sha256:")
    actual = hashlib.sha256(data).hexdigest()
    if expected and actual != expected:
        sys.exit(f"checksum mismatch: {actual} != {expected}")
    print("checksum OK" if expected else "no published checksum to verify")
    return release["tag_name"], data


def rings_of(geometry):
    """All rings (outer and holes) of a Polygon / MultiPolygon, as (N, 2) arrays."""
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [np.asarray(ring, dtype=np.float64) for poly in polys for ring in poly]


def fill(grid, rings, value, res):
    """Paint the cells whose centre is inside the rings (even-odd rule) with value."""
    height, width = grid.shape
    xs, ys = [], []
    for ring in rings:
        x = (ring[:, 0] + 180) / res
        y = (ring[:, 1] + 90) / res
        xs.append((x[:-1], x[1:]))
        ys.append((y[:-1], y[1:]))
    x0 = np.concatenate([a for a, _ in xs]); x1 = np.concatenate([b for _, b in xs])
    y0 = np.concatenate([a for a, _ in ys]); y1 = np.concatenate([b for _, b in ys])
    # Rows whose centre (r + 0.5) an edge crosses: ceil(min - 0.5) .. ceil(max - 0.5) - 1.
    lo = np.ceil(np.minimum(y0, y1) - 0.5).astype(np.int64)
    hi = np.ceil(np.maximum(y0, y1) - 0.5).astype(np.int64) - 1
    lo = np.clip(lo, 0, height - 1)
    hi = np.clip(hi, -1, height - 1)
    n = np.maximum(hi - lo + 1, 0)
    if not n.sum():
        return
    edge = np.repeat(np.arange(len(n)), n)
    rows = np.concatenate([np.arange(a, b + 1) for a, b in zip(lo[n > 0], hi[n > 0])])
    yc = rows + 0.5
    t = (yc - y0[edge]) / (y1[edge] - y0[edge])
    xc = x0[edge] + t * (x1[edge] - x0[edge])
    order = np.lexsort((xc, rows))
    rows, xc = rows[order], xc[order]
    # Consecutive crossings on a row pair up into inside spans.
    starts = np.flatnonzero(np.r_[True, rows[1:] != rows[:-1]])
    counts = np.diff(np.r_[starts, len(rows)])
    for s, c in zip(starts, counts):
        r = rows[s]
        xr = xc[s:s + c - (c % 2)]
        a = np.clip(np.ceil(xr[0::2] - 0.5).astype(np.int64), 0, width)
        b = np.clip(np.ceil(xr[1::2] - 0.5).astype(np.int64), 0, width)
        for ca, cb in zip(a, b):
            if cb > ca:
                grid[r, ca:cb] = value


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("--zip", help="use an already downloaded " + ASSET)
    p.add_argument("--out", default=OUT)
    args = p.parse_args()

    t0 = time.time()
    if args.zip:
        tag, data = "local", open(args.zip, "rb").read()
    else:
        tag, data = download()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.endswith(".json") or n.endswith(".geojson"))
        features = json.loads(z.read(name))["features"]
    del data
    print(f"{len(features)} zones read in {time.time() - t0:.0f}s, drawing the grid…")

    width, height = round(360 / RES), round(180 / RES)
    grid = np.zeros((height, width), dtype=np.uint16)
    names = []
    for i, f in enumerate(sorted(features, key=lambda f: f["properties"]["tzid"])):
        names.append(f["properties"]["tzid"])
        fill(grid, rings_of(f["geometry"]), len(names), RES)  # zone numbers start at 1
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(features)}")
    empty = int((grid == 0).sum())
    print(f"grid {width}×{height}, {empty} cells without zone ({100 * empty / grid.size:.3f}%)")

    head = b"TZG1" + struct.pack("<HHfH", width, height, RES, len(names))
    head += b"".join(struct.pack("<B", len(n.encode())) + n.encode() for n in names)
    body = __import__("zlib").compress(grid.astype("<u2").tobytes(), 9)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out + ".tmp", "wb") as f:
        f.write(head + body)
    os.replace(args.out + ".tmp", args.out)
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB, release {tag}) "
          f"in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
