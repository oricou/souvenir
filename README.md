# Souvenir

Private web viewer for the daily GPSLogger traces (`YYYYMMDD.zip`).

Everything is served locally by `server.py` (Python standard library only):
traces, OpenStreetMap vector tiles for France, fonts, icons and JS libraries.
A strict Content-Security-Policy (`connect-src 'self'`…) prevents the browser
from contacting any other site, so neither the traces nor the viewed area leak.

## Requirements

- Python 3 (the server needs nothing else).
- Optional, for photos: Pillow (`sudo apt install python3-pil`) for photo
  dates/positions and previews, ffmpeg (`sudo apt install ffmpeg`) for video
  previews. Without them, times come from `PXL_…` file names and full images
  are served.
- For `tools/edit_photo.py`: ExifTool (`sudo apt install libimage-exiftool-perl`).
- For `tools/build_timezones.py` (run once): NumPy (`sudo apt install python3-numpy`).

## Install

    make install    # pmtiles tool, world map (zoom 0-10, ~3.8 GB), time-zone grid
    make france     # optional: detailed map of France (zoom 0-15, ~10 GB)

Each file is only downloaded if missing. Maps come from the latest Protomaps
build of the last week (`make france BUILD=YYYYMMDD` to choose).

## Run

    python3 server.py            # then open http://127.0.0.1:8000/

Options: `--data DIR` (default `./data`), `--gps DIR` (default `DATA/gps`),
`--photos DIR` (default `DATA/photos`), `--port`, `--host` (default
`127.0.0.1`; use `0.0.0.0` to reach it from other devices on your LAN).

## Add days

Drop the `YYYYMMDD.zip` files into `data/gps/` (plain `.csv` or `.gpx` also work,
as do the old GPSLogger names with a space, `YYYYMMDD .gpx` / `.zip` / `.txt`).
When a day has several files: zip, then csv, then gpx, then txt. Empty files
are ignored.
No restart needed; reload the page.

## Photos

Drop photos and videos into `data/photos/` (subfolders allowed, e.g.
`data/photos/2026/09/`). Each one is shown on the day it was
taken (time read from EXIF / the video file, or from Pixel `PXL_…` names), in a
panel next to the controls, and as a square on the map in the day's colour: at its own GPS
position if the camera saved one, otherwise where the trace was at that time.
Previews are made once into `cache/thumbs/` (needs Pillow; ffmpeg for videos).
Photo times and sizes are indexed in `cache/photos.json`, so a restart does not
re-read thousands of files; the folders are re-scanned whenever a file is added,
removed or renamed. A photo edited in place is only re-read after a restart.
Deleting `cache/` is safe (everything is rebuilt, which takes ~1 s per 1,500
photos).

## Local time of each place

Times are shown in the local time of the place they were recorded, e.g. a walk
in New Zealand shows its New Zealand hours, followed by "(UTC+13)" when the
zone differs from this computer's. The zone of a position comes from
`tiles/timezones.bin`, built once (8 s, one 48 MB download, nothing sent) by:

    tools/build_timezones.py

It draws the time-zone boundaries of timezone-boundary-builder on a 0.05° grid
(≈5 km); Python's zoneinfo then gives the offset at each date, summer time
included. Photos without a recorded offset are read in the zone of their own
GPS position. Without the file, times are shown in this computer's zone.

## Keyboard shortcuts

| Key | Action |
|---|---|
| ← → | previous / next day |
| Page Up / Page Down | scroll the photo panel by one screen |
| Home / End | first / last photo of the panel |
| d | full-screen slideshow (diaporama) of the panel's photos |

In the slideshow: ← → previous / next photo, Space starts or stops automatic
playback (5 s per photo), Esc closes.

## Update the map

Two maps are stacked: a coarse `world` (zoom 0–10, 3.8 GB) and a detailed
`france` (zoom 0–15, 9.5 GB) drawn on top from zoom 10. Either can be missing;
the page uses whatever is in `tiles/` (restart the server after adding one).

To update a map, delete it and run `make install` / `make france` again.
`make install` also installs pmtiles (https://github.com/protomaps/go-pmtiles);
by hand, pick a recent build date from https://build.protomaps.com/ and run:

    ./tools/pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles \
        tiles/france-new.pmtiles --bbox=-5.8,41.2,9.8,51.3 --download-threads=8
    mv tiles/france-new.pmtiles tiles/france.pmtiles

    ./tools/pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles \
        tiles/world-new.pmtiles --maxzoom=10 --download-threads=8
    mv tiles/world-new.pmtiles tiles/world.pmtiles

Add `--dry-run` to see the size first (each extra zoom level is about ×2.5).

## Tools

- `tools/edit_photo.py PHOTO` edits a photo's date and GPS position
  interactively (fields pre-filled; `trace` takes the position from the GPS
  traces). For many photos at once: `--pos LAT,LON`, `--from-trace`,
  `--date "…"`, `--shift +1h`, `--clear-gps`, `--dry-run`. JPEG only. Uses
  ExifTool, so nothing but the metadata changes; originals are kept in
  `cache/photo_backups/`.
- `tools/dedup.py DIR [-r]` lists files with identical content; `--delete`
  removes the extra copies, `--trash DIR` moves them aside.

Both have `--help`.

## Layout

The project has four source files; everything else is data, downloaded map
files or third-party libraries.

```
souvenir/
├── server.py          the server
├── photos.py          photo times, positions and previews
├── README.md          this file
├── data/
│   ├── gps/           the traces, one zip per day
│   └── photos/        photos and videos (subfolders allowed)
├── static/            everything the browser loads
│   ├── index.html     page structure
│   ├── style.css      page look
│   ├── app.js         map and all the logic
│   ├── vendor/        JS libraries (downloaded)
│   └── assets/        map fonts and icons (downloaded)
├── cache/             photo index and previews (generated)
├── tiles/             the maps: world and France (downloaded)
└── tools/
    ├── pmtiles        tool used to download the map
    ├── edit_photo.py  set a photo's date / position
    ├── dedup.py       find and remove duplicate files
    └── build_timezones.py  build tiles/timezones.bin (time zone of each place)
```

### Source code

**`server.py`** (Python standard library only) does three things:

1. **API**: `/api/days` lists the days that have a trace or photos.
   `/api/track?day=20260928` opens that zip, reads the CSV (or the GPX if there
   is no CSV) and returns the points as JSON: time, lat, lon, altitude,
   accuracy, speed, source (gps/network) and battery. `/api/photos?days=…`
   returns the photos of those days with their position, computed by the
   server (`photos.locate`) and stored in the photo index.
2. **Map tiles**: `/api/maps` lists the maps in `tiles/`, and each
   `tiles/NAME.pmtiles` is served in pieces. The browser asks for
   byte ranges (HTTP `Range` requests), so only the part of the map on screen is
   read from disk.
3. **Everything else**: serves the files in `static/` and adds a security header
   to every response that forbids the browser from contacting any other site.
   That header is what guarantees nothing goes out. It listens on
   `127.0.0.1:8000` by default, so only this computer can reach it.

**`static/index.html`**: the page layout. The side panel holds the calendar,
the filters (accuracy, network positions, time range), the colour choice
(Vitesse / Heure / Jour) with its legend, the stop-size legend and the list of
displayed days. The map fills the rest.

**`static/style.css`**: the look. Light and dark themes follow the system; on a
phone the panel moves below the map.

**`static/app.js`**: all the logic.

- **Map setup**: builds the map from the local tiles, fonts and icons, in the
  Protomaps style with French labels: world underneath, France on top from
  zoom 10 (`WORLD_MAXZOOM`).
- **`filtered()`**: applies the filters to a day's points.
- **`findStays()`**: detects stops (≥ 6 points or ≥ 10 min within 20 m; you
  have only left once you are more than 100 m away).
- **`render()`**: turns the points into lines coloured by speed, time or day,
  plus small points, start/end markers and stop dots. Also computes each day's
  stats (distance, top speed, number of stops).
- Calendar, hover popups and keyboard ← →.
- **Settings**: the thresholds (`STAY_*`) and colour scales (`SCALES`) are
  constants at the top of the file.

### Data

**`data/gps/`**: one GPSLogger zip per day. Each zip holds the same trace in three
formats (CSV, GPX, KML); only the CSV is read, as it has the most detail
(accuracy, battery).

### Downloaded files (do not edit)

- **`tiles/france.pmtiles`** (9.5 GB): OpenStreetMap data for mainland France
  and Corsica, build of 2026-09-27, zoom 0–15. One file holding about 1.9
  million vector tiles, from which the browser draws the map.
- **`tiles/world.pmtiles`** (3.8 GB): the whole world, build of 2026-09-28,
  zoom 0–10 (villages and secondary roads; blurry when zoomed further in).
- **`static/vendor/`** (1.3 MB): MapLibre GL (draws the map with the graphics
  card), pmtiles.js (reads tiles out of the `.pmtiles` file) and basemaps.js
  (the Protomaps map style: colours, roads, labels).
- **`static/assets/`** (14 MB): the Noto Sans fonts used for map labels, split
  into small pieces, and the map icons (forests, stations…).
- **`tools/pmtiles`** (56 MB): the command used to cut France out of the world
  map. Only needed to update the map (see [Update the map](#update-the-map)).

Map data © OpenStreetMap contributors (ODbL).
