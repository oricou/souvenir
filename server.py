#!/usr/bin/env python3
"""Souvenir: a private viewer for daily GPSLogger traces.

Everything is served from this machine: the traces, the photos, the map
tiles (tiles/*.pmtiles), the fonts, the icons and the JS libraries.
The Content-Security-Policy forbids the browser from contacting any
other origin, so no trace (nor the viewed area) ever leaves the machine.

Usage: python3 server.py [--data DIR] [--gps DIR] [--photos DIR] [--host 0.0.0.0] [--port 8080]

By default the site is reachable from the whole local network: protect it with
--password (or the SOUVENIR_PASSWORD environment variable, which other users
cannot see in `ps`), or use --host 127.0.0.1 to keep it to this computer.
"""

import argparse
import base64
import csv
import hmac
import io
import json
import mimetypes
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import photos
import timezones

ROOT = os.path.dirname(os.path.abspath(__file__))
# Daily trace files. GPSLogger's old versions (2016-2017) put a space before the
# extension ("20160204 .gpx") and wrote a .txt CSV without the provider column.
DAY_RE = re.compile(r"^(\d{8}) ?\.(zip|csv|gpx|txt)$")
# When a day has several files: zip, then csv, then gpx (has the provider), then txt.
DAY_PRIO = {"zip": 0, "csv": 1, "gpx": 2, "txt": 3}
TILES_DIR = os.path.join(ROOT, "tiles")
TILES_RE = re.compile(r"^/tiles/([A-Za-z0-9_-]+)\.pmtiles$")
PHOTO_RE = re.compile(r"^/(photos|thumbs)/(.+)$")
THUMBS_DIR = os.path.join(ROOT, "cache", "thumbs")
PHOTO_INDEX = os.path.join(ROOT, "cache", "photos.json")

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


_days = {}  # gps_dir -> (folder mtime, {day: path})


def list_days(gps_dir):
    """{day: path} of the daily trace files in gps_dir (see DAY_RE / DAY_PRIO).
    Empty files (e.g. an interrupted copy) are ignored. Re-read only when the
    folder changes (a file added, removed or renamed)."""
    try:
        mtime = os.stat(gps_dir).st_mtime
    except OSError:
        return {}
    hit = _days.get(gps_dir)
    if hit and hit[0] == mtime:
        return hit[1]
    days = {}
    with os.scandir(gps_dir) as it:
        for entry in it:
            m = DAY_RE.match(entry.name)
            if not m or not entry.is_file() or entry.stat().st_size == 0:
                continue
            prio = DAY_PRIO[m.group(2)]
            cur = days.get(m.group(1))
            if cur is None or prio < cur[0]:
                days[m.group(1)] = (prio, entry.name)
    result = {d: os.path.join(gps_dir, v[1]) for d, v in sorted(days.items())}
    _days[gps_dir] = (mtime, result)
    return result


_tracks = {}  # path -> (mtime, points)


def get_track(gps_dir, day):
    """(signature, points) of a day's trace, parsed once per file version;
    (None, []) if there is no trace for that day."""
    path = list_days(gps_dir).get(day)
    if not path:
        return None, []
    mtime = os.path.getmtime(path)
    hit = _tracks.get(path)
    if not hit or hit[0] != mtime:
        try:
            points = sorted(load_track(path))
        except (zipfile.BadZipFile, ET.ParseError, UnicodeDecodeError, OSError) as e:
            print(f"warning: cannot read {path}: {e}", file=sys.stderr)
            points = []
        for p in points:  # 9th field: local offset (minutes from UTC) at that place and time
            p.append(timezones.offset(p[1], p[2], p[0]))
        hit = (mtime, points)
        _tracks[path] = hit
    return hit


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
    if path.endswith((".csv", ".txt")):
        with open(path, encoding="utf-8") as f:
            return parse_csv(f.read())
    if path.endswith(".gpx"):
        with open(path, "rb") as f:
            return parse_gpx(f.read())
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for ext in (".csv", ".gpx", ".txt"):  # same preference as between loose files
            found = [n for n in names if n.lower().endswith(ext)]
            if found:
                data = z.read(found[0])
                return parse_gpx(data) if ext == ".gpx" else parse_csv(data.decode("utf-8"))
    return []


class Handler(SimpleHTTPRequestHandler):
    gps_dir = None
    photo_dir = None
    photo_index = None  # photos.PhotoIndex
    password = None     # asked from other devices (HTTP Basic auth); None = open

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **kw)

    def authorized(self):
        """True if the request may proceed; otherwise sends 401 so the browser asks.
        This computer is never asked; any user name is accepted, only the password counts."""
        if not self.password or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                given = ""
            if hmac.compare_digest(given.encode(), self.password.encode()):
                return True
            time.sleep(1)  # a wrong password costs a second: guessing is slow
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Souvenir", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_HEAD(self):
        if self.authorized():
            super().do_HEAD()

    def end_headers(self):
        if getattr(self, "revalidate", False):
            # Page files: always check for a newer version (cheap 304 otherwise),
            # so an edited app.js / style.css is never served stale from cache.
            self.send_header("Cache-Control", "no-cache")
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
        if not self.authorized():
            return
        url = urlparse(self.path)
        if url.path == "/api/days":
            # Days with a trace or photos (or both).
            return self.send_json(sorted(set(list_days(self.gps_dir)) | set(self.photo_index.days())))
        if url.path == "/api/track":
            day = parse_qs(url.query).get("day", [""])[0]
            sig, points = get_track(self.gps_dir, day)
            if sig is None:
                return self.send_error(HTTPStatus.NOT_FOUND, "Unknown day")
            return self.send_json({"day": day, "points": points})
        if url.path == "/api/maps":
            return self.send_json(list_maps())
        if url.path == "/api/photos":
            query = parse_qs(url.query)
            days = [d for d in query.get("days", [""])[0].split(",") if d]
            found = self.photo_index.photos_of(days)
            if "bbox" not in query:
                return self.send_json(found)
            # "ici": only the photos inside west,south,east,north, plus the days' total.
            try:
                w, s, e, n = map(float, query["bbox"][0].split(","))
            except ValueError:
                return self.send_error(HTTPStatus.BAD_REQUEST, "bbox=west,south,east,north")
            inside = [p for p in found if p["lat"] is not None
                      and s <= p["lat"] <= n and w <= p["lon"] <= e]
            return self.send_json({"total": len(found), "photos": inside})
        m = PHOTO_RE.match(url.path)
        if m:
            return self.send_photo(unquote(m.group(2)), thumb=m.group(1) == "thumbs")
        m = TILES_RE.match(url.path)
        if m:
            return self.send_ranged(os.path.join(TILES_DIR, m.group(1) + ".pmtiles"))
        self.revalidate = True
        return super().do_GET()

    def send_photo(self, name, thumb):
        # Only names from the listing: no path tricks can reach other files.
        if not self.photo_index.get(name):
            return self.send_error(HTTPStatus.NOT_FOUND, "Unknown photo")
        if thumb:
            path = photos.thumbnail(self.photo_dir, name, THUMBS_DIR)
            if path:
                return self.send_ranged(path, "image/jpeg", revalidate=True)
            if os.path.splitext(name)[1].lower() in photos.VIDEO_EXT:
                return self.send_error(HTTPStatus.NOT_FOUND, "No video preview (ffmpeg missing)")
        path = os.path.join(self.photo_dir, name)
        return self.send_ranged(path, mimetypes.guess_type(name)[0] or "application/octet-stream",
                                revalidate=True)

    def send_ranged(self, path, content_type="application/octet-stream", revalidate=False):
        """Serve a file with HTTP Range support (needed by PMTiles and videos).

        revalidate: let the browser keep the file but check it on each use
        ("no-cache"); an unchanged file then costs a 304 instead of a download."""
        if not os.path.isfile(path):
            return self.send_error(HTTPStatus.NOT_FOUND, "File not found")
        st = os.stat(path)
        size, etag = st.st_size, f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if revalidate and not self.headers.get("Range") and self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
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
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", etag)
        if revalidate:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            while length > 0:
                chunk = f.read(min(length, 1 << 16))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the browser stopped a video download: normal
                length -= len(chunk)


def lan_address():
    """This computer's address on the local network (no packet is sent)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # UDP "connect" only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default=os.path.join(ROOT, "data"),
                   help="directory holding gps/ and photos/")
    p.add_argument("--gps", default=None,
                   help="directory with the daily YYYYMMDD.zip traces (default: DATA/gps)")
    p.add_argument("--photos", default=None,
                   help="directory with photos and videos (default: DATA/photos)")
    p.add_argument("--host", default="0.0.0.0",
                   help="address to listen on (default: all, i.e. the local network too; "
                        "127.0.0.1 for this computer only)")
    p.add_argument("--port", type=int, default=8080, help="default: 8080")
    p.add_argument("--password",
                   help="password asked from other devices (this computer is never asked); "
                        "default: $SOUVENIR_PASSWORD, else none")
    args = p.parse_args()
    Handler.password = args.password or os.environ.get("SOUVENIR_PASSWORD") or None
    Handler.gps_dir = os.path.abspath(args.gps or os.path.join(args.data, "gps"))
    Handler.photo_dir = os.path.abspath(args.photos or os.path.join(args.data, "photos"))
    if not os.path.isdir(Handler.gps_dir) and not os.path.isdir(Handler.photo_dir):
        sys.exit(f"Neither {Handler.gps_dir} nor {Handler.photo_dir} exists")
    if timezones.get():
        print("Time zones: local time of each place (tiles/timezones.bin)")
    else:
        print("Time zones: none (run tools/build_timezones.py), times shown in this computer's zone")
    maps = list_maps()
    if not maps:
        print("warning: no tiles/*.pmtiles, the map background will be empty")
    else:
        print(f"Maps: {', '.join(maps)}")
    print(f"Traces: {len(list_days(Handler.gps_dir))} day(s) in {Handler.gps_dir}")
    Handler.photo_index = photos.PhotoIndex(
        Handler.photo_dir, PHOTO_INDEX, lambda day: get_track(Handler.gps_dir, day))
    print(f"Photos: {len(Handler.photo_index)} in {Handler.photo_dir}")
    if args.host in ("0.0.0.0", "::", ""):
        print(f"Open http://127.0.0.1:{args.port}/ (this computer) or "
              f"http://{lan_address()}:{args.port}/ (other devices on the network)")
        print("Password: asked from other devices" if Handler.password else
              "warning: no password, every device on the network can see the site (see --password)")
    else:
        print(f"Open http://{args.host}:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
