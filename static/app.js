import { Map, Popup, LngLatBounds, NavigationControl, ScaleControl, addProtocol }
  from './vendor/maplibre-gl.mjs';

/* global pmtiles, basemaps */

// One colour per weekday, Monday first: every Sunday has the same colour (red, as on calendars).
const WEEKDAY_COLORS = ['#2563eb', '#16a34a', '#f59e0b', '#9333ea', '#0891b2', '#ff91a4', '#dc2626'];
const WEEKDAY_LETTERS = ['L', 'M', 'M', 'J', 'V', 'S', 'D'];
// Point fields as sent by /api/track.
// OFF: local offset (minutes from UTC) where the point was, null without tiles/timezones.bin.
const T = 0, LAT = 1, LON = 2, ELE = 3, ACC = 4, SPEED = 5, PROV = 6, BAT = 7, OFF = 8;

// Colour scales for the trace, keyed by the "Couleur" choice. `prop` is the segment
// property they read; `even` spaces the legend stops evenly instead of linearly.
const SCALES = {
  speed: {
    prop: 'kmh', unit: ' km/h', even: true,
    stops: [[0, '#3b4cc0'], [6, '#1fa187'], [25, '#a0da39'], [50, '#fde725'], [90, '#f98e09'], [130, '#d7301f']],
  },
  time: { // hours since local midnight: black at night, blue in the morning, red at midnight
    prop: 'hour', unit: ' h', hideTicks: [7],
    stops: [[0, '#000000'], [6, '#000000'], [7, '#2563eb'], [9, '#06b6d4'], [12, '#22c55e'], [15, '#eab308'], [18, '#f97316'], [24, '#dc2626']],
  },
};
const lineColor = (mode) => SCALES[mode]
  ? ['interpolate', ['linear'], ['get', SCALES[mode].prop], ...SCALES[mode].stops.flat()]
  : ['get', 'color'];

// Stays: at least STAY_MIN_POINTS points or STAY_MIN_MINUTES within STAY_RADIUS metres,
// see findStays().
const STAY_RADIUS = 20;
const STAY_MIN_POINTS = 6;
const STAY_MIN_MINUTES = 10; // where GPS is lost, arrival + departure points suffice
const STAY_LEAVE = 100;    // going farther than this from a stay means having left it
const STAY_PX = 1.8;       // dot radius in px = STAY_PX × √minutes (1 h ≈ 14 px, 8 h ≈ 39 px)

const $ = (id) => document.getElementById(id);
const colorBy = () => document.querySelector('input[name=colorby]:checked').value;
/* Default colour: "Heure" for one day, "Jour" for several days or for all dates (the
   colours of the photo squares). Applied only when that changes (one day <-> several or
   all), so a colour chosen by hand stays while moving from day to day. */
let lastMulti = null;
function defaultColor() {
  const multi = state.selected.length !== 1; // several days, or none ("Dates : toutes"): Jour
  if (multi === lastMulti) return;
  lastMulti = multi;
  document.querySelector(`input[name=colorby][value=${multi ? 'day' : 'time'}]`).checked = true;
}
const showGps = () => $('show-gps').checked;
const showPhotos = () => $('show-photos').checked;
const showHere = () => showPhotos() && $('show-here').checked; // "Ici": only the photos inside the map view
const MAX_DAY_LIST = 31; // beyond this many selected days, one summary instead of a list
const state = {
  days: [],               // days with a trace or photos, 'YYYYMMDD', sorted
  selected: [],           // selected days, in selection order
  anchor: null,           // last clicked day: start of a ⇧+click range, reference for "Semaine"
  datesKept: [],          // the days selected when "Dates" was folded, given back when unfolded
  tracks: new globalThis.Map(), // day -> points
  photos: new globalThis.Map(), // day -> [{name, kind, t, day, lat, lon, exact, w, h}]
  photoKey: null,               // what the photo panel currently shows
  here: { total: 0, photos: [] }, // "ici": the selected days' photos inside the view, from the server
  search: null,                 // {q, total, photos, error} while a search is shown, else null
  person: null,                 // {name, total, photos} while a person's photos are shown, else null
  people: { available: false, people: [] }, // names given to faces (api/people)
  admin: { enabled: false, logged: false },  // deleting photos (api/admin)
  highlighted: null,            // name of the photo outlined in the panel / enlarged on the map
  month: null,            // Date of the first day of the shown month
};

/* ---------- Map (fully local: tiles, glyphs and sprites served by server.py) ---------- */

const protocol = new pmtiles.Protocol();
addProtocol('pmtiles', protocol.tile);

const dark = matchMedia('(prefers-color-scheme: dark)').matches;
const base = new URL('.', location.href).href;

// Map archives in tiles/: a coarse "world" (zoom 0-10) under a detailed "france"
// (zoom 0-15). Where both exist, France is only drawn from WORLD_MAXZOOM on, and
// its layers sit on top so they hide the stretched world tiles inside France.
const WORLD_MAXZOOM = 10;
const maps = await (await fetch('api/maps')).json();
const sources = {}, layers = [];
for (const name of ['world', 'france', ...maps.filter((m) => m !== 'world' && m !== 'france')]) {
  if (!maps.includes(name)) continue;
  sources[name] = { type: 'vector', url: `pmtiles://${base}tiles/${name}.pmtiles`, attribution: '© OpenStreetMap' };
  const first = !layers.length;
  for (const l of basemaps.layers(name, basemaps.namedFlavor(dark ? 'dark' : 'light'), { lang: 'fr' })) {
    if (l.type === 'background' && !first) continue; // one background, from the bottom map
    const layer = { ...l, id: `${name}-${l.id}` };
    if (!first && maps.includes('world')) layer.minzoom = Math.max(l.minzoom ?? 0, WORLD_MAXZOOM);
    layers.push(layer);
  }
}

const map = new Map({
  container: 'map',
  style: {
    version: 8,
    glyphs: base + 'assets/fonts/{fontstack}/{range}.pbf',
    sprite: base + 'assets/sprites/v4/' + (dark ? 'dark' : 'light'),
    sources,
    layers,
  },
  center: [2.5, 46.6],
  zoom: 5,
  hash: false,
  attributionControl: { compact: true },
});
map.addControl(new NavigationControl(), 'top-right');
map.addControl(new ScaleControl({ unit: 'metric' }), 'bottom-right');

const mapReady = new Promise((resolve) => map.on('load', resolve));

// From WORLD_MAXZOOM on, when the whole view is inside a detailed map's rectangle
// (read from its pmtiles header: the extract's bbox, larger than the country),
// the world layers are fully covered: hide them so the browser neither loads nor
// draws their stretched tiles. Only saves work, the picture is the same.
if (maps.includes('world') && maps.length > 1) {
  const detail = await Promise.all(maps.filter((m) => m !== 'world').map(async (m) => {
    const h = await new pmtiles.PMTiles(`${base}tiles/${m}.pmtiles`).getHeader();
    return [h.minLon, h.minLat, h.maxLon, h.maxLat];
  }));
  const worldIds = layers.filter((l) => l.source === 'world').map((l) => l.id);
  let worldHidden = false;
  const updateWorld = () => {
    const b = map.getBounds();
    const covered = map.getZoom() >= WORLD_MAXZOOM && detail.some(([w, s, e, n]) =>
      b.getWest() >= w && b.getSouth() >= s && b.getEast() <= e && b.getNorth() <= n);
    if (covered === worldHidden) return;
    worldHidden = covered;
    for (const id of worldIds) map.setLayoutProperty(id, 'visibility', covered ? 'none' : 'visible');
  };
  mapReady.then(() => {
    updateWorld();
    map.on('move', updateWorld); // during the move, so nothing blank shows when leaving the rectangle
  });
}

mapReady.then(() => {
  const empty = { type: 'FeatureCollection', features: [] };
  map.addSource('lines', { type: 'geojson', data: empty });
  map.addSource('points', { type: 'geojson', data: empty });
  map.addSource('ends', { type: 'geojson', data: empty });
  map.addSource('stays', { type: 'geojson', data: empty });
  map.addSource('photos', { type: 'geojson', data: empty });
  // One square per weekday colour, normal and enlarged (highlighted photo).
  WEEKDAY_COLORS.forEach((c, i) => {
    map.addImage(`photo-${i}`, square(14, c), { pixelRatio: 2 });
    map.addImage(`photo-${i}-hl`, square(22, c), { pixelRatio: 2 });
  });

  map.addLayer({
    id: 'lines-casing', type: 'line', source: 'lines',
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': dark ? '#000' : '#fff', 'line-width': 6, 'line-opacity': 0.8 },
  });
  map.addLayer({
    id: 'lines', type: 'line', source: 'lines',
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': lineColor(colorBy()), 'line-width': 3.5 },
  });
  map.addLayer({
    id: 'points', type: 'circle', source: 'points', minzoom: 12,
    paint: {
      'circle-color': ['get', 'color'],
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 2, 17, 5],
      'circle-stroke-color': '#fff',
      'circle-stroke-width': 1,
      'circle-opacity': ['case', ['==', ['get', 'prov'], 'gps'], 1, 0.45],
    },
  });
  map.addLayer({
    id: 'ends', type: 'circle', source: 'ends',
    paint: {
      'circle-color': ['case', ['get', 'start'], '#fff', ['get', 'color']],
      'circle-radius': 7,
      'circle-stroke-color': ['get', 'color'],
      'circle-stroke-width': 3,
    },
  });
  map.addLayer({
    id: 'stays', type: 'circle', source: 'stays',
    layout: { 'circle-sort-key': ['-', ['get', 'min']] }, // small dots on top of big ones
    paint: {
      // Surface proportional to the time spent: radius ∝ √duration.
      'circle-radius': ['*', STAY_PX, ['sqrt', ['get', 'min']]],
      'circle-color': ['get', 'color'],
      'circle-opacity': 0.3,
      'circle-stroke-color': ['get', 'color'],
      'circle-stroke-width': 2,
    },
  });

  map.addLayer({
    id: 'photos', type: 'symbol', source: 'photos',
    layout: {
      'icon-image': photoIcon(null),
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
      'symbol-sort-key': ['get', 't'],
    },
  });

  // Photos first, then track points (small, unreachable under a big dot), then stays.
  const pick = (point) => {
    for (const id of ['photos', 'points']) {
      const [f] = map.queryRenderedFeatures(point, { layers: [id] });
      if (f) return f;
    }
    return map.queryRenderedFeatures(point, { layers: ['stays'] })
      .sort((a, b) => a.properties.min - b.properties.min)[0];
  };
  const popup = new Popup({ closeButton: false, closeOnClick: false, offset: 10, maxWidth: '200px' });
  map.on('mousemove', (e) => {
    const f = pick(e.point);
    map.getCanvas().style.cursor = f ? 'pointer' : '';
    if (!f) return popup.remove();
    const id = f.layer.id;
    const html = id === 'photos' ? photoInfo(f.properties) : id === 'points' ? pointInfo(f.properties) : stayInfo(f.properties);
    popup.setLngLat(f.geometry.coordinates).setHTML(html).addTo(map);
  });
  map.on('mouseout', () => popup.remove());
  map.on('click', 'photos', (e) => {
    if (onPhone()) setTab('photos'); // phones: show that photo in its panel
    highlightPhoto(e.features[0].properties.name, { scroll: true });
  });

  // Admin: Ctrl+drag (⌘+drag) a photo to where it was really taken: written into its EXIF GPS.
  map.on('mousedown', 'photos', (e) => {
    const ev = e.originalEvent;
    if (!(ev.ctrlKey || ev.metaKey) || ev.button !== 0 || !state.admin.enabled) return;
    e.preventDefault(); // neither pan nor rotate (Ctrl+drag) the map
    const { name, kind } = e.features[0].properties;
    if (kind === 'video') return showToast('Les vidéos ne peuvent pas être déplacées.');
    if (!state.admin.logged) return askAdmin(() => showToast('Mode admin : Ctrl+glisser une photo pour la déplacer.'));
    const copies = photoCopies(name);
    if (!copies.length) return;
    const before = { lat: copies[0].lat, lon: copies[0].lon, exact: copies[0].exact };
    let moved = false;
    popup.remove();
    map.getCanvas().style.cursor = 'grabbing';
    const move = (m) => {
      moved = true;
      for (const p of copies) { p.lat = m.lngLat.lat; p.lon = m.lngLat.lng; }
      map.getSource('photos').setData({ type: 'FeatureCollection', features: photoFeatures() });
    };
    map.on('mousemove', move);
    map.once('mouseup', async (m) => {
      map.off('mousemove', move);
      map.getCanvas().style.cursor = '';
      if (!moved) return;
      const done = await placePhoto(name, m.lngLat.lat, m.lngLat.lng);
      if (!done) return placeCopies(name, before); // refused: back where it was
      const entry = { moved: true, name, before };
      deletions.push(entry);
      showToast(`${name.split('/').pop()} déplacée`, () => undoDelete(entry));
    });
  });
});

/* ---------- Helpers ---------- */

const pad = (n) => String(n).padStart(2, '0');
const dayKey = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
const keyDate = (k) => new Date(+k.slice(0, 4), +k.slice(4, 6) - 1, +k.slice(6, 8));
const fmtDay = (k) => keyDate(k).toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' });
/* Times are shown in the local time of the place (off = minutes from UTC, from
   the server's time-zone map); with off unknown, in this computer's time. */
const num = (v) => (v == null || v === 'null' ? null : +v); // map feature properties may be "null"
const atPlace = (ms, off) => num(off) == null ? [new Date(ms), undefined] : [new Date(ms + num(off) * 60e3), 'UTC'];
const fmtTime = (ms, off) => {
  const [d, timeZone] = atPlace(ms, off);
  return d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', timeZone });
};
/** " (UTC+13)" when the place's zone differs from this computer's at that time, else "". */
const tzLabel = (ms, off) => {
  off = num(off);
  if (off == null || off === -new Date(ms).getTimezoneOffset()) return '';
  const h = Math.trunc(Math.abs(off) / 60), m = Math.abs(off) % 60;
  return ` (UTC${off < 0 ? '−' : '+'}${h}${m ? ':' + pad(m) : ''})`;
};
/** Hours since the start of the trace's day, in local time at the place. */
const localHour = (ms, off, day) => num(off) == null
  ? (ms - keyDate(day).getTime()) / 3.6e6
  : (ms + num(off) * 60e3 - Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6, 8))) / 3.6e6;
const fmtQuarter = (q) => q >= 96 ? '24:00' : `${pad(Math.floor(q / 4))}:${pad((q % 4) * 15)}`;
const weekday = (day) => (keyDate(day).getDay() + 6) % 7; // 0 = Monday
const colorOf = (day) => WEEKDAY_COLORS[weekday(day)];
/** Map icon of a photo square: its day's colour, enlarged for the highlighted photo. */
const photoIcon = (highlighted) => ['concat', 'photo-', ['to-string', ['get', 'wd']],
  ['case', ['==', ['get', 'name'], highlighted ?? ''], '-hl', '']];

function haversine(a, b) {
  const R = 6371e3, r = Math.PI / 180;
  const dLat = (b[LAT] - a[LAT]) * r, dLon = (b[LON] - a[LON]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[LAT] * r) * Math.cos(b[LAT] * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

const fmtDuration = (min) => min < 60 ? `${Math.round(min)} min`
  : `${Math.floor(min / 60)} h ${pad(Math.round(min % 60))}`;

/**
 * Find the places where the trace stayed still, in time order.
 * Indoors, GPS fixes wander 20-40 m, so a strict "all consecutive points within
 * STAY_RADIUS" rule would cut one stay into many pieces. Instead a stay grows
 * while points remain within STAY_LEAVE of its centroid (you have not left),
 * and it is kept if at least STAY_MIN_POINTS of them are within STAY_RADIUS of
 * the centroid (or within their own accuracy, for fuzzy fixes), or if such points
 * span at least STAY_MIN_MINUTES (GPS lost indoors: only arrival and departure).
 * The stay runs from its first to its last such point. Coming back later after
 * going farther than STAY_LEAVE gives another stay.
 * Returns [{first, last, lat, lon, n}] with first/last indexes into pts.
 */
function findStays(pts) {
  const stays = [];
  let i = 0;
  while (i < pts.length) {
    let lat = pts[i][LAT], lon = pts[i][LON], n = 1, j = i + 1;
    while (j < pts.length && haversine([0, lat, lon], pts[j]) <= STAY_LEAVE) {
      lat += (pts[j][LAT] - lat) / (n + 1);
      lon += (pts[j][LON] - lon) / (n + 1);
      n++; j++;
    }
    let first = -1, last = -1, core = 0;
    for (let k = i; k < j; k++) {
      if (haversine([0, lat, lon], pts[k]) <= Math.max(STAY_RADIUS, pts[k][ACC] ?? 0)) {
        if (first < 0) first = k;
        last = k;
        core++;
      }
    }
    if (core >= STAY_MIN_POINTS ||
        (core && pts[last][T] - pts[first][T] >= STAY_MIN_MINUTES * 60e3)) {
      stays.push({ first, last, lat, lon, n: core });
      i = last + 1; // trailing points may start the next stay
    } else {
      i++;
    }
  }
  return stays;
}

/** Square icon for the photo markers, drawn at 2× for sharp screens. */
function square(size, color) {
  const px = size * 2, c = document.createElement('canvas');
  c.width = c.height = px;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, px, px);
  ctx.fillStyle = color;
  ctx.fillRect(4, 4, px - 8, px - 8);
  return ctx.getImageData(0, 0, px, px);
}

// Names may contain "/" (subfolders): keep them as path separators.
// A photo turned (admin) gets a new version in its URL: the browser fetches it again.
const photoVersions = new globalThis.Map(); // name -> number of changes in this page
const photoUrl = (name, thumb) => `${thumb ? 'thumbs' : 'photos'}/${name.split('/').map(encodeURIComponent).join('/')}` +
  (photoVersions.has(name) ? `?v=${photoVersions.get(name)}` : '');

function photoInfo(p) {
  return `<img class="pop-img" src="${photoUrl(p.name, true)}" alt=""><br>` +
    `<b>${state.selected.length !== 1 ? fmtShortDay(p.day) + ' ' : ''}${fmtTime(p.t, p.off)}</b>${tzLabel(p.t, p.off)}` +
    (p.kind === 'video' ? ' · vidéo' : '') +
    (p.exact ? '' : '<br><span class="pop-note">position estimée d’après la trace</span>') +
    (state.admin.logged && p.kind !== 'video' ? '<br><span class="pop-note">Ctrl+glisser : déplacer</span>' : '');
}

/**
 * Where a photo was taken, as computed by the server (photos.locate): the
 * camera's GPS (exact) or the day's trace at that time; null if neither.
 */
const locatePhoto = (p) => p.lat != null && p.lon != null ? { lat: p.lat, lon: p.lon, exact: p.exact } : null;

const fmtDate = (ms, off) => {
  const [d, timeZone] = atPlace(ms, off);
  return d.toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone });
};

function stayInfo(p) {
  const start = fmtDate(p.start, p.off), end = fmtDate(p.end, p.off); // a stay can span midnight
  return `${start === end ? start : `${start} → ${end}`}<br>` +
    `<b>${fmtTime(p.start, p.off)} → ${fmtTime(p.end, p.off)}</b>${tzLabel(p.start, p.off)}<br>` +
    `${fmtDuration(p.min)} sur place · ${p.n} points`;
}

function pointInfo(p) {
  const rows = [`${fmtDate(p.t, p.off)}<br><b>${fmtTime(p.t, p.off)}</b>${tzLabel(p.t, p.off)} · ${p.prov || '?'}`];
  if (p.acc !== 'null' && p.acc != null) rows.push(`précision ±${Math.round(p.acc)} m`);
  if (p.speed !== 'null' && p.speed != null && p.speed > 0) rows.push(`vitesse ${(p.speed * 3.6).toFixed(1)} km/h`);
  if (p.ele !== 'null' && p.ele != null) rows.push(`altitude ${Math.round(p.ele)} m`);
  if (p.bat !== 'null' && p.bat != null) rows.push(`batterie ${Math.round(p.bat)} %`);
  return rows.join('<br>');
}

/* ---------- Filtering & rendering ---------- */

function filtered(day) {
  const pts = state.tracks.get(day) || [];
  const maxAcc = +$('acc').value;
  const withNet = $('network').checked;
  const from = +$('from').value, to = +$('to').value;
  // From / to: quarter-hours since midnight, local time at the place.
  const lo = from / 4, hi = to / 4;
  return pts.filter((p) => {
    if (!withNet && p[PROV] === 'network') return false;
    if (p[ACC] != null && p[ACC] > maxAcc) return false;
    const h = localHour(p[T], p[OFF], day);
    return h >= lo && h <= hi;
  });
}

function render(fit = false) {
  const lines = [], points = [], ends = [], stays = [];
  const bounds = new LngLatBounds();
  const list = $('days');
  list.textContent = '';

  const summary = { days: 0, dist: 0, stays: 0 };
  for (const day of state.selected) {
    const color = colorOf(day);
    const pts = showGps() ? filtered(day) : [];
    const dayStays = findStays(pts);
    const inStay = new Uint8Array(pts.length);
    for (const s of dayStays) {
      inStay.fill(1, s.first, s.last + 1);
      stays.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
        properties: {
          color, n: s.n, start: pts[s.first][T], end: pts[s.last][T], off: pts[s.first][OFF],
          min: (pts[s.last][T] - pts[s.first][T]) / 60e3,
        },
      });
    }
    let dist = 0, vmax = 0;
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      bounds.extend([p[LON], p[LAT]]);
      if (i) dist += haversine(pts[i - 1], p);
      if (p[SPEED] > vmax) vmax = p[SPEED];
      if (inStay[i]) continue; // shown by the stay's dot
      points.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [p[LON], p[LAT]] },
        properties: { color, t: p[T], acc: p[ACC], speed: p[SPEED], ele: p[ELE], prov: p[PROV], bat: p[BAT], off: p[OFF] },
      });
    }
    if (pts.length > 1 && colorBy() === 'day') {
      lines.push({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: pts.map((p) => [p[LON], p[LAT]]) },
        properties: { color },
      });
    } else {
      // One segment per pair of points, coloured by its speed or time of day.
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i];
        const ms = a[SPEED] != null && b[SPEED] != null
          ? (a[SPEED] + b[SPEED]) / 2
          : haversine(a, b) / Math.max(1, (b[T] - a[T]) / 1000);
        lines.push({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: [[a[LON], a[LAT]], [b[LON], b[LAT]]] },
          properties: {
            color, kmh: ms * 3.6,
            hour: Math.min(24, Math.max(0, localHour((a[T] + b[T]) / 2, a[OFF], day))),
          },
        });
      }
    }
    if (pts.length) {
      const f = pts[0], l = pts[pts.length - 1];
      ends.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [f[LON], f[LAT]] }, properties: { color, start: true } });
      ends.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [l[LON], l[LAT]] }, properties: { color, start: false } });
    }

    summary.days++;
    summary.dist += dist;
    summary.stays += dayStays.length;
    if (state.selected.length > MAX_DAY_LIST) continue;
    const li = document.createElement('li');
    li.style.setProperty('--c', color);
    const total = (state.tracks.get(day) || []).length;
    const nPhotos = (state.photos.get(day) || []).length;
    const photoText = nPhotos ? `${nPhotos} photo${plural(nPhotos)}` : '';
    let stats;
    if (!showGps()) stats = photoText || 'pas de photo';
    else if (pts.length) {
      const first = pts[0], last = pts[pts.length - 1];
      stats = `${fmtTime(first[T], first[OFF])} → ${fmtTime(last[T], last[OFF])}${tzLabel(first[T], first[OFF])}` +
        ` · ${(dist / 1000).toFixed(1)} km` +
        (vmax > 0 ? ` · max ${(vmax * 3.6).toFixed(0)} km/h` : '') +
        `<br>${pts.length} / ${total} points · ${dayStays.length} arrêt${plural(dayStays.length)}` +
        (photoText ? ` · ${photoText}` : '');
    } else if (!state.tracks.has(day)) stats = 'chargement…';
    else stats = (total ? `aucun point avec ces filtres (${total} au total)` : 'pas de trace') +
      (photoText ? ` · ${photoText}` : '');
    li.innerHTML = `<div class="t"><span class="sw"></span>${fmtDay(day)}</div><div class="s">${stats}</div>`;
    li.title = 'Centrer sur ce jour';
    li.onclick = () => fitTo([day]);
    list.append(li);
  }
  if (state.selected.length > MAX_DAY_LIST) {
    const sorted = [...state.selected].sort();
    const nPhotos = showHere() ? state.here.total : selectedPhotos().length;
    const li = document.createElement('li');
    li.innerHTML = `<div class="t">${state.selected.length} jours</div><div class="s">` +
      `${fmtDay(sorted[0])} → ${fmtDay(sorted[sorted.length - 1])}<br>` +
      (showGps() ? `${(summary.dist / 1000).toFixed(0)} km · ${summary.stays} arrêts · ` : '') +
      `${nPhotos} photo${plural(nPhotos)}</div>`;
    li.title = 'Tout centrer';
    li.onclick = () => fitTo(state.selected);
    list.append(li);
  }
  const shownPhotos = photoFeatures();
  // Frame all the selected photos, even with "Ici" (which then shows those in the new view).
  // With "ici" the view stays where it is: that place is what the photos are filtered on.
  if (showPhotos() && !showHere()) for (const p of selectedPhotos()) if (p.lat != null) bounds.extend([p.lon, p.lat]);

  renderLegend();
  mapReady.then(() => {
    map.setPaintProperty('lines', 'line-color', lineColor(colorBy()));
    // Night segments are black: give them a light outline on the dark map.
    map.setPaintProperty('lines-casing', 'line-color', !dark ? '#fff' : colorBy() === 'time' ? '#9ca3af' : '#000');
    map.getSource('lines').setData({ type: 'FeatureCollection', features: lines });
    map.getSource('points').setData({ type: 'FeatureCollection', features: points });
    map.getSource('ends').setData({ type: 'FeatureCollection', features: ends });
    map.getSource('stays').setData({ type: 'FeatureCollection', features: stays });
    map.getSource('photos').setData({ type: 'FeatureCollection', features: shownPhotos });
    if (fit && !showHere() && !bounds.isEmpty()) map.fitBounds(bounds, { padding: 60, maxZoom: 16, duration: 600 });
  });
  renderPhotos();
  renderCalendar();
  location.replace('#' + state.selected.join(','));
}

/* ---------- Photos ---------- */

/** The selected days' photos by time; "Dates : toutes" (folded): every photo (state.allPhotos). */
let allSorted = null; // state.allPhotos by time, made once
const selectedPhotos = () => datesFolded()
  ? (state.allPhotos ? (allSorted ??= [...state.allPhotos].sort((a, b) => a.t - b.t)) : [])
  : state.selected.flatMap((d) => state.photos.get(d) || []).sort((a, b) => a.t - b.t);
const inView = (p) => p.lat != null && map.getBounds().contains([p.lon, p.lat]);
/** The shown person's photos taken on the days selected in the calendar (all if none is). */
const personPhotos = () => {
  const days = new Set(state.selected);
  return (state.person?.photos || []).filter((p) => !days.size || days.has(p.day));
};
/** The selected days, in words: "le 12 oct. 2025", "37 jours, du 1 janv. 2025 au 31 déc. 2025". */
function daysLabel() {
  const days = [...state.selected].sort();
  if (!days.length) return 'tous les jours'; // nothing selected: no date filter
  if (days.length === 1) return `le ${fmtShortDay(days[0])}`;
  return `${days.length} jours, du ${fmtShortDay(days[0])} au ${fmtShortDay(days[days.length - 1])}`;
}
const shownPhotoList = () => !showPhotos() ? []
  : state.search ? searchPhotos() // all days, best match first ("ici": only in view)
  : state.person ? personPhotos().filter((p) => !showHere() || inView(p)) // by date
  : showHere() ? state.here.photos : selectedPhotos();
const fmtShortDay = (k) => keyDate(k).toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });
const plural = (n) => n > 1 ? 's' : '';

/* Search results: the "Pertinence" slider above them keeps the photos scoring at least
   that share of the best match (60 %: all those the server returns; 100 %: the best). */
const THRESHOLD_KEY = 'souvenir.searchThreshold';
/** The best match's score: from the server (before "ici" filtering), else the best returned. */
const searchBest = (s) => s.best ?? Math.max(0, ...s.photos.map((p) => p.score ?? 0));
function searchPhotos() {
  const { photos } = state.search, best = searchBest(state.search);
  if (!best) return photos; // only names: their photos by date, no scores
  const min = best * (+$('threshold').value / 100) - 1e-9;
  return photos.filter((p) => p.score == null || p.score >= min);
}
$('threshold').addEventListener('input', () => {
  $('threshold-out').textContent = `${$('threshold').value} %`;
  try { localStorage.setItem(THRESHOLD_KEY, $('threshold').value); } catch { /* ignore */ }
  if (state.search) updatePhotos();
});
try {
  const saved = localStorage.getItem(THRESHOLD_KEY);
  if (saved) $('threshold').value = saved;
} catch { /* ignore */ }
$('threshold-out').textContent = `${$('threshold').value} %`;

function photoFeatures() {
  return shownPhotoList().flatMap((p) => {
    const at = locatePhoto(p);
    return at ? [{
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [at.lon, at.lat] },
      properties: { name: p.name, t: p.t, kind: p.kind, exact: at.exact, wd: weekday(p.day) },
    }] : [];
  });
}

/** Fill the photo panel; rebuilt only when the photos or their positions change. */
function renderPhotos() {
  const list = shownPhotoList();
  const here = showHere(), found = state.search, person = state.person;
  const key = (found ? `search:${found.q}:` : '') + (person ? `person:${person.name}:` : '') + (here ? 'ici:' : '') +
    list.map((p) => p.name + (p.people || []).join()).join('|');
  if (key === state.photoKey) return;
  state.photoKey = key;
  const all = !showPhotos() ? 0 : found ? found.total : person ? personPhotos().length : here ? state.here.total : selectedPhotos().length;
  $('photos').hidden = $('photo-resizer').hidden = !all && !found && !person;
  // "ici · 20 / 76": photos shown in the panel / in the list (which the slideshow goes through).
  const count = $('photo-count');
  count.textContent = found ? `recherche${here ? ' ici' : ''} · ` : person ? '' : here ? 'ici · ' : '';
  if (person && !found) { // the person's name: click to correct it
    const who = document.createElement('button');
    who.type = 'button';
    who.className = 'person-name';
    who.textContent = person.name;
    who.title = 'Cliquer pour corriger le nom';
    who.onclick = () => editPersonName(who);
    count.append(who, `${here ? ' ici' : ''} · `);
  }
  const shown = document.createElement('span');
  shown.className = 'shown-count';
  shown.title = 'Photos affichées / en tout (« Plus de photos » pour les suivantes ; le diaporama les montre toutes)';
  count.append(shown);
  $('search-threshold').hidden = !(found && searchBest(found));
  $('photo-note').hidden = list.length > 0;
  $('photo-note').textContent = found
    ? (found.error ? `Recherche indisponible : ${found.error}` : `Aucune photo trouvée ${daysLabel()}${here ? ' dans cette zone de la carte' : ''}.`)
    : person ? `Aucune photo de ${person.name} ${daysLabel()}${here ? ' dans cette zone de la carte' : ''} (${person.total} en tout).`
    : !showPhotos() ? 'Photos masquées (case « Photo » dans les filtres).'
    : here ? 'Aucune photo de ces jours dans cette zone de la carte.'
    : state.selected.length ? 'Aucune photo pour les jours choisis.'
    : datesFolded() && !state.allPhotos ? 'Chargement des photos…'
    : 'Aucune photo.';
  $('tab-count').textContent = list.length ? String(list.length) : '';
  $('tab-here').hidden = !here; // phones: 🎯 on the 🔍 tab while "ici" filters the photos
  showAliasForm();
  const box = $('photo-list');
  box.textContent = '';
  const manyDays = found || person || state.selected.length !== 1;
  // A new list (search, person, days, "ici") or a longer one (another place on the map…)
  // starts again at its first PHOTO_PAGE photos; a shorter one (a photo deleted) does not.
  const context = [found?.q, person?.name, here, datesFolded(), state.selected.join()].join('|');
  if (context !== panel.context || list.length > panel.list.length) Object.assign(panel, { context, count: PHOTO_PAGE });
  Object.assign(panel, { list, manyDays });
  appendPhotos(0, Math.min(list.length, panel.count));
}

/* The photo panel shows its list PHOTO_PAGE photos at a time ("Plus de photos"), so that
   years of photos stay light; the map and the slideshow have all of them. */
const PHOTO_PAGE = 20;
const panel = { context: null, count: PHOTO_PAGE, list: [], manyDays: false };
/** Add the panel's photos from..to-1 (then the "Plus de photos" button if some are left). */
function appendPhotos(from, to) {
  const box = $('photo-list'), { list, manyDays } = panel;
  box.querySelector('.more')?.remove();
  // Two portrait photos in a row share a line, to save space.
  const portrait = (p) => p && p.h > p.w;
  let row = null;
  for (let i = from; i < to; i++) {
    const p = list[i];
    const fig = document.createElement('figure');
    fig.className = 'photo';
    fig.dataset.name = p.name;
    fig.style.setProperty('--c', colorOf(p.day));
    if (p.name === state.highlighted) fig.classList.add('hl');
    // Videos too are shown as their preview image: a click shows the place,
    // "ouvrir" plays them.
    const media = document.createElement('img');
    media.loading = 'lazy';
    media.alt = '';
    media.src = photoUrl(p.name, true);
    // Reserve the final height before the lazy image loads (width/height give the
    // aspect ratio), so scrolling to a photo is not thrown off by images growing.
    if (p.w && p.h) {
      media.width = p.w;
      media.height = p.h;
    } else {
      media.style.aspectRatio = '4 / 3';
    }
    media.onclick = () => (onPhone() ? photoTap(p) : showPhotoOnMap(p));
    media.ondblclick = () => { if (!onPhone()) window.open(photoUrl(p.name), '_blank', 'noopener'); }; // same as "ouvrir ↗"
    media.title = 'Clic : voir sur la carte · double-clic : ouvrir';
    const cap = document.createElement('figcaption');
    const when = document.createElement('b');
    when.textContent = (manyDays ? fmtShortDay(p.day) + ' ' : '') +
      fmtTime(p.t, p.off) + tzLabel(p.t, p.off) + (p.kind === 'video' ? ' · vidéo' : '');
    when.title = 'Voir sur la carte';
    when.style.cursor = 'pointer';
    when.onclick = () => showPhotoOnMap(p);
    const open = document.createElement('a');
    open.href = photoUrl(p.name);
    open.target = '_blank';
    open.rel = 'noopener';
    open.textContent = locatePhoto(p) ? 'ouvrir ↗' : 'hors trace · ouvrir ↗';
    cap.append(when, open);
    fig.append(media, cap);
    if (p.people?.length) {
      const who = document.createElement('div');
      who.className = 'who';
      who.textContent = '👤 ' + p.people.join(', ');
      fig.append(who);
    }
    if (row) {
      row.append(fig);
      row = null;
    } else if (portrait(p) && i + 1 < to && portrait(list[i + 1])) {
      row = document.createElement('div');
      row.className = 'photo-pair';
      row.append(fig);
      box.append(row);
    } else {
      box.append(fig);
    }
  }
  const shown = $('photo-count').querySelector('.shown-count');
  if (shown) shown.textContent = list.length ? `${to} / ${list.length}` : '0';
  if (to < list.length) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'more';
    const next = Math.min(PHOTO_PAGE, list.length - to);
    more.textContent = `Plus de photos (${next} de plus, ${list.length - to} restantes)`;
    more.onclick = () => showMorePhotos();
    box.append(more);
  }
}
/** Show PHOTO_PAGE more photos in the panel, or up to the photo at index `upTo`. */
function showMorePhotos(upTo = 0) {
  const from = Math.min(panel.count, panel.list.length);
  panel.count = Math.max(panel.count + PHOTO_PAGE, Math.ceil((upTo + 1) / PHOTO_PAGE) * PHOTO_PAGE);
  appendPhotos(from, Math.min(panel.list.length, panel.count));
}

/* Drag the border between the photo panel and the map to change its width. */
const PHOTO_WIDTH_KEY = 'souvenir.photoWidth';
function setPhotoWidth(px) {
  const max = Math.max(200, window.innerWidth - $('panel').offsetWidth - 200); // keep some map
  const w = Math.round(Math.min(max, Math.max(200, px)));
  $('photos').style.width = w + 'px';
  return w;
}
try {
  const saved = +localStorage.getItem(PHOTO_WIDTH_KEY);
  if (saved) setPhotoWidth(saved);
} catch { /* storage unavailable: default width */ }
$('photo-resizer').addEventListener('pointerdown', (e) => {
  const handle = e.currentTarget, startX = e.clientX, startW = $('photos').offsetWidth;
  handle.setPointerCapture(e.pointerId);
  handle.classList.add('dragging');
  document.body.classList.add('resizing');
  const move = (ev) => setPhotoWidth(startW + ev.clientX - startX);
  const up = () => {
    handle.removeEventListener('pointermove', move);
    handle.classList.remove('dragging');
    document.body.classList.remove('resizing');
    try { localStorage.setItem(PHOTO_WIDTH_KEY, $('photos').offsetWidth); } catch { /* ignore */ }
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up, { once: true });
  handle.addEventListener('pointercancel', up, { once: true });
});
$('photo-resizer').addEventListener('dblclick', () => {
  $('photos').style.width = '';
  try { localStorage.removeItem(PHOTO_WIDTH_KEY); } catch { /* ignore */ }
});

/* "ici": the server filters the selected days' photos (all of them with "Dates : toutes")
   on the view (years of photos are never sent to show the few taken here). Asked again
   when the map stops moving. */
let hereRequest = null;
async function loadHere() {
  if (state.search) return runSearch(); // a search shown: it is filtered on the view instead
  if (!showHere()) return;
  hereRequest?.abort();
  hereRequest = new AbortController();
  const b = map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(5)).join(',');
  const days = datesFolded() ? 'all=1' : 'days=' + [...state.selected].sort().join(','); // folded: every year
  try {
    const r = await fetch(`api/photos?${days}&bbox=${bbox}`, { signal: hereRequest.signal });
    state.here = r.ok ? await r.json() : { total: 0, photos: [] };
  } catch (e) {
    if (e.name === 'AbortError') return; // the map or selection changed: a newer request is on its way
    throw e;
  }
  updatePhotos();
}
map.on('moveend', loadHere);

/* Search by meaning (server: /api/search, vectors from tools/embed_photos.py): the
   results replace the photo list, best match first, all days; with "ici", only those
   in the view (asked again when the map moves). An empty box goes back to the list. */
let searchRequest = null;
async function runSearch() {
  const q = $('search').value.trim();
  searchRequest?.abort();
  if (q && state.person) { state.person = null; renderPeople(); }
  if (!q) {
    state.search = null;
    $('search-note').hidden = true;
    return updatePhotos();
  }
  searchRequest = new AbortController();
  let url = `api/search?q=${encodeURIComponent(q)}&limit=300&days=${[...state.selected].sort().join(',')}`;
  if (showHere()) {
    const b = map.getBounds();
    url += '&bbox=' + [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(5)).join(',');
  }
  $('search-note').hidden = false;
  $('search-note').textContent = 'Recherche…';
  let result;
  try {
    const r = await fetch(url, { signal: searchRequest.signal });
    result = r.ok ? await r.json() : { error: `erreur ${r.status}`, total: 0, photos: [] };
  } catch (e) {
    if (e.name === 'AbortError') return;
    result = { error: String(e), total: 0, photos: [] };
  }
  const first = !state.search || state.search.q !== q;
  state.search = { q, ...result };
  // How the query was understood: "Laurence et Paul · « à vélo » — 12 photos".
  const who = result.names?.length ? `👤 ${result.names.join(' et ')}${result.rest ? ` · « ${result.rest} »` : ''} — ` : '';
  $('search-note').textContent = result.error ? `Recherche indisponible : ${result.error}`
    : `${who}${result.total} photo${plural(result.total)} (${daysLabel()})` +
      (result.names?.length && !result.rest ? ', par date.' : ' — la plus proche en premier.') +
      (result.warning ? ` (${result.warning})` : '');
  updatePhotos();
  if (first && onPhone()) setTab('photos'); // phones: show the results
  if (first && !showHere()) { // a new search: frame its best results
    const b = new LngLatBounds();
    for (const p of result.photos.slice(0, 50)) if (p.lat != null) b.extend([p.lon, p.lat]);
    if (!b.isEmpty()) map.fitBounds(b, { padding: 60, maxZoom: 15, duration: 600 });
  }
}
$('search-form').addEventListener('submit', (e) => { e.preventDefault(); runSearch(); });
$('search').addEventListener('search', runSearch); // the ✕ of the box (and Enter)
$('search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { $('search').value = ''; runSearch(); }
});

/** Refresh only the photo squares and panel (cheaper than render()). */
function updatePhotos() {
  mapReady.then(() => map.getSource('photos').setData({ type: 'FeatureCollection', features: photoFeatures() }));
  renderPhotos();
}

function highlightPhoto(name, { scroll = false } = {}) {
  state.highlighted = name;
  for (const f of document.querySelectorAll('.photo.hl')) f.classList.remove('hl');
  if (scroll) { // further down than the panel shows: show up to it
    const i = panel.list.findIndex((p) => p.name === name);
    if (i >= panel.count) showMorePhotos(i);
  }
  const fig = [...document.querySelectorAll('.photo')].find((f) => f.dataset.name === name);
  if (fig) {
    fig.classList.add('hl');
    if (scroll) fig.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  mapReady.then(() => map.setLayoutProperty('photos', 'icon-image', photoIcon(name)));
}

/* Phones: one full-screen panel at a time, chosen with the tab bar (🔍 📷 🌍). */
const phoneQuery = matchMedia('(max-width: 700px)');
const onPhone = () => phoneQuery.matches;
function setTab(tab) {
  document.body.dataset.tab = tab;
  for (const b of document.querySelectorAll('#tabs button')) {
    b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  }
  if (tab === 'map') mapReady.then(() => map.resize());
}
for (const b of document.querySelectorAll('#tabs button')) b.onclick = () => setTab(b.dataset.tab);
setTab('map');

function showPhotoOnMap(p) {
  if (onPhone()) setTab('map'); // phones: go to the map to see it
  highlightPhoto(p.name);
  const at = locatePhoto(p);
  if (at) map.flyTo({ center: [at.lon, at.lat], zoom: Math.max(map.getZoom(), 15), duration: 800 });
}

/** Phones: a tap on a photo of the panel opens the slideshow there, a double-tap
    shows it on the map (the first tap waits to know which). */
let photoTapTimer = null, photoTapped = null;
function photoTap(p) {
  clearTimeout(photoTapTimer);
  if (photoTapped === p.name) { photoTapped = null; return showPhotoOnMap(p); }
  photoTapped = p.name;
  photoTapTimer = setTimeout(() => {
    photoTapped = null;
    highlightPhoto(p.name);
    openViewer();
  }, DOUBLE_MS);
}

/* ---------- Full-screen viewer: the panel's photos one by one ---------- */

const VIEWER_AUTO_MS = 5000; // automatic playback: time per photo
const viewer = { list: [], i: 0, timer: null, idle: null, ready: new globalThis.Map() };

/** Download and decode a full-size photo ahead of time (kept in viewer.ready). */
function preload(p) {
  if (!p || p.kind === 'video' || viewer.ready.has(p.name)) return;
  const img = new Image();
  img.alt = '';
  img.draggable = false; // the slideshow drags it itself when zoomed
  img.src = photoUrl(p.name);
  img.decode().catch(() => { /* shown as the preview, then retried normally */ });
  viewer.ready.set(p.name, img);
}
const viewerOpen = () => !$('viewer').hidden;

function openViewer() {
  viewer.list = shownPhotoList();
  if (!viewer.list.length) return;
  viewer.i = Math.max(0, viewer.list.findIndex((p) => p.name === state.highlighted));
  $('viewer').hidden = false;
  $('viewer').requestFullscreen?.().catch(() => { /* the overlay still fills the window */ });
  showInViewer();
  wakeViewer();
}

function closeViewer() {
  if (!viewerOpen()) return;
  stopAuto();
  $('viewer').hidden = true;
  viewer.ready.clear(); // free the decoded full-size images
  $('viewer').querySelector('.v-stage').textContent = ''; // stops a playing video
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  const p = viewer.list[viewer.i];
  if (p) showPhotoOnMap(p);
  highlightPhoto(p?.name, { scroll: true });
}

function showInViewer() {
  const p = viewer.list[viewer.i];
  viewer.zoom = { z: 1, x: 0, y: 0 };
  $('viewer').classList.remove('zoomed');
  const stage = $('viewer').querySelector('.v-stage');
  stage.textContent = '';
  if (p.kind === 'video') {
    const v = document.createElement('video');
    v.src = photoUrl(p.name);
    v.poster = photoUrl(p.name, true);
    v.controls = true;
    v.autoplay = true;
    v.onended = () => { if (viewer.timer) step(1); };
    stage.append(v);
  } else {
    preload(p);
    const full = viewer.ready.get(p.name);
    if (full.complete && full.naturalWidth) {
      stage.append(full); // already downloaded and decoded: shown at once
    } else {
      // Not ready yet: the preview shows at once, the original replaces it when decoded.
      const thumb = document.createElement('img');
      thumb.alt = '';
      thumb.draggable = false;
      thumb.src = photoUrl(p.name, true);
      stage.append(thumb);
      full.decode().then(() => {
        if (viewer.list[viewer.i] === p) { thumb.replaceWith(full); applyZoom(); }
      }, () => {});
    }
  }
  // Keep the neighbours ready (next two, previous one) and forget the others.
  const keep = viewer.list.slice(Math.max(0, viewer.i - 1), viewer.i + 3);
  keep.forEach(preload);
  for (const name of viewer.ready.keys()) {
    if (!keep.some((k) => k.name === name)) viewer.ready.delete(name);
  }
  const at = locatePhoto(p);
  $('viewer').querySelector('.v-caption').textContent = `${fmtDate(p.t, p.off)} · ${fmtTime(p.t, p.off)}${tzLabel(p.t, p.off)}` +
    (p.kind === 'video' ? ' · vidéo' : '') +
    (at ? ` · ${at.lat.toFixed(4)}, ${at.lon.toFixed(4)}${at.exact ? '' : ' (estimée)'}` : ' · hors trace') +
    (p.people?.length ? ` · 👤 ${p.people.join(', ')}` : '');
  $('viewer').querySelector('.v-count').textContent = `${viewer.i + 1} / ${viewer.list.length}`;
  $('viewer').querySelector('.v-prev').hidden = viewer.i === 0;
  $('viewer').querySelector('.v-next').hidden = viewer.i === viewer.list.length - 1;
  highlightPhoto(p.name);
  hideAsk();
  drawFaces();
  $('viewer').querySelector('.v-delete').hidden = !state.admin.enabled;
  $('viewer').querySelector('.v-rotate').hidden = !state.admin.enabled || !canRotate(p);
  $('viewer').querySelector('.v-lock').hidden = !state.admin.logged;
}

/* ---------- Admin: Suppr (or 🗑) moves the photo to data/trash, with "Annuler" ---------- */

/** Every copy of this photo the page holds (days, search, person, "ici", slideshow). */
function photoCopies(name) {
  const lists = [...state.photos.values(), state.allPhotos, state.search?.photos, state.person?.photos, state.here.photos, viewer.list];
  return [...new Set(lists.flatMap((l) => (l || []).filter((p) => p.name === name)))];
}
/** Update the page's copies of a photo (position {lat, lon, exact, off}, size {w, h}) and redraw it. */
function placeCopies(name, pos) {
  for (const p of photoCopies(name)) {
    for (const k of ['lat', 'lon', 'exact', 'off', 'w', 'h']) if (k in pos) p[k] = pos[k];
  }
  updatePhotos();
  if (viewerOpen()) showInViewer();
}
/** Write a photo's GPS position on the server (lat null: remove it, back to the trace); true if done. */
async function placePhoto(name, lat, lon) {
  const { ok, data } = await postJSON('api/photos/place', { name, lat, lon });
  if (!ok) {
    if (data.login) { state.admin.logged = false; askAdmin(); }
    else alert(`Impossible : ${data.error || ''}`);
    return false;
  }
  if (data.photo) placeCopies(name, data.photo);
  return true;
}
async function undoMove(entry) {
  const { name, before } = entry;
  // Its GPS as it was, or none if it was placed from the trace.
  const done = await placePhoto(name, before.exact ? before.lat : null, before.exact ? before.lon : null);
  if (!done) return;
  deletions.splice(deletions.indexOf(entry), 1);
  $('toast').hidden = true;
  const at = photoCopies(name)[0];
  if (at?.lat != null && !viewerOpen()) map.easeTo({ center: [at.lon, at.lat], duration: 400 });
}

async function loadAdmin() {
  try {
    const r = await fetch('api/admin');
    if (r.ok) state.admin = await r.json();
  } catch { /* keep what we have */ }
}
async function postJSON(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  return { ok: r.ok, data: await r.json().catch(() => ({})) };
}

/** Show the admin form or the toast where it can be seen: in the slideshow while it is
    open (in full screen, nothing outside it shows), else over the map. */
function overlay(el) {
  const parent = viewerOpen() ? $('viewer') : document.body;
  if (el.parentElement !== parent) parent.append(el);
  return el;
}

/** Ask the admin password (over the slideshow or the map), then run `next` once logged in. */
let afterLogin = null;
function askAdmin(next) {
  const form = overlay($('admin-form'));
  afterLogin = next;
  stopAuto();
  hideAsk();
  form.querySelector('.v-admin-error').textContent = '';
  form.pw.value = '';
  form.hidden = false;
  form.pw.focus();
}
{
  const form = $('admin-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const { ok, data } = await postJSON('api/admin/login', { password: form.pw.value });
    form.pw.value = '';
    if (!ok) {
      form.querySelector('.v-admin-error').textContent =
        data.error === 'wrong admin password' ? 'Mot de passe incorrect.' : (data.error || 'Refusé.');
      return;
    }
    state.admin.logged = true;
    form.hidden = true;
    $('viewer').querySelector('.v-lock').hidden = false;
    const next = afterLogin;
    afterLogin = null;
    next?.();
  });
  form.querySelector('.v-admin-cancel').onclick = () => { form.hidden = true; afterLogin = null; };
  form.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); form.hidden = true; afterLogin = null; }
    e.stopPropagation(); // typing the password must not drive the slideshow
  });
  form.addEventListener('pointerup', (e) => e.stopPropagation());
  $('viewer').querySelector('.v-lock').onclick = async (e) => {
    e.stopPropagation();
    await postJSON('api/admin/logout');
    state.admin.logged = false;
    $('viewer').querySelector('.v-lock').hidden = true;
  };
  $('viewer').querySelector('.v-delete').onclick = (e) => { e.stopPropagation(); deleteShown(); };
  $('viewer').querySelector('.v-rotate').onclick = (e) => { e.stopPropagation(); rotateShown(1); };
}

/** Remove a photo from every list the page holds (it is no longer on the server). */
function forgetPhoto(name) {
  const drop = (list) => list && list.splice(0, list.length, ...list.filter((p) => p.name !== name));
  for (const list of state.photos.values()) drop(list);
  drop(state.search?.photos);
  drop(state.person?.photos);
  drop(state.allPhotos);
  allSorted = null;
  drop(state.here.photos);
  if (state.search) state.search.total = state.search.photos.length;
  updatePhotos();
}

/** Suppr: the photo shown goes to data/trash; the slideshow moves on; "Annuler" brings it back. */
let toastTimer = null;
async function deleteShown() {
  const p = viewer.list[viewer.i];
  if (!p || !state.admin.enabled) return;
  if (!state.admin.logged) return askAdmin(deleteShown);
  const { ok, data } = await postJSON('api/photos/delete', { name: p.name });
  if (!ok) {
    if (data.login) { state.admin.logged = false; return askAdmin(deleteShown); } // session expired
    return alert(`Impossible : ${data.error || ''}`);
  }
  const index = viewer.i;
  viewer.list.splice(index, 1);
  forgetPhoto(p.name);
  loadPeople(); // their numbers of photos
  const entry = { p, trash: data.trash, index, list: viewer.list };
  deletions.push(entry);
  showToast(`${p.name.split('/').pop()} mis à la corbeille`, () => undoDelete(entry));
  if (!viewer.list.length) return closeViewer();
  viewer.i = Math.min(index, viewer.list.length - 1);
  showInViewer();
}

/* Admin: r (or ↻) turns the photo shown a quarter turn clockwise, ⇧R anticlockwise, by
   changing its EXIF Orientation (lossless). JPEG only: browsers do not all apply it to
   other formats. Its faces are found again by the next tools/find_faces.py. */
const canRotate = (p) => p.kind !== 'video' && /\.jpe?g$/i.test(p.name);
function rotateShown(quarters) {
  const p = viewer.list[viewer.i];
  if (p && state.admin.enabled && canRotate(p)) rotatePhoto(p.name, quarters);
}
async function rotatePhoto(name, quarters, { undoing = false } = {}) {
  if (!state.admin.logged) return askAdmin(() => rotatePhoto(name, quarters, { undoing }));
  const { ok, data } = await postJSON('api/photos/rotate', { name, quarters });
  if (!ok) {
    if (data.login) { state.admin.logged = false; return askAdmin(() => rotatePhoto(name, quarters, { undoing })); }
    return alert(`Impossible : ${data.error || ''}`);
  }
  photoVersions.set(name, (photoVersions.get(name) || 0) + 1); // its photo and preview changed
  viewer.ready.delete(name);
  state.photoKey = null; // the panel shows it again, turned
  placeCopies(name, data.photo || {}); // new width / height; redraws the panel and the slideshow
  if (!undoing) deletions.push({ turned: true, name, quarters });
}

/* Deletions, moves (on the map) and turns made since the page was opened, last one last:
   "Annuler" undoes its own, Ctrl+Z (⌘Z) undoes the last one, again the one before, etc. */
const deletions = [];
async function undoDelete(entry = deletions[deletions.length - 1]) {
  if (!entry) return showToast('Rien à annuler.');
  if (!state.admin.logged) return askAdmin(() => undoDelete(entry)); // the admin session expired
  if (entry.moved) return undoMove(entry);
  if (entry.turned) { // turn it back
    deletions.splice(deletions.indexOf(entry), 1);
    return rotatePhoto(entry.name, -entry.quarters, { undoing: true });
  }
  const { p, trash, index, list } = entry;
  const back = await postJSON('api/photos/restore', { name: p.name, trash });
  if (!back.ok) {
    if (back.data.login) { state.admin.logged = false; return undoDelete(entry); }
    return alert(`Impossible de la remettre : ${back.data.error || ''}`);
  }
  deletions.splice(deletions.indexOf(entry), 1);
  $('toast').hidden = true;
  state.photos.delete(p.day); // reloaded, with it
  state.allPhotos = null;
  await load();
  if (state.search) await runSearch();
  if (state.person) await showPerson(state.person.name, { keepView: true });
  render();
  loadPeople();
  if (viewerOpen() && list === viewer.list) { // back in this slideshow, shown
    list.splice(Math.min(index, list.length), 0, p);
    viewer.i = list.indexOf(p);
    showInViewer();
  }
  if (viewerOpen()) showToast(`${p.name.split('/').pop()} remise en place`);
}
function showToast(text, undo) {
  const toast = overlay($('toast'));
  toast.querySelector('span').textContent = text;
  toast.querySelector('button').hidden = !undo; // a plain message has no "Annuler"
  toast.querySelector('button').onclick = (e) => { e.stopPropagation(); toast.hidden = true; clearTimeout(toastTimer); undo?.(); };
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 8000);
}

/* ---------- People: name the faces in the slideshow (👤 or f) ---------- */

const FACES_KEY = 'souvenir.faces';
const facesCache = new globalThis.Map(); // photo name -> faces (api/faces)
viewer.faces = (() => { try { return localStorage.getItem(FACES_KEY) === '1'; } catch { return false; } })();

/** Boxes over the faces of the photo shown: white unknown, yellow recognised, green named by you. */
async function drawFaces() {
  const layer = $('viewer').querySelector('.v-faces-layer');
  $('viewer').querySelector('.v-faces').classList.toggle('on', viewer.faces);
  $('viewer').querySelector('.v-faces').hidden = !state.people.available;
  layer.textContent = '';
  const p = viewer.list[viewer.i];
  if (!viewer.faces || !state.people.available || !p || p.kind === 'video') return;
  if (!facesCache.has(p.name)) {
    const r = await fetch('api/faces?photo=' + encodeURIComponent(p.name));
    facesCache.set(p.name, r.ok ? await r.json() : []);
    if (viewer.list[viewer.i] !== p) return; // moved on meanwhile
  }
  const img = $('viewer').querySelector('.v-stage img');
  if (!img) return;
  if (!img.complete) { img.addEventListener('load', drawFaces, { once: true }); return; }
  layer.textContent = '';
  const r = img.getBoundingClientRect(), v = $('viewer').getBoundingClientRect();
  for (const f of facesCache.get(p.name)) {
    const [x, y, w, h] = f.box;
    const box = document.createElement('div');
    box.className = 'face ' + (f.kind || 'unknown');
    Object.assign(box.style, {
      left: `${r.left - v.left + x * r.width}px`, top: `${r.top - v.top + y * r.height}px`,
      width: `${w * r.width}px`, height: `${h * r.height}px`,
    });
    const label = document.createElement('span');
    label.textContent = f.name ? f.name + (f.kind === 'auto' ? ' ?' : '') : '?';
    box.append(label);
    box.title = f.name ? `${f.name} (${f.kind === 'auto' ? `reconnu, ${Math.round(f.similarity * 100)} %` : 'nommé'})` : 'Qui est-ce ?';
    box.onclick = (e) => { e.stopPropagation(); askFace(p, f); };
    box.onpointerup = (e) => e.stopPropagation(); // not a tap to change photo
    layer.append(box);
  }
}

function toggleFaces() {
  viewer.faces = !viewer.faces;
  try { localStorage.setItem(FACES_KEY, viewer.faces ? '1' : '0'); } catch { /* ignore */ }
  hideAsk();
  drawFaces();
}

/** "Qui est-ce ?": name a face, confirm its name, or say it is not that person. */
let asking = null; // {p, f} while the form is open
function askFace(p, f) {
  const form = $('viewer').querySelector('.v-ask');
  asking = { p, f };
  stopAuto();
  form.who.value = f.name || '';
  const not = form.querySelector('.v-ask-not');
  not.hidden = !f.name;
  not.textContent = f.name ? `Pas ${f.name}` : '';
  form.hidden = false;
  form.who.focus();
  form.who.select();
}
function hideAsk() {
  $('viewer').querySelector('.v-ask').hidden = true;
  asking = null;
}
async function answerFace(kind, name) {
  if (!asking || !name) return hideAsk();
  const { p, f } = asking;
  hideAsk();
  const r = await fetch(`api/people/${kind}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ photo: p.name, face: f.face, name }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) return alert(`Impossible : ${data.error || r.status}`);
  facesCache.clear(); // one answer can change who is recognised in many photos
  facesCache.set(p.name, data.faces);
  p.people = [...new Set(data.faces.filter((x) => x.name).map((x) => x.name))].sort();
  if (viewer.list[viewer.i] === p) showInViewer();
  await loadPeople();
  if (state.person) await showPerson(state.person.name, { keepView: true });
}
{
  const form = $('viewer').querySelector('.v-ask');
  form.addEventListener('submit', (e) => { e.preventDefault(); answerFace('name', form.who.value.trim()); });
  form.querySelector('.v-ask-not').onclick = () => answerFace('refuse', asking?.f.name);
  form.querySelector('.v-ask-cancel').onclick = hideAsk;
  form.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); hideAsk(); }
    e.stopPropagation(); // typing a name must not drive the slideshow
  });
  form.addEventListener('pointerup', (e) => e.stopPropagation());
  $('viewer').querySelector('.v-faces').onclick = (e) => { e.stopPropagation(); toggleFaces(); };
  window.addEventListener('resize', () => { if (viewerOpen()) { clampZoom(); applyZoom(); } });
}

/* The "Personnes" section: each name, its face and number of photos; a tap shows them. */
async function loadPeople() { // counts for the selected days (all, if none is)
  try {
    const r = await fetch('api/people?days=' + [...state.selected].sort().join(','));
    if (r.ok) state.people = await r.json();
  } catch { /* the server is restarting: keep what we have */ }
  renderPeople();
}
function renderPeople() {
  const { available, people } = state.people;
  $('people-section').hidden = !available;
  const box = $('people');
  box.textContent = '';
  $('people-names').textContent = '';
  for (const person of people) {
    for (const n of [person.name, ...(person.aliases || [])]) $('people-names').append(new Option(n));
    const b = document.createElement('button');
    b.className = (state.person?.name === person.name ? 'on' : '') + (person.photos ? '' : ' none');
    if (person.cover) {
      const img = document.createElement('img');
      img.alt = '';
      img.src = `face?photo=${encodeURIComponent(person.cover.photo)}&face=${person.cover.face}`;
      b.append(img);
    }
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = person.photos;
    b.append(person.name, n);
    b.title = `Les photos de ${person.name}` + (person.aliases?.length ? ` (aussi : ${person.aliases.join(', ')})` : '') +
      (state.selected.length ? ` — ${person.photos} photo${plural(person.photos)} sur ces jours, ${person.total} en tout`
                             : ` — ${person.total} photo${plural(person.total)}`);
    b.onclick = () => (state.person?.name === person.name ? leavePerson() : showPerson(person.name));
    box.append(b);
  }
  if (available && !people.length) {
    box.textContent = 'Nommez des visages dans le diaporama (bouton 👤).';
    box.className = 'people hint';
  } else {
    box.className = 'people';
  }
}
async function showPerson(name, { keepView = false } = {}) {
  const r = await fetch('api/people/photos?name=' + encodeURIComponent(name));
  if (!r.ok) return;
  state.person = await r.json();
  if (state.search) { state.search = null; $('search').value = ''; $('search-note').hidden = true; }
  renderPeople();
  updatePhotos();
  if (keepView) return;
  if (onPhone()) setTab('photos');
  if (!showHere()) {
    const b = new LngLatBounds();
    for (const p of personPhotos()) if (p.lat != null) b.extend([p.lon, p.lat]);
    if (!b.isEmpty()) map.fitBounds(b, { padding: 60, maxZoom: 15, duration: 600 });
  }
}
/* In a person's view: their other names, comma separated ("Laurence Masso, petite nièce"),
   used to name faces and in the search. */
let aliasFor = null; // whose aliases the form shows
function showAliasForm() {
  const form = $('alias-form'), person = state.person;
  form.hidden = !person;
  if (!person || aliasFor === person.name) return; // keep what is being typed
  aliasFor = person.name;
  const known = state.people.people.find((p) => p.name === person.name);
  form.aliases.value = (known?.aliases || []).join(', ');
  $('alias-note').textContent = '';
}
$('alias-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = state.person?.name;
  if (!name) return;
  const aliases = $('alias-form').aliases.value.split(',').map((a) => a.trim()).filter(Boolean);
  const r = await fetch('api/people/aliases', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, aliases }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { $('alias-note').textContent = `Impossible : ${data.error || r.status}`; return; }
  $('alias-form').aliases.value = data.aliases.join(', ');
  $('alias-note').textContent = data.aliases.length ? 'Enregistré : la recherche les reconnaît.' : 'Aucun autre nom.';
  await loadPeople();
});

/* In a person's view, click their name above the photos to correct it ("Laurance" ->
   "Laurence"); a name already given to someone else merges the two, after asking. */
function editPersonName(btn) {
  const old = state.person?.name;
  if (!old) return;
  const input = document.createElement('input');
  input.className = 'person-name-edit';
  input.value = old;
  input.size = Math.max(8, old.length + 2);
  input.setAttribute('aria-label', 'Nom de la personne');
  btn.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const to = input.value.trim();
    if (save && to && to !== old) return renamePerson(old, to);
    state.photoKey = null; // back to the name as it was
    renderPhotos();
  };
  input.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(false);
}
async function renamePerson(old, to, merge = false) {
  const { ok, data } = await postJSON('api/people/rename', { name: old, to, merge });
  if (!ok) {
    if (data.merge && !merge &&
        confirm(`« ${data.merge} » existe déjà. Réunir ${old} et ${data.merge} en une seule personne, ${data.merge} ?`)) {
      return renamePerson(old, to, true);
    }
    if (!data.merge) alert(`Impossible : ${data.error || ''}`);
    state.photoKey = null;
    return renderPhotos();
  }
  aliasFor = null; // the alias form shows the person under their new name
  facesCache.clear(); // faces show names
  state.photos.clear(); // each photo lists its people's names: reloaded
  state.allPhotos = null;
  await load();
  render();
  await showPerson(data.name, { keepView: true });
  await loadPeople();
}

/* Click the "Personnes" title to fold / unfold the list (remembered in this browser). */
const PEOPLE_FOLD_KEY = 'souvenir.peopleFolded';
function foldPeople(folded) {
  $('people-section').classList.toggle('folded', folded);
  $('people-title').setAttribute('aria-expanded', String(!folded));
  $('people-title').querySelector('.fold').textContent = folded ? '▸' : '▾';
  try { localStorage.setItem(PEOPLE_FOLD_KEY, folded ? '1' : '0'); } catch { /* ignore */ }
}
$('people-title').onclick = () => foldPeople(!$('people-section').classList.contains('folded'));
$('people-title').onkeydown = (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('people-title').click(); }
};
try { if (localStorage.getItem(PEOPLE_FOLD_KEY) === '1') foldPeople(true); } catch { /* ignore */ }

function leavePerson() {
  state.person = null;
  renderPeople();
  updatePhotos();
}

function step(delta) {
  const i = viewer.i + delta;
  if (i < 0 || i >= viewer.list.length) return stopAuto();
  viewer.i = i;
  showInViewer();
  if (viewer.timer) startAuto(); // restart the countdown for the new photo
}

function startAuto() {
  clearTimeout(viewer.timer);
  const p = viewer.list[viewer.i];
  // A video moves on when it ends (onended); a photo after VIEWER_AUTO_MS.
  viewer.timer = setTimeout(() => { if (p.kind !== 'video') step(1); }, VIEWER_AUTO_MS);
  $('viewer').querySelector('.v-play').textContent = '❚❚';
}

function stopAuto() {
  clearTimeout(viewer.timer);
  viewer.timer = null;
  $('viewer').querySelector('.v-play').textContent = '▶';
}

// Controls fade out after 3 s without moving the mouse.
function wakeViewer() {
  $('viewer').classList.remove('idle');
  clearTimeout(viewer.idle);
  viewer.idle = setTimeout(() => $('viewer').classList.add('idle'), 3000);
}

$('viewer-open').onclick = openViewer;

/* ⬇: the panel's whole list (as the slideshow, not only the photos shown) in one zip,
   named after what is shown. The server makes it while sending it (/export/TICKET). */
function exportTitle() {
  const here = showHere() ? ' ici' : '';
  if (state.search) return `recherche ${state.search.q}${here}`;
  if (state.person) return state.person.name + here;
  const days = [...state.selected].sort();
  return (!days.length ? 'toutes dates' : days.length === 1 ? days[0] : `${days[0]}-${days[days.length - 1]}`) + here;
}
const fmtBytes = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} Go` : `${Math.max(1, Math.round(b / 1e6))} Mo`;
$('photos-download').onclick = async () => {
  const list = shownPhotoList();
  if (!list.length) return;
  const { ok, data } = await postJSON('api/export', { names: list.map((p) => p.name), title: exportTitle() });
  if (!ok) return alert(`Impossible : ${data.error || ''}`);
  if (!confirm(`Télécharger ${data.count} photo${plural(data.count)} (${fmtBytes(data.bytes)}) dans un fichier zip ?`)) return;
  const a = document.createElement('a'); // a plain link: the browser shows the download
  a.href = `export/${data.ticket}`;
  a.download = '';
  document.body.append(a);
  a.click();
  a.remove();
};
$('viewer').querySelector('.v-close').onclick = closeViewer;
$('viewer').querySelector('.v-prev').onclick = (e) => { e.stopPropagation(); step(-1); };
$('viewer').querySelector('.v-next').onclick = (e) => { e.stopPropagation(); step(1); };
$('viewer').querySelector('.v-play').onclick = (e) => {
  e.stopPropagation();
  if (viewer.timer) stopAuto(); else startAuto();
};
$('viewer').addEventListener('mousemove', wakeViewer);
// Click on the left / right half of a photo, or swipe, to move. On a touch screen:
// pinch to zoom, one finger to move the zoomed photo. Double-click / double-tap:
// back to the map, on this photo.
let swipeX = null, drag = null, clickTimer = null;
const touches = new globalThis.Map(); // pointerId → {x, y} of the fingers on the screen
let pinch = null, pinched = false, lastTap = null, lastPointer = 'mouse';
const DOUBLE_MS = 300;
const fingers = () => {
  const [a, b] = [...touches.values()];
  return { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
};
const vStage = $('viewer').querySelector('.v-stage');
vStage.addEventListener('pointerdown', (e) => {
  lastPointer = e.pointerType;
  if (e.pointerType !== 'mouse') touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (touches.size === 2) { // a second finger: pinch, neither swipe nor tap
    pinch = fingers(); pinched = true;
    drag = null; swipeX = null; lastTap = null;
    clearTimeout(clickTimer);
    return;
  }
  if (pinched) return;
  swipeX = e.clientX;
  if (viewer.zoom.z > 1 && e.button === 0) { // zoomed: dragging moves the photo
    e.preventDefault(); // no text selection, no browser image drag
    drag = { x: e.clientX, y: e.clientY, zx: viewer.zoom.x, zy: viewer.zoom.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
});
// The browser's own image drag (to drop the photo elsewhere) would take the pointer
// after a few pixels and stop ours (pointercancel): not in the slideshow.
vStage.addEventListener('dragstart', (e) => e.preventDefault());
vStage.addEventListener('pointermove', (e) => {
  if (touches.has(e.pointerId)) touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pinch && touches.size >= 2) {
    const now = fingers();
    zoomAt(now.x, now.y, now.d / pinch.d); // spread / close the fingers
    viewer.zoom.x += now.x - pinch.x; // and move them together
    viewer.zoom.y += now.y - pinch.y;
    clampZoom();
    applyZoom();
    pinch = now;
    return;
  }
  if (!drag) return;
  if (!(e.buttons & 1)) { drag = null; return; } // the button is no longer held: not dragging
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return; // a press, not yet a drag
  drag.moved = true;
  viewer.zoom.x = drag.zx + dx;
  viewer.zoom.y = drag.zy + dy;
  clampZoom();
  applyZoom();
});
/** A finger left the screen: after a pinch, wait for all of them before anything else. */
function fingerUp(e) {
  touches.delete(e.pointerId);
  if (touches.size < 2) pinch = null;
  if (!pinched) return false;
  if (touches.size === 0) pinched = false;
  drag = null; swipeX = null;
  return true;
}
// The browser took the pointer back (button released elsewhere…): stop dragging.
vStage.addEventListener('lostpointercapture', () => { drag = null; });
vStage.addEventListener('pointercancel', (e) => { fingerUp(e); drag = null; });
vStage.addEventListener('pointerup', (e) => {
  if (fingerUp(e)) return;
  const wasDrag = !!drag, moved = wasDrag && drag.moved;
  drag = null;
  if (swipeX === null || e.target.tagName === 'VIDEO') return; // video: its own controls
  const dx = e.clientX - swipeX;
  swipeX = null;
  if (moved) return; // zoomed: the photo was moved, no change of photo
  if (!wasDrag && Math.abs(dx) > 50) return step(dx < 0 ? 1 : -1); // a swipe: at once
  if (e.pointerType !== 'mouse') { // double-tap: show on the map (a mouse has dblclick)
    const t = Date.now();
    if (lastTap && t - lastTap.t < DOUBLE_MS && Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < 40) {
      lastTap = null;
      clearTimeout(clickTimer); // the first tap was not a tap to change photo
      return closeViewer(); // which shows the photo on the map
    }
    lastTap = { t, x: e.clientX, y: e.clientY };
  }
  if (wasDrag) return; // zoomed: a click does not change photo
  // A click changes photo a little later: the first click of a double-click must not.
  const dir = e.clientX < window.innerWidth / 2 ? -1 : 1;
  clearTimeout(clickTimer);
  clickTimer = setTimeout(() => step(dir), DOUBLE_MS);
});

/* ---------- Zoom in the slideshow: wheel (towards the cursor), drag, Shift+arrows,
   pinch. The photo starts unzoomed; ← → still change photo. ---------- */

const ZOOM_MAX = 8, ZOOM_STEP = 1.2;
viewer.zoom = { z: 1, x: 0, y: 0 }; // scale, and translation in px of the photo's top-left
const zoomImg = () => $('viewer').querySelector('.v-stage img');

/** Keep the zoomed photo covering the screen (centred when smaller than it). */
function clampZoom() {
  const img = zoomImg(), stage = $('viewer').querySelector('.v-stage');
  if (!img) return;
  const z = viewer.zoom;
  const fit = (t, base, size, room) => size * z.z <= room
    ? (room - size * z.z) / 2 - base
    : Math.min(-base, Math.max(room - size * z.z - base, t));
  z.x = fit(z.x, img.offsetLeft, img.offsetWidth, stage.clientWidth);
  z.y = fit(z.y, img.offsetTop, img.offsetHeight, stage.clientHeight);
  if (z.z === 1) { z.x = 0; z.y = 0; }
}
function applyZoom() {
  const img = zoomImg(), z = viewer.zoom;
  if (img) {
    img.style.transformOrigin = '0 0';
    img.style.transform = z.z === 1 ? '' : `translate(${z.x}px, ${z.y}px) scale(${z.z})`;
  }
  $('viewer').classList.toggle('zoomed', z.z > 1);
  drawFaces(); // face boxes follow the zoom
}
/** Zoom by `factor`, keeping the point under (clientX, clientY) where it is. */
function zoomAt(clientX, clientY, factor) {
  const img = zoomImg();
  if (!img) return; // videos are not zoomed
  stopAuto();
  const r = $('viewer').querySelector('.v-stage').getBoundingClientRect();
  const px = clientX - r.left - img.offsetLeft, py = clientY - r.top - img.offsetTop;
  const z = viewer.zoom, nz = Math.min(ZOOM_MAX, Math.max(1, z.z * factor));
  z.x = px - (px - z.x) * nz / z.z;
  z.y = py - (py - z.y) * nz / z.z;
  z.z = nz;
  clampZoom();
  applyZoom();
}
$('viewer').querySelector('.v-stage').addEventListener('wheel', (e) => {
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
}, { passive: false });
$('viewer').querySelector('.v-stage').addEventListener('dblclick', () => {
  clearTimeout(clickTimer); // it was not a click to change photo
  if (lastPointer === 'mouse') closeViewer(); // shows the photo on the map (touch: on pointerup)
});
/** Shift+arrows: move the zoomed photo by a sixth of the screen. */
function panZoom(dx, dy) {
  if (viewer.zoom.z === 1) return;
  const stage = $('viewer').querySelector('.v-stage');
  viewer.zoom.x += dx * stage.clientWidth / 6;
  viewer.zoom.y += dy * stage.clientHeight / 6;
  clampZoom();
  applyZoom();
}
document.addEventListener('keydown', (e) => {
  if (!viewerOpen() || e.target.tagName === 'INPUT') return;
  if (e.key === 'f') { toggleFaces(); wakeViewer(); return; }
  if (e.key === 'Delete') { deleteShown(); wakeViewer(); return; }
  if ((e.key === 'r' || e.key === 'R') && !e.ctrlKey && !e.metaKey && !e.altKey) { // not Ctrl+R (reload)
    rotateShown(e.key === 'R' ? -1 : 1); wakeViewer(); return;
  }
  if (e.key.toLowerCase() === 'z' && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
    e.preventDefault(); undoDelete(); wakeViewer(); return;
  }
  if (e.shiftKey && e.key.startsWith('Arrow')) { // move in the zoomed photo
    e.preventDefault();
    const d = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[e.key];
    if (d) panZoom(...d);
    wakeViewer();
    return;
  }
  if (e.key === 'ArrowLeft') step(-1);
  else if (e.key === 'ArrowRight') step(1);
  else if (e.key === ' ' && e.target.tagName !== 'VIDEO') {
    e.preventDefault();
    if (viewer.timer) stopAuto(); else startAuto();
  } else if (e.key === 'Escape') closeViewer();
  else return;
  wakeViewer();
});
// Leaving full screen (Esc handled by the browser) closes the viewer too.
document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement) closeViewer(); });

function renderLegend() {
  const scale = SCALES[colorBy()];
  const ticks = $('legend').querySelector('.ticks');
  $('legend').hidden = false;
  ticks.classList.toggle('centered', !scale);
  if (!scale) { // "Jour": the 7 weekday colours
    const w = 100 / 7;
    $('legend').querySelector('.bar').style.background = `linear-gradient(to right, ${
      WEEKDAY_COLORS.map((c, i) => `${c} ${i * w}% ${(i + 1) * w}%`).join(', ')})`;
    ticks.textContent = '';
    WEEKDAY_LETTERS.forEach((l, i) => {
      const s = document.createElement('span');
      s.style.left = (i + 0.5) * w + '%';
      s.textContent = l;
      ticks.append(s);
    });
    return;
  }
  const { stops, unit, even } = scale;
  const max = stops[stops.length - 1][0];
  const pos = (v, i) => (even ? i / (stops.length - 1) : v / max) * 100;
  $('legend').querySelector('.bar').style.background =
    `linear-gradient(to right, ${stops.map(([v, c], i) => `${c} ${pos(v, i)}%`).join(', ')})`;
  ticks.textContent = '';
  stops.forEach(([v], i) => {
    if (scale.hideTicks?.includes(v)) return;
    const s = document.createElement('span');
    s.style.left = pos(v, i) + '%';
    s.textContent = v + (i === stops.length - 1 || !even ? unit : '');
    ticks.append(s);
  });
}

function fitTo(days) {
  const b = new LngLatBounds();
  if (showGps()) for (const d of days) for (const p of filtered(d)) b.extend([p[LON], p[LAT]]);
  if (showPhotos()) {
    for (const d of days) for (const p of state.photos.get(d) || []) if (p.lat != null) b.extend([p.lon, p.lat]);
  }
  if (!b.isEmpty()) map.fitBounds(b, { padding: 60, maxZoom: 16, duration: 600 });
}

async function select(days, { add = false } = {}) {
  if (add) {
    for (const d of days) {
      const i = state.selected.indexOf(d);
      if (i >= 0) state.selected.splice(i, 1); else state.selected.push(d);
    }
  } else {
    state.selected = [...days];
  }
  if (state.selected.length) state.month = keyDate(state.selected[state.selected.length - 1]);
  defaultColor();
  if (state.people.available) loadPeople(); // their number of photos on these days
  render();
  await load();
  render(true);
  loadHere();
}

/** Fetch what the selected days still miss: photos always, traces only if GPS is shown. */
async function load() {
  // With "ici", photos come filtered from the server (loadHere), not day by day.
  const needPhotos = showHere() ? [] : state.selected.filter((d) => !state.photos.has(d));
  // "Dates : toutes": every photo, once (no traces: far too many); the panel shows 20 at a time.
  const needAll = datesFolded() && !showHere() && !state.allPhotos;
  const needTracks = showGps() ? state.selected.filter((d) => !state.tracks.has(d)) : [];
  const batches = [];
  for (let i = 0; i < needPhotos.length; i += 100) batches.push(needPhotos.slice(i, i + 100));
  await Promise.all([
    needAll && (async () => {
      const r = await fetch('api/photos?all=1');
      state.allPhotos = r.ok ? await r.json() : [];
      allSorted = null;
    })(),
    ...batches.map(async (days) => {
      const r = await fetch('api/photos?days=' + days.join(','));
      const list = r.ok ? await r.json() : [];
      for (const d of days) state.photos.set(d, []);
      for (const p of list) state.photos.get(p.day)?.push(p);
    }),
    ...needTracks.map(async (d) => {
      const r = await fetch('api/track?day=' + d);
      state.tracks.set(d, r.ok ? (await r.json()).points : []);
    }),
  ]);
}

/** Select every available day from a to b (inclusive), e.g. a week or a ⇧+click range. */
function selectRange(a, b, opts) {
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  const days = state.days.filter((d) => d >= lo && d <= hi);
  if (days.length) select(days, opts);
}

/* ---------- Calendar ---------- */

function renderCalendar() {
  const m = state.month || new Date();
  const first = new Date(m.getFullYear(), m.getMonth(), 1);
  $('month-label').textContent = first.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
  syncYear();
  const grid = $('cal-grid');
  grid.textContent = '';
  for (const d of ['L', 'M', 'M', 'J', 'V', 'S', 'D']) {
    const e = document.createElement('div');
    e.className = 'dow';
    e.textContent = d;
    grid.append(e);
  }
  const offset = (first.getDay() + 6) % 7; // Monday first
  for (let i = 0; i < offset; i++) grid.append(document.createElement('div'));
  const has = new Set(state.days);
  const n = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
  for (let i = 1; i <= n; i++) {
    const k = dayKey(new Date(m.getFullYear(), m.getMonth(), i));
    const b = document.createElement('button');
    b.className = 'd';
    b.textContent = i;
    if (has.has(k)) {
      b.classList.add('has');
      b.title = fmtDay(k);
      b.onclick = (e) => {
        if (e.shiftKey && state.anchor) return selectRange(state.anchor, k);
        state.anchor = k;
        select([k], { add: e.ctrlKey || e.metaKey });
      };
      if (state.selected.includes(k)) {
        b.classList.add('sel');
        b.style.setProperty('--c', colorOf(k));
      }
    } else {
      b.disabled = true;
    }
    grid.append(b);
  }
}

function shiftMonth(delta) {
  const m = state.month || new Date();
  state.month = new Date(m.getFullYear(), m.getMonth() + delta, 1);
  renderCalendar();
}

/* "Semaine": Monday to Sunday around the last clicked day (or the shown month's first day with data). */
function selectWeek() {
  const m = state.month || new Date();
  const ref = state.anchor ? keyDate(state.anchor)
    : keyDate(state.days.find((d) => d.startsWith(dayKey(m).slice(0, 6))) || dayKey(m));
  const monday = new Date(ref.getFullYear(), ref.getMonth(), ref.getDate() - (ref.getDay() + 6) % 7);
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
  selectRange(dayKey(monday), dayKey(sunday));
}

/* The "Dates" title: unfolded, the calendar chooses the days that filter everything;
   folded, no day is selected, i.e. no date filter: the search, Personnes and the photos
   cover all dates (every photo is listed, the panel shows 20 at a time; the traces are
   not loaded: far too many). Unfolding
   gives back the days selected before. Remembered in this browser. */
const DATES_FOLD_KEY = 'souvenir.datesFolded';
const datesFolded = () => $('dates-section').classList.contains('folded');
function foldDates(folded) {
  if (folded === datesFolded()) return;
  $('dates-section').classList.toggle('folded', folded);
  $('dates-title').setAttribute('aria-expanded', String(!folded));
  $('dates-title').querySelector('.fold').textContent = folded ? '▸' : '▾';
  try { localStorage.setItem(DATES_FOLD_KEY, folded ? '1' : '0'); } catch { /* ignore */ }
  if (folded) {
    state.datesKept = [...state.selected];
    select([]);
  } else {
    select(state.datesKept.length ? state.datesKept : state.days.slice(-1));
  }
}
$('dates-title').onclick = () => foldDates(!datesFolded());
$('dates-title').onkeydown = (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('dates-title').click(); }
};

/* Year and month menus. The year menu shows the calendar's year; the month menu
   keeps the user's choice ("*" = whole year, the default), so choosing a year
   selects that month of it: handy to compare the same month across years. */
const shownYear = () => String((state.month || new Date()).getFullYear());
let monthChoice = '*';

/** Select a whole year (month "*") or one month of it. */
function selectYearMonth(y, m) {
  const [from, to] = m === '*' ? [`${y}0101`, `${y}1231`]
    : [`${y}${m}01`, `${y}${m}${new Date(+y, +m, 0).getDate()}`]; // day 0 of next month = last day
  if (state.days.some((d) => d >= from && d <= to)) return selectRange(from, to);
  // Nothing in that period: at least show it in the calendar, so the menus stay coherent.
  state.month = new Date(+y, m === '*' ? 0 : +m - 1, 1);
  renderCalendar();
}

function fillYears() {
  const menu = $('sel-year');
  menu.textContent = '';
  for (const y of [...new Set(state.days.map((d) => d.slice(0, 4)))].reverse()) {
    menu.append(new Option(y, y));
  }
  syncYear();
}
function syncYear() {
  const menu = $('sel-year'), y = shownYear();
  if (document.activeElement !== menu && [...menu.options].some((o) => o.value === y)) menu.value = y;
  fillMonths();
}
/** "Mois" (value "*": whole year) then the 12 months; months of the shown year without data are greyed out. */
function fillMonths() {
  const menu = $('sel-month'), y = shownYear();
  if (document.activeElement === menu) return;
  const withData = new Set(state.days.filter((d) => d.startsWith(y)).map((d) => d.slice(4, 6)));
  menu.textContent = '';
  menu.append(new Option('Mois', '*')); // all the months
  for (let m = 1; m <= 12; m++) {
    const mm = pad(m);
    const o = new Option(new Date(2000, m - 1, 1).toLocaleDateString('fr-FR', { month: 'short' }), mm);
    o.disabled = !withData.has(mm);
    menu.append(o);
  }
  menu.value = monthChoice;
}
// Choosing the value already shown must still select it: a menu forgets its value
// while open (so any choice is a change) and gets it back if nothing was chosen.
for (const id of ['sel-year', 'sel-month']) {
  $(id).addEventListener('focus', (e) => { e.target.selectedIndex = -1; });
  $(id).addEventListener('blur', syncYear);
}
$('sel-year').addEventListener('change', (e) => {
  const y = e.target.value;
  e.target.blur();
  selectYearMonth(y, monthChoice);
});
$('sel-month').addEventListener('change', (e) => {
  monthChoice = e.target.value;
  e.target.blur();
  selectYearMonth(shownYear(), monthChoice);
});

function stepDay(delta) {
  if (!state.days.length) return;
  const cur = state.selected[state.selected.length - 1];
  let i = state.days.indexOf(cur);
  i = i < 0 ? state.days.length - 1 : Math.min(state.days.length - 1, Math.max(0, i + delta));
  select([state.days[i]]);
}

/* ---------- Wiring ---------- */

function syncOutputs() {
  $('acc-out').textContent = `${$('acc').value} m`;
  $('from-out').textContent = fmtQuarter(+$('from').value);
  $('to-out').textContent = fmtQuarter(+$('to').value);
}

for (const id of ['acc', 'network', 'from', 'to']) {
  $(id).addEventListener('input', () => {
    if (+$('from').value > +$('to').value) {
      if (id === 'from') $('to').value = $('from').value; else $('from').value = $('to').value;
    }
    syncOutputs();
    render();
  });
}
for (const r of document.querySelectorAll('input[name=colorby]')) r.addEventListener('change', () => render());
$('sel-week').onclick = selectWeek;
$('show-gps').addEventListener('change', async () => {
  render();
  if (showGps()) { await load(); render(); }
});
$('show-photos').addEventListener('change', () => {
  $('show-here').disabled = !showPhotos();
  render();
  loadHere();
});
$('show-here').addEventListener('change', async () => {
  if (showHere()) {
    state.here = { total: 0, photos: [] };
    updatePhotos();
    loadHere();
  } else {
    await load(); // the per-day lists, not loaded while "ici" was on
    render();
  }
});
$('prev-month').onclick = () => shiftMonth(-1);
$('next-month').onclick = () => shiftMonth(1);
document.addEventListener('keydown', (e) => {
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) || viewerOpen()) return; // the viewer has its own keys
  if (e.key.toLowerCase() === 'z' && (e.ctrlKey || e.metaKey) && !e.shiftKey && deletions.length) {
    e.preventDefault(); return undoDelete(); // a photo deleted or moved
  }
  if (e.key === 'ArrowLeft' && !datesFolded()) stepDay(-1); // folded "Dates": no day to step
  if (e.key === 'ArrowRight' && !datesFolded()) stepDay(1);
  if (e.key === 'd' && !e.ctrlKey && !e.metaKey && !e.altKey && !$('photos').hidden) return openViewer(); // diaporama
  // Page Up / Down, Home / End scroll the photo panel, without clicking in it first.
  const panel = $('photos');
  if (panel.hidden) return;
  const page = panel.clientHeight * 0.9; // keep a bit of the previous screen in view
  const to = { PageDown: panel.scrollTop + page, PageUp: panel.scrollTop - page, Home: 0, End: panel.scrollHeight }[e.key];
  if (to === undefined) return;
  e.preventDefault();
  panel.scrollTo({ top: to, behavior: 'smooth' });
});

(async () => {
  syncOutputs();
  state.days = await (await fetch('api/days')).json();
  loadPeople();
  loadAdmin();
  fillYears();
  $('count').textContent = `${state.days.length} jour${state.days.length > 1 ? 's' : ''}`;
  const fromHash = location.hash.slice(1).split(',').filter((d) => state.days.includes(d));
  const initial = fromHash.length ? fromHash : state.days.slice(-1);
  let folded = false;
  try { folded = localStorage.getItem(DATES_FOLD_KEY) === '1'; } catch { /* ignore */ }
  if (folded) { // "Dates" left folded: all dates; unfolding will give these days
    state.datesKept = initial;
    $('dates-section').classList.add('folded');
    $('dates-title').setAttribute('aria-expanded', 'false');
    $('dates-title').querySelector('.fold').textContent = '▸';
    await select([]);
  } else if (initial.length) await select(initial);
  else renderCalendar();
})();
