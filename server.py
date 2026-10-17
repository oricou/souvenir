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
import signal
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import photos
import people
import search
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
    admin_password = None  # unlocks deleting photos (moved to trash_dir); None = no admin
    trash_dir = None
    admin_sessions = {}    # token -> expiry time (admin cookie)
    admin_lock = threading.Lock()
    ADMIN_IDLE = 30 * 60   # an admin session ends after 30 min without admin action
    search = None       # search.Search: photos by meaning
    exports = {}        # ticket -> (expiry, file name, [photo names]): a zip to download
    export_lock = threading.Lock()
    EXPORT_TTL = 15 * 60
    people = None       # people.People: names of the faces

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

    def send_json(self, obj, cookie=None):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(HTTPStatus.OK)
        if cookie:
            self.send_header("Set-Cookie", cookie)
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
        if url.path == "/api/search":
            return self.send_search(parse_qs(url.query))
        if url.path == "/api/photos":
            query = parse_qs(url.query)
            days = [d for d in query.get("days", [""])[0].split(",") if d]
            if "all" in query:  # "Dates : toutes": every day's photos
                days = sorted(self.photo_index.days())
            found = self.with_people(self.photo_index.photos_of(days))
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
        if url.path == "/api/admin":
            return self.send_json({"enabled": bool(self.admin_password), "logged": self.is_admin()})
        if url.path == "/api/people":  # ?days=…: count each person's photos on those days
            days = [d for d in parse_qs(url.query).get("days", [""])[0].split(",") if d]
            # Only photos still there (a deleted photo's faces stay in cache/faces.* until
            # tools/find_faces.py runs again).
            among = self.photo_index.names_of_days(days) if days else set(self.photo_index.by_name)
            return self.send_json({"available": self.people.available(), "people": self.people.people(among)})
        if url.path == "/api/faces":
            photo = parse_qs(url.query).get("photo", [""])[0]
            return self.send_json(self.people.faces_of(photo) if self.photo_index.get(photo) else [])
        if url.path == "/api/people/photos":
            name = parse_qs(url.query).get("name", [""])[0]
            name = self.people.canonical(name)
            found = self.people.photos_of(name)
            info = self.photo_index.photos_named(list(found))
            result = sorted((info[n] for n in found if n in info), key=lambda p: p["t"])
            return self.send_json({"name": name, "total": len(result), "photos": self.with_people(result)})
        m = re.fullmatch(r"/export/([\w-]+)", url.path)
        if m:
            return self.send_export(m.group(1))
        if url.path == "/face":
            return self.send_face(parse_qs(url.query))
        m = PHOTO_RE.match(url.path)
        if m:
            return self.send_photo(unquote(m.group(2)), thumb=m.group(1) == "thumbs")
        m = TILES_RE.match(url.path)
        if m:
            return self.send_ranged(os.path.join(TILES_DIR, m.group(1) + ".pmtiles"))
        self.revalidate = True
        return super().do_GET()

    def do_POST(self):
        """The user's answers about people: {photo, face, name} to /api/people/name
        ("this face is name") or /api/people/refuse ("this face is not name"), and
        {name, aliases} to /api/people/aliases (their other names), {name, to, merge}
        to /api/people/rename (a corrected name; merge: with the person already called so)."""
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if path == "/api/people/aliases":
            return self.post_aliases()
        if path == "/api/export":
            return self.post_export()
        if path == "/api/people/rename":
            return self.post_rename()
        if path.startswith("/api/admin/") or path in ("/api/photos/delete", "/api/photos/restore",
                                                      "/api/photos/place", "/api/photos/rotate"):
            return self.post_admin(path)
        if path not in ("/api/people/name", "/api/people/refuse"):
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 10_000:
                raise ValueError("body size")
            body = json.loads(self.rfile.read(length))
            photo, face, name = str(body["photo"]), int(body["face"]), str(body["name"]).strip()
            if not self.photo_index.get(photo) or not name or len(name) > 100:
                raise ValueError("photo or name")
            if path.endswith("/name"):
                name = self.people.name_face(photo, face, name)
            else:
                self.people.refuse_face(photo, face, name)
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        return self.send_json({"ok": True, "name": name, "faces": self.people.faces_of(photo)})

    # ---------- export: the photos of the panel as one zip ----------

    def post_export(self):
        """{names: [...], title}: remember which photos to zip; returns {ticket, count,
        bytes}. The page then downloads /export/TICKET (a plain link: the CSP forbids
        forms, and the browser shows the download's progress itself)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 5_000_000:
                raise ValueError("body size")
            body = json.loads(self.rfile.read(length))
            names = [n for n in dict.fromkeys(map(str, body["names"])) if self.photo_index.get(n)]
            if not names:
                raise ValueError("no photo to export")
            title = " ".join(re.sub(r"[^\w .,'-]+", " ", str(body.get("title", ""))).split())[:60] or "photos"
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e).strip("'\"")})
        size = 0
        for n in names:
            try:
                size += os.path.getsize(os.path.join(self.photo_dir, n))
            except OSError:
                pass
        ticket = secrets.token_urlsafe(16)
        with self.export_lock:
            now = time.time()
            for t in [t for t, e in self.exports.items() if e[0] < now]:
                del self.exports[t]
            self.exports[ticket] = (now + self.EXPORT_TTL, f"souvenir {title}.zip", names)
        return self.send_json({"ticket": ticket, "count": len(names), "bytes": size})

    def send_export(self, ticket):
        """The zip of an export, written while it is sent (no temporary file): photos
        stored as they are (already compressed), with their subfolders."""
        with self.export_lock:
            entry = self.exports.get(ticket)
        if not entry or entry[0] < time.time():
            return self.send_error(HTTPStatus.NOT_FOUND, "export expired: ask it again")
        _, filename, names = entry
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            # The connection is not seekable: zipfile then writes each size after its file.
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for n in names:
                    try:
                        z.write(os.path.join(self.photo_dir, n), arcname=n)
                    except OSError:
                        continue  # deleted meanwhile
        except (BrokenPipeError, ConnectionResetError):
            pass  # download cancelled

    def post_rename(self):
        """{name, to, merge}: correct a person's name; 409 {merge: other} if `to` is
        already another person and merge is not true."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 10_000:
                raise ValueError("body size")
            body = json.loads(self.rfile.read(length))
            name = self.people.rename(str(body["name"]), str(body["to"]), merge=body.get("merge") is True)
        except LookupError as e:
            if not isinstance(e, KeyError):  # `to` is someone else: merge them?
                return self.send_json_status(HTTPStatus.CONFLICT, {"error": "already a person", "merge": e.args[0]})
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e).strip("'\"")})
        except (ValueError, TypeError) as e:
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        return self.send_json({"ok": True, "name": name})

    def post_aliases(self):
        """{name, aliases: [...]}: the other names of a person."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 10_000:
                raise ValueError("body size")
            body = json.loads(self.rfile.read(length))
            aliases = body["aliases"]
            if not isinstance(aliases, list) or len(aliases) > 20:
                raise ValueError("aliases: a list of at most 20 names")
            name, aliases = self.people.set_aliases(str(body["name"]), aliases)
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e).strip("'\"")})
        return self.send_json({"ok": True, "name": name, "aliases": aliases})

    # ---------- admin: deleting photos (moved to the trash, never erased) ----------

    def is_admin(self):
        """A valid admin cookie (renewed by each admin action)."""
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)souvenir_admin=([\w-]+)", cookie)
        if not m or not self.admin_password:
            return False
        with self.admin_lock:
            expiry = self.admin_sessions.get(m.group(1))
            if not expiry or expiry < time.time():
                self.admin_sessions.pop(m.group(1), None)
                return False
            self.admin_sessions[m.group(1)] = time.time() + self.ADMIN_IDLE
            return True

    def post_admin(self, path):
        """/api/admin/login {password}, /api/admin/logout, and for an admin:
        /api/photos/delete {name} (moved to the trash), /api/photos/restore {name, trash},
        /api/photos/place {name, lat, lon} (GPS written into the photo; lat null: removed),
        /api/photos/rotate {name, quarters} (1: a quarter turn clockwise, -1: anticlockwise)."""
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 10_000:
                raise ValueError("body size")
            body = json.loads(self.rfile.read(length)) if length else {}
        except ValueError as e:
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        if not self.admin_password:
            return self.send_json_status(HTTPStatus.FORBIDDEN, {"error": "no admin password set on the server"})
        if path == "/api/admin/login":
            if not hmac.compare_digest(str(body.get("password", "")).encode(), self.admin_password.encode()):
                time.sleep(1)  # a wrong password costs a second: guessing is slow
                return self.send_json_status(HTTPStatus.FORBIDDEN, {"error": "wrong admin password"})
            token = secrets.token_urlsafe(32)
            with self.admin_lock:
                now = time.time()
                for t in [t for t, e in self.admin_sessions.items() if e < now]:
                    del self.admin_sessions[t]
                self.admin_sessions[token] = now + self.ADMIN_IDLE
            return self.send_json({"ok": True}, cookie=f"souvenir_admin={token}; Path=/; HttpOnly; SameSite=Strict")
        if path == "/api/admin/logout":
            m = re.search(r"(?:^|;\s*)souvenir_admin=([\w-]+)", self.headers.get("Cookie", ""))
            with self.admin_lock:
                self.admin_sessions.pop(m.group(1) if m else "", None)
            return self.send_json({"ok": True}, cookie="souvenir_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        if not self.is_admin():
            return self.send_json_status(HTTPStatus.FORBIDDEN, {"error": "admin login needed", "login": True})
        name = str(body.get("name", ""))
        try:
            if path == "/api/photos/rotate":
                meta = self.photo_index.get(name)
                if not meta:
                    raise ValueError("unknown photo")
                if os.path.splitext(name)[1].lower() not in (".jpg", ".jpeg"):
                    raise ValueError("only JPEG photos can be turned")
                quarters = body.get("quarters")
                if quarters not in (1, -1):
                    raise ValueError("quarters: 1 or -1")
                photos.rotate(os.path.join(self.photo_dir, name), quarters)
                self.photo_index.reread(name)
                return self.send_json({"ok": True, "photo": self.photo_index.photos_named([name]).get(name)})
            if path == "/api/photos/place":
                meta = self.photo_index.get(name)
                if not meta:
                    raise ValueError("unknown photo")
                if meta["kind"] != "image":
                    raise ValueError("only photos can be moved, not videos")
                lat, lon = body.get("lat"), body.get("lon")
                if lat is not None:
                    lat, lon = float(lat), float(lon)
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("lat/lon out of range")
                photos.write_gps(os.path.join(self.photo_dir, name), lat, lon)
                self.photo_index.reread(name)
                return self.send_json({"ok": True, "photo": self.photo_index.photos_named([name]).get(name)})
            if path == "/api/photos/delete":
                if not self.photo_index.get(name):
                    raise ValueError("unknown photo")
                dst = os.path.join(self.trash_dir, name)
                base, ext = os.path.splitext(dst)
                n = 2
                while os.path.exists(dst):  # never overwrite something already in the trash
                    dst, n = f"{base} ({n}){ext}", n + 1
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(os.path.join(self.photo_dir, name), dst)
                return self.send_json({"ok": True, "trash": os.path.relpath(dst, self.trash_dir)})
            # restore: put a photo of the trash back where it was ("Annuler")
            src = os.path.realpath(os.path.join(self.trash_dir, str(body.get("trash", ""))))
            dst = os.path.realpath(os.path.join(self.photo_dir, name))
            if (not src.startswith(os.path.realpath(self.trash_dir) + os.sep) or not os.path.isfile(src)
                    or not dst.startswith(os.path.realpath(self.photo_dir) + os.sep) or os.path.exists(dst)):
                raise ValueError("nothing to restore")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            return self.send_json({"ok": True})
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            return self.send_json_status(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    def send_json_status(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def with_people(self, photo_list):
        """The photos, each with the names of the people recognised in it."""
        if self.people.available():
            for p in photo_list:
                p["people"] = self.people.names_in(p["name"])
        return photo_list

    def send_face(self, query):
        """A small square image of a face: /face?photo=NAME&face=INDEX."""
        photo = query.get("photo", [""])[0]
        meta = self.photo_index.get(photo)
        try:
            index = int(query.get("face", [""])[0])
            if not meta:
                raise KeyError
            source = (photos.thumbnail(self.photo_dir, photo, THUMBS_DIR) if meta["kind"] == "video"
                      else os.path.join(self.photo_dir, photo))
            path = self.people.crop(photo, index, source)
        except (ValueError, KeyError, OSError):
            return self.send_error(HTTPStatus.NOT_FOUND, "Unknown face")
        return self.send_ranged(path, "image/jpeg", revalidate=True)

    def send_search(self, query):
        """Photos matching ?q= (by meaning), best first; ?bbox= keeps those in an area
        ("ici"), ?days= those of some days, ?limit= the number returned."""
        text = query.get("q", [""])[0].strip()
        if not text:
            return self.send_json({"total": 0, "photos": []})
        # Known names in the query ("Laurence et Paul à vélo") keep only the photos where
        # all of them are recognised; the rest of the text ranks those photos by meaning.
        names, rest = self.people.split_query(text) if self.people.available() else ([], text)
        among, warning = None, None
        if names:
            among = set.intersection(*(set(self.people.photos_of(n)) for n in names))
        if "days" in query:  # the days selected in the calendar: rank among their photos only
            in_days = self.photo_index.names_of_days(d for d in query["days"][0].split(",") if d)
            among = in_days if among is None else among & in_days
        if names and not rest:  # just names: all their photos, by date
            ranked = [(n, None) for n in among]
        else:
            try:
                ranked = self.search.ranked(rest, among)
            except search.SearchError as e:
                if not names:
                    return self.send_json({"error": str(e), "total": 0, "photos": []})
                ranked, warning = [(n, None) for n in among], f"« {rest} » ignoré : {e}"
        info = self.photo_index.photos_named([n for n, _ in ranked])
        found = self.with_people([{**info[n], "score": s} for n, s in ranked if n in info])
        if ranked and ranked[0][1] is None:
            found.sort(key=lambda p: p["t"])
        if "bbox" in query:
            try:
                w, s, e, n = map(float, query["bbox"][0].split(","))
            except ValueError:
                return self.send_error(HTTPStatus.BAD_REQUEST, "bbox=west,south,east,north")
            found = [p for p in found if p["lat"] is not None
                     and s <= p["lat"] <= n and w <= p["lon"] <= e]
        try:
            limit = max(1, min(1000, int(query.get("limit", ["200"])[0])))
        except ValueError:
            limit = 200
        result = {"total": len(found), "photos": found[:limit], "names": names, "rest": rest,
                  "best": ranked[0][1] if ranked else None}  # the page's slider: a share of it
        if warning:
            result["warning"] = warning
        return self.send_json(result)

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
    p.add_argument("--admin-password",
                   help="password to delete (to DATA/trash), turn and move photos; "
                        "default: $SOUVENIR_ADMIN_PASSWD, else the site changes no photo")
    p.add_argument("--embed-api",
                   help="OpenAI-compatible API of the embedding model used for search "
                        "(e.g. vLLM on a GPU); default: llama.cpp on this CPU (make search)")
    p.add_argument("--embed-key",
                   help="key of that API if it needs one (default: $SOUVENIR_API_KEY)")
    args = p.parse_args()
    # Take the port first: if it is busy (another Souvenir?), say so at once, before
    # reading the indexes and starting the search model for nothing.
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot listen on {args.host}:{args.port} ({e.strerror}): another program uses it? "
                 f"(ss -ltnp | grep :{args.port})")
    Handler.search = search.Search(args.embed_api, args.embed_key or os.environ.get("SOUVENIR_API_KEY"))
    Handler.password = args.password or os.environ.get("SOUVENIR_PASSWORD") or None
    Handler.admin_password = args.admin_password or os.environ.get("SOUVENIR_ADMIN_PASSWD") or None
    Handler.trash_dir = os.path.abspath(os.path.join(args.data, "trash"))
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
        Handler.photo_dir,
        # Another photo folder (a test…) gets its own index: never replaces the site's.
        PHOTO_INDEX if Handler.photo_dir == os.path.join(ROOT, "data", "photos")
        else f"{PHOTO_INDEX[:-5]}-{hashlib.sha1(Handler.photo_dir.encode()).hexdigest()[:10]}.json",
        lambda day: get_track(Handler.gps_dir, day))
    print(f"Photos: {len(Handler.photo_index)} in {Handler.photo_dir}")
    # systemd stops services with SIGTERM: exit normally, so the search model
    # (a child process) is stopped too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Handler.search.start()
    Handler.people = people.People(store=os.path.join(os.path.abspath(args.data), "people.json"))
    if Handler.people.available():
        names = Handler.people.people()
        print(f"People: {len(Handler.people.faces)} faces found, {len(names)} names given"
              + (f" ({', '.join(p['name'] for p in names[:5])}…)" if names else ""))
    else:
        print("People: no faces found yet (make faces, then tools/find_faces.py)")
    print(f"Search: {Handler.search.status()}")
    if args.host in ("0.0.0.0", "::", ""):
        print(f"Open http://127.0.0.1:{args.port}/ (this computer) or "
              f"http://{lan_address()}:{args.port}/ (other devices on the network)")
        print("Access: password protected (the browsers of other devices will ask for it; "
              "this computer has free access)" if Handler.password else
              "warning: no password, every device on the network can see the site (see --password)")
    else:
        print(f"Open http://{args.host}:{args.port}/")
    print(f"Admin: deleting photos moves them to {Handler.trash_dir}" if Handler.admin_password else
          "Admin: off (no admin password: set SOUVENIR_ADMIN_PASSWD to delete photos)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
