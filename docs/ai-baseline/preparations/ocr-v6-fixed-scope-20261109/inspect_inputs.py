"""112개 입력의 고정 범위 등록·비채점 필터 점검. 입력 파일에는 쓰지 않음."""
from pathlib import Path
import sys,json,hashlib
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
from tools.baseline.ocr_scope import filter_section
P=Path(__file__).resolve().parent;B=ROOT/'docs/ai-baseline'
def read(p):return json.loads(Path(p).read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,d):(P/n).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def region(id,kind,rect,reason,overrides=()):return dict(id=id,kind=kind,rect=rect,reason=reason,verified=True,overrides=list(overrides))

def main():
 protected=read(B/'analyses/ocr-scope-refinement-20261109/source-hashes.json')['protected'];assert all(sha(p)==h for p,h in protected.items())
 truth=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl';th=sha(truth)
 units=[json.loads(x) for x in truth.read_text(encoding='utf8').splitlines()]
 prior={x['id']:x for x in read(B/'analyses/ocr-scope-refinement-20261109/scope-annotations.json')['sections']}
 idx={x['id']:x for x in read(B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json')['sections']}
 all_scopes=[];all_links=[];reports=[];audits=[];document_audit=[]
 for u in units:
  sid=u['id'];im=u['image'];ip=Path(im['path']);ip=ip if ip.is_absolute() else ROOT.parents[1]/ip;assert sha(ip)==im['sha256']
  scope={'id':sid,'scope_version':'source-scope-v6-candidate-20261109-1','policy_version':'ocr-v6-user-policy-20261109-final','image_sha256':im['sha256'],'image_path':str(ip),'width':im['width'],'height':im['height'],'default':'unknown','coverage_verified':False,'regions':[]}
  for z in prior[sid]['regions']:
   if z['id']=='I0':continue
   x,y,xx,yy=z['rect_percent'];scope['regions'].append(dict(id=z['id'],kind=z['kind'],rect=[x*im['width']/100,y*im['height']/100,xx*im['width']/100,yy*im['height']/100],reason=z['meaning'],verified=False))
  links={'truth_sha256':th,'scope_version':scope['scope_version'],'lines':{l['line_id']:{'verified':False,'rect':None,'reason':'텍스트 전용 GT; 검증된 원본 위치 연결 없음'} for l in u['lines']}}
  if sid=='GS-02_008__sec_1_05':
   scope.update(default='exclude',coverage_verified=True,regions=[
    region('T1','include',[170,125,750,520],'상단 상세페이지 설명'),
    region('T2','include',[170,650,730,780],'현미경 이미지 위 별도 설명'),
    region('T3','include',[90,1155,738,1185],'현미경 이미지 아래 WD 수치'),
    region('T4','include',[85,1385,470,1535],'특허증 옆 별도 설명'),
    region('T5','include',[40,1585,230,1635],'하단 원료 각주'),
    region('DOC','exclude',[485,1385,685,1570],'특허증 내부')])
   for l in u['lines']:
    n=int(l['line_id'][1:]);key='T1' if n<=4 else 'T2' if n<=6 else 'T3' if n<=9 else 'T4' if n<=12 else 'DOC' if n<=14 else 'T5'
    links['lines'][l['line_id']]={'verified':True,'rect':next(r['rect'] for r in scope['regions'] if r['id']==key),'anchor_region':key,'reason':'원본 설명/문서 의미 영역 연결; 줄 bbox 정확도 평가 아님'}
  if sid=='GS-03_014__sec_1_01':
   scope.update(default='exclude',coverage_verified=True,regions=[
    region('T1','include',[85,205,950,655],'상단 제목·효능 설명'),
    region('T2','include',[85,675,915,724],'보고서 위 별도 영문 제목과 +'),
    region('DOC1','exclude',[85,724,465,1239],'왼쪽 시험성적서'),
    region('DOC2','exclude',[539,724,920,1239],'오른쪽 시험성적서'),
    region('QUOTE1','include',[53,1025,495,1123],'왼쪽 별도 확대 인용문',['DOC1']),
    region('QUOTE2','include',[510,1025,945,1123],'오른쪽 별도 확대 인용문',['DOC2'])])
   for l in u['lines']:
    n=int(l['line_id'][1:]);key='T1' if n<=6 else 'T2' if n<=10 else 'DOC1' if n<=24 else 'DOC2' if n==25 else 'QUOTE1' if n<=29 else 'QUOTE2' if n<=33 else 'DOC2'
    # 내부 문구 앵커는 인용 창과 겹치지 않는 원본 본문 구간이다.
    rect=[90,730,460,1000] if key=='DOC1' else [545,730,915,1000] if key=='DOC2' else next(r['rect'] for r in scope['regions'] if r['id']==key)
    links['lines'][l['line_id']]={'verified':True,'rect':rect,'anchor_region':key,'reason':'원본의 문서 내부/별도 인용 구분; 전사 수정 없음'}
  ref=idx[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256'];d=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:d=d[int(k)] if isinstance(d,list) else d[k]
  output=[]
  for n,r in enumerate(d['regions']):
   box=r.get('bbox');rect=[box['x'],box['y'],box['x']+box['w'],box['y']+box['h']] if box else None
   output.append({'id':str(n)+':'+r['region_key'],'text':r['text'],'rect':rect,'mixed':sid=='GS-01_002__sec_1_23' and r['region_key']=='reg_0001'})
  for l in u['lines']:links['lines'][l['line_id']]['text_sha256']=hashlib.sha256(l['text'].encode('utf8')).hexdigest()
  result=filter_section(scope,[{'id':l['line_id'],'text':l['text']} for l in u['lines']],output,image_sha256=im['sha256'],gt_sha256=th,gt_links=links)
  counts={k:sum(a['decision']==k for a in result['audit'] if a['kind']=='ocr') for k in ['include','exclude','hold']}
  status='검증 완료' if result['ready'] else '사용자 확인 필요' if not scope['coverage_verified'] else '채점 차단'
  if sid=='GS-01_002__sec_1_23':status='채점 차단'
  reports.append({'section_id':sid,'status':status,'filter_ready':result['ready'],'official_scoring_authorized':False,'ocr_counts':counts,'blockers':result['blockers'],'input_sha256':im['sha256'],'output_sha256':ref['sha256']})
  audits.append(dict(section_id=sid,**result));all_scopes.append(scope);all_links.append({'section_id':sid,**links})
  if sid in ['GS-02_008__sec_1_05','GS-03_014__sec_1_01']:
   for a in result['audit']:
    if a['kind']=='ocr':document_audit.append(dict(section_id=sid,**a))
 assert len(units)==112 and len(document_audit)==86
 write('fixed-regions.json',all_scopes);write('gt-region-links.json',all_links);write('filter-audit.json',audits);write('section-status.json',reports);write('document-86-audit.json',document_audit)
 assert all(sha(p)==h for p,h in protected.items())
 from collections import Counter
 summary={'sections':dict(Counter(r['status'] for r in reports)),'document_86':dict(Counter(r['decision'] for r in document_audit)),'protected_files_unchanged':len(protected),'cer_called':False,'gt_modified':False,'scope_sha256':sha(P/'fixed-regions.json'),'filter_sha256':sha(ROOT/'tools/baseline/ocr_scope.py')}
 write('validation.json',summary);print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
