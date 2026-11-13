"""기존 연결 자료의 대칭 검증. GT/OCR/기존 연결 자료 쓰기 및 채점 없음."""
import json, hashlib, unicodedata, importlib.util
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[4]
B=ROOT/'docs/ai-baseline'
P=Path(__file__).resolve().parent
R=B/'analyses/ocr-scope-refinement-20261109'
def read(p):return json.loads(Path(p).read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(name,v):(P/name).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def norm(t):return ''.join(unicodedata.normalize('NFC',t).split())

def main():
 before={str(p):sha(p) for p in R.iterdir() if p.is_file()}
 protected=read(R/'source-hashes.json')['protected']
 assert all(sha(p)==h for p,h in protected.items())
 data=read(R/'sections.json');assert len(data)==33
 units={u['id']:u for u in map(json.loads,(B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl').read_text(encoding='utf8').splitlines())}
 sp=importlib.util.spec_from_file_location('read_only_geometry',R/'build.py');geo=importlib.util.module_from_spec(sp);sp.loader.exec_module(geo)
 idx={s['id']:s for s in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 lexical=[];empty=[];sections=[];auto=0;gtcount=0;ocrcount=0
 for s in data:
  sid=s['id'];u=units[sid];assert sha(s['image']['path'])==u['image']['sha256']
  ref=idx[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256']
  d=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for key in ref['pointer'].split('/')[1:]:d=d[int(key)] if isinstance(d,list) else d[key]
  assert len(d['regions'])==len(s['ocr'])
  assert [x['line_id'] for x in s['gt']]==[x['line_id'] for x in u['lines']]
  for g,l in zip(s['gt'],u['lines']):
   assert g['text']==l['text'] and hashlib.sha256(g['text'].encode()).hexdigest()==g['text_sha256'];gtcount+=1
  for o in s['ocr']:
   orig=d['regions'][o['output_index']];assert orig['text']==o['text'] and orig['bbox']==o['bbox'] and orig['region_key']==o['region_key'];ocrcount+=1
   state,reason=geo.classify(geo.pct(o['bbox'],s['image']['width'],s['image']['height']),s['regions'])
   if o['method']=='automatic_geometry':assert o['scope']==state;auto+=1
   if o['scope']=='empty_no_text':empty.append({'section_id':sid,'region_key':o['region_key'],'geometry_scope':state,'gt_correspondence':'개별 대응 미실시'})
   if norm(o['text']):
    candidates=[g for g in s['gt'] if norm(g['text'])==norm(o['text'])]
    if candidates and all(g['scope']!=o['scope'] for g in candidates):lexical.append({'section_id':sid,'region_key':o['region_key'],'text':o['text'],'gt_candidates':[g['line_id'] for g in candidates],'resolution':'동일 문자가 다른 원본 위치에 있음; 대칭 오류 아님. 문자열만으로 연결 금지'})
  pending=s['gt_pending'] or s['ocr_pending']
  status='사용자 판단 필요' if pending else '검증 완료'
  note='현재 증거의 정책·연결 일관성 점검 통과; GT 완전성·사람 승인·공식 채점 준비 완료 아님'
  if pending:note='기존 GT/OCR 보류 유지; 미결 항목 표 참조'
  if sid=='GS-03_018__sec_1_08':status='채점 차단';note='reg_0044 용기 인쇄 반사상이 include. 대칭 범위 수정/확인 전 차단'
  sections.append({'section_id':sid,'status':status,'reason':note,'scoring_ready':False})
 for r in empty:r['gt_correspondence']='빈 영역 개수로 삭제 문자 수 산정 불가; 포함 GT 잔여분을 전체 대응에서 검증'
 assert len(lexical)==2 and auto==1063 and gtcount==969 and ocrcount==1193
 assert all(sha(p)==h for p,h in before.items()) and all(sha(p)==h for p,h in protected.items())
 write('section-status.json',sections);write('empty-output-audit.json',empty);write('lexical-alerts.json',lexical)
 # 기존 연결은 보존하고 새 발견만 보류 sidecar로 기록한다.
 write('hold-overlay.json',{'base_sections_sha256':sha(R/'sections.json'),'scoring_authorized':False,'additional_holds':[{'section_id':'GS-03_018__sec_1_08','region_key':'reg_0044','previous_scope':'include','audit_scope':'undetermined','recommendation':'용기 인쇄 반사 범위로 제외 권고; 기존 결과 수정 없음'}]})
 summary={'sections_status':dict(Counter(s['status'] for s in sections)),'gt_verified_text_hashes':gtcount,'ocr_verified_source_regions':ocrcount,'automatic_rule_reproduction':auto,'semantic_accuracy_rate':None,'semantic_accuracy_note':'독립적인 전수 영역 정답/사람 검수 없음. 기하 재현율을 의미 정확도로 부르지 않음','confirmed_scope_risk_count':1,'lexical_false_alarms':len(lexical),'empty_geometry':dict(Counter(x['geometry_scope'] for x in empty)),'protected_files_unchanged':len(protected),'prior_refinement_files_unchanged':len(before),'cer_computed':False,'browser':'미수행; 이전 안전 중단 재시도/우회 없음'}
 write('validation.json',summary);write('input-hashes.json',before)
 lines=['# 혼합 33개 섹션 대칭 점검 상태','', '검증 완료는 이번 정책·연결 일관성 점검의 범위다. 모든 행의 공식 채점은 미승인 상태다. 자동 1,063개는 규칙 재현과 교차 대조를 수행했으며 독립 의미 정답 대비 전수 정확도 검증은 아니다.','', '| 섹션 | 상태 | 근거 |','|---|---|---|']
 lines += [f'| {s["section_id"]} | {s["status"]} | {s["reason"]} |' for s in sections]
 (P/'section-status.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
 print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
