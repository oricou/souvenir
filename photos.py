"""Photos and videos of the days: when they were taken, where, and previews.

The time comes from the file itself (EXIF for photos, the "mvhd" box for
MP4/MOV videos), falling back to Pixel-style names (PXL_YYYYMMDD_HHMMSSmmm,
in UTC). GPS coordinates are read from EXIF when the camera recorded them;
otherwise the page places the photo on the day's trace from its time.

Where a photo was taken: its EXIF GPS if any (an admin can write one by
dragging the photo on the map, see write_gps()), otherwise the day's trace at
that time (see locate()). Positions are stored with the trace file's signature
and recomputed only when that trace changes.

Reading this metadata costs ~2 ms per file, too slow to redo for thousands
of photos at each start: PhotoIndex keeps it in memory, indexed by day, and
saves it (with the positions) to a JSON file. The folders are only re-scanned when one of them
changes (adding, removing or renaming a file changes its folder's mtime), and
then only new or modified files are read again.

Pillow (for photo previews and EXIF) and ffmpeg (for video previews) are
optional: without them, originals are served and times come from file names.
"""

import bisect
import json
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import timezones
import shutil
import struct
import subprocess
import threading
from datetime import datetime, timedelta, timezone

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
THUMB_WIDTH = 640  # twice the panel width, sharp on high-DPI screens
PIXEL_NAME = re.compile(r"PXL_(\d{8})_(\d{6})(\d{3})?")
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
META_VERSION = 4  # bump when _meta() changes, to re-read every file once
LOCATE_MAX_GAP = 15 * 60 * 1000  # ms: farther than this from the trace = not placed
# Trace point fields (see server.load_track).
T, LAT, LON, PROV = 0, 1, 2, 6


def locate(t, points):
    """(lat, lon) of the trace at time t (ms), interpolated between the GPS points
    just before and after (all points if the day has no GPS fix); None if t is
    more than LOCATE_MAX_GAP before the first or after the last point."""
    pts = [p for p in points if p[PROV] == "gps"] or points
    if not pts:
        return None
    i = bisect.bisect_left([p[T] for p in pts], t)
    if i == 0 or i == len(pts):
        p = pts[0] if i == 0 else pts[-1]
        return (p[LAT], p[LON]) if abs(p[T] - t) <= LOCATE_MAX_GAP else None
    a, b = pts[i - 1], pts[i]
    f = (t - a[T]) / ((b[T] - a[T]) or 1)
    return a[LAT] + (b[LAT] - a[LAT]) * f, a[LON] + (b[LON] - a[LON]) * f


def _exif(path):
    """(utc datetime, lat, lon, width, height, offset) from a photo's EXIF; None if
    unknown. offset: minutes from UTC of the camera's clock, where known.

    width/height are as displayed, i.e. swapped for photos rotated by 90°.
    """
    unknown = (None,) * 6  # unreadable file, or no Pillow: the PXL_ name may still date it
    if Image is None:
        return unknown
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            w, h = im.size
    except Exception:
        return unknown
    if ex.get(0x0112) in (5, 6, 7, 8):  # Orientation: rotated 90° either way
        w, h = h, w
    sub = ex.get_ifd(0x8769)  # Exif IFD
    when = None  # the camera's clock time, local to where the photo was taken
    raw = sub.get(0x9003) or ex.get(0x0132)  # DateTimeOriginal, DateTime
    if raw:
        try:
            when = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
            off = sub.get(0x9011) or sub.get(0x9010)  # OffsetTimeOriginal, OffsetTime
            m = re.match(r"([+-])(\d\d):(\d\d)", str(off or ""))
            if m:
                delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
                when = when.replace(tzinfo=timezone(delta if m.group(1) == "+" else -delta))
        except ValueError:
            when = None
    lat = lon = None
    gps = ex.get_ifd(0x8825)
    try:
        if gps.get(2) and gps.get(4):
            dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            lat = dms(gps[2]) * (-1 if gps.get(1) == "S" else 1)
            lon = dms(gps[4]) * (-1 if gps.get(3) == "W" else 1)
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        lat = lon = None
    if when is not None and when.tzinfo is None:
        # No offset recorded: the clock time is local to the photo's own position
        # if it has one (old cameras abroad), else to this machine's zone.
        zone = None
        if lat is not None and timezones.get():
            try:
                zone = ZoneInfo(timezones.get().zone_at(lat, lon) or "")
            except (ZoneInfoNotFoundError, ValueError):
                zone = None
        when = when.replace(tzinfo=zone) if zone else when.astimezone()
    off = int(when.utcoffset().total_seconds() // 60) if when is not None else None
    return (when.astimezone(timezone.utc) if when else None), lat, lon, w, h, off


def _boxes(f, pos, stop):
    """Yield (type, payload position, payload end) of the MP4 boxes in [pos, stop)."""
    while pos + 8 <= stop:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = stop - pos
        if size < header:
            return
        yield kind, pos + header, pos + size
        pos += size


def _mp4(path):
    """(UTC start, width, height) of an MP4/MOV recording; None if unknown.

    The start is the mvhd creation time minus the duration (phones stamp the
    end of the recording). Width/height come from the video track's tkhd, swapped
    when its matrix rotates the picture by 90° (phones film portrait that way).
    """
    start = w = h = None
    try:
        with open(path, "rb") as f:
            for kind, pos, stop in _boxes(f, 0, os.path.getsize(path)):
                if kind != b"moov":
                    continue
                for kind2, pos2, stop2 in _boxes(f, pos, stop):
                    if kind2 == b"mvhd":
                        f.seek(pos2)
                        version = f.read(4)[0]
                        if version == 1:
                            created, _, scale, duration = struct.unpack(">QQIQ", f.read(28))
                        else:
                            created, _, scale, duration = struct.unpack(">IIII", f.read(16))
                        if created:
                            start = MP4_EPOCH + timedelta(seconds=created)
                            if scale:
                                start -= timedelta(seconds=duration / scale)
                    elif kind2 == b"trak" and w is None:
                        for kind3, pos3, _ in _boxes(f, pos2, stop2):
                            if kind3 != b"tkhd":
                                continue
                            f.seek(pos3)
                            version = f.read(4)[0]
                            f.seek(pos3 + (4 + 32 if version == 1 else 4 + 20) + 16)
                            a, b = struct.unpack(">ii", f.read(8))
                            f.seek(28, 1)  # rest of the 3x3 matrix
                            tw, th = (v >> 16 for v in struct.unpack(">II", f.read(8)))
                            if tw and th:  # audio tracks have no size
                                w, h = (th, tw) if a == 0 and abs(b) == 0x10000 else (tw, th)
    except (OSError, struct.error, IndexError):
        pass
    return start, w, h


def _from_name(name):
    m = PIXEL_NAME.search(name)
    if not m:
        return None
    when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return when.replace(tzinfo=timezone.utc, microsecond=int(m.group(3) or 0) * 1000)


def _meta(path, name):
    ext = os.path.splitext(name)[1].lower()
    kind = "video" if ext in VIDEO_EXT else "image"
    if kind == "video":
        (when, w, h), lat, lon, off = _mp4(path), None, None, None
    else:
        when, lat, lon, w, h, off = _exif(path)
    if when is None:
        when, off = _from_name(name), None
    if when is None:
        return None
    # The day a photo belongs to: its local date where it was taken when known
    # (off: the camera's offset, or its GPS position's zone), else this machine's.
    local = when + timedelta(minutes=off) if off is not None else when.astimezone()
    return {"name": name, "kind": kind, "t": int(when.timestamp() * 1000),
            "day": local.strftime("%Y%m%d"), "lat": lat, "lon": lon, "w": w, "h": h, "off": off}


def place_offset(meta, lat, lon):
    """Minutes from UTC of the local time where the photo was taken: from its
    position's time zone, else the camera's own offset; None if unknown."""
    if lat is not None:
        off = timezones.offset(lat, lon, meta["t"])
        if off is not None:
            return off
    return meta.get("off")


class PhotoIndex:
    """Metadata of all photos under photo_dir (subfolders included), by day and name.

    Names are paths relative to photo_dir, with "/" separators.
    """

    def __init__(self, photo_dir, cache_file, track_for=lambda day: (None, [])):
        """track_for(day) -> (signature, points) of that day's trace; the signature
        (e.g. the file's mtime) changes whenever the trace changes, None if no trace."""
        self.photo_dir = photo_dir
        self.cache_file = cache_file
        self.track_for = track_for
        self.files = {}    # name -> {"mtime", "size", "meta"}
        self.positions = {}  # name -> {"sig", "lat", "lon"} for photos placed on the trace
        self.dirs = {}     # folder path -> mtime, as of the last scan
        self.by_day = {}   # "YYYYMMDD" -> [meta, ...] sorted by time
        self.by_name = {}  # name -> meta
        self.lock = threading.Lock()
        try:
            with open(cache_file, encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("version") == META_VERSION and saved.get("photo_dir") == photo_dir:
                self.files = saved["files"]
                self.positions = saved.get("positions", {})
        except (OSError, ValueError, KeyError):
            pass
        self.refresh()

    def _changed(self):
        if not self.dirs:
            return True
        for path, mtime in self.dirs.items():
            try:
                if os.stat(path).st_mtime != mtime:
                    return True
            except OSError:
                return True
        return False

    def refresh(self):
        """Re-scan if a folder changed; cheap (one stat per folder) otherwise."""
        with self.lock:
            if not os.path.isdir(self.photo_dir) or not self._changed():
                return
            dirs, files, dirty = {}, {}, False
            for root, subdirs, names in os.walk(self.photo_dir):
                subdirs[:] = [d for d in subdirs if not d.startswith(".")]
                dirs[root] = os.stat(root).st_mtime
                for n in names:
                    if os.path.splitext(n)[1].lower() not in IMAGE_EXT | VIDEO_EXT:
                        continue
                    path = os.path.join(root, n)
                    name = os.path.relpath(path, self.photo_dir).replace(os.sep, "/")
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    old = self.files.get(name)
                    if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
                        files[name] = old
                    else:
                        files[name] = {"mtime": st.st_mtime, "size": st.st_size,
                                       "meta": _meta(path, name)}
                        dirty = True
            dirty = dirty or files.keys() != self.files.keys()
            self.dirs, self.files = dirs, files
            self.positions = {n: p for n, p in self.positions.items() if n in files}
            self._build()
            if dirty:
                self._save()

    def _build(self):
        """by_name and by_day from self.files (lock held)."""
        self.by_name = {n: f["meta"] for n, f in self.files.items() if f["meta"]}
        by_day = {}
        for m in self.by_name.values():
            by_day.setdefault(m["day"], []).append(m)
        for lst in by_day.values():
            lst.sort(key=lambda m: m["t"])
        self.by_day = by_day

    def reread(self, name):
        """Read a photo's metadata again, whatever its date and size: after write_gps(),
        which keeps the date, a second position takes as many bytes as the first one,
        so refresh() would not see the change."""
        self.refresh()
        path = os.path.join(self.photo_dir, name)
        st = os.stat(path)
        meta = _meta(path, name)
        with self.lock:
            self.files[name] = {"mtime": st.st_mtime, "size": st.st_size, "meta": meta}
            self._build()
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        tmp = self.cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": META_VERSION, "photo_dir": self.photo_dir,
                       "files": self.files, "positions": self.positions},
                      f, separators=(",", ":"))
        os.replace(tmp, self.cache_file)

    def names_of_days(self, days):
        """Names of the photos of these days."""
        self.refresh()
        return {m["name"] for d in days for m in self.by_day.get(d, [])}

    def days(self):
        """Days that have at least one photo."""
        self.refresh()
        return list(self.by_day)

    def photos_of(self, days):
        """Photos of these days, each with its position: lat/lon (None if it cannot
        be placed) and exact (True for the camera's own GPS)."""
        self.refresh()
        out, dirty = [], False
        with self.lock:
            for day in days:
                photos = self.by_day.get(day, [])
                sig = points = None
                if any(m["lat"] is None for m in photos):
                    sig, points = self.track_for(day)
                for m in photos:
                    if m["lat"] is not None:
                        out.append({**m, "exact": True, "off": place_offset(m, m["lat"], m["lon"])})
                        continue
                    pos = self.positions.get(m["name"])
                    if not pos or pos["sig"] != sig:
                        at = locate(m["t"], points) if points else None
                        pos = {"sig": sig, "lat": at and at[0], "lon": at and at[1]}
                        self.positions[m["name"]] = pos
                        dirty = True
                    out.append({**m, "lat": pos["lat"], "lon": pos["lon"], "exact": False,
                                "off": place_offset(m, pos["lat"], pos["lon"])})
            if dirty:
                self._save()
        return out

    def photos_named(self, names):
        """{name: photo with its position (as photos_of)} for these photo names."""
        self.refresh()
        wanted = set(names)
        days = sorted({self.by_name[n]["day"] for n in wanted if n in self.by_name})
        return {p["name"]: p for p in self.photos_of(days) if p["name"] in wanted}

    def get(self, name):
        self.refresh()
        return self.by_name.get(name)

    def __len__(self):
        return len(self.by_name)


def data_size(path):
    """Size of a file without its metadata, so that writing tags (a GPS position…)
    does not change it while the image stays the same. For a JPEG: without its APPn
    (EXIF, XMP…) and COM segments; other files: their whole size."""
    size = os.path.getsize(path)
    if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg"):
        return size
    meta = 0
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            return size
        while True:  # the segments before the image data, each "FF xx" + its length
            if f.read(1) != b"\xff":
                return size  # not as expected: count everything
            m = f.read(1)
            while m == b"\xff":  # fill bytes
                m = f.read(1)
            if not m or m[0] in (0xDA, 0xD9):  # start of scan: the image data follows
                break
            if m[0] == 0x01 or 0xD0 <= m[0] <= 0xD8:  # markers without a length
                continue
            n = int.from_bytes(f.read(2), "big")
            if n < 2:
                return size
            if 0xE0 <= m[0] <= 0xEF or m[0] == 0xFE:
                meta += n + 2
            f.seek(n - 2, 1)
    return size - meta


def same_image(path, old, now):
    """Whether a cache entry made from a photo (old: its "mtime", "size" and "dsize")
    still holds for the file at path, now {"mtime", "size"}: same date and size, or
    same date and same data_size() (only tags were written, by write_gps() for one).
    Sets now["dsize"], computed only when needed."""
    if old.get("mtime") != now["mtime"]:
        return False
    if old.get("size") == now["size"]:
        now["dsize"] = old.get("dsize") or data_size(path)
        return True
    now["dsize"] = data_size(path)
    return old.get("dsize") == now["dsize"]


def write_gps(path, lat, lon):
    """Write a GPS position into a photo's EXIF with ExifTool (lat None: remove it).
    The file keeps its date, and its image data is not touched (same data_size()):
    tools/embed_photos.py and tools/find_faces.py see it as the same photo."""
    exiftool = shutil.which("exiftool")
    if not exiftool:
        raise RuntimeError("ExifTool is missing: sudo apt install libimage-exiftool-perl")
    if lat is None:
        tags = ["-gps:all="]
    else:
        tags = [f"-GPSLatitude={abs(lat):.7f}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                f"-GPSLongitude={abs(lon):.7f}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    st = os.stat(path)
    r = subprocess.run([exiftool, "-P", "-overwrite_original", "-m", "-q", *tags, path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "exiftool failed")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # -P keeps whole seconds only


def thumbnail(photo_dir, name, cache_dir):
    """Path of a JPEG preview of photo_dir/name (made once), or None if impossible."""
    src = os.path.join(photo_dir, name)
    dst = os.path.join(cache_dir, name + ".jpg")
    if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.{threading.get_ident()}.tmp"
    try:
        if os.path.splitext(name)[1].lower() in VIDEO_EXT:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return None
            # The frame at 1 s (skips a dark first frame); very short videos
            # (e.g. a single-frame timelapse) have none: take the first one.
            for seek in ("1", "0"):
                subprocess.run(
                    [ffmpeg, "-v", "error", "-y", "-ss", seek, "-i", src, "-frames:v", "1",
                     "-vf", f"scale={THUMB_WIDTH}:-2", "-f", "image2", tmp],
                    check=True, timeout=60)
                if os.path.isfile(tmp) and os.path.getsize(tmp):
                    break
            else:
                return None
        else:
            if Image is None:
                return None
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
                im.convert("RGB").save(tmp, "JPEG", quality=82)
        os.replace(tmp, dst)
        return dst
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
