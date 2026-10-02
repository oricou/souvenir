#!/usr/bin/env python3
"""Set or fix the date and GPS position stored in photos (JPEG, PNG, WebP).

Interactive, one photo at a time: each field is shown pre-filled and can be
edited (Enter keeps it):
    tools/edit_photo.py data/photos/IMG_1234.jpg

Batch, the same change for many photos:
    tools/edit_photo.py *.jpg --pos 48.8279,2.3565       # set the position
    tools/edit_photo.py *.jpg --from-trace               # position from the GPS traces
    tools/edit_photo.py *.jpg --shift +1h                # camera clock was 1 h late
    tools/edit_photo.py a.jpg --date "2019-12-28 11:12"  # set the date (local time)
    tools/edit_photo.py *.jpg --clear-gps --dry-run      # show what would change

The metadata is written by ExifTool (apt install libimage-exiftool-perl), which changes nothing
else in the file: the image data is checked to be identical after writing.
Before a photo is changed its original is copied to cache/photo_backups/
(--no-backup to skip). The site sees the change on the next page load.

Positions can be typed as "48.8279, 2.3565", as 48°49'40.4"N 2°21'23.4"E,
or as "trace" (where the GPS trace in data/gps was at the photo's time).
Dates are local time; the time zone (e.g. +01:00) is kept, or guessed from
this computer's zone for that date when the photo has none.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import photos  # noqa: E402  (positions from the trace, same as the site)
import server  # noqa: E402

GPS_DIR = os.path.join(ROOT, "data", "gps")
BACKUP_DIR = os.path.join(ROOT, "cache", "photo_backups")
EXIFTOOL = shutil.which("exiftool")  # apt install libimage-exiftool-perl


# ---------- reading / writing with ExifTool ----------

def exiftool(*args):
    r = subprocess.run([EXIFTOOL, *args], capture_output=True, text=True)
    if r.returncode not in (0, 1) or (r.returncode == 1 and not r.stdout):
        raise RuntimeError(r.stderr.strip() or "exiftool failed")
    return r.stdout


def read(path):
    """Current {'when': aware datetime or None, 'lat', 'lon', 'method', 'hash'}."""
    tags = json.loads(exiftool("-j", "-n", "-api", "ImageHashType=SHA256",
                               "-DateTimeOriginal", "-CreateDate", "-OffsetTimeOriginal",
                               "-OffsetTime", "-GPSLatitude", "-GPSLongitude",
                               "-GPSProcessingMethod", "-ImageDataHash", path))[0]
    when = None
    raw = tags.get("DateTimeOriginal") or tags.get("CreateDate")
    if isinstance(raw, str) and re.match(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", raw):
        naive = datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
        off = parse_offset(str(tags.get("OffsetTimeOriginal") or tags.get("OffsetTime") or ""))
        when = naive.replace(tzinfo=off) if off else naive.astimezone()
    lat, lon = tags.get("GPSLatitude"), tags.get("GPSLongitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        lat = lon = None
    return {"when": when, "lat": lat, "lon": lon,
            "method": str(tags.get("GPSProcessingMethod") or "").strip(),
            "hash": tags.get("ImageDataHash")}


def write(path, old, new, backup, dry_run):
    """Write the fields of `new` that differ from `old`; returns a description."""
    args, changes = [], []
    if new["when"] != old["when"] and new["when"] is not None:
        w = new["when"]
        stamp, off = w.strftime("%Y:%m:%d %H:%M:%S"), fmt_offset(w)
        args += [f"-DateTimeOriginal={stamp}", f"-CreateDate={stamp}",
                 f"-OffsetTimeOriginal={off}", f"-OffsetTimeDigitized={off}"]
        changes.append(f"date {fmt_when(old['when'])} → {fmt_when(w)}")
    if not same_pos(new, old):
        if new["lat"] is None:
            args += ["-gps:all="]
        else:
            args += [f"-GPSLatitude={abs(new['lat']):.7f}", f"-GPSLatitudeRef={'N' if new['lat'] >= 0 else 'S'}",
                     f"-GPSLongitude={abs(new['lon']):.7f}", f"-GPSLongitudeRef={'E' if new['lon'] >= 0 else 'W'}",
                     "-GPSMapDatum=WGS-84", f"-GPSProcessingMethod={new['method'] or 'MANUAL'}"]
            if new["when"]:
                utc = new["when"].astimezone(timezone.utc)
                args += [f"-GPSDateStamp={utc:%Y:%m:%d}", f"-GPSTimeStamp={utc:%H:%M:%S}"]
        changes.append(f"position {fmt_pos(old)} → {fmt_pos(new)}")
    if not args:
        return "unchanged"
    if dry_run:
        return "would change: " + "; ".join(changes)
    if backup:
        rel = os.path.relpath(os.path.abspath(path), ROOT)
        if rel.startswith(".."):  # a photo outside the project: keep its full path
            rel = os.path.abspath(path).lstrip(os.sep)
        dest = os.path.join(BACKUP_DIR, rel)
        if not os.path.exists(dest):  # keep the very first original
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(path, dest)
    # -overwrite_original writes a new file and renames it over the old one: the
    # folder's mtime changes, so the site's photo index notices the edit.
    exiftool("-overwrite_original", "-m", *args, path)
    after = read(path)
    if old["hash"] and after["hash"] != old["hash"]:
        raise RuntimeError("image data changed while writing metadata (restore from backup!)")
    return "changed: " + "; ".join(changes)


# ---------- parsing and formatting ----------

def parse_offset(text):
    m = re.fullmatch(r"\s*([+-])(\d\d):?(\d\d)\s*", text or "")
    if not m:
        return None
    d = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(d if m.group(1) == "+" else -d)


def fmt_offset(when):
    s = when.strftime("%z")
    return f"{s[:3]}:{s[3:]}"


def fmt_when(w):
    return w.strftime("%Y-%m-%d %H:%M:%S ") + fmt_offset(w) if w else "(none)"


def same_pos(a, b):
    """Equal positions, ignoring the rounding of EXIF degrees/minutes/seconds (~0.2 m)."""
    if a["lat"] is None or b["lat"] is None:
        return a["lat"] is None and b["lat"] is None
    return abs(a["lat"] - b["lat"]) < 2e-6 and abs(a["lon"] - b["lon"]) < 2e-6


def fmt_pos(p):
    return f"{p['lat']:.6f}, {p['lon']:.6f}" if p["lat"] is not None else "(none)"


def parse_when(text, keep_offset=None):
    """'2019-12-28 11:12[:13]', '2019:12:28 11:12:13' or '28/12/2019 11:12', optionally
    followed by an offset like +01:00. Without offset: keep_offset, else this computer's."""
    text = text.strip()
    m = re.match(r"(.*?\d)\s*([+-]\d\d:?\d\d)?$", text)
    body, off = m.group(1), parse_offset(m.group(2) or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(body, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unreadable date: {text!r} (e.g. 2019-12-28 11:12:13)")
    tz = off or keep_offset
    return naive.replace(tzinfo=tz) if tz else naive.astimezone()


def parse_shift(text):
    """'+1h', '-30m', '+1h30m', '-2d', '+45s'."""
    m = re.fullmatch(r"\s*([+-])((?:\d+[dhms])+)\s*", text)
    if not m:
        raise ValueError(f"unreadable shift: {text!r} (e.g. +1h, -30m, +1h30m)")
    units = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
    delta = timedelta(**{units[u]: int(n) for n, u in re.findall(r"(\d+)([dhms])", m.group(2))})
    return delta if m.group(1) == "+" else -delta


DMS = re.compile(r"""(\d+(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*['′]\s*)?(?:(\d+(?:\.\d+)?)\s*["″]\s*)?([NSEW])""", re.I)


def parse_pos(text):
    """(lat, lon) from '48.8279, 2.3565' or 48°49'40.4"N 2°21'23.4"E; None for '' or '-'."""
    text = text.strip()
    if text in ("", "-", "none"):
        return None
    dms = DMS.findall(text)
    if len(dms) == 2:
        vals = {}
        for d, mi, s, ref in dms:
            v = float(d) + float(mi or 0) / 60 + float(s or 0) / 3600
            vals["lat" if ref.upper() in "NS" else "lon"] = -v if ref.upper() in "SW" else v
        if len(vals) == 2:
            return vals["lat"], vals["lon"]
    nums = re.findall(r"[+-]?\d+(?:\.\d+)?", text)
    if len(nums) == 2:
        lat, lon = map(float, nums)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    raise ValueError(f"unreadable position: {text!r} (e.g. 48.8279, 2.3565)")


def from_trace(when):
    """((lat, lon), minutes to the nearest GPS point) of the trace at `when`, or (None, None)."""
    if not when:
        return None, None
    day = when.astimezone().strftime("%Y%m%d")  # same day rule as the site
    _, points = server.get_track(GPS_DIR, day)
    t = int(when.timestamp() * 1000)
    at = photos.locate(t, points) if points else None
    gps = [p[0] for p in points if p[6] == "gps"]
    gap = min(abs(g - t) for g in gps) / 60000 if gps else None
    return at, gap


# ---------- interactive editing ----------

def ask(label, current):
    """input() pre-filled with the current value, editable with the arrow keys.
    Without a terminal (or readline) the value is shown instead, and an empty
    answer keeps it."""
    readline = None
    if sys.stdin.isatty():
        try:
            import readline
            readline.set_startup_hook(lambda: readline.insert_text(current))
        except ImportError:  # pragma: no cover
            readline = None
    if not readline:
        label = f"{label} [{current}]"
    try:
        answer = input(f"{label}: ")
    finally:
        if readline:
            readline.set_startup_hook()
    return answer if readline or answer.strip() else current


def interactive(path, old, args):
    print(f"\n{path}")
    print(f"  date      {fmt_when(old['when'])}")
    print(f"  position  {fmt_pos(old)}" + (f"  ({old['method']})" if old["method"] else ""))
    at, gap = from_trace(old["when"])
    if at:
        print(f"  trace     {at[0]:.6f}, {at[1]:.6f}"
              + (f"  (nearest GPS point {gap:.0f} min away)" if gap is not None else ""))
    print("  Edit the fields (Enter keeps them; position: 'trace' or '-' to remove).")
    new = dict(old)
    while True:
        try:
            text = ask("  date", old["when"].strftime("%Y-%m-%d %H:%M:%S ") + fmt_offset(old["when"])
                       if old["when"] else "")
            new["when"] = parse_when(text, old["when"] and old["when"].tzinfo) if text.strip() else old["when"]
            break
        except ValueError as e:
            print(f"  {e}")
    while True:
        try:
            text = ask("  position", f"{old['lat']:.6f}, {old['lon']:.6f}" if old["lat"] is not None else "")
            if text.strip().lower() == "trace":
                at, _ = from_trace(new["when"])
                if not at:
                    print("  no trace at that time")
                    continue
                new["lat"], new["lon"], new["method"] = *at, "GPSLOGGER"
            else:
                pos = parse_pos(text)
                new["lat"], new["lon"] = pos if pos else (None, None)
                if pos and not same_pos(new, old):
                    new["method"] = "MANUAL"
            break
        except ValueError as e:
            print(f"  {e}")
    preview = write(path, old, new, backup=False, dry_run=True)
    if preview == "unchanged":
        return "unchanged"
    print(f"  {preview.replace('would change: ', '')}")
    if input("  Save? [y/N] ").strip().lower() not in ("y", "yes", "o", "oui"):
        return "skipped"
    return write(path, old, new, backup=not args.no_backup, dry_run=False)


# ---------- batch ----------

def batch(path, old, args):
    new = dict(old)
    if args.date:
        new["when"] = parse_when(args.date, old["when"] and old["when"].tzinfo)
    if args.shift:
        if not new["when"]:
            return "skipped: no date to shift"
        new["when"] = new["when"] + parse_shift(args.shift)
    if args.clear_gps:
        new["lat"] = new["lon"] = None
    elif args.pos:
        new["lat"], new["lon"] = parse_pos(args.pos)
        new["method"] = "MANUAL"
    elif args.from_trace:
        if old["lat"] is not None and not args.force:
            return "skipped: already has a position (--force to replace)"
        at, _ = from_trace(new["when"])
        if not at:
            return "skipped: no trace at that time"
        new["lat"], new["lon"], new["method"] = *at, "GPSLOGGER"
    return write(path, old, new, backup=not args.no_backup, dry_run=args.dry_run)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("photos", nargs="+", help="photos: .jpg, .jpeg, .png or .webp")
    p.add_argument("--date", help='new date, local time: "2019-12-28 11:12[:13] [+01:00]"')
    p.add_argument("--shift", help="move the date: +1h, -30m, +1h30m, -1d …")
    gps = p.add_mutually_exclusive_group()
    gps.add_argument("--pos", help='new position: "48.8279,2.3565"')
    gps.add_argument("--from-trace", action="store_true",
                     help="position from the GPS trace at the photo's time")
    gps.add_argument("--clear-gps", action="store_true", help="remove the position")
    p.add_argument("--force", action="store_true",
                   help="with --from-trace: also replace positions the photo already has")
    p.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    p.add_argument("--no-backup", action="store_true",
                   help="do not copy originals to cache/photo_backups/")
    # "--shift -30m": argparse would take "-30m" for an option; glue it to --shift.
    argv = sys.argv[1:]
    for i, a in enumerate(argv[:-1]):
        if a == "--shift" and re.fullmatch(r"-(?:\d+[dhms])+", argv[i + 1]):
            argv[i:i + 2] = [f"--shift={argv[i + 1]}"]
            break
    args = p.parse_args(argv)
    if not EXIFTOOL:
        sys.exit("ExifTool not found: sudo apt install libimage-exiftool-perl")
    edits = args.date or args.shift or args.pos or args.from_trace or args.clear_gps
    for path in args.photos:
        if os.path.splitext(path)[1].lower() not in photos.IMAGE_EXT:  # same formats as the site
            print(f"{path}: skipped (photos only: {', '.join(sorted(photos.IMAGE_EXT))})")
            continue
        try:
            old = read(path)
            result = batch(path, old, args) if edits else interactive(path, old, args)
        except (ValueError, RuntimeError, OSError) as e:
            result = f"error: {e}"
        except (KeyboardInterrupt, EOFError):
            print("\nstopped")
            break
        print(f"{path}: {result}")


if __name__ == "__main__":
    main()
