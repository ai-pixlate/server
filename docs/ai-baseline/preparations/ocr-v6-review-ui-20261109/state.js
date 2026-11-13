(function(root){
 'use strict';
 const choices=['include','exclude','hold'];
 function empty(hash){return {schema:'v6-focused-notes-2',data_sha256:hash,records:{},approved:false,frozen:false};}
 function validate(s,hash,ids){
  if(!s||s.schema!=='v6-focused-notes-2'||s.data_sha256!==hash||s.approved!==false||s.frozen!==false||!s.records||Array.isArray(s.records))throw Error('현재 화면의 검수 기록만 불러올 수 있습니다.');
  for(const [id,r] of Object.entries(s.records))if(!ids.includes(id)||!r||!choices.includes(r.decision)||typeof r.note!=='string'||typeof r.at!=='string')throw Error('검수 항목 또는 저장 형식이 다릅니다.');
  return s;
 }
 function put(s,id,decision,note,ids){validate(s,s.data_sha256,ids);if(!ids.includes(id)||!choices.includes(decision))throw Error('항목과 판정을 선택하세요.');const out=JSON.parse(JSON.stringify(s));out.records[id]={decision,note,at:new Date().toISOString()};return out;}
 function boot(storage,data,hash){
  let reset='이전 61개 화면의 임시 저장 키 초기화 완료';
  try{for(const key of data.reset_keys){storage.removeItem(key);if(storage.getItem(key)!==null)throw Error('이전 키가 남아 있음');}}catch(e){reset='브라우저 저장소 접근/삭제 실패: 이전 기록 삭제 여부 미확인';}
  let state=empty(hash);const key='pixlate-v6-focused-2:'+hash;
  try{const raw=storage.getItem(key);if(raw)state=validate(JSON.parse(raw),hash,data.items.map(x=>x.id));}catch(e){reset+=' / 새 기록 복원 불가: '+e.message;}
  return {state,key,reset};
 }
 function visible(items,state,unreviewed){return items.filter(x=>!unreviewed||!state.records[x.id]);}
 const api={empty,validate,put,boot,visible};if(typeof module!=='undefined')module.exports=api;root.ReviewState=api;
})(typeof window==='undefined'?globalThis:window);
