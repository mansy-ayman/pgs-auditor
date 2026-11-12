// popup.js - satellite map renderer with spatial outlier filter for exact farm extent fit
const TILE = 256, MIN_Z = 3, MAX_Z = 19;
const tileUrl = (z, x, y) =>
  `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`;

const $ = (id) => document.getElementById(id);
const mapEl = $('map'), tilePane =$('tiles'), svg = $('overlay'), notice =$('notice');

let data = null;
let currentDocs = [];
let view = { lat: 28, lng: 29, z: 5 };
let needFit = false;
let rafPending = false;
const tiles = new Map();

const getLat = (p) => (p && typeof p === 'object' && 'lat' in p) ? p.lat : (Array.isArray(p) ? p[0] : 0);
const getLng = (p) => (p && typeof p === 'object' && 'lng' in p) ? p.lng : (Array.isArray(p) ? p[1] : 0);

let validCache = null;
function getValidPoints() { return validCache || (validCache = computeValidPoints()); }

function computeValidPoints() {
  const ext = (data && data.extracted_info) || {};
  let poly = (data && (data.farmPolygon || ext.farm_polygon)) || [];
  let soils = (data && (data.soilSamples || ext.soil_samples)) || [];

  if (poly.length >= 3) {
    let cLat = 0, cLng = 0;
    poly.forEach(p => { cLat += getLat(p); cLng += getLng(p); });
    cLat /= poly.length; cLng /= poly.length;
    soils = soils.filter(s => Math.abs(getLat(s) - cLat) < 0.05 && Math.abs(getLng(s) - cLng) < 0.05);
  } else if (soils.length > 0) {
    const sortedLats = soils.map(getLat).sort((a, b) => a - b);
    const sortedLngs = soils.map(getLng).sort((a, b) => a - b);
    const medLat = sortedLats[Math.floor(sortedLats.length / 2)];
    const medLng = sortedLngs[Math.floor(sortedLngs.length / 2)];
    poly = poly.filter(p => Math.abs(getLat(p) - medLat) < 0.05 && Math.abs(getLng(p) - medLng) < 0.05);
    soils = soils.filter(s => Math.abs(getLat(s) - medLat) < 0.05 && Math.abs(getLng(s) - medLng) < 0.05);
  }
  return { poly, soils };
}

const worldSize = (z) => TILE * Math.pow(2, z);

function project(lat, lng, z) {
  const s = worldSize(z);
  const sin = Math.max(-0.9999, Math.min(0.9999, Math.sin((lat * Math.PI) / 180)));
  return { x: ((lng + 180) / 360) * s, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s };
}

function unproject(x, y, z) {
  const s = worldSize(z);
  const n = Math.PI - (2 * Math.PI * y) / s;
  return { lat: (180 / Math.PI) * Math.atan(Math.sinh(n)), lng: (x / s) * 360 - 180 };
}

function render() {
  if (!mapEl) return;
  const w = mapEl.clientWidth, h = mapEl.clientHeight;
  if (!w || !h) return;
  const z = view.z;
  const c = project(view.lat, view.lng, z);
  const ox = c.x - w / 2, oy = c.y - h / 2;
  const n = Math.pow(2, z);

  const x0 = Math.floor(ox / TILE), x1 = Math.floor((ox + w) / TILE);
  const y0 = Math.max(0, Math.floor(oy / TILE)), y1 = Math.min(n - 1, Math.floor((oy + h) / TILE));
  const need = new Set();

  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = Math.max(0, x0); tx <= Math.min(n - 1, x1); tx++) {
      const key = `${z}/${tx}/${ty}`;
      need.add(key);
      let img = tiles.get(key);
      if (!img) {
        img = new Image();
        img.className = 'tile';
        img.alt = '';
        img.draggable = false;
        img.decoding = 'async';
        img.onerror = () => { img.style.visibility = 'hidden'; };
        img.src = tileUrl(z, tx, ty);
        if (tilePane) tilePane.appendChild(img);
        tiles.set(key, img);
      }
      img.style.transform = `translate(${Math.round(tx * TILE - ox)}px, ${Math.round(ty * TILE - oy)}px)`;
    }
  }
  for (const [key, img] of tiles) {
    if (!need.has(key)) { img.remove(); tiles.delete(key); }
  }
  drawOverlay(ox, oy);
}

function drawOverlay(ox, oy) {
  if (!svg) return;
  const toXY = (p) => {
    const q = project(getLat(p), getLng(p), view.z);
    return [q.x - ox, q.y - oy];
  };
  let out = '';
  const { poly, soils } = getValidPoints();

  if (poly.length >= 2) {
    const pts = poly.map(toXY).map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    out += poly.length >= 3
      ? `<polygon class="poly" points="${pts}"/>`
      : `<polyline class="poly" fill="none" points="${pts}"/>`;
  }
  soils.forEach((p, i) => {
    const [x, y] = toXY(p);
    out += `<g class="soil-g" data-i="${i}"><circle class="soil" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="9"/>` +
           `<text class="soil-n" x="${x.toFixed(1)}" y="${y.toFixed(1)}">${i + 1}</text></g>`;
  });
  svg.innerHTML = out;
}

function schedule() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; render(); });
}

function fit() {
  const { poly, soils } = getValidPoints();
  const pts = poly.length > 0 ? [...poly, ...soils] : soils;

  if (!mapEl) return;
  const w = mapEl.clientWidth, h = mapEl.clientHeight;
  if (!pts.length || !w || !h) return;

  const lats = pts.map(getLat), lngs = pts.map(getLng);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
  const pad = 56;

  let z = 18;
  for (; z > MIN_Z; z--) {
    const a = project(maxLat, minLng, z), b = project(minLat, maxLng, z);
    if (b.x - a.x <= w - 2 * pad && b.y - a.y <= h - 2 * pad) break;
  }
  const a = project(maxLat, minLng, z), b = project(minLat, maxLng, z);
  const mid = unproject((a.x + b.x) / 2, (a.y + b.y) / 2, z);
  view = { lat: mid.lat, lng: mid.lng, z };
}

function zoomAt(dz, cx, cy) {
  if (!mapEl) return;
  const nz = Math.max(MIN_Z, Math.min(MAX_Z, view.z + dz));
  if (nz === view.z) return;
  const w = mapEl.clientWidth, h = mapEl.clientHeight;
  const c = project(view.lat, view.lng, view.z);
  const under = unproject(c.x - w / 2 + cx, c.y - h / 2 + cy, view.z);
  const q = project(under.lat, under.lng, nz);
  const ctr = unproject(q.x - cx + w / 2, q.y - cy + h / 2, nz);
  view = { lat: ctr.lat, lng: ctr.lng, z: nz };
  render();
}

let drag = null;
if (mapEl) {
  mapEl.addEventListener('mousedown', (e) => {
    if (e.target.closest('.ctrl')) return;
    drag = { x: e.clientX, y: e.clientY };
    mapEl.classList.add('dragging');
  });
}

window.addEventListener('mousemove', (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  drag = { x: e.clientX, y: e.clientY };
  const c = project(view.lat, view.lng, view.z);
  const p = unproject(c.x - dx, c.y - dy, view.z);
  view.lat = p.lat; view.lng = p.lng;
  schedule();
});

window.addEventListener('mouseup', () => {
  if (drag && mapEl) { drag = null; mapEl.classList.remove('dragging'); }
});

let lastWheel = 0;
if (mapEl) {
  mapEl.addEventListener('wheel', (e) => {
    e.preventDefault();
    const now = Date.now();
    if (now - lastWheel < 120) return;
    lastWheel = now;
    const r = mapEl.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1 : -1, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  mapEl.addEventListener('dblclick', (e) => {
    const r = mapEl.getBoundingClientRect();
    zoomAt(1, e.clientX - r.left, e.clientY - r.top);
  });
}

const tip = $('tip');
if (svg) {
  svg.addEventListener('click', (e) => {
    const g = e.target.closest && e.target.closest('.soil-g');
    if (!g) { if (tip) tip.style.display = 'none'; return; }
    const p = getValidPoints().soils[Number(g.dataset.i)];
    if (!p || !tip || !mapEl) return;
    const r = mapEl.getBoundingClientRect();
    tip.innerHTML = `عينة تربة رقم ${Number(g.dataset.i) + 1}<small>${getLat(p).toFixed(6)}, ${getLng(p).toFixed(6)}</small>`;
    tip.style.left = Math.min(e.clientX - r.left + 10, r.width - 170) + 'px';
    tip.style.top = Math.max(0, e.clientY - r.top - 50) + 'px';
    tip.style.display = 'block';
  });
}

if (mapEl) mapEl.addEventListener('mousedown', () => { if (tip) tip.style.display = 'none'; });

if ($('btn-zo'))$('btn-zo').addEventListener('click', () => zoomAt(1, mapEl.clientWidth / 2, mapEl.clientHeight / 2));
if ($('btn-zi'))$('btn-zi').addEventListener('click', () => zoomAt(-1, mapEl.clientWidth / 2, mapEl.clientHeight / 2));
if ($('btn-fit'))$('btn-fit').addEventListener('click', () => { fit(); render(); });

if (mapEl) {
  new ResizeObserver(() => {
    if (needFit && mapEl.clientWidth > 0) { fit(); needFit = false; }
    render();
  }).observe(mapEl);
}

function applyData(d) {
  data = d;
  validCache = null;
  currentDocs = d.docs || [];
  
  const { poly, soils } = getValidPoints();

  const ext = d.extracted_info || {};
  const declared = d.areaFaddan ?? ext.area_faddan ?? null;
  const drawn = d.calculatedAreaFaddan ?? ext.calculated_area_faddan ?? null;
  const bad = declared > 0 && drawn != null && Math.abs(drawn - declared) / declared > 0.10;
  
  const dv = $('declared-val'), rv =$('drawn-val');
  if (dv) { dv.textContent = declared == null ? '--' : `${Number(declared).toFixed(2)} فدان`; dv.className = bad ? 'bad' : ''; }
  if (rv) { rv.textContent = drawn == null ? '--' : `${Number(drawn).toFixed(2)} فدان`; rv.className = bad ? 'bad' : ''; }

  const hasAny = poly.length > 0 || soils.length > 0;
  if (mapEl) mapEl.style.display = hasAny ? '' : 'none';
  if ($('empty'))$('empty').style.display = hasAny ? 'none' : 'flex';
  if (notice) {
    notice.style.display = hasAny && poly.length === 0 ? 'block' : 'none';
    notice.textContent = 'لا توجد إحداثيات لحدود المزرعة';
  }

  for (const img of tiles.values()) img.remove();
  tiles.clear();
  
  needFit = true;
  
  if (hasAny && mapEl) {
    const tryFit = () => {
      if (!needFit) return;
      if (mapEl.clientWidth > 0) { 
        fit(); 
        needFit = false; 
        render(); 
      } else {
        requestAnimationFrame(tryFit); 
      }
    };
    tryFit();
  }
}

// All document logging and button logic has been cleanly removed
chrome.storage.local.get(['farm_view'], (r) => {
  if (r.farm_view) applyData(r.farm_view);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local') {
    if (changes.farm_view && changes.farm_view.newValue) applyData(changes.farm_view.newValue);
  }
});