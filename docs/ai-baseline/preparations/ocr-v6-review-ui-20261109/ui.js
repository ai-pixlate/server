(()=>{'use strict';
const $=id=>document.getElementById(id), D=window.REVIEW_DATA, S=window.ReviewState, ids=D.items.map(x=>x.id);
let storage;try{storage=window.localStorage;}catch(e){storage={removeItem(){throw e;},getItem(){throw e;},setItem(){throw e;}};}
const boot=S.boot(storage,D,window.DATA_HASH);let state=boot.state,current=ids[0],dirty=false,view=[],drag=null;
$('reset').textContent=boot.reset+'。과거 공식 검수 기록은 초기화 대상이 아닙니다.';
const item=()=>D.items.find(x=>x.id===current);
const visible=()=>S.visible(D.items,state,$('unreviewed').checked);
function notice(s){$('notice').textContent=s;}
function allowed(){return !dirty||window.confirm('저장하지 않은 현재 선택을 버리고 이동할까요?');}
function box(){ $('canvas').setAttribute('viewBox',view.join(' ')); }
function fit(whole=false){const x=item();view=whole?[0,0,x.image.width,x.image.height]:x.focus.slice();box();}
function zoom(f){const x=item(),w=Math.min(x.image.width*2,Math.max(60,view[2]*f)),h=w*view[3]/view[2];view=[view[0]+(view[2]-w)/2,view[1]+(view[3]-h)/2,w,h];box();}
function svg(tag,attrs){const el=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,v);return el;}
function render(){
 const list=visible(),done=Object.keys(state.records).length,holds=Object.values(state.records).filter(x=>x.decision==='hold').length;
 $('progress').textContent=`저장 ${done}/${ids.length} · 판단 보류 ${holds}`;$('bar').value=done;
 if(!list.some(x=>x.id===current))current=list[0]?.id||null;
 $('picker').replaceChildren(...list.map(x=>{const o=document.createElement('option');o.value=x.id;o.textContent=x.section_id+' · '+x.title;return o;}));$('picker').value=current||'';
 $('review').hidden=!current;$('empty').hidden=!!current;
 const n=list.findIndex(x=>x.id===current);$('prev').disabled=n<=0;$('next').disabled=n<0||n===list.length-1;
 if(!current)return;
 const x=item(),r=state.records[current];$('title').textContent=x.title;$('question').textContent=x.question;$('reason').textContent=x.reason;
 $('gt').textContent='관련 GT (원문 유지)\n'+x.gt.map(g=>g.line_id+' · '+g.text).join('\n');
 $('ocr').textContent=x.ocr.length?'관련 OCR (원문 유지)\n'+x.ocr.map(o=>o.region_key+' · '+o.text).join('\n'):'대응 OCR 영역 없음 · 확정된 1:1 연결 아님';
 $('linkage').textContent=x.linkage;$('recommendation').textContent='AI 추천 — 사용자 선택과 별개\n'+x.recommendation;
 $('status').textContent=r?'저장됨 · '+({include:'포함',exclude:'제외',hold:'판단 보류'}[r.decision]):'미검수';
 document.querySelectorAll('[name="decision"]').forEach(e=>e.checked=e.value===(r?.decision||''));$('note').value=r?.note||'';
 $('source').href=x.image.url;$('imageError').textContent='';const active=current;
 const img=svg('image',{href:x.image.url,x:0,y:0,width:x.image.width,height:x.image.height});img.addEventListener('error',()=>{if(current===active)$('imageError').textContent='원본 이미지를 열 수 없습니다. 원본 경로를 확인하고 이 항목은 판단 보류하세요.';});
 const overlay=window.REVIEW_DISPLAY_OVERLAYS?.[x.id];const boxes=overlay?.image_sha256===x.image.sha256?overlay.boxes:x.boxes;const nodes=[img];boxes.forEach((b,i)=>{nodes.push(svg('rect',{x:b[0],y:b[1],width:b[2],height:b[3],fill:'#ff9900','fill-opacity':'.08',stroke:'#e85d00','stroke-width':3,'vector-effect':'non-scaling-stroke','pointer-events':'none'}));const label=svg('text',{x:b[0]+8,y:b[1]+30,fill:'#9d3400','font-size':28,'font-weight':'bold',stroke:'white','stroke-width':1,'paint-order':'stroke'});label.textContent=String.fromCharCode(65+i);nodes.push(label);});
 $('canvas').replaceChildren(...nodes);fit();dirty=false;
}
function move(delta){if(!allowed())return;const list=visible(),n=list.findIndex(x=>x.id===current);current=list[Math.max(0,Math.min(list.length-1,n+delta))]?.id||null;render();}
function save(next){const id=current;if(!id)return;const choice=document.querySelector('[name="decision"]:checked');if(!choice){notice('포함 / 제외 / 판단 보류 중 하나를 선택하세요.');return;}
 const list=visible(),index=list.findIndex(x=>x.id===id),nextId=list[index+1]?.id;
 const updated=S.put(state,id,choice.value,$('note').value,ids);state=updated;dirty=false;
 try{storage.setItem(boot.key,JSON.stringify(state));notice('현재 항목을 저장했습니다. 파일 내보내기로 별도 보관할 수 있습니다.');}catch(e){notice('브라우저 저장 실패: 페이지를 닫기 전에 결과를 내보내세요.');}
 if(next&&nextId)current=nextId;render();
}
$('picker').addEventListener('change',()=>{if(allowed()){current=$('picker').value;render();}else $('picker').value=current;});
$('unreviewed').addEventListener('change',()=>{if(allowed())render();else $('unreviewed').checked=!$('unreviewed').checked;});
$('prev').onclick=()=>move(-1);$('next').onclick=()=>move(1);$('save').onclick=()=>save(false);$('saveNext').onclick=()=>save(true);
document.querySelectorAll('[name="decision"]').forEach(e=>e.onchange=()=>{dirty=true;});$('note').oninput=()=>{dirty=true;};
$('focus').onclick=()=>fit();$('whole').onclick=()=>fit(true);$('zoomIn').onclick=()=>zoom(.8);$('zoomOut').onclick=()=>zoom(1.25);
$('canvas').addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY>0?1.2:1/1.2);},{passive:false});
$('canvas').addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY,view.slice()];$('canvas').setPointerCapture(e.pointerId);});
$('canvas').addEventListener('pointermove',e=>{if(!drag)return;const rect=$('canvas').getBoundingClientRect(),scale=Math.max(drag[2][2]/rect.width,drag[2][3]/rect.height);view=[drag[2][0]-(e.clientX-drag[0])*scale,drag[2][1]-(e.clientY-drag[1])*scale,...drag[2].slice(2)];box();});
for(const ev of ['pointerup','pointercancel'])$('canvas').addEventListener(ev,()=>{drag=null;});
$('canvas').addEventListener('keydown',e=>{if(e.key==='+'||e.key==='=')zoom(.8);else if(e.key==='-')zoom(1.25);else if(e.key.startsWith('Arrow')){if(e.key==='ArrowLeft')view[0]-=view[2]*.1;if(e.key==='ArrowRight')view[0]+=view[2]*.1;if(e.key==='ArrowUp')view[1]-=view[3]*.1;if(e.key==='ArrowDown')view[1]+=view[3]*.1;box();}else return;e.preventDefault();});
$('export').onclick=()=>{if(!allowed())return;const url=URL.createObjectURL(new Blob([JSON.stringify(state,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='ocr-v6-focused-notes-'+new Date().toISOString().replace(/:/g,'-')+'.json';a.click();URL.revokeObjectURL(url);};
$('import').onchange=async e=>{try{if(!allowed())return;const file=e.target.files[0];if(!file)return;const incoming=S.validate(JSON.parse(await file.text()),window.DATA_HASH,ids);state=incoming;try{storage.setItem(boot.key,JSON.stringify(state));notice('현재 화면 형식의 기록을 불러왔습니다.');}catch(err){notice('불러왔으나 브라우저 저장 실패. 파일로 보관하세요.');}render();}catch(err){notice(err.message);}finally{e.target.value='';}};
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
for(const t of D.triage.filter(x=>x.category!=='policy_question')){const li=document.createElement('li');li.textContent=t.id+' — '+t.reason;$('technical').appendChild(li);}
render();
})();
