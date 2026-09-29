import { Map, Popup, LngLatBounds, NavigationControl, ScaleControl, addProtocol }
  from './vendor/maplibre-gl.mjs';

/* global pmtiles, basemaps */

const COLORS = ['#e6194b', '#2563eb', '#16a34a', '#f59e0b', '#9333ea', '#0891b2', '#db2777', '#65a30d'];
// Point fields as sent by /api/track.
const T = 0, LAT = 1, LON = 2, ELE = 3, ACC = 4, SPEED = 5, PROV = 6, BAT = 7;

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
const state = {
  days: [],               // available days, 'YYYYMMDD', sorted
  selected: [],           // selected days, in selection order
  tracks: new globalThis.Map(), // day -> points
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

mapReady.then(() => {
  const empty = { type: 'FeatureCollection', features: [] };
  map.addSource('lines', { type: 'geojson', data: empty });
  map.addSource('points', { type: 'geojson', data: empty });
  map.addSource('ends', { type: 'geojson', data: empty });
  map.addSource('stays', { type: 'geojson', data: empty });

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

  const popup = new Popup({ closeButton: false, closeOnClick: false, offset: 8 });
  map.on('mousemove', (e) => {
    // Track points take precedence: they are small and would be unreachable under a big dot.
    const [f] = map.queryRenderedFeatures(e.point, { layers: ['points'] }).length
      ? map.queryRenderedFeatures(e.point, { layers: ['points'] })
      : map.queryRenderedFeatures(e.point, { layers: ['stays'] })
        .sort((a, b) => a.properties.min - b.properties.min);
    map.getCanvas().style.cursor = f ? 'pointer' : '';
    if (!f) return popup.remove();
    const html = f.layer.id === 'points' ? pointInfo(f.properties) : stayInfo(f.properties);
    popup.setLngLat(f.geometry.coordinates).setHTML(html).addTo(map);
  });
  map.on('mouseout', () => popup.remove());
});

/* ---------- Helpers ---------- */

const pad = (n) => String(n).padStart(2, '0');
const dayKey = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
const keyDate = (k) => new Date(+k.slice(0, 4), +k.slice(4, 6) - 1, +k.slice(6, 8));
const fmtDay = (k) => keyDate(k).toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' });
const fmtTime = (ms) => new Date(ms).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
const fmtQuarter = (q) => q >= 96 ? '24:00' : `${pad(Math.floor(q / 4))}:${pad((q % 4) * 15)}`;
const colorOf = (day) => COLORS[state.selected.indexOf(day) % COLORS.length];

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

function stayInfo(p) {
  return `<b>${fmtTime(p.start)} → ${fmtTime(p.end)}</b><br>` +
    `${fmtDuration(p.min)} sur place · ${p.n} points`;
}

function pointInfo(p) {
  const rows = [`<b>${fmtTime(p.t)}</b> · ${p.prov || '?'}`];
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
  const midnight = keyDate(day).getTime();
  // Quarter-hours since local midnight (DST-safe enough for display purposes).
  const lo = midnight + from * 15 * 60e3, hi = midnight + to * 15 * 60e3;
  return pts.filter((p) =>
    (withNet || p[PROV] !== 'network') &&
    (p[ACC] == null || p[ACC] <= maxAcc) &&
    p[T] >= lo && p[T] <= hi);
}

function render(fit = false) {
  const lines = [], points = [], ends = [], stays = [];
  const bounds = new LngLatBounds();
  const list = $('days');
  list.textContent = '';

  for (const day of state.selected) {
    const color = colorOf(day);
    const pts = filtered(day);
    const dayStays = findStays(pts);
    const inStay = new Uint8Array(pts.length);
    for (const s of dayStays) {
      inStay.fill(1, s.first, s.last + 1);
      stays.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
        properties: {
          color, n: s.n, start: pts[s.first][T], end: pts[s.last][T],
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
        properties: { color, t: p[T], acc: p[ACC], speed: p[SPEED], ele: p[ELE], prov: p[PROV], bat: p[BAT] },
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
      const midnight = keyDate(day).getTime();
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
            hour: Math.min(24, Math.max(0, ((a[T] + b[T]) / 2 - midnight) / 3.6e6)),
          },
        });
      }
    }
    if (pts.length) {
      const f = pts[0], l = pts[pts.length - 1];
      ends.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [f[LON], f[LAT]] }, properties: { color, start: true } });
      ends.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [l[LON], l[LAT]] }, properties: { color, start: false } });
    }

    const li = document.createElement('li');
    li.style.setProperty('--c', color);
    const total = (state.tracks.get(day) || []).length;
    const stats = pts.length
      ? `${fmtTime(pts[0][T])} → ${fmtTime(pts[pts.length - 1][T])} · ${(dist / 1000).toFixed(1)} km` +
        (vmax > 0 ? ` · max ${(vmax * 3.6).toFixed(0)} km/h` : '') +
        `<br>${pts.length} / ${total} points · ${dayStays.length} arrêt${dayStays.length > 1 ? 's' : ''}`
      : state.tracks.has(day) ? `aucun point avec ces filtres (${total} au total)` : 'chargement…';
    li.innerHTML = `<div class="t"><span class="sw"></span>${fmtDay(day)}</div><div class="s">${stats}</div>`;
    li.title = 'Centrer sur ce jour';
    li.onclick = () => fitTo([day]);
    list.append(li);
  }

  renderLegend();
  mapReady.then(() => {
    map.setPaintProperty('lines', 'line-color', lineColor(colorBy()));
    // Night segments are black: give them a light outline on the dark map.
    map.setPaintProperty('lines-casing', 'line-color', !dark ? '#fff' : colorBy() === 'time' ? '#9ca3af' : '#000');
    map.getSource('lines').setData({ type: 'FeatureCollection', features: lines });
    map.getSource('points').setData({ type: 'FeatureCollection', features: points });
    map.getSource('ends').setData({ type: 'FeatureCollection', features: ends });
    map.getSource('stays').setData({ type: 'FeatureCollection', features: stays });
    if (fit && !bounds.isEmpty()) map.fitBounds(bounds, { padding: 60, maxZoom: 16, duration: 600 });
  });
  renderCalendar();
  location.replace('#' + state.selected.join(','));
}

function renderLegend() {
  const scale = SCALES[colorBy()];
  $('legend').hidden = !scale;
  if (!scale) return;
  const { stops, unit, even } = scale;
  const max = stops[stops.length - 1][0];
  const pos = (v, i) => (even ? i / (stops.length - 1) : v / max) * 100;
  $('legend').querySelector('.bar').style.background =
    `linear-gradient(to right, ${stops.map(([v, c], i) => `${c} ${pos(v, i)}%`).join(', ')})`;
  const ticks = $('legend').querySelector('.ticks');
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
  for (const d of days) for (const p of filtered(d)) b.extend([p[LON], p[LAT]]);
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
  render();
  await Promise.all(state.selected.filter((d) => !state.tracks.has(d)).map(async (d) => {
    const r = await fetch('api/track?day=' + d);
    state.tracks.set(d, r.ok ? (await r.json()).points : []);
  }));
  render(true);
}

/* ---------- Calendar ---------- */

function renderCalendar() {
  const m = state.month || new Date();
  const first = new Date(m.getFullYear(), m.getMonth(), 1);
  $('month-label').textContent = first.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
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
      b.onclick = (e) => select([k], { add: e.ctrlKey || e.metaKey || e.shiftKey });
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
$('prev-month').onclick = () => shiftMonth(-1);
$('next-month').onclick = () => shiftMonth(1);
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') stepDay(-1);
  if (e.key === 'ArrowRight') stepDay(1);
});

(async () => {
  syncOutputs();
  state.days = await (await fetch('api/days')).json();
  $('count').textContent = `${state.days.length} jour${state.days.length > 1 ? 's' : ''}`;
  const fromHash = location.hash.slice(1).split(',').filter((d) => state.days.includes(d));
  const initial = fromHash.length ? fromHash : state.days.slice(-1);
  if (initial.length) await select(initial);
  else renderCalendar();
})();
