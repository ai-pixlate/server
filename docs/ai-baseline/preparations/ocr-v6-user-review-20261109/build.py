"""기존 검토 화면의 SVG·원문 비교 구조를 재사용하는 별도 검수 패키지 생성."""
from pathlib import Path
import json,hashlib
ROOT=Path(__file__).resolve().parents[4];B=ROOT/'docs/ai-baseline';P=Path(__file__).resolve().parent
def read(p):return json.loads(Path(p).read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(n,d):(P/n).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 truth=B/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl'
 units={x['id']:x for x in map(json.loads,truth.read_text(encoding='utf8').splitlines())}
 index=B/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json';idx={r['id']:r for r in read(index)['sections']}
 prior=B/'analyses/ocr-scope-refinement-20261109/sections.json';sections={s['id']:s for s in read(prior)}
 protected=read(B/'analyses/ocr-scope-refinement-20261109/source-hashes.json')['protected'];assert all(sha(p)==h for p,h in protected.items())
 seeds=[
 ('D01','GS-01_001__sec_1_01',['L054'],None,[70,49,79,56],'GT + 위치 판정','제외 권고 / 위치 확인 필요','패키지 내부 단독 + 후보. 정확한 GT 위치 근거가 없어 후보 영역으로 표시한다. 상단 1+1과 혼동하지 말 것.'),
 ('D02','GS-01_002__sec_1_01',['L005'],None,None,'빈 GT 위치 확인','보류 권고','원본 초안에 위치 앵커 없음. 임의 bbox를 만들지 않는다. 빈 정답의 원래 의도와 물리적 위치를 확인해 달라.'),
 ('D03','GS-02_002__sec_1_01',['L005'],None,[63.8,33.15,80.7,42.75],'좌표형 GT 1','보류 / 새 GT 교정 권고','기록된 원본 GT bbox를 사용한다. 텍스트가 좌표 메타데이터로 보이며 원본에 같은 숫자 문구가 없다. 단순 범위 제외와 GT 교정을 구분한다.'),
 ('D04','GS-02_002__sec_1_01',['L006'],None,[59.5,32.2,78,41.9],'좌표형 GT 2','보류 / 새 GT 교정 권고','D03과 다른 GT 줄. 삭제/교정 후보이며 실제 수정은 하지 않는다.'),
 ('D05','GS-01_002__sec_1_06',[],'reg_0001',None,'OCR o 범위 귀속','포함(삽입 후보) 권고 / 결정 전 보류','넓은 OCR 영역이 얼굴과 용기 모서리를 함께 덮는다. 실제 문자가 온 위치를 확정할 수 없으므로 GT 불일치만으로 제외하지 않는다.'),
 ('D06','GS-01_002__sec_1_23',['L001','L002'],'reg_0001',None,'슬로건+로고 결합 OCR','보류 권고','Good all은 일반 문구, goodal은 로고. 반환값 전체 포함/제외 모두 비대칭 위험. 문자열 분리 금지; 공통 처리 정책 메모 필요.'),
 ('D07','GS-03_018__sec_1_08',[],'reg_0044',None,'용기 인쇄 반사상','제외 권고 / 사용자 확인 필요','바닥에 비친 용기 인쇄. 기존 include 자동 분류와 별도 보류 기록을 구분한다. GT 추가 후보가 아니다.'),
 ('G01','GS-02_008__sec_1_05',['L010','L011','L012','L013','L014'],None,[59,82.5,82,93],'특허증 보완 후보','기존 포함 정책 유지 / 후보 검토','현재 L013·014는 제목만 있고 증서 내부 필드가 빠져 있다. 판독 불가 부분을 추측하거나 빈 GT로 승인하지 않는다.'),
 ('G02','GS-03_014__sec_1_01',[],None,[54,58,92,100],'시험성적서 보완 후보','기존 포함 정책 유지 / 후보 검토','왼쪽 결론은 L011–024에 이미 있다. 오른쪽 표·하단 반복 결론·기관 정보와 기존 전사를 대조한다. 두 보고서 전체가 미전사라는 이전 요약은 부정확하다.'),
 ]
 candidates={
 'D03':[{'existing':'313, 638, 404, 807','candidate':'새 GT 버전에서 이 메타데이터형 줄 삭제 또는 원본 위치 재연결 검토','uncertain':'올바른 교정값은 미확정. 포장 글자를 OCR 출력에서 복사하지 않음'}],
 'D04':[{'existing':'304, 595, 396, 780','candidate':'새 GT 버전에서 이 메타데이터형 줄 삭제 또는 원본 위치 재연결 검토','uncertain':'올바른 교정값은 미확정'}],
 'G01':[{'existing':'L013 특허증 / L014 Certificate of Patent','candidate':'제목 유지. 특허번호·출원번호·출원일·등록일·발명 명칭·권리자/발명자·하단 문구의 누락 검토','uncertain':'번호·날짜·성명은 이 해상도에서 확정 불가. 별도 설명의 10-2270816을 증서 전사로 대신 사용하지 않음'},
 {'existing':'증서 내부 필드 별도 줄 없음','candidate':'판독 가능한 필드명 후보: 특허 / 특허증. 전체 줄 후보는 고해상도 원본 확인 후 작성','uncertain':'이 목록은 완성 전사 아님; 더 선명한 원본 필요 여부 사용자 확인'}],
 'G02':[{'existing':'L011–024 왼쪽 결론, L025 오른쪽 제목, L030–033 별도 인용문 존재','candidate':'기존 문구 유지. 오른쪽 표 행: 시험제목 및 시험 제품 / 시험목적 / 시험의뢰기관 / 연구기관 / 시험물질관리책임자 / 연구원의 구성 / 결과의 누락 확인','uncertain':'표의 작은 값·주소·인명·날짜 및 인용문에 가린 행은 추측 금지'},
 {'existing':'L018 대입한 뒤 최석배수를 곱하였다.','candidate':'대입한 뒤 희석배수를 곱하였다. (원본 대조용 교정 후보)','uncertain':'사용자가 원본 작은 글씨를 재확인해야 하며 아직 적용 안 됨'},
 {'existing':'하단 결과 표의 반복 문구 별도 줄 없음','candidate':'하단 결과 표 문구가 별도 인용문과 반복돼도 원본에 두 번 있으면 횟수 보존 검토','uncertain':'반복 횟수와 보이는 범위 확인 필요. OCR 텍스트로 채우지 않음'}]}
 items=[];hashes={str(truth):sha(truth),str(index):sha(index),str(prior):sha(prior)}
 for iid,sid,lids,reg,rect,title,recommend,reason in seeds:
  u=units[sid];im=dict(u['image']);ip=Path(im['path']);im['path']=str(ip if ip.is_absolute() else ROOT.parents[1]/ip);assert sha(im['path'])==im['sha256'];ref=idx[sid]['output'];raw=Path(ref['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==ref['sha256'];hashes[im['path']]=im['sha256'];hashes[ref['path']]=ref['sha256']
  d=json.loads(raw.decode().splitlines()[ref['line']-1] if ref.get('line') else raw)
  for k in ref['pointer'].split('/')[1:]:d=d[int(k)] if isinstance(d,list) else d[k]
  if iid in ['D03','D04']:
   src=ROOT/'docs/ai-experiments/ocr_truth/v1/GS-02_002__sec_1_01.json';srcd=read(src);b=next(l['bbox'] for l in srcd['lines'] if l['line_id']==lids[0]);rect=[b['x']/im['width']*100,b['y']/im['height']*100,(b['x']+b['w'])/im['width']*100,(b['y']+b['h'])/im['height']*100];hashes[str(src)]=sha(src)
  match=next((r for r in d['regions'] if r['region_key']==reg),None)
  if reg:
   assert match;b=match['bbox'];rect=[b['x']/im['width']*100,b['y']/im['height']*100,(b['x']+b['w'])/im['width']*100,(b['y']+b['h'])/im['height']*100]
  items.append({'id':iid,'section_id':sid,'title':title,'image':dict(im,url=Path(im['path']).as_uri()),'focus_rect_percent':rect,'location_status':'위치 없음' if rect is None else 'OCR 원시 bbox' if reg else '기존 GT bbox(정확도 미검수)' if iid in ['D03','D04'] else '검토용 후보 범위(확정 위치 아님)','gt':u['lines'],'focus_gt':lids,'ocr':d['regions'],'focus_ocr':reg,'output_ref':ref,'recommendation':recommend,'reason':reason,'candidates':candidates.get(iid,[]),'decision':None,'approved':False,'frozen':False,'truth_sha256':sha(truth)})
 assert len(items)==9 and len({i['id'] for i in items})==9
 assert {i['section_id'] for i in items[:6]}=={s['id'] for s in sections.values() if s['status']=='보류'}
 payload={'version':'v6-user-review-draft-1','status':'검수 준비 / 미승인 / 미동결','items':items,'gt_candidate_sections':['GS-02_008__sec_1_05','GS-03_014__sec_1_01','GS-02_002__sec_1_01']}
 write('review-data.json',payload);digest=sha(P/'review-data.json');(P/'data.js').write_text('const REVIEW_DATA='+json.dumps(payload,ensure_ascii=False).replace('</','<\\/')+';\nconst DATA_SHA='+json.dumps(digest)+';\n',encoding='utf8')
 write('source-hashes.json',hashes);assert all(sha(p)==h for p,h in protected.items())
 write('validation.json',{'items':9,'scope_decisions':7,'prior_pending_sections_covered':5,'new_reflection_sections':1,'gt_candidate_sections':3,'unique_image_sections':len({i['section_id'] for i in items}),'all_source_hashes_verified':len(hashes),'protected_files_unchanged':len(protected),'data_sha256':digest,'browser_visual':'미수행 — 이전 안전 중단 재시도하지 않음','gt_modified':False,'approved':False,'frozen':False})
 print('review package generated:',digest)
if __name__=='__main__':main()
