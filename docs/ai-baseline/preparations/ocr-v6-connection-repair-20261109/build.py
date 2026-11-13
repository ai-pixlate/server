"""기존 30개 차단을 재검증한다. 원문/동결본/점수는 수정하지 않는다."""
from pathlib import Path
import json, hashlib, sys
from collections import Counter
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from tools.baseline.ocr_scope import filter_section, v5_inputs
P=Path(__file__).resolve().parent
B=ROOT/'docs/ai-baseline'
OLD=B/'preparations/ocr-v6-policy-application-20261109'
VERSION='source-scope-v6-connections-20261109-2'
def read(p):return json.loads(Path(p).read_text('utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,x):(P/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

# 원본의 설명 블록/콜아웃 경계를 직접 대조한 좌표. 출력 문자열에 맞춘 분할 없음.
REPAIRS={
 'GS-01_002__sec_1_18':{'T1':[0,0,1000,620]},
 'GS-01_002__sec_1_21':{'T2':[335,150,435,230],'T3':[755,150,850,230]},
 'GS-02_008__sec_1_02':{'T4':[40,1065,420,1100]},
 'GS-03_016__sec_1_01':{'T1':[430,190,950,403]},
 'GS-03_017__sec_1_01':{'T1':[430,0,950,60]},
 'GS-02_009__sec_1_05':{'T2':[505,695,690,850],'T3':[505,945,690,1100],'T4':[505,1200,690,1360]},
 'GS-02_009__sec_1_06':{'T3':[270,910,465,1010],'T4':[460,1160,690,1270],'T5':[100,1200,320,1305]},
 'GS-02_014__sec_1_08':{'T1':[0,0,830,285],'T2':[0,770,830,1538]},
 'GS-03_004__sec_1_01':{'T2':[0,550,1000,1760]},
 'GS-03_002__sec_1_05':{'T3':[595,1630,900,1755]},
 'GS-02_007__sec_1_02':{'T2':[340,810,505,895],'T4':[535,990,695,1085],'T5':[580,1090,720,1170],'T6':[585,1180,745,1270],'T8':[545,1360,710,1455],'T9':[480,1450,650,1540],'T10':[375,1545,580,1640]},
 'GS-02_009__sec_1_02':{'T2':[40,710,790,900],'T3':[40,1240,790,1430],'T4':[40,1770,790,1960],'T5':[40,2300,790,2490]},
 'GS-02_010__sec_1_02':{'T2':[0,900,830,980],'T3':[0,1290,830,1375],'T4':[0,1480,830,2157]},
}
REASONS={
 'GS-01_002__sec_1_18':'상단 선케어 설명의 영문 마지막 줄까지 포함. y=670 이후 용기 사진과 분리.',
 'GS-01_002__sec_1_21':'휴대용 스틱형의 실제 글자 블록만 포함. 왼쪽 용기 인쇄와 원형 배지 여백을 동일 의미 영역으로 취급하지 않음.',
 'GS-02_008__sec_1_02':'기존 포함 정책의 현미경 수치 주석 전체 높이 연결. 사진 내부 비텍스트 부분과는 분리. 새 제외 정책 없음.',
 'GS-03_016__sec_1_01':'절단된 HOW TO USE 전경 제목 전체 너비 포함. 배경 장식 및 좌하단 사진은 제외 유지.',
 'GS-03_017__sec_1_01':'섹션 상단에 잘려 남은 TO USE 전경 문구도 대상. 오인식 출력 자체는 보존.',
 'GS-02_009__sec_1_05':'세 원형 도해의 별도 효능·수치·증가 설명을 온전히 포함. 원형이라는 이유로 인증 로고 제외하지 않음.',
 'GS-02_009__sec_1_06':'얼굴 사진 위 별도 피부각도 설명과 수치를 포함. 얼굴 전체로 포함 영역을 확대하지 않음.',
 'GS-02_014__sec_1_08':'상단 각주 및 하단 추천 설명 전체 포함. 중간 용기 인쇄 제외 유지.',
 'GS-03_004__sec_1_01':'피부 사진 아래 사용 전/직후/24시간 후의 캡션 전체 포함. 피부 사진은 제외.',
 'GS-03_002__sec_1_05':'하단 24H 내추럴톤업 및 각주의 원본 영역만 포함. 왼쪽 위 포장 TXA-MA 인쇄와 분리.',
 'GS-02_007__sec_1_02':'얼굴 사진 위 별도 9개 효능 콜아웃의 제목·수치 연결. 얼굴 전체를 포함하지 않음.',
 'GS-02_009__sec_1_02':'사진 아래 고객 후기 카드의 텍스트 블록 시작점을 보정. 후기 원문과 고객 표시 모두 포함.',
 'GS-02_010__sec_1_02':'미도포/도포 캡션 띠와 그래프 제목은 별도 시험 결과 설명이므로 포함. 사진의 검은 비텍스트 점은 제외 유지.',
}

# 별도 설명과 사진 인쇄를 구별한 원본 의미 영역. GT 줄의 정밀 bbox가 아니다.
ADDITIONS={
 'GS-02_014__sec_1_11':[
  ('I-note1','include',[535,135,755,215],[1]),
  ('I-note2','include',[125,345,225,415],[2]),
  ('I-note3','include',[435,380,590,490],[3]),
  ('E-label','exclude',[150,220,500,340],list(range(4,14))),
  ('I-body','include',[25,780,805,1880],list(range(14,27)))],
 'GS-03_002__sec_1_06':[
  ('I-head','include',[80,60,920,365],[1,2,3,4]),
  ('I-weeks','include',[345,440,945,590],[5]),
  ('I-call1','include',[600,1180,920,1345],[6,7]),
  ('I-call2','include',[620,1360,920,1520],[8,9]),
  ('E-package','exclude',[50,1450,550,1880],list(range(10,19))),
  ('I-testhead','include',[80,1900,950,1990],[19]),
  ('I-captions','include',[80,2410,950,2485],[20,21]),
  ('I-foot','include',[80,2500,950,2590],[22,23])],
 'GS-02_014__sec_1_09':[
  ('I-title','include',[150,190,690,370],[1,2]),
  ('I-q1','include',[190,490,775,610],[3]),
  ('I-a1','include',[55,665,640,875],[4,5,6,7]),
  ('I-q2','include',[190,945,775,1115],[9,10]),
  ('I-a2','include',[55,1150,640,1470],[11,12,13,14,15]),
  ('I-q3','include',[190,1545,775,1720],[16,17]),
  ('I-a3','include',[55,1755,640,2080],[18,19,20,21,22]),
  ('I-q4','include',[190,2150,775,2290],[23]),
  ('I-a4','include',[55,2310,640,2535],[24,25,26]),
  ('I-q5','include',[190,2625,775,2760],[27]),
  ('I-a5','include',[55,2790,640,3125],[28,29,30,31,32])],
 'GS-03_002__sec_1_01':[
  ('E-logo','exclude',[370,150,650,250],[1]),
  ('I-head','include',[65,260,945,710],[2,3,4,5]),
  ('E-background','exclude',[0,735,1000,1300],[6]),
  ('E-product','exclude',[720,1530,1000,2060],list(range(7,15))),
  ('I-english','include',[75,1890,325,2030],[15,16,17,18]),
  ('I-benefits','include',[70,2370,950,2495],[19,20,21,22,24,25]),
  ('E-clock','exclude',[760,2180,950,2330],[23]),
  ('I-foot','include',[100,2510,930,2600],[26,27])],
 'GS-03_002__sec_1_04':[
  ('I-solution','include',[380,115,615,185],[1]),
  ('E-logo','exclude',[360,240,630,325],[2]),
  ('I-head','include',[90,365,925,700],[3,4,5]),
  ('I-tone','include',[350,850,610,935],[6]),
  ('I-body','include',[110,980,480,1100],[7,8]),
  ('E-products-badges','exclude',[40,1110,980,2190],list(range(9,42))),
  ('I-results','include',[180,2250,840,2630],list(range(42,47))),
  ('I-foot','include',[210,2650,800,2740],[47,48])],
 'GS-02_014__sec_1_06':[
  ('I-head','include',[40,60,790,350],[1,2,3]),
  ('I-title1','include',[40,440,500,500],[4]),
  ('I-badge1','include',[610,460,760,600],[5,6]),
  ('I-caption1','include',[40,870,790,950],[7,8]),
  ('I-title2','include',[40,1040,600,1115],[9]),
  ('I-badge2','include',[610,1070,760,1210],[10,11]),
  ('I-caption2','include',[40,1460,790,1545],[12,13]),
  ('I-metrics','include',[40,1570,790,1720],[14,15,16,17]),
  ('I-title3','include',[40,1910,500,1970],[18]),
  ('I-badge3','include',[610,1930,760,2080],[19,20]),
  ('I-angle1','include',[175,2240,290,2290],[21]),
  ('I-angle2','include',[500,2235,640,2300],[22]),
  ('I-caption3','include',[40,2330,790,2415],[23,24]),
  ('I-foot','include',[40,2480,790,2590],[25,26,27])],
}

def main():
 protected=read(B/'analyses/ocr-scope-refinement-20261109/source-hashes.json')['protected']
 sources=read(OLD/'provenance.json')['source_hashes']
 snapshots={str(p):sha(p) for folder in [OLD,B/'preparations/ocr-v6-review-ui-20261109'] for p in folder.iterdir() if p.is_file()}
 for p,h in {**protected,**sources}.items():assert sha(p)==h,p
 scopes=read(OLD/'fixed-regions.json');links={x['section_id']:x for x in read(OLD/'gt-region-links.json')}
 previous={x['section_id']:x for x in read(OLD/'section-status.json')}
 units_path=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl';th=sha(units_path)
 units={x['id']:x for x in map(json.loads,units_path.read_text('utf8').splitlines())}
 inputs={x['id']:x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 changes=[];rows=[];audits=[];causes=[]
 for s in scopes:
  sid=s['id'];l=links[sid];s['scope_version']=l['scope_version']=VERSION
  if not previous[sid]['filter_ready']:
   bs=previous[sid]['blockers']
   cause=('혼합 OCR 문자열 및 GT 연결' if sid=='GS-01_002__sec_1_23' else '배경·전경 겹침' if sid=='GS-03_014__sec_1_03' else '과대 OCR 사각형·각주 교차' if sid=='GS-03_005__sec_1_01' else 'GT 위치·영역 연결 부족' if any('gt_location_unverified' in b for b in bs) else 'OCR 경계 교차')
   causes.append(dict(section_id=sid,primary_cause=cause,previous_blockers=bs))
  for region in s['regions']:
   if region['id'] in REPAIRS.get(sid,{}):
    old=region['rect'];new=REPAIRS[sid][region['id']]
    for loc in l['lines'].values():
     if loc.get('rect')==old:loc.update(rect=new,reason=REASONS[sid],anchor_kind='source_semantic_area_not_line_bbox')
    region.update(rect=new,reason=REASONS[sid])
    changes.append(dict(section_id=sid,region_id=region['id'],before=old,after=new,reason=REASONS[sid],image_sha256=s['image_sha256']))
  if sid in ADDITIONS:
   s.update(section_type='mixed',default='exclude',coverage_verified=True,regions=[])
   for rid,kind,rect,nums in ADDITIONS[sid]:
    s['regions'].append(dict(id=rid,kind=kind,rect=rect,verified=True,reason='원본에서 별도 설명/콜아웃과 용기 인쇄를 구분'))
    for n in nums:l['lines'][f'L{n:03d}'].update(verified=True,rect=rect,reason='원본 직접 대조 의미 영역 연결; 전사 원문 및 순서 불변',anchor_kind='source_semantic_area_not_line_bbox')
   changes.append(dict(section_id=sid,groups=ADDITIONS[sid],image_sha256=s['image_sha256'],reason='원본 직접 확인; 문구를 추가하거나 수정하지 않음'))
  if sid in ['GS-01_001__sec_1_01','GS-01_002__sec_1_01','GS-01_002__sec_1_15']:
   if sid=='GS-01_001__sec_1_01':
    groups=[('I-top','include',[100,90,900,380],list(range(1,5))),('E-package','exclude',[120,430,880,900],list(range(5,60))),('I-captions','include',[180,910,820,970],[60,61]),('I-bottom','include',[150,1040,850,1200],[62,63,64]),('E-logo','exclude',[410,1220,610,1300],[65])]
   elif sid=='GS-01_002__sec_1_01':
    groups=[('I-top','include',[200,110,780,620],[1,2,3,4]),('I-fresh','include',[815,735,925,845],[6,7]),('E-product','exclude',[300,940,700,1850],[8,9,12,13,14,15,16]),('E-certified','exclude',[765,880,980,1135],[10,11])]
    l['lines']['L005'].update(required_decision='exclude',policy_evidence='ocr-v6-policy-followup-20261109: D02 사용자 빈 GT 제외',verified=False,rect=None)
   else:
    groups=[('I-head','include',[170,280,820,790],[1,2,3,4]),('I-body','include',[60,840,960,1030],[5,6,7]),('I-reef','include',[380,1560,620,1630],[8]),('I-close','include',[170,1680,830,1830],[9,10])]
   s.update(section_type='mixed',default='exclude',coverage_verified=True,regions=[])
   for rid,kind,rect,line_nums in groups:
    s['regions'].append(dict(id=rid,kind=kind,rect=rect,verified=True,reason='원본 직접 대조: 별도 설명 포함, 포장·로고·배경 제외'))
    for n in line_nums:l['lines'][f'L{n:03d}'].update(verified=True,rect=rect,reason='원본에서 해당 의미 영역의 문구 대조; GT 전사와 순서 불변',anchor_kind='source_semantic_area_not_line_bbox')
   changes.append(dict(section_id=sid,reason='원본의 모든 GT 줄을 포함 설명 또는 제외 포장/로고에 연결. Reef Safe 타원은 일반 설명.',image_sha256=s['image_sha256'],groups=groups))
  ref=inputs[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256']
  payload=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:payload=payload[int(k)] if isinstance(payload,list) else payload[k]
  out=[]
  for n,r in enumerate(payload['regions']):
   b=r.get('bbox');out.append(dict(id=f"{n}:{r['region_key']}",text=r['text'],rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']] if b else None,mixed=sid=='GS-01_002__sec_1_23' and r['region_key']=='reg_0001'))
  gt=[dict(id=x['line_id'],text=x['text']) for x in units[sid]['lines']]
  r=filter_section(s,gt,out,image_sha256=s['image_sha256'],gt_sha256=th,gt_links=l)
  assert len(r['audit'])==len(gt)+len(out)
  assert [x['text'] for x in r['audit'] if x['kind']=='gt']==[x['text'] for x in gt]
  assert [x['text'] for x in r['audit'] if x['kind']=='ocr']==[x['text'] for x in out]
  if r['ready'] and r['applicability']=='applicable':v5_inputs(r)
  rows.append(dict(section_id=sid,status='검증 완료' if r['ready'] else '기술적 연결 차단',filter_ready=r['ready'],policy_user_pending=False,official_scoring_authorized=False,blockers=r['blockers']))
  audits.append(dict(section_id=sid,**r))
 for p,h in {**protected,**sources,**snapshots}.items():assert sha(p)==h,p
 assert {x['section_id'] for x in rows if x['filter_ready']} >= {sid for sid,x in previous.items() if x['filter_ready']}
 oldaudit={x['section_id']:x for x in read(OLD/'filter-audit.json')};transitions=[]
 for row in audits:
  old={(x['kind'],x['id']):x for x in oldaudit[row['section_id']]['audit']}
  for a in row['audit']:
   prev=old[a['kind'],a['id']]
   if prev['decision']!=a['decision']:transitions.append(dict(section_id=row['section_id'],**a,before=prev['decision']))
 summary=dict(before=dict(Counter(x['status'] for x in previous.values())),after=dict(Counter(x['status'] for x in rows)),primary_causes=dict(Counter(x['primary_cause'] for x in causes)),newly_ready=[x['section_id'] for x in rows if x['filter_ready'] and not previous[x['section_id']]['filter_ready']],protected_unchanged=len(protected),source_hashes_verified=len(sources),prior_preparation_files_unchanged=len(snapshots),cer_called=False)
 for name,value in [('fixed-regions.json',scopes),('gt-region-links.json',list(links.values())),('section-status.json',rows),('filter-audit.json',audits),('source-repairs.json',changes),('cause-classification.json',causes),('decision-transitions.json',transitions),('validation.json',summary)]:write(name,value)
 write('provenance.json',dict(protected=protected,sources=sources,previous_files=snapshots,filter_sha256=sha(ROOT/'tools/baseline/ocr_scope.py')))
 print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
