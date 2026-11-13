const assert=require('node:assert/strict'),fs=require('node:fs'),crypto=require('node:crypto'),vm=require('node:vm'),S=require('./review-state.js');
const data=JSON.parse(fs.readFileSync(__dirname+'/review-data.json')),hash=crypto.createHash('sha256').update(fs.readFileSync(__dirname+'/review-data.json')).digest('hex'),ids=data.items.map(x=>x.id);
assert.equal(ids.length,new Set(ids).size);assert.equal(new Set(data.items.map(x=>x.section_id)).size,33);
assert(data.items.every(x=>x.decision===null&&!x.approved&&!x.frozen&&x.ai_reviewed));
let s=S.empty(hash);let n=S.put(s,ids[0],'hold','경계 확인',hash,ids,'test');assert.equal(Object.keys(s.records).length,0);assert.equal(n.approved,false);
assert.throws(()=>S.validate(n,'wrong',ids));assert.throws(()=>S.validate({...n,approved:true},hash,ids));assert.throws(()=>S.validate({...n,frozen:true},hash,ids));assert.throws(()=>S.put(s,'unknown','include','',hash,ids,'t'));
for(const [p,h] of Object.entries(JSON.parse(fs.readFileSync(__dirname+'/source-hashes.json'))))assert.equal(crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex'),h);
// DOM 모의 검증: 실제 브라우저 시각/동작 검증을 대신하지 않는다.
const els={};const get=id=>els[id]??(els[id]={value:id==='zoom'?'100':'',style:{},innerHTML:'',textContent:'',scrollIntoView(){}});
const ctx={REVIEW_DATA:data,DATA_SHA:hash,ReviewState:S,document:{getElementById:get},localStorage:{getItem(){return null},setItem(){}},window:{addEventListener(){}},confirm(){return true},console};vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/review-ui.js','utf8'),ctx);
for(const id of ids){vm.runInContext('go('+JSON.stringify(id)+')',ctx);assert(get('picture').innerHTML.includes('<image'));assert(get('detail').innerHTML.includes('사용자 검수 의견'));assert.equal(get('decision').value,'');}
console.log('검수 상태·해시 방어 및 '+ids.length+'개 화면 DOM 모의 렌더 통과; 실제 브라우저 검증 미수행');
