
'use strict';
const farmCode=new URLSearchParams(location.search).get('farm_code')||'';
const $=id=>document.getElementById(id);
let session=null,nw=0,nh=0,fitScale=1,zoom=1,marks=[],undoStack=[],redoStack=[],ready=false,markersVisible=true;
$('farmCode').textContent=farmCode||'Farm code unavailable';
const fmt=n=>Number(n||0).toLocaleString('en-US');
function snapshot(){undoStack.push(marks.map(m=>({...m})));if(undoStack.length>1500)undoStack.shift();redoStack=[];}
function redraw(){const svg=$('markers');svg.replaceChildren();marks.forEach((m,i)=>{
  const group=document.createElementNS('http://www.w3.org/2000/svg','g');
  // Double outline makes the marker easy to distinguish on green canopy/grass.
  const outer=document.createElementNS(svg.namespaceURI,'circle');outer.setAttribute('cx',m.x);outer.setAttribute('cy',m.y);outer.setAttribute('r',Math.max(15,14/(fitScale*zoom)));outer.setAttribute('class','marker-halo');
  const ring=document.createElementNS(svg.namespaceURI,'circle');ring.setAttribute('cx',m.x);ring.setAttribute('cy',m.y);ring.setAttribute('r',Math.max(9,9/(fitScale*zoom)));ring.setAttribute('class','marker-ring');
  const dot=document.createElementNS(svg.namespaceURI,'circle');dot.setAttribute('cx',m.x);dot.setAttribute('cy',m.y);dot.setAttribute('r',Math.max(5,4.5/(fitScale*zoom)));dot.setAttribute('class','marker-dot');
  group.append(outer,ring,dot);group.dataset.index=i;svg.appendChild(group);
});
$('liveCount').textContent=fmt(marks.length);$('undo').disabled=!undoStack.length;$('redo').disabled=!redoStack.length;
$('manualCount').value=String(marks.length);}
function add(x,y){snapshot();marks.push({x,y});redraw();}
function removeNearest(p){let idx=-1,dmin=Infinity;marks.forEach((m,i)=>{const d=Math.hypot(m.x-p.x,m.y-p.y);if(d<dmin){dmin=d;idx=i}});const threshold=28/(fitScale*zoom);if(idx>=0&&dmin<=threshold){snapshot();marks.splice(idx,1);redraw();return true;}return false;}
function point(e){const r=$('satellite').getBoundingClientRect();return{x:(e.clientX-r.left)*nw/r.width,y:(e.clientY-r.top)*nh/r.height};}
function sizeImage(center=false){if(!ready)return;const w=Math.round(nw*fitScale*zoom),h=Math.round(nh*fitScale*zoom);$('imageStage').style.width=w+'px';$('imageStage').style.height=h+'px';$('satellite').style.width=w+'px';$('satellite').style.height=h+'px';$('markers').setAttribute('viewBox',`0 0 ${nw} ${nh}`);$('markers').setAttribute('preserveAspectRatio','none');$('zoomValue').textContent=Math.round(zoom*100)+'%';$('markers').style.display=markersVisible?'block':'none';redraw();if(center)window.scrollTo({left:Math.max(0,(w-innerWidth)/2),top:0,behavior:'auto'});}
function fit(){if(!ready)return;const header=$('topbar').getBoundingClientRect().height;const status=$('status').getBoundingClientRect().height;const available=Math.max(160,innerHeight-header-status-22);fitScale=available/nh;zoom=1;$('zoom').value='1';sizeImage(true);}
$('markers').addEventListener('click',e=>{if(!ready)return;const p=point(e);add(p.x,p.y);});
$('markers').addEventListener('contextmenu',e=>{e.preventDefault();if(!ready)return;const p=point(e);if(!removeNearest(p))$('status').textContent='Right-click close to a marker ring to remove it.';});
$('toggleMarkers').onclick=()=>{markersVisible=!markersVisible;$('markers').style.display=markersVisible?'block':'none';$('toggleMarkers').textContent=markersVisible?'Hide Markers':'Show Markers';};
$('undo').onclick=()=>{if(!undoStack.length)return;redoStack.push(marks.map(m=>({...m})));marks=undoStack.pop();redraw()};
$('redo').onclick=()=>{if(!redoStack.length)return;undoStack.push(marks.map(m=>({...m})));marks=redoStack.pop();redraw()};
$('zoom').oninput=()=>{zoom=Number($('zoom').value);sizeImage()};$('fit').onclick=fit;
$('manualCount').addEventListener('input',()=>{const v=parseInt($('manualCount').value,10);if(Number.isFinite(v)&&v>=0){$('liveCount').textContent=fmt(v);}});
function loadImage(){const img=$('satellite');img.onload=()=>{nw=img.naturalWidth;nh=img.naturalHeight;ready=true;$('imagePlaceholder').hidden=true;$('imageState').textContent='Satellite ready · farm boundary shown';fit();};img.onerror=()=>{$('imagePlaceholder').hidden=false;$('imagePlaceholder').textContent='Satellite image unavailable. You can still enter the manual count and Finish.';$('imageState').textContent='Image unavailable — manual count remains enabled';};img.src=BASE+'/tree-counter/image/'+encodeURIComponent(farmCode)+'?boundary=1';}
async function loadSession(){try{const r=await fetch(BASE+'/tree-counter/session?farm_code='+encodeURIComponent(farmCode));if(!r.ok)throw Error('Python app is not responding');session=await r.json();$('submittedCount').textContent=fmt(session.submitted_total);const countRows=session.submitted_counts||[];$('submittedDetails').textContent=countRows.length?`Checklist values: ${countRows.map(fmt).join(' + ')}`:'No tree count values were extracted';$('finish').disabled=false;$('manualCount').value='0';loadImage();}catch(e){$('status').textContent=e.message;$('finish').disabled=false;}}
$('finish').onclick=async()=>{if(!farmCode){$('status').textContent='Farm code missing';return;}$('finish').disabled=true;$('finish').textContent='Sending...';let value=parseInt($('manualCount').value,10);if(!Number.isFinite(value)||value<0)value=marks.length;try{const r=await fetch(BASE+'/tree-counter/result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({farm_code:farmCode,manual_count:value,image_loaded:ready})});const d=await r.json();if(!r.ok)throw Error(d.message||'Send failed');$('status').textContent=d.mismatch?'Count submitted — mismatch found.':'Count submitted — accepted.';$('finish').textContent='Finished';}catch(e){$('status').textContent=e.message;$('finish').disabled=false;$('finish').textContent='Retry';}};
loadSession();
