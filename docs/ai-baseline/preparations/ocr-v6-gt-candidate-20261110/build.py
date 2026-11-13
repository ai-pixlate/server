"""승인된 비활성화·영역 후보 생성. 실제 CER/GT 동결 없음."""
import json,sys,hashlib,copy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
from tools.baseline.ocr_scope import filter_section
P=Path(__file__).resolve().parent;B=ROOT/'docs/ai-baseline'
OLD=B/'preparations/ocr-v6-readiness-20261109'
def read(p):return json.loads(Path(p).read_text('utf8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,v):(P/n).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

def main():
 protected=read(OLD/'provenance.json')['immutable']
 for p,h in protected.items():assert sha(p)==h,p
 source=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl'
 original=[json.loads(x) for x in source.read_text('utf8').splitlines()];units=copy.deepcopy(original)
 changes=[]
 for u in units:
  for line in u['lines']:
   if u['id']=='GS-02_002__sec_1_01' and line['line_id'] in ('L005','L006'):
    line['active']=False
    line['change_history']=[dict(date='2026-11-10',reason='not_present_in_source',approval='user_chat_20261110',previous_text=line['text'])]
    changes.append(dict(section_id=u['id'],line_id=line['line_id'],text=line['text'],active=False))
   if u['id']=='GS-02_001__sec_1_03' and line['line_id']=='L045':
    before=line['text'];assert before=='상품앞 표시품목 : 부가세 면세품목'
    line['text']='상품앞 @표시품목 : 부가세 면세품목'
    line['change_history']=[dict(date='2026-11-10',reason='user_visual_confirmation_of_source_symbol',approval='user_chat_L045_at_20261110',previous_text=before)]
    changes.append(dict(section_id=u['id'],line_id=line['line_id'],before=before,after=line['text'],reviewer='user',evidence='원본 육안 검수로 @ 확인',freeze_approved=False))
 assert sum(len(u['lines']) for u in units)==1861
 diffs=[(a['id'],x['line_id']) for a,c in zip(original,units) for x,y in zip(a['lines'],c['lines']) if x['text']!=y['text']]
 assert diffs==[('GS-02_001__sec_1_03','L045')]
 assert [(u['id'],[x['line_id'] for x in u['lines']]) for u in units]==[(u['id'],[x['line_id'] for x in u['lines']]) for u in original]
 truth=P/'text_units.jsonl';truth.write_text(''.join(json.dumps(u,ensure_ascii=False)+'\n' for u in units),encoding='utf8');th=sha(truth)
 scopes=read(OLD/'fixed-regions.json');links={x['section_id']:x for x in read(OLD/'gt-region-links.json')}
 units={u['id']:u for u in units};idx={u['id']:u for u in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 fixed=['GS-01_002__sec_1_23','GS-03_005__sec_1_01','GS-03_014__sec_1_03']
 audits=[];status=[]
 for s in scopes:
  sid=s['id'];l=links[sid];s['scope_version']=l['scope_version']='source-scope-v6-candidate-20261110';l['truth_sha256']=th
  if sid=='GS-02_001__sec_1_03':
   l['lines']['L045']['text_sha256']=hashlib.sha256('상품앞 @표시품목 : 부가세 면세품목'.encode('utf8')).hexdigest()
  if sid in fixed:s['unresolved_connections']=sorted(set(s.get('unresolved_connections',[])+['user_fixed_undetermined_20261110']))
  def region(name,kind,rect,nums):
   s['regions'].append(dict(id=name,kind=kind,rect=rect,verified=True,reason='원본 설명과 로고/포장 공간 구분'))
   for n in nums:l['lines'][f'L{n:03}'].update(rect=rect,verified=True,anchor_kind='source_semantic_area_not_line_bbox')
  if sid=='GS-01_002__sec_1_04':
   s.update(regions=[],default='exclude',coverage_verified=True)
   region('title','include',[75,65,940,315],[1,2])
   for j,start in enumerate([3,10,17]):
    x=300*j
    region(f'badge{j}-description','include',[60+x,390,350+x,595],list(range(start,start+4)))
    region(f'badge{j}-left','include',[60+x,595,145+x,690],[])
    region(f'badge{j}-right','include',[250+x,595,350+x,690],[])
    region(f'badge{j}-caption','include',[145+x,641,250+x,690],[start+6])
    region(f'badge{j}-logo','exclude',[145+x,595,250+x,641],[start+4,start+5])
   region('results','include',[180,800,945,1310],list(range(24,33)))
   region('footnote','include',[245,1410,760,1460],[33])
   region('packaging','exclude',[0,1500,1000,2620],list(range(34,46)))
  if sid=='GS-02_002__sec_1_01':
   s.update(regions=[],default='exclude',coverage_verified=True)
   region('headline','include',[110,200,900,430],[2,3])
   region('description','include',[760,930,915,1100],[12,13])
   region('composition','include',[175,1890,860,2025],[34,35,36,37])
   region('plus','include',[480,1660,530,1720],[24])
   region('brand','exclude',[260,70,770,145],[1])
   region('print','exclude',[200,600,740,1250],[4,9,10,14,15,16])
   region('rank','exclude',[745,650,970,900],[7,8,11])
   region('small-left','exclude',[320,1430,440,1870],list(range(17,24)))
   region('small-right','exclude',[580,1590,675,1870],list(range(25,34)))
   for n in [5,6]:l['lines'][f'L{n:03}'].update(deactivation_evidence='user_chat_20261110',deactivation_reason='not_present_in_source',required_decision='exclude',policy_evidence='user_chat_20261110')
  ref=idx[sid]['output'];assert sha(ref['path'])==ref['sha256'];assert sha(s['image_path'])==s['image_sha256']
  raw=Path(ref['path']).read_text('utf8');d=json.loads(raw.splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:d=d[int(k)] if isinstance(d,list) else d[k]
  out=[]
  for n,r in enumerate(d['regions']):
   b=r['bbox'];out.append(dict(id=f"{n}:{r['region_key']}",text=r['text'],rect=[b['x'],b['y'],b['x']+b['w'],b['y']+b['h']],mixed=sid==fixed[0] and n==0))
  gt=[dict(id=x['line_id'],text=x['text'],active=x.get('active',True)) for x in units[sid]['lines']]
  result=filter_section(s,gt,out,image_sha256=s['image_sha256'],gt_sha256=th,gt_links=l)
  audits.append(dict(section_id=sid,**result));status.append(dict(section_id=sid,ready=result['ready'],applicability=result['applicability'],blockers=result['blockers']))
 plan=dict(version='ocr-v6-fixed-partial-candidate-20261110',frozen=False,input_version='original-112-no-crop',
  included=[x['section_id'] for x in status if x['ready'] and x['applicability']=='applicable'],not_applicable=[x['section_id'] for x in status if x['applicability']=='not_applicable'],undetermined=fixed,
  truth_version='ocr-text-v3-v6-candidate-20261110',truth_sha256=th,source_truth_sha256=sha(source),review_history_path=str(source.parent/'approval.json'),review_history_sha256=sha(source.parent/'approval.json'))
 plan['human_review_scope']='신규 100개 중 사용자 검수 47개·AI만 검수 53개, 기존 v1 12개 이력 승계'
 plan['candidate_revision']=2
 plan['additional_review']='2026-11-10 사용자 L045 기호 @ 확인; 기존 47/53 검수 범위 확대 아님; 최종 동결 승인 아님'
 plan['implementation_hashes']={str(p.relative_to(ROOT)):sha(p) for p in [ROOT/'tools/baseline/ocr_scope.py',ROOT/'tools/baseline/ocr_v6.py',ROOT/'tools/baseline/ocr_v5.py',ROOT/'tools/baseline/ocr.py',B/'protocols/v1/ocr-v6-scope-draft.md']}
 for n,v in [('fixed-regions.json',scopes),('gt-region-links.json',list(links.values())),('filter-audit.json',audits),('section-status.json',status),('evaluation-plan.json',plan),('changes.json',changes)]:write(n,v)
 for p,h in protected.items():assert sha(p)==h,p
 write('validation.json',dict(total=112,ready=sum(x['ready'] for x in status),blocked=[x for x in status if not x['ready']],line_count=1861,inactive=2,frozen=False,cer_called=False,protected_files=len(protected),files={p.name:sha(p) for p in P.iterdir() if p.is_file() and p.name!='validation.json'}))
 print('ready',sum(x['ready'] for x in status));print([(x['section_id'],x['blockers']) for x in status if not x['ready']])
if __name__=='__main__':main()
