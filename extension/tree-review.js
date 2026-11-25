'use strict';

const farmCode = new URLSearchParams(location.search).get('farm_code') || '';
const BASE = 'http://127.0.0.1:5000';
const $ = id => document.getElementById(id);
const mv = $('mapView');
const SVGNS = 'http://www.w3.org/2000/svg';
const TILE = 256;
const MIN_Z = 14;
const MAX_Z = 20;
const tileUrl = (x, y, z) => `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`;
const fmt = n => Number(n || 0).toLocaleString('en-US');

let session = null;
let polygon = [];
let marks = [];
let undoStack = [];
let redoStack = [];
let center = { lat: 28, lng: 29 };
let z = 18;
let ready = false;
let markersVisible = true;
let drag = null;
let moved = false;
let rafId = 0;
let lastWheel = 0;
const tileEls = new Map();

$('farmCode').textContent = farmCode || 'Farm code unavailable';

/* ---------- Map math (Web Mercator) ---------- */
function worldPx(lat, lng, zl) {
  const s = TILE * Math.pow(2, zl);
  const sin = Math.max(-0.9999, Math.min(0.9999, Math.sin(lat * Math.PI / 180)));
  return {
    x: (lng + 180) / 360 * s,
    y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s
  };
}

function unproject(x, y, zl) {
  const s = TILE * Math.pow(2, zl);
  const n = Math.PI - 2 * Math.PI * y / s;
  return { lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lng: x / s * 360 - 180 };
}

function origin() {
  const w = mv.clientWidth, h = mv.clientHeight;
  const c = worldPx(center.lat, center.lng, z);
  return { ox: c.x - w / 2, oy: c.y - h / 2, w, h };
}

function toScreen(lat, lng) {
  const { ox, oy } = origin();
  const p = worldPx(lat, lng, z);
  return { x: p.x - ox, y: p.y - oy };
}

/* ---------- Undo / counts ---------- */
function snap() {
  undoStack.push(marks.map(m => ({ ...m })));
  if (undoStack.length > 1500) undoStack.shift();
  redoStack = [];
}

function updateCounts() {
  $('liveCount').textContent = fmt(marks.length);
  $('undo').disabled = !undoStack.length;
  $('redo').disabled = !redoStack.length;
  $('manualCount').value = String(marks.length);
}

/* ---------- Rendering ---------- */
function scheduleRender() {
  if (rafId) return;
  rafId = requestAnimationFrame(() => { rafId = 0; render(); });
}

function render() {
  if (!ready) return;
  const { ox, oy, w, h } = origin();
  const n = Math.pow(2, z);
  const x0 = Math.floor(ox / TILE), x1 = Math.floor((ox + w) / TILE);
  const y0 = Math.max(0, Math.floor(oy / TILE)), y1 = Math.min(n - 1, Math.floor((oy + h) / TILE));
  const need = new Set();

  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      const wx = ((tx % n) + n) % n;
      const key = z + '/' + wx + '/' + ty;
      need.add(key);
      let img = tileEls.get(key);
      if (!img) {
        img = new Image();
        img.draggable = false;
        img.alt = '';
        img.src = tileUrl(wx, ty, z);
        $('tiles').appendChild(img);
        tileEls.set(key, img);
      }
      img.style.transform = `translate(${Math.round(tx * TILE - ox)}px, ${Math.round(ty * TILE - oy)}px)`;
    }
  }
  for (const [k, img] of tileEls) {
    if (!need.has(k)) { img.remove(); tileEls.delete(k); }
  }
  drawOverlay();
}

function drawOverlay() {
  const svg = $('overlay');
  svg.replaceChildren();

  if (polygon.length >= 3) {
    const pts = polygon
      .map(p => { const q = toScreen(+p[0], +p[1]); return `${q.x.toFixed(1)},${q.y.toFixed(1)}`; })
      .join(' ');
    const pg = document.createElementNS(SVGNS, 'polygon');
    pg.setAttribute('points', pts);
    pg.setAttribute('class', 'farm-boundary');
    svg.appendChild(pg);
  }

  if (markersVisible) {
    marks.forEach(m => {
  const q = toScreen(m.lat, m.lng);
  const ring = document.createElementNS(SVGNS, 'circle');
  ring.setAttribute('cx', q.x); ring.setAttribute('cy', q.y); ring.setAttribute('r', 11);
  ring.setAttribute('class', 'marker-ring');
  svg.appendChild(ring);
});
  }
  updateCounts();
}

/* ---------- Zoom / fit / layout ---------- */
function zoomAt(nz, lx, ly) {
  nz = Math.max(MIN_Z, Math.min(MAX_Z, nz));
  if (nz === z) return;
  const { ox, oy, w, h } = origin();
  const under = unproject(ox + lx, oy + ly, z);
  const p = worldPx(under.lat, under.lng, nz);
  center = unproject(p.x - lx + w / 2, p.y - ly + h / 2, nz);
  z = nz;
  $('zoom').value = String(z);
  render();
}

function fitPolygon() {
  if (polygon.length < 3) return;
  const lats = polygon.map(p => +p[0]), lngs = polygon.map(p => +p[1]);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
  const w = mv.clientWidth, h = mv.clientHeight, pad = 40;
  let best = MIN_Z;
  for (let zl = MAX_Z; zl >= MIN_Z; zl--) {
    const a = worldPx(maxLat, minLng, zl), b = worldPx(minLat, maxLng, zl);
    if (b.x - a.x <= w - 2 * pad && b.y - a.y <= h - 2 * pad) { best = zl; break; }
  }
  z = best;
  const a = worldPx(maxLat, minLng, z), b = worldPx(minLat, maxLng, z);
  center = unproject((a.x + b.x) / 2, (a.y + b.y) / 2, z);
  $('zoom').value = String(z);
  render();
}

function layoutMap() {
  const top = $('topbar').offsetHeight;
  const st = $('status').offsetHeight + $('imageState').offsetHeight;
  mv.style.height = Math.max(220, innerHeight - top - st - 26) + 'px';
  if (ready) render();
}

/* ---------- Mouse: drag to pan, click to add, right-click to remove ---------- */
mv.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY, lat: center.lat, lng: center.lng };
  moved = false;
  mv.classList.add('dragging');
});

window.addEventListener('mousemove', e => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (!moved && Math.hypot(dx, dy) < 4) return;
  moved = true;
  const c = worldPx(drag.lat, drag.lng, z);
  const p = unproject(c.x - dx, c.y - dy, z);
  center = { lat: p.lat, lng: p.lng };
  scheduleRender();
});

window.addEventListener('mouseup', () => {
  drag = null;
  mv.classList.remove('dragging');
});

mv.addEventListener('click', e => {
  if (!ready || moved) return;
  const r = mv.getBoundingClientRect();
  const { ox, oy } = origin();
  const ll = unproject(ox + e.clientX - r.left, oy + e.clientY - r.top, z);
  snap();
  marks.push({ lat: ll.lat, lng: ll.lng });
  drawOverlay();
});

mv.addEventListener('contextmenu', e => {
  e.preventDefault();
  if (!ready) return;
  const r = mv.getBoundingClientRect();
  const lx = e.clientX - r.left, ly = e.clientY - r.top;
  let best = -1, bestD = 1e9;
  marks.forEach((m, i) => {
    const s = toScreen(m.lat, m.lng);
    const d = Math.hypot(s.x - lx, s.y - ly);
    if (d < bestD) { bestD = d; best = i; }
  });
  if (best >= 0 && bestD <= 14) {
    snap();
    marks.splice(best, 1);
    drawOverlay();
  } else {
    $('status').textContent = 'Right-click inside a marker ring to remove it.';
  }
});

mv.addEventListener('wheel', e => {
  e.preventDefault();
  const now = Date.now();
  if (now - lastWheel < 200) return;
  lastWheel = now;
  const r = mv.getBoundingClientRect();
  zoomAt(z + (e.deltaY < 0 ? 1 : -1), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });

/* ---------- Buttons ---------- */
$('toggleMarkers').onclick = () => {
  markersVisible = !markersVisible;
  $('toggleMarkers').textContent = markersVisible ? 'Hide Markers' : 'Show Markers';
  drawOverlay();
};

$('undo').onclick = () => {
  if (!undoStack.length) return;
  redoStack.push(marks.map(m => ({ ...m })));
  marks = undoStack.pop();
  drawOverlay();
};

$('redo').onclick = () => {
  if (!redoStack.length) return;
  undoStack.push(marks.map(m => ({ ...m })));
  marks = redoStack.pop();
  drawOverlay();
};

$('zoom').oninput = () => {
  const { w, h } = origin();
  zoomAt(+$('zoom').value, w / 2, h / 2);
};

$('fit').onclick = fitPolygon;

/* ---------- Session ---------- */
async function loadSession() {
  try {
    const r = await fetch(BASE + '/tree-counter/session?farm_code=' + encodeURIComponent(farmCode));
    if (!r.ok) throw Error('Python app is not responding or farm review session is missing');
    session = await r.json();

    const countRows = session.submitted_counts || [];
    const empty = countRows.length === 0;
    $('submittedCount').textContent = fmt(session.submitted_total);
    $('submittedDetails').textContent = empty
      ? 'No tree count values were extracted'
      : `Checklist values: ${countRows.map(fmt).join(' + ')}`;
    $('submittedCount').classList.toggle('warn', empty);
    $('submittedDetails').classList.toggle('warn', empty);

    polygon = (session.farm_polygon || []).filter(p => p && p.length >= 2);
    if (polygon.length >= 3) {
      const lat = polygon.reduce((s, p) => s + (+p[0]), 0) / polygon.length;
      const lng = polygon.reduce((s, p) => s + (+p[1]), 0) / polygon.length;
      center = { lat, lng };
    } else {
      $('status').textContent = 'No farm boundary received. Showing the map without the boundary.';
    }

    ready = true;
    layoutMap();
    if (polygon.length >= 3) fitPolygon(); else render();

    $('finish').disabled = false;
    $('manualCount').value = '0';
  } catch (e) {
    $('status').textContent = e.message;
    $('finish').disabled = true;
  }
}

/* ---------- Cancel (skip the tree check, show the checklist problems in Python) ---------- */
$('cancel').onclick = async () => {
  if (!farmCode) { $('status').textContent = 'Farm code missing'; return; }
  
  $('cancel').disabled = true;
  $('finish').disabled = true;
  $('cancel').textContent = 'Cancelling…';

  try {
    const r = await fetch(BASE + '/tree-counter/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ farm_code: farmCode })
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.message || 'Cancel failed');
    $('status').textContent = 'Tree count cancelled. Checklist problems are shown in the tool.';
    $('cancel').textContent = 'Cancelled';
  } catch (e) {
    $('status').textContent = e.message;
    $('cancel').disabled = false;
    $('finish').disabled = false;
    $('cancel').textContent = 'Cancel';
  }
};

/* ---------- Finish ---------- */
$('finish').onclick = async () => {
  if (!farmCode) { $('status').textContent = 'Farm code missing'; return; }
  $('finish').disabled = true;
  $('finish').textContent = 'Sending…';

  let value = parseInt($('manualCount').value, 10);
  if (!Number.isFinite(value) || value < 0) value = marks.length;

  try {
    const r = await fetch(BASE + '/tree-counter/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ farm_code: farmCode, manual_count: value, image_loaded: ready })
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.message || 'Send failed');
    $('status').textContent = d.mismatch ? 'Count submitted — mismatch found.' : 'Count submitted — accepted.';
    $('finish').textContent = 'Finished';
  } catch (e) {
    $('status').textContent = e.message;
    $('finish').disabled = false;
    $('finish').textContent = 'Retry';
  }
};

/* ---------- Start ---------- */
const zs = $('zoom');
zs.min = MIN_Z;
zs.max = MAX_Z;
zs.step = 1;
zs.value = String(z);
window.addEventListener('resize', layoutMap);
loadSession();