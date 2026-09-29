#!/usr/bin/env python3
"""Souvenir: a private viewer for daily GPSLogger traces.

Everything is served from this machine: the traces, the map tiles
(tiles/*.pmtiles), the fonts, the icons and the JS libraries.
The Content-Security-Policy forbids the browser from contacting any
other origin, so no trace (nor the viewed area) ever leaves the machine.

Usage: python3 server.py [--data DIR] [--host 127.0.0.1] [--port 8000]
"""

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DAY_RE = re.compile(r"^(\d{8})\.(zip|csv|gpx)$")
TILES_DIR = os.path.join(ROOT, "tiles")
TILES_RE = re.compile(r"^/tiles/([A-Za-z0-9_-]+)\.pmtiles$")

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "worker-src 'self' blob:; "
    "child-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)

mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/x-protobuf", ".pbf")
mimetypes.add_type("application/octet-stream", ".pmtiles")


def list_days(data_dir):
    days = {}
    for name in os.listdir(data_dir):
        m = DAY_RE.match(name)
        if m:
            # Prefer zip, then csv, then gpx if several exist for one day.
            prio = {"zip": 0, "csv": 1, "gpx": 2}[m.group(2)]
            cur = days.get(m.group(1))
            if cur is None or prio < cur[0]:
                days[m.group(1)] = (prio, name)
    return {d: os.path.join(data_dir, v[1]) for d, v in sorted(days.items())}


def list_maps():
    """Names of the map archives in tiles/ (e.g. ["france", "world"])."""
    if not os.path.isdir(TILES_DIR):
        return []
    return sorted(n[:-8] for n in os.listdir(TILES_DIR) if n.endswith(".pmtiles"))


def _float(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def parse_csv(text):
    pts = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            t = int(row["timestamp_ms"]) if row.get("timestamp_ms") else _ms(row["time"])
            pts.append([t, float(row["lat"]), float(row["lon"]),
                        _float(row.get("elevation")), _float(row.get("accuracy")),
                        _float(row.get("speed")), row.get("provider") or "",
                        _float(row.get("battery"))])
        except (KeyError, ValueError):
            continue
    return pts


def parse_gpx(data):
    pts = []
    root = ET.fromstring(data)
    for el in root.iter():
        if not el.tag.endswith("trkpt"):
            continue
        sub = {c.tag.split("}")[-1]: c.text for c in el}
        if not sub.get("time"):
            continue
        hdop = _float(sub.get("hdop"))
        pts.append([_ms(sub["time"]), float(el.get("lat")), float(el.get("lon")),
                    _float(sub.get("ele")), hdop * 5 if hdop else None,
                    _float(sub.get("speed")), sub.get("src") or "", None])
    return pts


def load_track(path):
    if path.endswith(".csv"):
        with open(path, encoding="utf-8") as f:
            return parse_csv(f.read())
    if path.endswith(".gpx"):
        with open(path, "rb") as f:
            return parse_gpx(f.read())
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        csvs = [n for n in names if n.lower().endswith(".csv")]
        if csvs:
            return parse_csv(z.read(csvs[0]).decode("utf-8"))
        gpxs = [n for n in names if n.lower().endswith(".gpx")]
        if gpxs:
            return parse_gpx(z.read(gpxs[0]))
    return []


class Handler(SimpleHTTPRequestHandler):
    data_dir = None
    cache = {}  # path -> (mtime, points)

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **kw)

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        # Tile range requests are very chatty; only log failed requests.
        if str(getattr(code, "value", code)).startswith(("4", "5")):
            super().log_request(code, size)

    def send_json(self, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/days":
            return self.send_json(sorted(list_days(self.data_dir)))
        if url.path == "/api/track":
            day = parse_qs(url.query).get("day", [""])[0]
            path = list_days(self.data_dir).get(day)
            if not path:
                return self.send_error(HTTPStatus.NOT_FOUND, "Unknown day")
            mtime = os.path.getmtime(path)
            hit = self.cache.get(path)
            if not hit or hit[0] != mtime:
                hit = (mtime, sorted(load_track(path)))
                self.cache[path] = hit
            return self.send_json({"day": day, "points": hit[1]})
        if url.path == "/api/maps":
            return self.send_json(list_maps())
        m = TILES_RE.match(url.path)
        if m:
            return self.send_ranged(os.path.join(TILES_DIR, m.group(1) + ".pmtiles"))
        return super().do_GET()

    def send_ranged(self, path):
        """Serve a file with HTTP Range support (needed by PMTiles)."""
        if not os.path.isfile(path):
            return self.send_error(HTTPStatus.NOT_FOUND, "Map tiles not downloaded yet")
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        length = end - start + 1
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", f'"{int(os.path.getmtime(path))}-{size}"')
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            while length > 0:
                chunk = f.read(min(length, 1 << 16))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default=os.path.join(ROOT, "data"),
                   help="directory with the daily YYYYMMDD.zip files")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: localhost only)")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    if not os.path.isdir(args.data):
        sys.exit(f"Data directory not found: {args.data}")
    Handler.data_dir = os.path.abspath(args.data)
    maps = list_maps()
    if not maps:
        print("warning: no tiles/*.pmtiles, the map background will be empty")
    else:
        print(f"Maps: {', '.join(maps)}")
    print(f"Souvenir: {len(list_days(Handler.data_dir))} day(s) in {Handler.data_dir}")
    print(f"Open http://{args.host}:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
