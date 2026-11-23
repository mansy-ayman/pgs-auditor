'use strict';
const BASE='http://127.0.0.1:5000';
const farmCode=new URLSearchParams(location.search).get('farm_code')||'';
const $=id=>document.getElementById(id);
const SVGNS='http://www.w3.org/2000/svg';
let session=null,nw=0,nh=0,fitScale=1,zoom=1,marks=[],undoStack=[],redoStack=[],mode='add',ready=false;
$('farmCode').textContent=farmCode||'Farm code unavailable';
const fmt=n=>Number(n||0).toLocaleString('en-US');
function snapshot(){undoStack.push(marks.map(m=>({...m})));if(undoStack.length>1500)undoStack.shift();redoStack=[];}
function dotRadius(){return Math.max(9,Math.min(nw,nh)*.0035);}
function ringRadius(){return dotRadius()*2.6;}
function circle(x,y,r,cls){const c=document.createElementNS(SVGNS,'circle');c.setAttribute('cx',x);c.setAttribute('cy',y);c.setAttribute('r',r);c.setAttribute('class',cls);return c;}
function redraw(){
  const svg=$('markers');svg.replaceChildren();
  const r=dotRadius(),R=ringRadius();
  marks.forEach(m=>{svg.appendChild(circle(m.x,m.y,R,'ring'));svg.appendChild(circle(m.x,m.y,r,'marker'));});
  $('liveCount').textContent=fmt(marks.length);
  $('undo').disabled=!undoStack.length;$('redo').disabled=!redoStack.length;
}
function add(p){snapshot();marks.push({x:p.x,y:p.y});redraw();}
function remove(i){if(i<0||i>=marks.length)return;snapshot();marks.splice(i,1);redraw();}
function point(e){const r=$('satellite').getBoundingClientRect();return{x:(e.clientX-r.left)*nw/r.width,y:(e.clientY-r.top)*nh/r.height};}
function nearest(p){let idx=-1,dmin=Infinity;marks.forEach((m,i)=>{const d=Math.hypot(m.x-p.x,m.y-p.y);if(d<dmin){dmin=d;idx=i}});return dmin<=ringRadius()?idx:-1;}
function availableHeight(){
  const top=document.querySelector('.topbar'), st=$('status');
  const used=(top?top.offsetHeight:0)+(st?st.offsetHeight+10:0)+24;
  return Math.max(120, innerHeight-used);
}
function sizeImage(){
  if(!ready)return;
  const w=Math.round(nw*fitScale*zoom), h=Math.round(nh*fitScale*zoom);
  const stage=$('imageStage');
  stage.style.width=w+'px'; stage.style.height=h+'px';
  stage.style.marginLeft=Math.round((innerWidth-w)/2)+'px';
  $('satellite').style.width=w+'px'; $('satellite').style.height=h+'px';
  $('markers').setAttribute('viewBox',`0 0 ${nw} ${nh}`);
  $('markers').setAttribute('preserveAspectRatio','none');
  $('zoomValue').textContent=Math.round(zoom*100)+'%';
  redraw();
}
function fit(){if(!ready)return; fitScale=availableHeight()/nh; zoom=1; $('zoom').value='1'; sizeImage();}
window.addEventListener('resize',()=>{if(ready){fitScale=availableHeight()/nh;sizeImage();}});
// LEFT click = add a tree.  RIGHT click = remove the tree whose ring it is inside.
$('markers').addEventListener('click', e=>{
  if(!ready)return;
  const p=point(e);
  if(mode==='remove'){const i=nearest(p); if(i>=0)remove(i); return;}
  add(p);
});
$('markers').addEventListener('contextmenu', e=>{
  e.preventDefault();
  if(!ready)return;
  const i=nearest(point(e));
  if(i>=0)remove(i);
});
$('addMode').onclick=()=>{mode='add';$('addMode').classList.add('active');$('removeMode').classList.remove('active');$('status').textContent='Left-click a tree to add it. Right-click inside a ring to remove it.'};
$('removeMode').onclick=()=>{mode='remove';$('removeMode').classList.add('active');$('addMode').classList.remove('active');$('status').textContent='Left-click inside a ring to remove that tree. Right-click works in both modes.'};
$('undo').onclick=()=>{if(!undoStack.length)return;redoStack.push(marks.map(m=>({...m})));marks=undoStack.pop();redraw()};
$('redo').onclick=()=>{if(!redoStack.length)return;undoStack.push(marks.map(m=>({...m})));marks=redoStack.pop();redraw()};
$('zoom').oninput=()=>{zoom=Number($('zoom').value);sizeImage()};$('fit').onclick=fit;
function loadImage(){
  const img=$('satellite');
  img.onload=()=>{nw=img.naturalWidth;nh=img.naturalHeight;ready=true;$('imagePlaceholder').hidden=true;$('finish').disabled=false;fit();$('status').textContent='Left-click to add trees. Right-click inside a ring to remove.'};
  img.onerror=()=>{$('status').textContent='Image is not ready. Wait and refresh this tab.'};
  img.src=BASE+'/tree-counter/image/'+encodeURIComponent(farmCode);
}
async function loadSession(){try{const r=await fetch(BASE+'/tree-counter/session?farm_code='+encodeURIComponent(farmCode));if(!r.ok)throw Error('Python app is not responding');session=await r.json();loadImage()}catch(e){$('status').textContent=e.message}}
$('finish').onclick=async()=>{if(!session||!ready)return;$('finish').disabled=true;$('finish').textContent='Sending...';try{const r=await fetch(BASE+'/tree-counter/result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({farm_code:farmCode,manual_count:marks.length})});const d=await r.json();if(!r.ok)throw Error(d.message||'Send failed');$('status').textContent=d.mismatch?'Count sent — mismatch found.':'Count sent — accepted.';$('finish').textContent='Sent'}catch(e){$('status').textContent=e.message;$('finish').disabled=false;$('finish').textContent='Retry'}};
loadSession();
