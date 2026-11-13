(function(root){
 const choices=['include','exclude','hold'];
 function empty(hash){return {schema:'v6-review-notes-1',data_sha256:hash,records:{},approved:false,frozen:false};}
 function validate(state,hash,ids){
  if(!state||state.schema!=='v6-review-notes-1'||state.data_sha256!==hash||state.approved!==false||state.frozen!==false||!state.records||typeof state.records!=='object')throw Error('검수 자료 버전/해시 또는 상태 불일치');
  for(const [id,r] of Object.entries(state.records))if(!ids.includes(id)||!choices.includes(r.decision)||typeof r.note!=='string'||typeof r.at!=='string')throw Error('알 수 없는 항목/판정 형식');
  return state;
 }
 function put(state,id,decision,note,hash,ids,at){validate(state,hash,ids);if(!ids.includes(id)||!choices.includes(decision))throw Error('항목/판정 오류');let next=JSON.parse(JSON.stringify(state));next.records[id]={decision,note,at};return validate(next,hash,ids);}
 const api={empty,validate,put};if(typeof module!=='undefined')module.exports=api;else root.ReviewState=api;
})(globalThis);
