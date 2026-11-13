"""남은 연결과 채점 준비 감사. CER/모델 실행 없음."""
import json, hashlib, sys
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
from tools.baseline.ocr_scope import filter_section,v5_inputs
P=Path(__file__).resolve().parent;B=ROOT/'docs/ai-baseline'
OLD=B/'preparations/ocr-v6-connection-repair-20261109'
def read(p):return json.loads(Path(p).read_text('utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,x):(P/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
GROUPS={
 'GS-02_008__sec_1_04':[
  ('I-title','include',[40,140,790,325],[1,2]),
  ('I-color','include',[495,450,710,545],[3,4]),
  ('E-label','exclude',[130,625,740,775],list(range(5,13))),
  ('I-price','include',[60,790,750,975],[13,14,15]),
  ('I-body','include',[35,1060,790,1170],[16,17]),
  ('I-study','include',[35,1260,790,1405],[18,19]),
  ('E-reference','exclude',[30,1440,800,2040],[20]),
  ('E-paperheading','exclude',[100,2130,730,2255],[21,22,23,24]),
  ('I-quote1','include',[50,2265,385,2365],[25,26]),
  ('I-quote2','include',[405,2400,780,2505],[27,28]),
  ('I-quote3','include',[88,2605,440,2705],[29,30]),
  ('I-foot','include',[40,2890,800,2940],[31])],
 'GS-03_018__sec_1_08':[
  ('I-title','include',[100,250,900,795],[1,2,3,4]),
  ('I-mist','include',[330,930,575,985],[5]),
  ('I-wash','include',[725,975,900,1070],[6,7]),
  ('E-mist','exclude',[370,1000,720,1420],list(range(8,15))),
  ('I-english','include',[105,1140,325,1260],[15,16,17,18]),
  ('E-wash','exclude',[600,1180,955,1725],list(range(19,27))),
  ('I-lotion','include',[80,1330,310,1425],[27,28]),
  ('E-lotion','exclude',[170,1430,600,1725],list(range(29,38))),
  ('I-levels','include',[70,1840,940,1905],[38,39,40]),
  ('E-mini1','exclude',[130,1960,310,2315],list(range(41,49))),
  ('I-mini1','include',[120,2325,320,2410],[49,50]),
  ('E-mini2','exclude',[420,1960,600,2315],list(range(51,60))),
  ('I-mini2','include',[395,2325,625,2410],[60,61]),
  ('E-mini3','exclude',[735,1960,835,2315],list(range(62,69))),
  ('I-mini3','include',[690,2325,880,2410],[69,70]),
  ('I-metrics','include',[105,2435,900,2675],list(range(71,79))),
  ('I-foot','include',[100,2780,900,2890],[79,80,81])],
 'GS-02_001__sec_1_03':[
  ('I-instructions','include',[75,85,760,505],list(range(1,14))),
  ('I-phone-top','include',[285,630,700,740],[14,15,16]),
  ('I-phone-order','include',[390,755,710,855],[17,18,19,20]),
  ('I-phone-bottom','include',[285,875,710,920],[21,22]),
  ('I-offline','include',[250,1000,750,1210],[23,24,25,26,27]),
  ('I-receipts','include',[255,1230,740,1445],list(range(28,53))),
  ('I-submit','include',[75,1580,760,1810],list(range(53,58))),
  ('I-menu','include',[420,1840,780,2055],[58,59,60,61,63,64,65]),
  ('I-qr-caption','include',[250,2005,420,2050],[62]),
  ('I-dates','include',[60,2160,790,2450],list(range(66,74)))],
}

def main():
 previous=read(OLD/'section-status.json');prov=read(OLD/'provenance.json')
 immutable={**prov['protected'],**prov['sources'],**{str(p):sha(p) for p in OLD.iterdir() if p.is_file()}}
 for p,h in immutable.items():assert sha(p)==h,p
 scopes=read(OLD/'fixed-regions.json');links={x['section_id']:x for x in read(OLD/'gt-region-links.json')}
 tp=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl';th=sha(tp)
 units={x['id']:x for x in map(json.loads,tp.read_text('utf8').splitlines())}
 idx={x['id']:x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 rows=[];audits=[]
 for s in scopes:
  sid=s['id'];l=links[sid];s['scope_version']=l['scope_version']='source-scope-v6-readiness-20261109-3'
  if sid in GROUPS:
   s.update(section_type='mixed',default='exclude',coverage_verified=True,regions=[])
   seen=set()
   for rid,kind,rect,nums in GROUPS[sid]:
    s['regions'].append(dict(id=rid,kind=kind,rect=rect,verified=True,reason='원본 직접 대조: 일반 설명/인용문과 포장·사진·문서 내부를 구분'))
    for n in nums:
     lid=f'L{n:03d}';assert lid not in seen;seen.add(lid)
     l['lines'][lid].update(rect=rect,verified=True,anchor_kind='source_semantic_area_not_line_bbox',reason='전사 변경 없이 원본 의미 영역에 연결')
   assert seen==set(l['lines'])
  ref=idx[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256']
  d=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:d=d[int(k)] if isinstance(d,list) else d[k]
  out=[]
  for n,r in enumerate(d['regions']):
   b=r.get('bbox');out.append(dict(id=f"{n}:{r['region_key']}",text=r['text'],rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']] if b else None,mixed=sid=='GS-01_002__sec_1_23' and r['region_key']=='reg_0001'))
  gt=[dict(id=x['line_id'],text=x['text']) for x in units[sid]['lines']]
  r=filter_section(s,gt,out,image_sha256=s['image_sha256'],gt_sha256=th,gt_links=l)
  assert len(r['audit'])==len(gt)+len(out)
  for kind,original in [('gt',gt),('ocr',out)]:assert [(x['id'],x['text']) for x in r['audit'] if x['kind']==kind]==[(x['id'],x['text']) for x in original]
  if r['ready'] and r['applicability']=='applicable':v5_inputs(r)
  rows.append(dict(section_id=sid,status='검증 완료' if r['ready'] else '채점 차단',filter_ready=r['ready'],blockers=r['blockers'],gt_completeness='not_certified',official_scoring_authorized=False))
  audits.append(dict(section_id=sid,**r))
 assert {x['section_id'] for x in rows if x['filter_ready']} >= {x['section_id'] for x in previous if x['filter_ready']}
 for p,h in immutable.items():assert sha(p)==h,p
 summary=dict(before=dict(Counter(x['status'] for x in previous)),after=dict(Counter(x['status'] for x in rows)),newly_ready=[x['section_id'] for x in rows if x['filter_ready'] and not next(y for y in previous if y['section_id']==x['section_id'])['filter_ready']],protected_files=len(prov['protected']),source_hashes=len(prov['sources']),cer_called=False,gt_changed=False)
 for n,v in [('fixed-regions.json',scopes),('gt-region-links.json',list(links.values())),('filter-audit.json',audits),('section-status.json',rows),('validation.json',summary),('source-groups.json',GROUPS),('provenance.json',dict(immutable=immutable,filter_sha256=sha(ROOT/'tools/baseline/ocr_scope.py')))]:write(n,v)
 print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
