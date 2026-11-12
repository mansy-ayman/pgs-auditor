// content.js - reads submission table, sends to Python, and manages floating satellite map overlay
const PYTHON_URL = 'http://127.0.0.1:5000/kobo-data';
let lastKey = '';
let popupKey = '';
let retryAt = 0;
let missed = 0;

// ============================================================================
// DOM TABLE EXTRACTION
// ============================================================================
function extractTable() {
  const table = [];
  const rows = document.querySelectorAll(
    '.submission-data-table__row--columns, .submission-data-table__row, tr'
  );
  let index = 1;

  rows.forEach((row) => {
    if (row.tagName === 'TABLE' || row.querySelector('.submission-data-table__row--columns')) return;

    let label = '', value = '', link = '';
    const labelEl = row.querySelector('.submission-data-table__column--label');
    const dataEl = row.querySelector(
      '.submission-data-table__column--data, .submission-data-table__column--value'
    );

    if (labelEl || dataEl) {
      label = labelEl ? labelEl.innerText : '';
      value = dataEl ? dataEl.innerText : '';
      const a = dataEl ? dataEl.querySelector('a[href]') : null;
      if (a) link = a.href;
    } else {
      const cells = row.querySelectorAll('th, td, .submission-data-table__column');
      if (cells.length === 1) {
        label = cells[0].innerText;
      } else if (cells.length >= 2) {
        label = cells[0].innerText;
        value = cells[1].innerText;
        const a = cells[1].querySelector('a[href]');
        if (a) link = a.href;
      }
    }

    label = label.replace(/\s+/g, ' ').trim();
    value = value.replace(/\s+/g, ' ').trim();
    if (label.toLowerCase() === 'question' && value.toLowerCase() === 'response') return;

    if (label || value) table.push({ index: index++, label, value, link });
  });

  return table;
}

function cellValue(labelEl) {
  const row = labelEl.closest('.submission-data-table__row--columns, .submission-data-table__row, tr');
  const d = row && row.querySelector(
    '.submission-data-table__column--data, .submission-data-table__column--value, td:nth-child(2)'
  );
  return d ? d.textContent.replace(/\s+/g, ' ').trim() : '';
}

function readFarmKey() {
  let labels = document.querySelectorAll('.submission-data-table__column--label, .submission-data-table__label');
  if (labels.length === 0) labels = document.querySelectorAll('tr > th:first-child, tr > td:first-child');
  let code = '', id = '';
  for (const el of labels) {
    const t = el.textContent.trim();
    if (!code && (t.includes('كود المزرعة') || t.includes('كود'))) code = cellValue(el);
    else if (!id && t === '_id') id = cellValue(el);
    if (code && id) break;
  }
  return code ? `${code}|${id}` : '';
}

// ============================================================================
// SPATIAL GEOMETRY & PARSING ([lat, lng] ARRAYS)
// ============================================================================
const ARABIC_DIGITS = { '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9' };

function parsePoints(text) {
  if (!text) return [];
  const pts = [];
  const seen = new Set();

  const re = /(-?\d+\.\d{4,})\s+(-?\d+\.\d{4,})(?:\s+-?\d+(?:\.\d{1,3})?(?![\d.])){0,2}/g;
  let m;

  while ((m = re.exec(text)) !== null) {
    const lat = parseFloat(m[1]), lng = parseFloat(m[2]);
    if (lat >= 20 && lat <= 33 && lng >= 24 && lng <= 38) {
      const key = `${lat.toFixed(6)},${lng.toFixed(6)}`;
      if (!seen.has(key)) {
        seen.add(key);
        pts.push([lat, lng]);
      }
    }
  }
  return pts;
}

function filterOutliers(points, refPolygon = []) {
  if (!points || points.length === 0) return [];

  let centerLat = 0, centerLng = 0;

  if (refPolygon && refPolygon.length >= 3) {
    refPolygon.forEach(p => { centerLat += p[0]; centerLng += p[1]; });
    centerLat /= refPolygon.length;
    centerLng /= refPolygon.length;
  } else {
    const sortedLats = points.map(p => p[0]).sort((a, b) => a - b);
    const sortedLngs = points.map(p => p[1]).sort((a, b) => a - b);
    centerLat = sortedLats[Math.floor(sortedLats.length / 2)];
    centerLng = sortedLngs[Math.floor(sortedLngs.length / 2)];
  }

  const MAX_DEVIATION = 0.05;

  return points.filter(p => {
    return Math.abs(p[0] - centerLat) <= MAX_DEVIATION && 
           Math.abs(p[1] - centerLng) <= MAX_DEVIATION;
  });
}

function calculateFaddanArea(coords) {
  if (!coords || coords.length < 3) return null;
  
  let avgLat = 0, avgLng = 0;
  const n = coords.length;
  for (let i = 0; i < n; i++) {
    avgLat += coords[i][0];
    avgLng += coords[i][1];
  }
  avgLat /= n; avgLng /= n;

  const latRad = (avgLat * Math.PI) / 180;
  const metersPerLat = 111132.92;
  const metersPerLng = 111412.84 * Math.cos(latRad);

  const enuPoints = coords.map(p => ({
    x: (p[1] - avgLng) * metersPerLng,
    y: (p[0] - avgLat) * metersPerLat
  }));

  let areaM2 = 0;
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    areaM2 += enuPoints[i].x * enuPoints[j].y;
    areaM2 -= enuPoints[j].x * enuPoints[i].y;
  }
  areaM2 = Math.abs(areaM2) / 2.0;

  return parseFloat((areaM2 / 4200.83).toFixed(2));
}

// ===== DOCUMENT LIST =====
const DOC_RULES = [
  ['الحيازة', 'ارفق صورة الحيازة', 'الحيازة', false],
  ['التنوع الحيوي', 'الرجاء ارفاق بيان التنوع الحيوي', 'التنوع الحيوي', false],
  ['GMO', 'الرجاء ارفاق بيان GMO', 'GMO', false],
  ['الدفتر', 'ارفاق صورة الدفتر', 'الدفتر', false],
  ['ملف الاسطبل', 'ارفق ملف الاسطبل', 'ملف الاسطبل', false],
  ['اذن الصرف', 'ارفق اذن الصرف موضحا فيه الكمية', 'اذن الصرف', false],
  ['العماله النسائية', 'اذكر قائمة باسماء العماله النسائية الدائمة والموسمية', 'قائمة العمالة النسائية', true],
  ['العمالة الدائمه', 'الرجاء ارفاق قائمه باسماء العمالة الدائمه والموسميه', 'العمالة الدائمة', false],
  ['تشغيل النساء', 'الرجاء ارفاق سياسة تشغيل النساء', 'سياسة تشغيل النساء', false],
  ['محضر الاجتماعات', 'محضر الاجتماعات', 'محضر الاجتماعات', false],
  ['مقابلة مع مالك المزرعة', 'مقابلة مع مالك المزرعة', 'مقابلة مع مالك المزرعة', false]
];

function normAr(s) {
  return String(s || '')
    .replace(/[٠-٩]/g, (d) => '٠١٢٣٤٥٦٧٨٩'.indexOf(d))
    .replace(/[۰-۹]/g, (d) => '۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
    .replace(/[\u0640\u064B-\u065F\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/g, '')
    .replace(/\u00a0/g, ' ')
    .replace(/[أإآ]/g, 'ا').replace(/ى/g, 'ي').replace(/ة/g, 'ه')
    .replace(/\s+/g, ' ').trim();
}

// Reads the animals answer, and how many animals there are
// (the first "العدد" row that comes after the animals question)
function getAnimalInfo(table) {
  const i = table.findIndex((r) => normAr(r.label).includes(normAr('هل توجد حيوانات فى المزرعه')));
  if (i === -1) return { has: true, count: null };            // question not on the form: behave like before
  if (!normAr(table[i].value).includes(normAr('نعم'))) return { has: false, count: null };  // no animals

  let count = null;
  for (let j = i + 1; j < Math.min(i + 8, table.length); j++) {
    if (normAr(table[j].label) === normAr('العدد')) {
      const n = parseFloat(normAr(table[j].value).replace(',', '.'));
      if (!isNaN(n)) count = n;                               // "6.0" becomes 6
      break;
    }
  }
  return { has: true, count };
}

function buildDocList(table) {
  const docs = [];
  const animals = getAnimalInfo(table);
  const nl = table.map((r) => normAr(r.label));
  for (const [shortName, search, title, pink] of DOC_RULES) {
    // no animals on the farm -> skip the stable file question
    if (shortName === 'ملف الاسطبل' && !animals.has) continue;

    const needle = normAr(search);
    for (let k = 0; k < table.length; k++) {
      const row = table[k];
      const lbl = nl[k];
      const link = (row.link || '').trim();
      const val = (row.value || '').trim();
      const url = link.startsWith('http') ? link : val;
      if (lbl.includes(needle) && url.startsWith('http')) {
        let finalTitle = title;
        if (shortName === 'ملف الاسطبل' && animals.count !== null) {
          finalTitle = `${title} - عدد الحيوانات: ${animals.count}`;   // "Stable file - Number of animals: 6"
        }
        if (!docs.some((d) => d.url === url)) docs.push({ name: shortName, title: finalTitle, pink, url });
        break;
      }
    }
  }
  return docs;
}

function buildExtraInfo(table) {
  let farmPolygon = [];
  let soilSamples = [];
  let areaFaddan = null;
  let ctx = '';
  const soilSeen = new Set();

  // The rows that REALLY are the soil-sample questions on the checklist
  const SOIL_LABEL = normAr('نقاط اخذ عينة للتربة');
  const exactSoilRows = table.filter((r) => normAr(r.label).includes(SOIL_LABEL));
  const useExactSoil = exactSoilRows.length > 0;

  for (const row of table) {
    const label = row.label || '';
    const value = row.value || '';

    const isPoly = label.includes('ارسم') || label.includes('حدود');
    const isSoil = label.includes('عين') || label.includes('تربة');
    if (isPoly) ctx = 'polygon';
    else if (isSoil) ctx = 'soil';

    let pts = parsePoints(value);
    if (pts.length === 0 && !value) pts = parsePoints(label);

    if (pts.length > 0) {
      if (ctx === 'polygon') {
        farmPolygon.push(...pts);
        pts.forEach((p) => console.log('[KoBo] polygon point', p, '<- row', row.index, '|', label));
      } else if (ctx === 'soil' && !useExactSoil) {
        // old way, only used when the form has no "نقاط اخذ عينة للتربة" rows at all
        for (const p of pts) {
          const k = `${p[0].toFixed(6)},${p[1].toFixed(6)}`;
          if (!soilSeen.has(k)) {
            soilSeen.add(k); soilSamples.push(p);
            console.log('[KoBo] soil point (fallback)', p, '<- row', row.index, '|', label);
          }
        }
      }
    }

    if (areaFaddan === null && (label.includes('المساحة بالفدان') || label.includes('إجمالي المساحة'))) {
      const v = parseFloat(value.replace(/[٠-٩]/g, (d) => ARABIC_DIGITS[d]).replace(',', '.'));
      if (!isNaN(v)) areaFaddan = v;
    }
  }

  // Soil samples: ONLY from the "نقاط اخذ عينة للتربة" rows, in the same order as the table
  if (useExactSoil) {
    for (const row of exactSoilRows) {
      for (const p of parsePoints(row.value || '')) {
        const k = `${p[0].toFixed(6)},${p[1].toFixed(6)}`;
        if (!soilSeen.has(k)) {
          soilSeen.add(k); soilSamples.push(p);
          console.log('[KoBo] soil point', p, '<- row', row.index, '|', row.label);
        }
      }
    }
  }

  if (farmPolygon.length > 3) {
    const f = farmPolygon[0], l = farmPolygon[farmPolygon.length - 1];
    if (f[0] === l[0] && f[1] === l[1]) farmPolygon.pop();
  }

  if (farmPolygon.length >= 3) {
    soilSamples = filterOutliers(soilSamples, farmPolygon);
  } else {
    farmPolygon = filterOutliers(farmPolygon);
    soilSamples = filterOutliers(soilSamples);
  }

  return {
    farm_polygon: farmPolygon,
    soil_samples: soilSamples,
    area_faddan: areaFaddan,
    calculated_area_faddan: calculateFaddanArea(farmPolygon),
    docs: buildDocList(table)
  };
}

// ============================================================================
// SHADOW DOM OVERLAY INJECTION
// ============================================================================
const OVERLAY_ID = 'kobo-farm-map';
let overlayBuilding = false;

function clampBox(b) {
  const vw = window.innerWidth, vh = window.innerHeight;
  const width = Math.max(300, Math.min(b.width, vw));
  const height = Math.max(240, Math.min(b.height, vh));
  const left = Math.max(0, Math.min(b.left, vw - 80));
  const top = Math.max(0, Math.min(b.top, vh - 40));
  return { left, top, width, height };
}

function showFarmOverlay(info) {
  try {
    chrome.storage.local.set({
      farm_view: {
        areaFaddan: info.area_faddan,
        calculatedAreaFaddan: info.calculated_area_faddan,
        farmPolygon: info.farm_polygon,
        soilSamples: info.soil_samples,
        docs: info.docs || [],
        stamp: Date.now()
      }
    }, ensureOverlay);
  } catch (e) {}
}

function ensureOverlay() {
  const existing = document.getElementById(OVERLAY_ID);
  if (existing) { existing.style.display = 'block'; return; }
  if (overlayBuilding) return;
  overlayBuilding = true;
  chrome.storage.local.get('overlay_box', (r) => {
    overlayBuilding = false;
    const def = { width: 520, height: 460, left: Math.max(0, window.innerWidth - 540), top: 80 };
    buildOverlay(clampBox({ ...def, ...((r && r.overlay_box) || {}) }));
  });
}

function buildOverlay(box) {
  const host = document.createElement('div');
  host.id = OVERLAY_ID;
  host.style.cssText = `position:fixed;z-index:2147483647;display:block;left:${box.left}px;top:${box.top}px;width:${box.width}px;height:${box.height}px;`;

  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
    <style>
      .box { position:absolute; inset:0; display:flex; flex-direction:column; background:#18181b;
             border:1px solid #3f3f46; border-radius:10px; overflow:hidden;
             box-shadow:0 10px 30px rgba(0,0,0,.6); font-family:'Segoe UI',Tahoma,Arial,sans-serif; }
      .hdr { display:flex; justify-content:space-between; align-items:center; padding:6px 10px;
             background:#27272a; color:#fbbf24; font-size:13px; font-weight:bold; cursor:move;
             user-select:none; direction:rtl; touch-action:none; }
      .x { background:none; border:none; color:#a1a1aa; font-size:18px; cursor:pointer; line-height:1; }
      iframe { flex:1; width:100%; border:0; background:#18181b; }
      .rz { position:absolute; right:0; bottom:0; width:18px; height:18px; cursor:nwse-resize; touch-action:none;
            background:linear-gradient(135deg,transparent 55%,#a1a1aa 55%,#a1a1aa 65%,transparent 65%,
                       transparent 77%,#a1a1aa 77%,#a1a1aa 87%,transparent 87%); }
    </style>
    <div class="box">
      <div class="hdr"><span>🛰️ خريطة المزرعة</span><button class="x" title="إغلاق">&times;</button></div>
      <iframe></iframe>
      <div class="rz"></div>
    </div>`;
  root.querySelector('iframe').src = chrome.runtime.getURL('popup.html');

  const hdr = root.querySelector('.hdr'), rz = root.querySelector('.rz');
  const save = () => {
    const r = host.getBoundingClientRect();
    try { chrome.storage.local.set({ overlay_box: { left: r.left, top: r.top, width: r.width, height: r.height } }); } catch (e) {}
  };

  root.querySelector('.x').addEventListener('click', () => { host.style.display = 'none'; });

  let drag = null;
  hdr.addEventListener('pointerdown', (e) => {
    if (e.target.closest && e.target.closest('.x')) return;
    const r = host.getBoundingClientRect();
    drag = { x: e.clientX, y: e.clientY, l: r.left, t: r.top };
    hdr.setPointerCapture(e.pointerId);
  });
  hdr.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const r = host.getBoundingClientRect();
    const b = clampBox({ left: drag.l + e.clientX - drag.x, top: drag.t + e.clientY - drag.y, width: r.width, height: r.height });
    host.style.left = b.left + 'px';
    host.style.top = b.top + 'px';
  });
  hdr.addEventListener('pointerup', () => { if (drag) { drag = null; save(); } });

  let rs = null;
  rz.addEventListener('pointerdown', (e) => {
    const r = host.getBoundingClientRect();
    rs = { x: e.clientX, y: e.clientY, w: r.width, h: r.height };
    rz.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  rz.addEventListener('pointermove', (e) => {
    if (!rs) return;
    const r = host.getBoundingClientRect();
    const w = Math.max(300, Math.min(rs.w + e.clientX - rs.x, window.innerWidth - r.left));
    const h = Math.max(240, Math.min(rs.h + e.clientY - rs.y, window.innerHeight - r.top));
    host.style.width = w + 'px';
    host.style.height = h + 'px';
  });
  rz.addEventListener('pointerup', () => { if (rs) { rs = null; save(); } });

  window.addEventListener('resize', () => {
    const r = host.getBoundingClientRect();
    const b = clampBox({ left: r.left, top: r.top, width: r.width, height: r.height });
    host.style.left = b.left + 'px'; host.style.top = b.top + 'px';
    host.style.width = b.width + 'px'; host.style.height = b.height + 'px';
  });

  document.body.appendChild(host);
}

// ============================================================================
// CONTROLLER LOOP
// ============================================================================
function markSent(key, ok) {
  if (ok) lastKey = key;
  else retryAt = Date.now() + 5000;
}

function sendTable() {
  // Extension was reloaded while this tab stayed open -> this old script is dead. Refresh the page (F5).
  if (!chrome.runtime || !chrome.runtime.id) { console.warn('KoBo extension reloaded - press F5 on this page'); return; }
  const key = readFarmKey();
  if (!key) {
    if (++missed >= 2) { lastKey = ''; popupKey = ''; }
    return;
  }
  missed = 0;
  if (key === lastKey || Date.now() < retryAt) return;

  const table = extractTable();
  if (table.length === 0) return;

  const extracted_info = buildExtraInfo(table);
  const payload = { table, extracted_info };

  if (popupKey !== key) {
    popupKey = key;
    showFarmOverlay(extracted_info);
  }

  chrome.runtime.sendMessage({ type: 'SEND_TO_PYTHON', payload }, (response) => {
    if (!chrome.runtime.lastError && response && response.success) {
      markSent(key, true);
      return;
    }
    fetch(PYTHON_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then((r) => markSent(key, r.ok))
      .catch(() => markSent(key, false));
  });
}

let debounceT = null;
function scheduleSend() {
  if (document.hidden) return;              // do nothing in background tabs
  clearTimeout(debounceT);
  debounceT = setTimeout(sendTable, 800);   // wait for the page to settle
}
new MutationObserver(scheduleSend).observe(document.body, { childList: true, subtree: true });
document.addEventListener('visibilitychange', scheduleSend);
setInterval(scheduleSend, 5000);            // slow safety net
scheduleSend();