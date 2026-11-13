"""원본 검토 sidecar를 공통 필터로 검증하고 사용자 검수 자료 생성. CER 호출 없음."""
from pathlib import Path
import json,hashlib,sys,copy
from collections import Counter
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
from tools.baseline.ocr_scope import filter_section, classify, v5_inputs
P=Path(__file__).resolve().parent;B=ROOT/'docs/ai-baseline';OLD=B/'preparations/ocr-v6-type-validation-20261109'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,d):(P/n).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def witness(scope,state):
 # 의미 영역 연결의 검증용 내부점. 실제 GT 줄 bbox가 아니다.
 xs=sorted({0,scope['width']}|{v for r in scope['regions'] for v in (r['rect'][0],r['rect'][2])})
 ys=sorted({0,scope['height']}|{v for r in scope['regions'] for v in (r['rect'][1],r['rect'][3])})
 for x,xx in zip(xs,xs[1:]):
  for y,yy in zip(ys,ys[1:]):
   rect=[x+(xx-x)*.4,y+(yy-y)*.4,x+(xx-x)*.6,y+(yy-y)*.6]
   if classify(scope,rect)[0]==state:return rect
 raise ValueError('no_scope_witness')
def main():
 protected=read(B/'analyses/ocr-scope-refinement-20261109/source-hashes.json')['protected']
 assert all(sha(p)==h for p,h in protected.items())
 previous=read(OLD/'section-status.json');target={x['section_id'] for x in previous if x['status']=='사용자 확인 필요'}
 decisions=read(P/'source-decisions.json');assert len(decisions)==65 and {d['section_id'] for d in decisions}==target
 dm={d['section_id']:d for d in decisions};scopes=read(OLD/'fixed-regions.json');links={x['section_id']:x for x in read(OLD/'gt-region-links.json')}
 truth=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl';th=sha(truth)
 units={x['id']:x for x in map(json.loads,truth.read_text(encoding='utf8').splitlines())}
 idx={x['id']:x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 evidence={str(truth):th};rows=[];audits=[];items=[];newready=[]
 for s in scopes:
  sid=s['id'];u=units[sid];l=links[sid];assert sha(s['image_path'])==s['image_sha256'];evidence[s['image_path']]=s['image_sha256']
  d=dm.get(sid)
  if d:
   d['image_sha256']=s['image_sha256'];d['truth_sha256']=th
   s['scope_version']='source-scope-v6-ai-review-20261109-1';l['scope_version']=s['scope_version']
   if d['coverage_candidate']:
    s.update(coverage_verified=True,section_type=d['mode'],regions=[],default='unknown' if d['mode']=='full_include' else 'exclude')
    if d['mode']=='full_include':s['section_evidence']=dict(verified=True,image_sha256=s['image_sha256'],source='source-decisions.json:'+sid,reason=d['note'],exclusion_elements=False)
    else:
     assigned={}
     for k,z in enumerate(d['zones']):
      x,y,xx,yy=z['rect_percent'];rect=[x*s['width']/100,y*s['height']/100,xx*s['width']/100,yy*s['height']/100]
      s['regions'].append(dict(id='T'+str(k+1),kind='include',rect=rect,verified=True,reason=d['note']))
      for line in z['gt_lines']:
       lid=f'L{line:03d}';assert lid in l['lines'] and lid not in assigned
       assigned[lid]=rect
     for lid,loc in l['lines'].items():
      loc.update(verified=True,rect=assigned.get(lid) or witness(s,'exclude'),reason='원본 대조 의미 범위 연결; 좌표는 범위 검증 witness이며 GT 줄 위치 아님',anchor_kind='source_semantic_scope_witness',source_decision=sid,semantic_scope='include' if lid in assigned else 'exclude')
  ref=idx[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256'];evidence[ref['path']]=ref['sha256']
  payload=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:payload=payload[int(k)] if isinstance(payload,list) else payload[k]
  out=[]
  for n,r in enumerate(payload['regions']):
   b=r.get('bbox');out.append(dict(id=str(n)+':'+r['region_key'],text=r['text'],rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']] if b else None,mixed=sid=='GS-01_002__sec_1_23' and r['region_key']=='reg_0001'))
  result=filter_section(s,[dict(id=z['line_id'],text=z['text']) for z in u['lines']],out,image_sha256=s['image_sha256'],gt_sha256=th,gt_links=l)
  if result['ready'] and result['applicability']=='applicable':v5_inputs(result)
  status='검증 완료' if result['ready'] else '채점 차단' if sid=='GS-01_002__sec_1_23' else '사용자 확인 필요'
  row=dict(section_id=sid,status=status,filter_ready=result['ready'],ai_reviewed=bool(d),human_approved=False,official_scoring_authorized=False,blockers=result['blockers'])
  rows.append(row);audits.append(dict(section_id=sid,**result))
  if d and result['ready']:newready.append(sid)
  if d and not result['ready']:
   holds=[a for a in result['audit'] if a['kind']=='ocr' and a['decision']=='hold']
   if d['coverage_candidate']:
    # 실제 교차 영역만 사용자 화면에 올린다. 텍스트 유사도로 GT 연결하지 않는다.
    focus=[(a,next(o for o in out if o['id']==a['id'])) for a in holds]
   else:focus=[(None,None)]
   for a,o in focus:
    rect=[o['rect'][0]/s['width']*100,o['rect'][1]/s['height']*100,o['rect'][2]/s['width']*100,o['rect'][3]/s['height']*100] if o and o['rect'] else None
    iid=sid+(':'+o['id'] if o else ':scope')
    items.append(dict(id=iid,section_id=sid,title='경계 교차' if o else '원본 범위·GT 연결 확인',image=dict(u['image'],path=s['image_path'],url=Path(s['image_path']).as_uri()),focus_rect_percent=rect,location_status='OCR 원시 bbox; 범위 경계 후보와 교차' if o else '섹션 원본 전체; 아래 영역 후보 표시',gt=u['lines'],focus_gt=[lid for lid,loc in l['lines'].items() if o and loc.get('semantic_scope')=='include' and loc.get('rect') and o.get('rect') and loc['rect'][0]<o['rect'][2] and o['rect'][0]<loc['rect'][2] and loc['rect'][1]<o['rect'][3] and o['rect'][1]<loc['rect'][3]],ocr=payload['regions'],focus_ocr=o['id'].split(':',1)[1] if o else None,output_ref=ref,recommendation='보류 — 문자열 분리 금지; 원본 포함·제외 의미 및 경계 확인' if o else '설명 포함·사진/패키지/심볼 제외; 표시한 경계·연결만 확인',reason=d['note'],candidates=[],decision=None,approved=False,frozen=False,truth_sha256=th,scope_regions=s['regions'],ai_reviewed=True,gt_scope_links=l['lines'],focus_gt_note='공통 의미 영역이 겹치는 후보 목록; 줄별 정확 위치나 일대일 문자열 대응 아님'))
 assert all(sha(p)==h for p,h in protected.items())
 assert {x['section_id'] for x in rows if x['status']=='검증 완료'} >= {x['section_id'] for x in previous if x['status']=='검증 완료'}
 summary=dict(before=dict(Counter(x['status'] for x in previous)),after=dict(Counter(x['status'] for x in rows)),ai_visual_reviewed=65,new_ready=len(newready),new_ready_ids=newready,review_items=len(items),review_sections=len({x['section_id'] for x in items}),boundary_sections=len({x['section_id'] for x in items if x['focus_ocr']}),semantic_sections=len({x['section_id'] for x in items if not x['focus_ocr']}),protected_files_unchanged=len(protected),source_hashes_verified=len(evidence),cer_called=False,browser_visual='미수행: 이전 Computer Use 안전 중단 재시도 안 함')
 for n,x in [('source-decisions.json',decisions),('fixed-regions.json',scopes),('gt-region-links.json',list(links.values())),('section-status.json',rows),('filter-audit.json',audits),('source-hashes.json',evidence),('validation.json',summary)]:write(n,x)
 write('review-data.json',dict(version='v6-ai-scope-review-1',status='AI 1차 검수; 사용자 미승인; 미동결',items=items))
 (P/'data.js').write_text('const REVIEW_DATA='+json.dumps(read(P/'review-data.json'),ensure_ascii=False).replace('</','<\\/')+';\nconst DATA_SHA='+json.dumps(sha(P/'review-data.json'))+';\n',encoding='utf8')
 print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
