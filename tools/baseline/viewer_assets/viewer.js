"use strict";
const data=JSON.parse(document.getElementById("viewer-data").textContent);
const $=id=>document.getElementById(id);
const labels={generated:"최종 생성",excluded:"전체 제외 · 적합성 미판정",failed:"상품 실행 실패 영향",missing:"생성 기록 있음 · 파일 누락",no_output:"미생성 · 원인 확인 필요",unknown:"상태 확인 필요"};
let currentCase=data.cases[0],currentSource=currentCase?.sources[0],mode="whole",sectionIndex=0,variantIndex=0,zoom="fit",pictures={};
const text=(id,value)=>{$(id).textContent=value??"";};
const option=(value,label)=>{const el=document.createElement("option");el.value=value;el.textContent=label;return el;};
text("run",data.run_id);text("version",`${data.product_version??"버전 미기록"} · ${(data.product_commit??"SHA 미기록").slice(0,12)}`);
for(const [status,count] of Object.entries(data.counts)){const el=document.createElement("span");el.textContent=`${labels[status]} `;const b=document.createElement("b");b.textContent=count;el.append(b);$("summary").append(el);}
const freeze=document.createElement("span");freeze.textContent=data.reference_frozen?"기준 자료 동결 기록 있음 · 뷰어는 채점하지 않음":"기준 자료 미동결 · 공식 채점 전";$("summary").append(freeze);
for(const c of data.cases)$("case").append(option(c.id,`${c.id} · ${c.name}`));
for(const [key,label] of Object.entries(labels))$("status").append(option(key,label));
text("warning-count",`(${data.warnings.length})`);
for(const warning of data.warnings){const li=document.createElement("li");li.textContent=warning;$("warnings").append(li);}
function list(){
  const query=$("query").value.trim().toLowerCase(),filter=$("status").value;
  const shown=currentCase?.sources.filter(s=>(filter==="all"||s.status===filter)&&s.key.toLowerCase().includes(query))??[];
  $("sources").replaceChildren();
  if(!shown.includes(currentSource)){currentSource=shown[0];sectionIndex=0;variantIndex=0;}
  for(const s of shown){const b=document.createElement("button");b.className="source";b.setAttribute("aria-current",String(s===currentSource));const title=document.createElement("strong"),status=document.createElement("small");title.textContent=s.key;status.textContent=labels[s.status];b.append(title,status);b.onclick=()=>{currentSource=s;sectionIndex=0;variantIndex=0;list();};$("sources").append(b);}
  if(!shown.length){const p=document.createElement("p");p.textContent="조건에 맞는 원본이 없습니다.";$("sources").append(p);}
  render();
}
function applyZoom(){
  for(const side of ["left","right"]){const pic=pictures[side],img=$(side+"-image"),pane=$(side+"-pane");if(pic?.url){const width=zoom==="fit"?Math.max(20,pane.clientWidth-28):pic.width*zoom/100;img.style.width=width+"px";}}
  text("zoom-value",zoom==="fit"?"폭 맞춤":zoom+"%");
}
function show(side,picture,title,missing){
  pictures[side]=picture;const img=$(side+"-image"),empty=$(side+"-empty"),link=$(side+"-link"),pane=$(side+"-pane");text(side+"-title",title);pane.scrollTop=0;pane.scrollLeft=0;
  img.hidden=true;img.removeAttribute("src");img.alt=title;empty.hidden=false;link.hidden=true;link.removeAttribute("href");
  if(picture?.url){img.hidden=false;empty.hidden=true;img.onerror=()=>{img.hidden=true;empty.hidden=false;empty.textContent="이미지 파일을 열 수 없습니다. 뷰어 assets 폴더를 확인하세요.";};img.onload=()=>{applyZoom();};img.src=picture.url;link.href=picture.url;link.hidden=false;}
  else empty.textContent=picture?.reason||missing||"해당 이미지가 없습니다.";
}
function render(){
  const s=currentSource,c=currentCase;$("whole").setAttribute("aria-pressed",String(mode==="whole"));$("section").setAttribute("aria-pressed",String(mode==="section"));
  $("section-control").hidden=mode!=="section";$("section-select").replaceChildren();$("variant").replaceChildren();
  text("source-title",s?.key??"원본 선택 없음");text("source-state",s?`${c.name} · ${labels[s.status]}`:"");
  let variants=[],left=null,right=null,caption="",missing=s?labels[s.status]:"원본을 선택하세요.";
  if(s&&mode==="section"){
    s.sections.forEach((sec,i)=>$("section-select").append(option(String(i),`${sec.order??i+1} · 섹션 ${sec.id} · ${sec.bucket??"연결 기록"}`)));
    $("section-select").value=String(sectionIndex);const sec=s.sections[sectionIndex];left=sec?.original;variants=sec?.previews??[];
    caption=sec?`원본 y=${sec.offset??"미기록"}, 높이=${sec.height??"미기록"} · ${sec.reason??"제외 사유 없음"}`:"연결된 섹션 기록이 없습니다. 실패·전체 제외 원본을 다른 이미지로 대체하지 않습니다.";
    if(sec?.bucket==="exclude")missing="제외 섹션 · 최초 미리보기 없음\n제외 적합성은 별도 검수 대상입니다.";
  }else if(s){left=s.original;variants=s.finals;caption="원본 전체와 실제 N6 최종 산출물을 비교합니다. 제외된 영역 때문에 높이가 다를 수 있습니다.";}
  variants.forEach((p,i)=>$("variant").append(option(String(i),`${i+1}/${variants.length} · ${p.artifact_id??"이미지"}`)));
  if(variantIndex>=variants.length)variantIndex=0;$("variant").value=String(variantIndex);$("variant-control").hidden=variants.length<=1;right=variants[variantIndex];text("coordinates",caption);
  show("left",left,mode==="whole"?"원본 이미지":"원본 섹션",missing);show("right",right,mode==="whole"?"service_final · 최종 결과":"machine_snapshot · 최초 미리보기",missing);applyZoom();
  const logs=[];if(c?.job_status==="failed")logs.push(`상품 job ${c.job_id} / ${c.job_step} 실패. 여러 원본에 전파된 실행 실패이며 원본별 독립 품질 결함 수가 아닙니다.`);
  for(const e of c?.errors??[])logs.push(`상품 task ${e.task_id} · ${e.stage} · 시도 ${e.attempt??"?"}\n${e.code}: ${e.message??""}`);
  for(const e of s?.errors??[])logs.push(`원본 실행 기록 ${e.stage} · 시도 ${e.attempt} · ${e.status}\n${(e.codes??[]).join(", ")}`);
  if(s?.result_records.length)logs.push("종료 상태 요약(실제 단계 호출 횟수가 아님): "+JSON.stringify(s.result_records));
  text("error-count",`(상품 ${c?.errors.length??0} / 원본 ${s?.errors.length??0})`);text("errors",logs.join("\n\n")||"기록된 오류 없음 또는 오류 증거 미제공. 품질 정상 판정이 아닙니다.");
}
$("case").onchange=()=>{currentCase=data.cases.find(c=>c.id===$("case").value);currentSource=null;list();};
$("query").oninput=list;$("status").onchange=list;
$("whole").onclick=()=>{mode="whole";variantIndex=0;render();};$("section").onclick=()=>{mode="section";variantIndex=0;render();};
$("section-select").onchange=()=>{sectionIndex=Number($("section-select").value);variantIndex=0;render();};$("variant").onchange=()=>{variantIndex=Number($("variant").value);render();};
$("fit").onclick=()=>{zoom="fit";applyZoom();};$("actual").onclick=()=>{zoom=100;$("zoom").value="100";applyZoom();};$("zoom").oninput=()=>{zoom=Number($("zoom").value);applyZoom();};window.addEventListener("resize",applyZoom);
let syncing=false;
for(const [a,b] of [["left-pane","right-pane"],["right-pane","left-pane"]])$(a).addEventListener("scroll",()=>{if(!$("sync").checked||syncing)return;syncing=true;const from=$(a),to=$(b);to.scrollTop=(from.scrollTop/Math.max(1,from.scrollHeight-from.clientHeight))*Math.max(0,to.scrollHeight-to.clientHeight);requestAnimationFrame(()=>{syncing=false;});});
list();
