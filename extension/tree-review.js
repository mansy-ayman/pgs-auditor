'use strict';

/* ========== Setup ========== */
const farmCode = new URLSearchParams(location.search).get('farm_code') || '';
const $ = id => document.getElementById(id);
const BASE = 'http://127.0.0.1:5000';

let session = null;
let marks = [];
let undoStack = [];
let redoStack = [];
let ready = false;
let markersVisible = true;

let zoom = 1;
let mapZoom = 18;
let minX = 0;
let minY = 0;
let mapW = 0;
let mapH = 0;
let tileCanvas = null;
let tileCtx = null;
let polygon = [];

$('farmCode').textContent = farmCode || 'Farm code unavailable';

const fmt = n => Number(n || 0).toLocaleString('en-US');

/* ========== Undo / redo ========== */
function snap() {
  undoStack.push(marks.map(m => ({ ...m })));
  if (undoStack.length > 1500) undoStack.shift();
  redoStack = [];
}

/* ========== Markers ========== */
function redraw() {
  const svg = $('markers');
  svg.replaceChildren();

  marks.forEach((m, i) => {
    const g = document.createElementNS(svg.namespaceURI, 'g');
    g.dataset.index = i;

    // Hollow ring and center: imagery remains fully visible inside and between markers.
    const ring = document.createElementNS(svg.namespaceURI, 'circle');
    ring.setAttribute('cx', m.x);
    ring.setAttribute('cy', m.y);
    ring.setAttribute('r', Math.max(10, 10 / zoom));
    ring.setAttribute('class', 'marker-ring');

    const dot = document.createElementNS(svg.namespaceURI, 'circle');
    dot.setAttribute('cx', m.x);
    dot.setAttribute('cy', m.y);
    dot.setAttribute('r', Math.max(3.5, 4 / zoom));
    dot.setAttribute('class', 'marker-dot');

    g.append(ring, dot);
    svg.append(g);
  });

  $('liveCount').textContent = fmt(marks.length);
  $('undo').disabled = !undoStack.length;
  $('redo').disabled = !redoStack.length;
  $('manualCount').value = String(marks.length);
}

function add(x, y) {
  snap();
  marks.push({ x, y });
  redraw();
}

function removeNearest(p) {
  let ix = -1;
  let d = 1e9;

  marks.forEach((m, i) => {
    const z = Math.hypot(m.x - p.x, m.y - p.y);
    if (z < d) {
      d = z;
      ix = i;
    }
  });

  if (ix >= 0 && d < 25 / zoom) {
    snap();
    marks.splice(ix, 1);
    redraw();
    return true;
  }
  return false;
}

function coord(e) {
  const r = $('mapStage').getBoundingClientRect();
  return {
    x: (e.clientX - r.left) * mapW / r.width,
    y: (e.clientY - r.top) * mapH / r.height
  };
}

/* ========== Map math ========== */
function latLonToTile(lat, lon, z) {
  const n = 2 ** z;
  const latR = Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180;
  return {
    x: (lon + 180) / 360 * n,
    y: (1 - Math.asinh(Math.tan(latR)) / Math.PI) / 2 * n
  };
}

function project(lat, lon) {
  const t = latLonToTile(lat, lon, mapZoom);
  return {
    x: (t.x - minX) * 256,
    y: (t.y - minY) * 256
  };
}

function drawPolygon() {
  const svg = $('markers');
  const ns = svg.namespaceURI;
  const path = document.createElementNS(ns, 'polygon');

  const pts = polygon
    .map(p => {
      const q = project(Number(p[0]), Number(p[1]));
      return `${q.x},${q.y}`;
    })
    .join(' ');

  path.setAttribute('points', pts);
  path.setAttribute('class', 'farm-boundary');
  svg.append(path);
}

/* ========== Esri imagery ========== */
async function loadEsri() {
  const poly = session.farm_polygon || [];
  if (poly.length < 3) {
    throw Error('The submission did not include a usable farm polygon.');
  }
  polygon = poly;

  const minLat = Math.min(...poly.map(p => +p[0]));
  const maxLat = Math.max(...poly.map(p => +p[0]));
  const minLon = Math.min(...poly.map(p => +p[1]));
  const maxLon = Math.max(...poly.map(p => +p[1]));

  // Select a zoom that covers the polygon in a manageable tile grid,
  // then show the sharpest suitable Esri level.
  for (mapZoom = 19; mapZoom >= 14; mapZoom--) {
    const a = latLonToTile(maxLat, minLon, mapZoom);
    const b = latLonToTile(minLat, maxLon, mapZoom);
    const tilesX = Math.ceil(b.x) - Math.floor(a.x) + 1;
    const tilesY = Math.ceil(b.y) - Math.floor(a.y) + 1;
    if (tilesX <= 18 && tilesY <= 18) break;
  }

  const nw = latLonToTile(maxLat, minLon, mapZoom);
  const se = latLonToTile(minLat, maxLon, mapZoom);
  minX = Math.floor(nw.x) - 1;
  minY = Math.floor(nw.y) - 1;
  const maxX = Math.ceil(se.x) + 1;
  const maxY = Math.ceil(se.y) + 1;
  mapW = (maxX - minX + 1) * 256;
  mapH = (maxY - minY + 1) * 256;

  $('mapCanvas').width = mapW;
  $('mapCanvas').height = mapH;
  tileCanvas = $('mapCanvas');
  tileCtx = tileCanvas.getContext('2d');
  tileCtx.fillStyle = '#202830';
  tileCtx.fillRect(0, 0, mapW, mapH);

  $('markers').setAttribute('viewBox', `0 0 ${mapW} ${mapH}`);
  $('markers').setAttribute('preserveAspectRatio', 'none');

  const total = (maxX - minX + 1) * (maxY - minY + 1);
  let done = 0;

  const tasks = [];
  for (let y = minY; y <= maxY; y++) {
    for (let x = minX; x <= maxX; x++) {
      tasks.push({ x, y });
    }
  }

  // Fetch in small batches to avoid overwhelming ArcGIS and browser.
  for (let i = 0; i < tasks.length; i += 6) {
    await Promise.all(tasks.slice(i, i + 6).map(async t => {
      const img = new Image();
      const url = `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${mapZoom}/${t.y}/${t.x}`;

      await new Promise((resolve, reject) => {
        img.onload = () => {
          tileCtx.drawImage(img, (t.x - minX) * 256, (t.y - minY) * 256, 256, 256);
          resolve();
        };
        img.onerror = () => reject(Error('Esri tile failed to load'));
        img.src = url;
      });

      done++;
      $('imageState').textContent = `Loading Esri World Imagery ${done}/${total} tiles · level ${mapZoom}`;
    }));
  }

  ready = true;
  $('imagePlaceholder').hidden = true;
  $('imageState').textContent = `Esri World Imagery ready · level ${mapZoom} · farm boundary shown`;

  $('mapStage').style.width = (mapW * zoom) + 'px';
  $('mapStage').style.height = (mapH * zoom) + 'px';
  $('mapCanvas').style.width = (mapW * zoom) + 'px';
  $('mapCanvas').style.height = (mapH * zoom) + 'px';
  $('markers').style.width = (mapW * zoom) + 'px';
  $('markers').style.height = (mapH * zoom) + 'px';
  $('markers').style.display = markersVisible ? 'block' : 'none';

  drawPolygon();
  redraw();
  fit();
}

/* ========== Zoom ========== */
function fit() {
  if (!ready) return;
  const avail = Math.max(180, innerHeight - $('topbar').offsetHeight - 90);
  zoom = Math.min(1.35, avail / mapH);
  $('zoom').value = String(zoom);
  applyZoom();
}

function applyZoom() {
  if (!ready) return;
  const w = mapW * zoom;
  const h = mapH * zoom;

  $('mapStage').style.width = w + 'px';
  $('mapStage').style.height = h + 'px';
  $('mapCanvas').style.width = w + 'px';
  $('mapCanvas').style.height = h + 'px';
  $('markers').style.width = w + 'px';
  $('markers').style.height = h + 'px';
  $('zoomValue').textContent = Math.round(zoom * 100) + '%';

  redraw();
}

/* ========== Events ========== */
$('markers').addEventListener('click', e => {
  if (!ready) return;
  const p = coord(e);
  add(p.x, p.y);
});

$('markers').addEventListener('contextmenu', e => {
  e.preventDefault();
  if (ready) removeNearest(coord(e));
});

$('toggleMarkers').onclick = () => {
  markersVisible = !markersVisible;
  $('markers').style.display = markersVisible ? 'block' : 'none';
  $('toggleMarkers').textContent = markersVisible ? 'Hide Markers' : 'Show Markers';
};

$('undo').onclick = () => {
  if (undoStack.length) {
    redoStack.push(marks.map(m => ({ ...m })));
    marks = undoStack.pop();
    redraw();
  }
};

$('redo').onclick = () => {
  if (redoStack.length) {
    undoStack.push(marks.map(m => ({ ...m })));
    marks = redoStack.pop();
    redraw();
  }
};

$('zoom').oninput = () => {
  zoom = Number($('zoom').value);
  applyZoom();
};

$('fit').onclick = fit;

/* ========== Session ========== */
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

    // Red warning when no tree count values were extracted
    $('submittedCount').classList.toggle('warn', empty);
    $('submittedDetails').classList.toggle('warn', empty);

    $('finish').disabled = false;
    $('manualCount').value = '0';

    await loadEsri();
  } catch (e) {
    $('status').textContent = e.message;
    $('imagePlaceholder').hidden = false;
    $('imagePlaceholder').textContent =
      'Esri imagery/polygon could not load. Check internet and the farm boundary; do not finish until imagery is visible.';
    $('finish').disabled = true;
  }
}

/* ========== Finish ========== */
$('finish').onclick = async () => {
  if (!ready || !farmCode) return;

  $('finish').disabled = true;
  $('finish').textContent = 'Sending…';

  const count = marks.length;
  $('manualCount').value = String(count);

  try {
    const r = await fetch(BASE + '/tree-counter/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        farm_code: farmCode,
        manual_count: count,
        image_loaded: true
      })
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.message || 'Send failed');

    $('status').textContent = d.mismatch
      ? 'Count submitted — mismatch found.'
      : 'Count submitted — accepted.';
    $('finish').textContent = 'Finished';
  } catch (e) {
    $('status').textContent = e.message;
    $('finish').disabled = false;
    $('finish').textContent = 'Retry';
  }
};

loadSession();