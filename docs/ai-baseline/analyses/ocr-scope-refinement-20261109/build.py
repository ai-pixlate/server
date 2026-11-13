"""읽기 전용 증거로 범위 연결 후보를 생성한다. OCR/채점/GT 쓰기 기능 없음."""
import json, hashlib, copy
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[4]
B = ROOT / 'docs/ai-baseline'
OLD = B / 'analyses/ocr-scope-linkage-20261109'
OUT = Path(__file__).resolve().parent
def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(name, value): (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

# 원본 이미지 33개 재확인에 따른 영역 보정. OCR 텍스트/GT 유사도로 영역을 만들지 않는다.
# E=제외, I=사진/논문 위 별도 일반 설명, R=의미 미확정. I0는 기본 포함.
EDITS = {
 'GS-01_002__sec_1_01': {'X1':('exclude',[77,46,97,58],'비건 인증 마크만 제외; 위 산뜻 밀착은 일반 설명')},
 'GS-01_002__sec_1_04': {'R1':None,
  'E3':('exclude',[14,22.8,29,24.6],'설문 결과 내부 POWDER ROOM 로고'),
  'E4':('exclude',[44,22.8,59,24.6],'설문 결과 내부 POWDER ROOM 로고'),
  'E5':('exclude',[74,22.8,89,24.6],'설문 결과 내부 POWDER ROOM 로고')},
 'GS-01_002__sec_1_21': {'I1':('include',[32,14,45,26],'휴대용 스틱형 별도 콜아웃'), 'I2':('include',[75,14,88,26],'휴대용 스틱형 별도 콜아웃')},
 'GS-01_002__sec_1_23': {'E3':('exclude',[17,42,46,58],'제품 사진 내부 인쇄; 오른쪽 검증 설명 보호')},
 'GS-02_001__sec_1_02': {'X1':('exclude',[28,16,72,63],'카드 자체 인쇄'), 'I1':('include',[62,9,84,48],'카드에 겹친 참여고객 전원증정 안내')},
 'GS-02_002__sec_1_01': {'E1':('exclude',[8,24,92,61],'상단 용기 인쇄'), 'I1':('include',[72,32,99,52],'판매순위 및 모공 탄력 별도 안내'), 'I2':('include',[46,75,53,79],'두 제품 사이 구성 + 표시')},
 'GS-02_007__sec_1_03': {'I1':('include',[3,90,60,94],'사진 위 별도 집계 기준 각주')},
 'GS-02_008__sec_1_04': {'I1':('include',[7,72.9,46,77],'논문 위 콜라겐 설명'), 'X4':None, 'X5':('include',[47,77.4,91,81.6],'논문 위 눈가주름 설명'), 'X6':('include',[6,84,51,87.5],'논문 위 탄력 설명')},
 'GS-02_014__sec_1_05': {'E1':('exclude',[44,52,85,64],'손에 든 용기 인쇄; 이전 사각형 누락 보완'), 'E2':('exclude',[28,69,46,93],'하단 세럼 용기 인쇄')},
 'GS-02_014__sec_1_08': {'I1':('include',[25,16,74,18.5],'사진 위 별도 각주')},
 'GS-02_014__sec_1_09': {'E1':('exclude',[35,24.5,66,27.5],'질문 상자 사이 노출된 용기 인쇄만 제외')},
 'GS-02_014__sec_1_11': {'I1':('include',[68,7.5,87,11],'사진 위 the vita A 개발 메모'), 'I2':('include',[15,18.4,25,20.5],'사진 위 retinal 메모'), 'I3':('include',[54,20.5,69,23.7],'사진 위 tightening 메모')},
 'GS-03_002__sec_1_04': {'I1':('include',[12,37,49,41],'별도 BODY BRIGHTENING'), 'I2':('include',[36,30,60,32.5],'별도 TONE UP')},
 'GS-03_002__sec_1_05': {'I1':('include',[10,43,42,47],'별도 2주 착색미백 설명'), 'I2':('include',[57,75,91,80],'별도 24H 내추럴톤업 설명')},
 'GS-03_018__sec_1_08': {'I1':('include',[9,38,36,42],'사진 옆 BRIGHTENING 설명'), 'I2':('include',[8,44,32,48],'별도 BODY LOTION'), 'E2':('exclude',[12,65,90,78],'하단 3개 용기만; 아래 제품 설명 보호')},
}

def scope_at(x,y,regions):
 hits=[r for r in regions if r['id']!='I0' and r['rect_percent'][0]<=x<=r['rect_percent'][2] and r['rect_percent'][1]<=y<=r['rect_percent'][3]]
 if any(r['kind']=='review' for r in hits): return 'undetermined'
 # 명시된 별도 설명 창은 배경 사진/논문에서 차감한다. OCR별 우선순위 아님.
 if any(r['kind']=='include' for r in hits): return 'include'
 return 'exclude' if any(r['kind']=='exclude' for r in hits) else 'include'

def classify(rect,regions):
 if not rect or len(rect)!=4: return 'undetermined','좌표 없음'
 x,y,xx,yy=rect
 if not(0<=x<xx<=100 and 0<=y<yy<=100): return 'undetermined','좌표 범위 이탈/면적 없음'
 xs=sorted({x,xx}|{v for r in regions for v in (r['rect_percent'][0],r['rect_percent'][2]) if x<v<xx})
 ys=sorted({y,yy}|{v for r in regions for v in (r['rect_percent'][1],r['rect_percent'][3]) if y<v<yy})
 states={scope_at((a+b)/2,(c+d)/2,regions) for a,b in zip(xs,xs[1:]) for c,d in zip(ys,ys[1:])}
 return (next(iter(states)),'단일 의미 범위 전체 포함') if len(states)==1 and 'undetermined' not in states else ('undetermined','포함/제외/보류 경계 교차')

def pct(b,w,h): return [b['x']/w*100,b['y']/h*100,(b['x']+b['w'])/w*100,(b['y']+b['h'])/h*100] if b else None

def main():
 protected=read(OLD/'preserved-hashes.json')
 assert all(sha(p)==h for p,h in protected.items())
 a=read(OLD/'scope-annotations.json'); before=copy.deepcopy(a)
 a.update(version='translation-scope-20261109-revision3',geometry_approved=False,semantics='I0 기본 포함; 명시 I 창은 E에서 차감; R/경계 교차는 보류. 점수 계산용 확정 마스크 아님')
 changes=[]
 for r in a['sections']:
  if r['classification']!='혼합':continue
  regs={z['id']:z for z in r['regions']}
  for key,value in EDITS.get(r['id'],{}).items():
   old=regs.get(key)
   if value is None: regs.pop(key,None)
   else:
    kind,rect,reason=value;regs[key]={'id':key,'kind':kind,'rect_percent':rect,'meaning':reason,'precision':'scope_only_not_bbox_accuracy','approved_geometry':False}
   changes.append({'section_id':r['id'],'region_id':key,'before':old,'after':regs.get(key),'basis':'원본 이미지 시각 재확인; GT/OCR 문자열로 경계 최적화하지 않음'})
  r['regions']=list(regs.values());r['geometry_review_status']='33개 원본 재검토; 단일 범위 자동 후보 및 경계 보류 분리';r['automatic_scope_ready']=False
 units={x['id']:x for x in map(json.loads,(B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl').read_text(encoding='utf8').splitlines())}
 mixed={r['id']:r for r in a['sections'] if r['classification']=='혼합'}
 for r in a['sections']:assert sha(r['image']['path'])==r['image']['sha256']==units[r['id']]['image']['sha256']
 gt=read(OLD/'gt-line-links.json');gtchanges=[]
 for g in gt['lines']:
  sid=g['section_id'];n=int(g['line_id'][1:]);new=None;reason=None
  if sid=='GS-01_002__sec_1_04' and 3<=n<=23:
   new='exclude' if n in [7,8,14,15,21,22] else 'include';reason='원본은 소비자 설문 결과 설명; POWDER ROOM 로고만 별도 제외'
  if sid=='GS-02_002__sec_1_01' and n in [7,8,11]:new='include';reason='원본 판매순위/제품 유형 안내이며 인증 로고 아님'
  if sid=='GS-02_014__sec_1_11' and 2<=n<=7:new='include' if n<=3 else 'exclude';reason='별도 손글씨 메모 포함; 시료 용기에 직접 적힌 숫자는 용기 인쇄/표기 범위 제외'
  if sid=='GS-02_014__sec_1_09' and n==8:new='exclude';reason='기존 좌표를 원본과 대조: Q&A 사이 용기 글자 위치. 빈 GT 텍스트 유지'
  if new:
   gtchanges.append({'section_id':sid,'line_id':g['line_id'],'before':g['scope'],'after':new,'reason':reason});g['scope']=new;g['reason']=reason;g['link_status']='ai_semantic_linked'
  regs=mixed[sid]['regions'];g['region_candidates']=[r['id'] for r in regs if r['kind']==g['scope']] or ['I0']
  g['geometry_status']='의미 범위 연결; GT 줄별 bbox 동결/검증 없음';g['human_approved']=False
  assert hashlib.sha256(next(l['text'] for l in units[sid]['lines'] if l['line_id']==g['line_id']).encode()).hexdigest()==g['text_sha256']
 idx={x['id']:x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 prior={(x['section_id'],x['output_index']):x for x in read(OLD/'priority-82-review.json')}
 results=[];data=[];rawcache={}
 for sid,r in mixed.items():
  ref=idx[sid]['output'];path=ref['path']
  if path not in rawcache:rawcache[path]=Path(path).read_bytes()
  raw=rawcache[path];assert hashlib.sha256(raw).hexdigest()==ref['sha256']
  doc=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for key in ref['pointer'].split('/')[1:]:doc=doc[int(key)] if isinstance(doc,list) else doc[key]
  regs=doc['regions'];out=[]
  for i,z in enumerate(regs):
   rect=pct(z.get('bbox'),r['image']['width'],r['image']['height']);scope,reason=classify(rect,r['regions']);method='automatic_geometry'
   pv=prior.get((sid,i))
   if pv:
    if pv['scope'] in ['include','exclude']:
     scope=pv['scope'];reason='revision2 원본 경계 시각 확인 재사용';method='prior_visual'
    elif sid=='GS-02_002__sec_1_01' and z['region_key']=='reg_0011':scope='include';reason='원본 TOP.1 판매순위 문구는 일반 안내';method='current_visual'
    elif pv['scope']=='undetermined':scope='undetermined';reason=pv['reason'];method='manual_required'
   # 자동 분류 후 경계 후보 14개를 원본 위치 crop로 재확인한 기록.
   verified={
    ('GS-01_002__sec_1_21',7):'exclude',('GS-01_002__sec_1_21',8):'exclude',
    ('GS-02_001__sec_1_02',4):'exclude',('GS-02_002__sec_1_01',15):'exclude',('GS-02_002__sec_1_01',16):'exclude',
    ('GS-02_008__sec_1_04',114):'exclude',('GS-02_008__sec_1_04',115):'exclude',
    ('GS-02_014__sec_1_09',8):'exclude',('GS-02_014__sec_1_11',10):'include',('GS-02_014__sec_1_11',11):'include',
    ('GS-03_002__sec_1_05',18):'exclude',('GS-03_018__sec_1_08',63):'include',('GS-03_018__sec_1_08',64):'include',('GS-03_018__sec_1_08',65):'include'}
   if (sid,i) in verified:
    scope=verified[sid,i];reason='原本'.replace('原本','원본')+' 위치 crop에서 문구 전체가 '+('용기/카드/논문' if scope=='exclude' else '별도 안내/개발 메모')+'임을 확인; 문자열 분할 없음';method='current_visual'
   if not str(z.get('text','')).strip():scope='empty_no_text';reason='빈 반환 보존; 포함 GT 삭제/면제 근거 아님';method='empty_record'
   item={'section_id':sid,'output_index':i,'region_key':z['region_key'],'text':z.get('text',''),'bbox':z.get('bbox'),'scope':scope,'method':method,'reason':reason,'output_sha256':ref['sha256'],'text_sha256':hashlib.sha256(str(z.get('text','')).encode()).hexdigest(),'previous_priority':bool(pv),'scoring_ready':False}
   out.append(item);results.append(item)
  gs=[g for g in gt['lines'] if g['section_id']==sid];pending=sum(x['scope']=='undetermined' for x in out);gp=sum(x['scope']=='undetermined' for x in gs)
  status='보류' if pending or gp else '연결 완료(범위 후보)'
  data.append({'id':sid,'image':r['image'],'regions':r['regions'],'gt':[dict(g,text=next(l['text'] for l in units[sid]['lines'] if l['line_id']==g['line_id'])) for g in gs],'ocr':out,'status':status,'ocr_pending':pending,'gt_pending':gp,'scoring_ready':False,'blockers':['범위/GT 새 버전 미승인']+(['OCR 경계 보류'] if pending else [])+(['GT 범위 보류'] if gp else [])})
 summary={'sections':33,'ocr_total':len(results),'ocr_scopes':dict(Counter(x['scope'] for x in results)),'ocr_methods':dict(Counter(x['method'] for x in results)),'remaining_1111':dict(Counter(x['scope'] for x in results if not x['previous_priority'])),'gt_scopes':dict(Counter(x['scope'] for x in gt['lines'])),'sections_status':dict(Counter(x['status'] for x in data)),'scoring_ready_sections':0,'changed_region_records':len(changes)}
 write('scope-annotations.json',a);write('annotation-changes.json',changes);write('gt-line-links.json',gt);write('gt-decisions.json',gtchanges);write('ocr-region-links.json',results);write('sections.json',data);write('summary.json',summary)
 write('review-queue.json',{'gt':[x for x in gt['lines'] if x['scope']=='undetermined'],'ocr':[x for x in results if x['scope']=='undetermined']})
 write('source-hashes.json',{'prior_annotations':sha(OLD/'scope-annotations.json'),'truth':sha(B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl'),'inputs':sha(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json'),'protected':protected})
 (OUT/'data.js').write_text('const DATA = '+json.dumps(data,ensure_ascii=False).replace('</','<\\/')+';\n',encoding='utf8')
 assert len(results)==1193 and len(gt['lines'])==969 and len(mixed)==33
 assert len({(x['section_id'],x['output_index']) for x in results})==1193
 assert all(sha(p)==h for p,h in protected.items())
 write('validation.json',{'images_sha256':112,'gt_text_hashes':969,'ocr_source_regions':1193,'protected_files':len(protected),'all_preserved':True,'scoring_executed':False,'browser_visual':'pending'})
 print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
