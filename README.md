# Souvenir

Private web viewer for the daily GPSLogger traces (`YYYYMMDD.zip`).

Everything is served locally by `server.py` (Python standard library only):
traces, OpenStreetMap vector tiles for France, fonts, icons and JS libraries.
A strict Content-Security-Policy (`connect-src 'self'`…) prevents the browser
from contacting any other site, so neither the traces nor the viewed area leak.

## Run

    python3 server.py            # then open http://127.0.0.1:8000/

Options: `--data DIR` (default `./data`), `--port`, `--host` (default
`127.0.0.1`; use `0.0.0.0` to reach it from other devices on your LAN).

## Add days

Drop the `YYYYMMDD.zip` files into `data/` (plain `.csv` or `.gpx` also work).
No restart needed; reload the page.

## Update the map

Two maps are stacked: a coarse `world` (zoom 0–10, 3.8 GB) and a detailed
`france` (zoom 0–15, 9.5 GB) drawn on top from zoom 10. Either can be missing;
the page uses whatever is in `tiles/` (restart the server after adding one).

Pick a recent build date from https://build.protomaps.com/ and run:

    ./tools/pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles \
        tiles/france-new.pmtiles --bbox=-5.8,41.2,9.8,51.3 --download-threads=8
    mv tiles/france-new.pmtiles tiles/france.pmtiles

    ./tools/pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles \
        tiles/world-new.pmtiles --maxzoom=10 --download-threads=8
    mv tiles/world-new.pmtiles tiles/world.pmtiles

Add `--dry-run` to see the size first (each extra zoom level is about ×2.5).

## Layout

The project has four source files; everything else is data, downloaded map
files or third-party libraries.

```
souvenir/
├── server.py          the server
├── README.md          this file
├── data/              the traces, one zip per day
├── static/            everything the browser loads
│   ├── index.html     page structure
│   ├── style.css      page look
│   ├── app.js         map and all the logic
│   ├── vendor/        JS libraries (downloaded)
│   └── assets/        map fonts and icons (downloaded)
├── tiles/             the maps: world and France (downloaded)
└── tools/pmtiles      tool used to download the map
```

### Source code

**`server.py`** (Python standard library only) does three things:

1. **API**: `/api/days` lists the days in `data/`. `/api/track?day=20260928`
   opens that zip, reads the CSV (or the GPX if there is no CSV) and returns the
   points as JSON: time, lat, lon, altitude, accuracy, speed, source
   (gps/network) and battery.
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

**`data/`**: one GPSLogger zip per day. Each zip holds the same trace in three
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
