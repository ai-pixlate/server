const assert=require('node:assert/strict'),fs=require('node:fs'),crypto=require('node:crypto'),vm=require('node:vm'),S=require('./state.js');
const raw=fs.readFileSync(__dirname+'/review-data.json'),data=JSON.parse(raw),hash=crypto.createHash('sha256').update(raw).digest('hex'),ids=data.items.map(x=>x.id);
assert.equal(ids.length,2);assert.equal(data.triage.length,61);assert.equal(new Set(data.triage.map(x=>x.section_id)).size,33);
assert.equal(data.triage.filter(x=>x.category==='technical_pending').length,58);
assert(data.items.every(x=>x.decision===null&&!x.approved));
const historic='pixlate-v6-review:historical',map=new Map([[historic,'preserve'],[data.reset_keys[0],'old inputs']]);
const storage={getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};
let boot=S.boot(storage,data,hash);assert(!map.has(data.reset_keys[0]));assert.equal(map.get(historic),'preserve');assert.deepEqual(boot.state.records,{});
assert.throws(()=>S.validate({schema:'v6-review-notes-1'},hash,ids));assert.throws(()=>S.put(boot.state,'unknown','include','',ids));
const els={},radios=['include','exclude','hold'].map(value=>({value,checked:false}));
function element(){return {value:'',checked:false,hidden:false,children:[],attrs:{},events:{},textContent:'',addEventListener(k,f){this.events[k]=f;},setAttribute(k,v){this.attrs[k]=String(v);},replaceChildren(...c){this.children=c;},appendChild(c){this.children.push(c);},getBoundingClientRect(){return {width:800,height:650};},setPointerCapture(){},click(){}};}
const get=id=>els[id]??(els[id]=element());
const document={getElementById:get,createElement:element,createElementNS:element,querySelectorAll:()=>radios,querySelector:()=>radios.find(x=>x.checked)};
const window={REVIEW_DATA:data,DATA_HASH:hash,ReviewState:S,localStorage:storage,confirm:()=>true,addEventListener(){}};
vm.runInNewContext(fs.readFileSync(__dirname+'/display-overlays.js','utf8'),{window});
vm.runInNewContext(fs.readFileSync(__dirname+'/ui.js','utf8'),{window,document,console,Blob,URL});
assert(radios.every(x=>!x.checked));assert.equal(get('canvas').children[0].attrs.href,data.items[0].image.url);
radios[1].checked=true;radios[1].onchange();get('note').value='first only';get('saveNext').onclick();
assert(radios.every(x=>!x.checked));assert.equal(get('canvas').children[0].attrs.href,data.items[1].image.url);
assert.equal(get('canvas').children[1].attrs.x,'910'); // 검정 제목 영역을 덮지 않는 표시 전용 강조
let saved=JSON.parse(map.get(boot.key));assert.equal(saved.records[ids[0]].note,'first only');assert(!saved.records[ids[1]]);
radios[2].checked=true;get('note').value='second hold';get('save').onclick();
saved=JSON.parse(map.get(boot.key));assert.equal(saved.records[ids[1]].decision,'hold');assert.equal(saved.records[ids[0]].decision,'exclude');
assert.deepEqual(S.boot(storage,data,hash).state,saved);assert.equal(S.visible(data.items,saved,true).length,0);
get('unreviewed').checked=true;get('unreviewed').events.change();assert.equal(get('review').hidden,true);
get('unreviewed').checked=false;get('unreviewed').events.change();get('next').onclick();assert.equal(get('note').value,'second hold');
get('prev').onclick();assert.equal(get('note').value,'first only');
const before=get('canvas').attrs.viewBox;get('zoomIn').onclick();assert.notEqual(get('canvas').attrs.viewBox,before);get('focus').onclick();assert.equal(get('canvas').attrs.viewBox,before);
for(const x of data.items){assert.equal(crypto.createHash('sha256').update(fs.readFileSync(x.image.path)).digest('hex'),x.image.sha256);for(const [a,b,w,h]of x.boxes){assert(a>=0&&b>=0&&w>0&&h>0&&a+w<=x.image.width&&b+h<=x.image.height);}}
assert.equal(data.items[1].ocr.length,0);
const refused=S.boot({removeItem(){},getItem(k){return data.reset_keys.includes(k)?'old inputs':null;}},data,hash);
assert(refused.reset.includes('미확인'));assert.deepEqual(refused.state.records,{});
const unavailable=S.boot({removeItem(){throw Error('disabled');},getItem(){throw Error('disabled');}},data,hash);
assert(unavailable.reset.includes('미확인'));assert.deepEqual(unavailable.state.records,{});
assert.throws(()=>S.validate({...saved,data_sha256:'stale'},hash,ids));
assert.throws(()=>S.validate({...saved,records:{wrong:{decision:'exclude',note:'',at:'now'}}},hash,ids));
const chat=JSON.parse(fs.readFileSync(__dirname+'/user-review-chat.json'));S.validate(chat,hash,ids);
assert.equal(Object.keys(chat.records).length,2);assert(Object.values(chat.records).every(x=>x.decision==='exclude'));
console.log('PASS: 대상/연결·해시·범위·기록 초기화 격리·자동 선택 방지·항목별 저장·재조회·필터·이전/다음·확대 DOM 모의 검증. 브라우저 시각 검증 아님.');
