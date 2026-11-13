"""기존 증거를 읽는 유형별 필터 검증. OCR/CER 실행 없음."""
from pathlib import Path
import json, hashlib, sys
from collections import Counter
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
from tools.baseline.ocr_scope import filter_section, v5_inputs
P=Path(__file__).resolve().parent;B=ROOT/'docs/ai-baseline'
def read(p):return json.loads(Path(p).read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,d):(P/n).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 old=B/'preparations/ocr-v6-fixed-scope-20261109'
 protected=read(B/'analyses/ocr-scope-refinement-20261109/source-hashes.json')['protected']
 assert all(sha(p)==h for p,h in protected.items())
 truth=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl';th=sha(truth)
 units={x['id']:x for x in map(json.loads,truth.read_text(encoding='utf8').splitlines())}
 scopes=read(old/'fixed-regions.json');links={x['section_id']:x for x in read(old/'gt-region-links.json')}
 idx={x['id']:x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 evidence={x['id']:x for x in read(P/'source-review.json')}
 prior={x['id']:x for x in read(B/'analyses/ocr-scope-refinement-20261109/sections.json')}
 rows=[];audits=[];queue=[]
 for s in scopes:
  sid=s['id'];u=units[sid];l=links[sid];assert sha(s['image_path'])==s['image_sha256']
  s['scope_version']='source-scope-v6-type-validation-20261109-1';l['scope_version']=s['scope_version']
  e=evidence.get(sid)
  if e and e['verified']:
   assert e['image_sha256']==s['image_sha256']
   s.update(section_type=e['mode'],section_evidence=e,coverage_verified=True,regions=[],default='exclude' if e['mode']=='full_exclude' else 'unknown')
  ref=idx[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256']
  d=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:d=d[int(k)] if isinstance(d,list) else d[k]
  out=[]
  for n,r in enumerate(d['regions']):
   b=r.get('bbox');out.append(dict(id=str(n)+':'+r['region_key'],text=r['text'],rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']] if b else None,mixed=sid=='GS-01_002__sec_1_23' and r['region_key']=='reg_0001'))
  result=filter_section(s,[dict(id=z['line_id'],text=z['text']) for z in u['lines']],out,image_sha256=s['image_sha256'],gt_sha256=th,gt_links=l)
  if result['ready'] and result['applicability']=='applicable':v5_inputs(result) # 배열 연결만; 채점 없음
  status='검증 완료' if result['ready'] else '채점 차단' if sid=='GS-01_002__sec_1_23' else '사용자 확인 필요'
  row=dict(section_id=sid,status=status,applicability=result['applicability'],filter_ready=result['ready'],input_sha256=s['image_sha256'],output_sha256=ref['sha256'],blockers=result['blockers'])
  rows.append(row);audits.append(dict(section_id=sid,**result))
  if not result['ready']:
   previous=prior.get(sid,{})
   queue.append(dict(section_id=sid,image_path=s['image_path'],reason=e['reason'] if e else 'Good all / goodal 혼합 문자열 안전 분리 미검증' if status=='채점 차단' else '기존 의미 연결은 재사용 가능하나 공통 고정 영역 경계와 GT 앵커 검증 미완료',regions=s['regions'],prior_gt_links=previous.get('gt',[]),prior_ocr_links=previous.get('ocr',[]),request='전사 전체 재검수 대신 제외 요소 경계·별도 설명 보존 및 기존 연결의 보류 항목 확인'))
 assert len(rows)==112 and all(sha(p)==h for p,h in protected.items())
 summary=dict(before=dict(Counter(x['status'] for x in read(old/'section-status.json'))),after=dict(Counter(x['status'] for x in rows)),full_include_verified=sum(s.get('section_type')=='full_include' for s in scopes),full_exclude_verified=sum(s.get('section_type')=='full_exclude' for s in scopes),protected_files_unchanged=len(protected),cer_called=False,filter_sha256=sha(ROOT/'tools/baseline/ocr_scope.py'),source_review_sha256=sha(P/'source-review.json'))
 for n,d in [('fixed-regions.json',scopes),('gt-region-links.json',list(links.values())),('section-status.json',rows),('filter-audit.json',audits),('review-queue.json',queue),('validation.json',summary)]:write(n,d)
 print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
