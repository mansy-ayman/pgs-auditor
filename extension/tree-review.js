'use strict';
const BASE='http://127.0.0.1:5000';
const farmCode=new URLSearchParams(location.search).get('farm_code')||'';
const $=id=>document.getElementById(id);
let session=null,nw=0,nh=0,fitScale=1,zoom=1,marks=[],undoStack=[],redoStack=[],mode='add',ready=false;
$('farmCode').textContent=farmCode||'Farm code unavailable';
const fmt=n=>Number(n||0).toLocaleString('en-US');
function snapshot(){undoStack.push(marks.map(m=>({...m})));if(undoStack.length>1500)undoStack.shift();redoStack=[];}
function redraw(){const svg=$('markers');svg.replaceChildren();marks.forEach((m,i)=>{const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('cx',m.x);c.setAttribute('cy',m.y);c.setAttribute('r',Math.max(7,Math.min(nw,nh)*.003));c.setAttribute('class','marker');c.addEventListener('click',e=>{e.stopPropagation();remove(i)});svg.appendChild(c)});$('liveCount').textContent=fmt(marks.length);$('undo').disabled=!undoStack.length;$('redo').disabled=!redoStack.length;}
function add(x,y){snapshot();marks.push({x,y});redraw();}
function remove(i){if(i<0||i>=marks.length)return;snapshot();marks.splice(i,1);redraw();}
function point(e){const r=$('satellite').getBoundingClientRect();return{x:(e.clientX-r.left)*nw/r.width,y:(e.clientY-r.top)*nh/r.height};}
function nearest(p){let idx=-1,dmin=Infinity;marks.forEach((m,i)=>{const d=Math.hypot(m.x-p.x,m.y-p.y);if(d<dmin){dmin=d;idx=i}});return dmin<45/(fitScale*zoom)?idx:-1;}
function sizeImage(center=false){if(!ready)return;const w=Math.round(nw*fitScale*zoom),h=Math.round(nh*fitScale*zoom);$('imageStage').style.width=w+'px';$('imageStage').style.height=h+'px';$('satellite').style.width=w+'px';$('satellite').style.height=h+'px';$('markers').setAttribute('viewBox',`0 0 ${nw} ${nh}`);$('markers').setAttribute('preserveAspectRatio','none');$('zoomValue').textContent=Math.round(zoom*100)+'%';redraw();if(center)window.scrollTo({left:Math.max(0,(w-innerWidth)/2),top:0,behavior:'auto'});}
function fit(){if(!ready)return;fitScale=Math.min(innerWidth/nw,1);zoom=1;$('zoom').value='1';sizeImage(true);}
$('markers').addEventListener('contextmenu',e=>{e.preventDefault();if(!ready)return;const p=point(e);if(mode==='add')add(p.x,p.y);else{const i=nearest(p);if(i>=0)remove(i)}});
$('markers').addEventListener('click',e=>{if(e.target.classList.contains('marker')||mode!=='remove'||!ready)return;const i=nearest(point(e));if(i>=0)remove(i)});
$('addMode').onclick=()=>{mode='add';$('addMode').classList.add('active');$('removeMode').classList.remove('active');$('status').textContent='Right-click a tree to add a marker. Click a marker to remove it.'};
$('removeMode').onclick=()=>{mode='remove';$('removeMode').classList.add('active');$('addMode').classList.remove('active');$('status').textContent='Click a marker or right-click near a marker to remove it.'};
$('undo').onclick=()=>{if(!undoStack.length)return;redoStack.push(marks.map(m=>({...m})));marks=undoStack.pop();redraw()};
$('redo').onclick=()=>{if(!redoStack.length)return;undoStack.push(marks.map(m=>({...m})));marks=redoStack.pop();redraw()};
$('zoom').oninput=()=>{zoom=Number($('zoom').value);sizeImage()};$('fit').onclick=fit;
function loadImage(){const img=$('satellite');img.onload=()=>{nw=img.naturalWidth;nh=img.naturalHeight;ready=true;$('imagePlaceholder').hidden=true;$('finish').disabled=false;fit();$('status').textContent='Right-click to add trees. Click markers to remove.'};img.onerror=()=>{$('status').textContent='Image is not ready. Wait and refresh this tab.'};img.src=BASE+'/tree-counter/image/'+encodeURIComponent(farmCode);}
async function loadSession(){try{const r=await fetch(BASE+'/tree-counter/session?farm_code='+encodeURIComponent(farmCode));if(!r.ok)throw Error('Python app is not responding');session=await r.json();loadImage()}catch(e){$('status').textContent=e.message}}
$('finish').onclick=async()=>{if(!session||!ready)return;$('finish').disabled=true;$('finish').textContent='Sending...';try{const r=await fetch(BASE+'/tree-counter/result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({farm_code:farmCode,manual_count:marks.length})});const d=await r.json();if(!r.ok)throw Error(d.message||'Send failed');$('status').textContent=d.mismatch?'Count sent — mismatch found.':'Count sent — accepted.';$('finish').textContent='Sent'}catch(e){$('status').textContent=e.message;$('finish').disabled=false;$('finish').textContent='Retry'}};
loadSession();
