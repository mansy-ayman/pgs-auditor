// background.js - MV3 service worker: talks to Python, opens document tabs one by one
const BASE = 'http://127.0.0.1:5000';
const MAX_LOGS = 100;

// ---------------- logging (shown in the popup) ----------------
let logBuf = [], logTimer = null;
function addLog(msg) {
  logBuf.push(`[${new Date().toLocaleTimeString('en-US', { hour12: false })}] ${msg}`);
  if (logTimer) return;
  logTimer = setTimeout(() => {
    logTimer = null;
    const batch = logBuf; logBuf = [];
    chrome.storage.local.get(['gui_logs'], (res) => {
      chrome.storage.local.set({ gui_logs: [...(res.gui_logs || []), ...batch].slice(-MAX_LOGS) });
    });
  }, 1000);
}

// ---------------- document queue ----------------
const FALLBACK_MS = 6000;      // max wait per document (starts when the tab is created, NOT after load)
const AUTO_OPEN_PDF = true;    // open downloaded PDFs in the viewer tab with the header

let runId = 0;                     // lets a new batch cancel an old one
let session = null;                // the batch currently being opened
const headerQueue = new Map();     // tabId -> { doc, i, total }  (tabs waiting for their header)
const pendingDownloads = new Map();// downloadId -> { doc, i, total }  (PDF downloads started by our tabs)
const docByUrl = new Map();        // url -> { doc, i, total }

const normUrl = (u) => String(u || '').split('#')[0];
const isBlankUrl = (u) => !u || u === 'about:blank' || u.startsWith('chrome://newtab');

// Chrome puts this background script to sleep after ~30 seconds of quiet, and it forgets everything.
// This keeps it awake while documents are opening or PDFs are downloading.
let keepAliveTimer = null;
function keepAlive() {
  if (keepAliveTimer) return;
  keepAliveTimer = setInterval(() => {
    chrome.runtime.getPlatformInfo(() => {});
    if (!session && pendingDownloads.size === 0) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
  }, 20000);
}

// Runs INSIDE the opened page: puts the file name + rotate/zoom controls at the top
function injectViewer(text, pink) {
  document.title = text;
  if (document.getElementById('kobo-name-banner')) return;

  const BAR_H = 44;
  // a picture opened directly in Chrome = a page with just one <img> in it
  const isImage = document.images.length === 1 && !!document.body && document.body.children.length <= 1;
  if (!isImage) return;   // video / anything else: no header at all

  // ---------- header bar (everything in the middle) ----------
  const bar = document.createElement('div');
  bar.id = 'kobo-name-banner';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;height:' + BAR_H + 'px;z-index:2147483647;' +
    'background:#1e1e1e;border-bottom:2px solid #2980b9;box-sizing:border-box;padding:0 12px;' +
    'display:flex;align-items:center;justify-content:center;gap:18px;' +
    'font:bold 16px Segoe UI,Tahoma,Arial,sans-serif;color:' + (pink ? '#ff69b4' : '#ffffff') + ';';
  const title = document.createElement('span');
  title.textContent = text;
  title.style.cssText = 'direction:rtl;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
  bar.appendChild(title);

  // ---------- controls ----------
  const mkBtn = (label, tip) => {
    const b = document.createElement('button');
    b.textContent = label; b.title = tip;
    b.style.cssText = 'background:#34495e;color:#fff;border:1px solid #555;padding:4px 8px;border-radius:4px;' +
      'cursor:pointer;font:bold 12px Segoe UI,Arial,sans-serif;';
    return b;
  };
  const ctr = document.createElement('div');
  ctr.style.cssText = 'display:flex;align-items:center;gap:8px;direction:ltr;';
  const slider = document.createElement('input');
  slider.type = 'range'; slider.min = '0.5'; slider.max = '4'; slider.step = '0.1'; slider.value = '1';
  slider.style.cssText = 'width:110px;cursor:pointer;accent-color:#2ecc71;';
  const zval = document.createElement('span');
  zval.textContent = '100%';
  zval.style.cssText = 'color:#2ecc71;font-size:12px;min-width:38px;';
  const bL = mkBtn('↺ 90°', 'Rotate left');
  const bR = mkBtn('↻ 90°', 'Rotate right');
  const bX = mkBtn('Reset', 'Reset');
  bX.style.background = '#c0392b';
  ctr.append('🔍', slider, zval, bL, bR, bX);
  bar.appendChild(ctr);

  // ---------- scrollable area UNDER the header (so nothing hides behind it) ----------
  const orig = document.images[0];
  let nw = orig.naturalWidth || 1000, nh = orig.naturalHeight || 1000;
  const view = document.createElement('div');
  view.id = 'kobo-view';
  view.style.cssText = 'position:fixed;top:' + BAR_H + 'px;left:0;right:0;bottom:0;overflow:auto;' +
    'background:#1e1e1e;z-index:2147483646;';
  const stage = document.createElement('div');
  stage.style.cssText = 'position:relative;';
  const img = document.createElement('img');
  img.src = orig.currentSrc || orig.src;
  img.draggable = false;
  img.style.cssText = 'position:absolute;left:50%;top:50%;max-width:none;max-height:none;' +
    'transform-origin:center center;box-shadow:0 5px 15px rgba(0,0,0,.5);';
  stage.appendChild(img);
  view.appendChild(stage);

  let rot = 0, zoom = 1;
  function layout(center) {
    const cw = view.clientWidth, ch = view.clientHeight;
    const odd = rot % 180 !== 0;
    const bw = odd ? nh : nw, bh = odd ? nw : nh;               // size after rotating
    const fit = Math.min(1, (cw - 24) / bw, (ch - 24) / bh);    // make it fit the screen at 100%
    const k = fit * zoom;
    const sw = Math.max(cw, bw * k), sh = Math.max(ch, bh * k); // the stage is as big as the picture, so scrolling reaches every edge
    stage.style.width = sw + 'px';
    stage.style.height = sh + 'px';
    img.style.width = nw + 'px';
    img.style.height = nh + 'px';
    img.style.transform = 'translate(-50%,-50%) rotate(' + rot + 'deg) scale(' + k + ')';
    if (center) { view.scrollLeft = (sw - cw) / 2; view.scrollTop = (sh - ch) / 2; }
  }
  img.onload = () => { nw = img.naturalWidth || nw; nh = img.naturalHeight || nh; layout(true); };
  slider.addEventListener('input', () => { zoom = parseFloat(slider.value); zval.textContent = Math.round(zoom * 100) + '%'; layout(false); });
  bL.addEventListener('click', () => { rot = (rot + 270) % 360; layout(true); });
  bR.addEventListener('click', () => { rot = (rot + 90) % 360; layout(true); });
  bX.addEventListener('click', () => { rot = 0; zoom = 1; slider.value = '1'; zval.textContent = '100%'; layout(true); });
  window.addEventListener('resize', () => layout(false));

  document.documentElement.style.overflow = 'hidden';
  document.documentElement.appendChild(view);
  document.documentElement.appendChild(bar);
  layout(true);
}

// Adds the header to a tab (only once per tab, only on the REAL page - never on about:blank)
function tryHeader(tabId) {
  const h = headerQueue.get(tabId);
  if (!h) return;
  chrome.tabs.get(tabId, (t) => {
    if (chrome.runtime.lastError || !t) { headerQueue.delete(tabId); return; }
    if (isBlankUrl(t.url) || t.pendingUrl || t.status !== 'complete') return;   // wait for the real page
    headerQueue.delete(tabId);
    const text = `📄 [${h.i + 1}/${h.total}] ${h.doc.title || h.doc.name}`;
    try {
      chrome.scripting.executeScript(
        { target: { tabId }, func: injectViewer, args: [text, !!h.doc.pink] },
        () => { if (chrome.runtime.lastError) addLog('⚠️ header: ' + chrome.runtime.lastError.message); }
      );
    } catch (e) { addLog('⚠️ header: ' + e.message); }
  });
}

// Even if a page finishes loading LATE (after we already moved on), it still gets its header
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === 'complete') {
    tryHeader(tabId);
    if (session && session.tabId === tabId) checkSession(session);
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  headerQueue.delete(tabId);
  if (session && session.tabId === tabId) advance(session);      // tab closed = it was a download
});

// Move to the next doc (only once per doc)
function advance(s) {
  if (!s || s.done || s !== session) return;
  s.done = true;
  clearTimeout(s.timer);
  if (s.tabId != null) tryHeader(s.tabId);
  openDoc(s.docs, s.i + 1, s.run);
}

// Is the doc tab fully loaded / gone?
function checkSession(s) {
  if (s.done || s.tabId == null) return;
  chrome.tabs.get(s.tabId, (t) => {
    if (s.done) return;
    if (chrome.runtime.lastError || !t) { advance(s); return; }                  // tab closed = download
    if (t.status === 'complete' && !isBlankUrl(t.url) && !t.pendingUrl) advance(s); // fully loaded
  });
}

// Opens doc i in a new focused tab. Moves to the next doc as soon as:
//   - the page finished loading, or
//   - the tab closed (PDF / file download), or
//   - FALLBACK_MS passed since the tab was created.
function openDoc(docs, i, myRun) {
  if (myRun !== runId) return;
  if (i >= docs.length) { session = null; addLog('✅ All documents opened'); return; }
  const doc = docs[i];
  const info = { doc, i, total: docs.length };
  docByUrl.set(normUrl(doc.url), info);

  const s = { docs, i, run: myRun, info, tabId: null, done: false, timer: null };
  session = s;
  keepAlive();

  chrome.tabs.create({ url: doc.url, active: true }, (tab) => {
    if (chrome.runtime.lastError || !tab) { addLog('⚠️ could not open: ' + doc.name); advance(s); return; }
    s.tabId = tab.id;
    headerQueue.set(tab.id, info);
    checkSession(s);          // in case it already finished / closed before this callback ran
  });
}

function openAllDocs(docs) {
  if (!docs || !docs.length) return;

  // Python sends back its own plain titles, so put back the full titles
  // (like "ملف الاسطبل - عدد الحيوانات: 6") that the extension saved, matched by link
  chrome.storage.local.get(['farm_view'], (r) => {
    const saved = (r && r.farm_view && r.farm_view.docs) || [];
    const titleByUrl = new Map(saved.map((d) => [normUrl(d.url), d.title]));
    docs = docs.map((d) => {
      const t = titleByUrl.get(normUrl(d.url));
      return t ? { ...d, title: t } : d;
    });

    runId += 1;
    if (session) { clearTimeout(session.timer); session.done = true; }
    docByUrl.clear();
    addLog(`📂 Opening ${docs.length} documents one by one...`);
    openDoc(docs, 0, runId);
  });
}

// ---------------- PDF downloads -> open in viewer with header ----------------
function openPdfViewer(info, filename) {
  const q = new URLSearchParams({
    u: info.doc.url,
    t: info.doc.title || info.doc.name || '',
    pink: info.doc.pink ? '1' : '0',
    n: String(info.i + 1),
    total: String(info.total)
  });
  if (filename) {   // the file already saved on this PC: the viewer reads it from there (fast)
    q.set('f', 'file:///' + encodeURI(filename.replace(/\\/g, '/')).replace(/#/g, '%23').replace(/\?/g, '%3F'));
  }
  chrome.tabs.create({ url: chrome.runtime.getURL('viewer.html') + '?' + q.toString(), active: false });
}

chrome.downloads.onCreated.addListener((item) => {
  let info = docByUrl.get(normUrl(item.url)) || docByUrl.get(normUrl(item.finalUrl));
  // link did not match (redirects): guess it belongs to the document being opened right now
  if (!info && session && !session.done && session.info) info = session.info;
  console.log('[KoBo] download started:', item.url, info ? '-> doc ' + info.doc.name : '-> no doc found');
  if (!info) return;
  // this document turned into a download (PDF) -> it is not a page, go to the next one now
  if (session && session.info === info) advance(session);
  if (AUTO_OPEN_PDF) { pendingDownloads.set(item.id, info); keepAlive(); }
});

chrome.downloads.onChanged.addListener((delta) => {
  if (!pendingDownloads.has(delta.id) || !delta.state) return;
  if (delta.state.current === 'interrupted') { pendingDownloads.delete(delta.id); return; }
  if (delta.state.current !== 'complete') return;
  const info = pendingDownloads.get(delta.id);
  pendingDownloads.delete(delta.id);
  chrome.downloads.search({ id: delta.id }, (items) => {
    const it = items && items[0];
    if (!it) return;
    console.log('[KoBo] download finished:', it.filename, it.mime);
    // open the viewer for PDFs (the viewer checks the real file type itself)
    const maybePdf = /pdf/i.test(it.mime || '') || /\.pdf$/i.test(it.filename || '') ||
                     !it.mime || it.mime === 'application/octet-stream';
    if (maybePdf) openPdfViewer(info, it.filename);
  });
});

// ---------------- poll the Python server ----------------
let pollDelay = 3000, pollTimer = null, polling = false;
function pollQueue() {
  if (polling) return;
  polling = true;
  fetch(`${BASE}/open-queue`)
    .then((res) => { if (!res.ok) throw new Error(`HTTP ${res.status}`); return res.json(); })
    .then((data) => {
      pollDelay = 3000;                                   // server is alive
      const docs = data.docs || data.documents || (Array.isArray(data) ? data : null);
      if (docs && docs.length > 0) {
        addLog(`📥 Received ${docs.length} documents from Python`);
        openAllDocs(docs);
      }
    })
    .catch(() => { pollDelay = Math.min(pollDelay * 2, 30000); })   // Python off: back off to 30 s
    .finally(() => { polling = false; clearTimeout(pollTimer); pollTimer = setTimeout(pollQueue, pollDelay); });
}
chrome.alarms.create('poll_python_queue', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'poll_python_queue') pollQueue(); });
pollQueue();

// ---------------- messages ----------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // content.js -> Python
  if (msg.type === 'SEND_TO_PYTHON') {
    fetch(`${BASE}/kobo-data`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(msg.payload)
    })
      .then((r) => {
        if (r.ok) addLog('✅ Data sent to Python server');
        sendResponse({ success: r.ok });
      })
      .catch((err) => {
        addLog(`❌ Failed sending to Python: ${err.message}`);
        sendResponse({ success: false });
      });
    return true;
  }

  // popup button
  if ((msg.type === 'OPEN_DOCS_SEQUENTIALLY' || msg.type === 'OPEN_DOCS') && msg.docs) {
    openAllDocs(msg.docs);
    sendResponse({ success: true });
    return;
  }

  // viewer tab (PDF downloaded) asks to be closed
  if (msg.type === 'CLOSE_ME' && sender.tab) {
    chrome.tabs.remove(sender.tab.id);
    sendResponse({ success: true });
    return;
  }
});

// >>> PGS AUTO-RELOAD (auto-generated by make_release.py - do not edit between these two lines) >>>
(function () {
  // The updater replaces the extension files on disk. Chrome does not reload them by itself, so once a minute
  // this compares the version on disk with the running version and reloads the extension when they differ -
  // but never while documents are being opened or PDFs are downloading.
  const CHECK_ALARM = 'pgs_version_check';

  async function checkForNewVersion() {
    try {
      const res = await fetch(chrome.runtime.getURL('manifest.json') + '?t=' + Date.now(), { cache: 'no-store' });
      const onDisk = (await res.json()).version;
      if (!onDisk || onDisk === chrome.runtime.getManifest().version) return;
      if (typeof session !== 'undefined' && session) return;
      if (typeof pendingDownloads !== 'undefined' && pendingDownloads.size > 0) return;
      addLog('🔄 New extension version ' + onDisk + ' found - reloading...');
      await chrome.storage.local.set({ pgs_reinject: Date.now() });
      chrome.runtime.reload();
    } catch (e) { /* try again next minute */ }
  }

  chrome.alarms.create(CHECK_ALARM, { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener((a) => { if (a.name === CHECK_ALARM) checkForNewVersion(); });

  // After such a reload, pages that were already open lose their content script. Put it back so the
  // farm data keeps flowing to Python without pressing F5.
  chrome.storage.local.get(['pgs_reinject'], (r) => {
    if (!r || !r.pgs_reinject) return;
    chrome.storage.local.remove('pgs_reinject');
    chrome.tabs.query({}, (tabs) => {
      (tabs || []).forEach((t) => {
        if (t.id != null && /^https?:/.test(t.url || '')) {
          chrome.scripting.executeScript({ target: { tabId: t.id }, files: ['content.js'] }, () => void chrome.runtime.lastError);
        }
      });
    });
  });
})();
// <<< PGS AUTO-RELOAD <<<
