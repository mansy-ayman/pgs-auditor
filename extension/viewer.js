// viewer.js - shows ONE document (opened by background.js after a PDF download) with the file-name header.
// URL params: u = file url, t = title, pink = 1/0, n = index (1-based), total
let rotation = 0;
let scale = 1.0;

const $ = (id) => document.getElementById(id);

function applyTransforms() {
  const el = $('current-doc');
  if (el) el.style.transform = `rotate(${rotation}deg) scale(${scale})`;
}

function setStatus(html) { $('status').innerHTML = html; }

function showControls(on) {
  document.querySelector('.banner-controls').style.visibility = on ? 'visible' : 'hidden';
}

function showImage(src) {
  const container = $('media-container');
  container.innerHTML = '';
  const img = document.createElement('img');
  img.id = 'current-doc';
  img.className = 'doc-media';
  img.onload = () => setStatus('');
  img.onerror = () => setStatus('⚠️ تعذر عرض الملف');
  img.src = src;
  container.appendChild(img);
  showControls(true);
}

function showPdf(blob) {
  const container = $('media-container');
  container.innerHTML = '';
  const frame = document.createElement('iframe');
  frame.id = 'current-doc';
  frame.className = 'pdf-frame';
  frame.src = URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }));
  container.appendChild(frame);
  document.body.classList.add('pdf-mode');
  showControls(false);       // Chrome's own PDF toolbar has zoom/rotate
  setStatus('');
}

async function isPdf(blob) {
  if (blob.type === 'application/pdf') return true;
  try { return (await blob.slice(0, 5).text()) === '%PDF-'; } catch (e) { return false; }
}

async function init() {
  const p = new URLSearchParams(location.search);
  const url = p.get('u');
  const label = p.get('t') || 'document';
  const n = p.get('n'), total = p.get('total');

  // ---- header with the file name ----
  document.title = label;
  const titleEl = $('doc-title');
  titleEl.textContent = `📄 ${n && total ? `[${n}/${total}] ` : ''}${label}`;
  titleEl.style.color = p.get('pink') === '1' ? '#ff69b4' : '#ffffff';

  if (!url) { setStatus('لا يوجد رابط'); return; }
  setStatus('جاري التحميل...');

  try {
    let blob = null;
    const f = p.get('f');                 // local copy on this PC (fast)
    if (f) {
      try { const r = await fetch(f); if (r.ok) blob = await r.blob(); } catch (e) { /* no file access: use the internet copy */ }
    }
    if (!blob) {
      const res = await fetch(url, { credentials: 'include' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      blob = await res.blob();
    }
    if (await isPdf(blob)) showPdf(blob);
    else if (blob.type.startsWith('image/')) showImage(URL.createObjectURL(blob));
    else setStatus('⚠️ نوع الملف غير مدعوم للعرض');
  } catch (e) {
    setStatus(`⚠️ ${e.message} - <a href="${url}" target="_blank">فتح الرابط مباشرة</a>`);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  init();

  $('kobo-zoom-slider').addEventListener('input', (e) => {
    scale = parseFloat(e.target.value);
    $('kobo-zoom-val').textContent = `${Math.round(scale * 100)}%`;
    applyTransforms();
  });
  $('kobo-rotate-left').addEventListener('click', () => { rotation = (rotation - 90 + 360) % 360; applyTransforms(); });
  $('kobo-rotate-right').addEventListener('click', () => { rotation = (rotation + 90) % 360; applyTransforms(); });
  $('kobo-rotate-reset').addEventListener('click', () => {
    rotation = 0; scale = 1;
    $('kobo-zoom-slider').value = '1';
    $('kobo-zoom-val').textContent = '100%';
    applyTransforms();
  });
});
