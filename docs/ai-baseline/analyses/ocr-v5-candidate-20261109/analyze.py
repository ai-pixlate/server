"""원본 읽기 전용 v5 후보 비교. 결과는 새 디렉터리에만 작성한다."""
import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

W=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(W))
from tools.baseline.ocr import verify, pointer_value, text_sequence
from tools.baseline.ocr_v5 import score_lines

def read(p): return json.loads(Path(p).read_text(encoding='utf8'))
def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image-root',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists(): raise ValueError('기존 출력 덮어쓰기 금지')
    truth=W/'docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109'
    base=W/'docs/ai-baseline/evaluations/ocr-v4-v2-v1-0-1-20261109'
    protected={str(p):digest(p) for f in (truth,base,W/'docs/ai-baseline/references/ocr-text/ocr-text-v1-20261104',W/'docs/ai-baseline/evaluations/ocr-v4-v1-0-1-20261105') for p in f.rglob('*') if p.is_file()}
    _,units=verify(truth)
    approval=read(truth/'approval.json')
    previous={r['id']:r for r in read(base/'results-112.json')['sections']}
    audit=[]; results=[]
    for unit in units:
        sid=unit['id']; old=previous[sid]
        image=Path(unit['image']['path'])
        if not image.is_absolute(): image=args.image_root/image
        if not image.is_file() or digest(image)!=unit['image']['sha256']: raise ValueError('이미지 연결 오류 '+sid)
        out=old['provenance']['output']; path=Path(out['path'])
        if digest(path)!=out['sha256']: raise ValueError('출력 해시 오류 '+sid)
        raw=path.read_text(encoding='utf8')
        data=json.loads(raw.splitlines()[out['line']-1]) if out.get('line') else json.loads(raw)
        data=pointer_value(data,out['pointer'])
        gt=[l['text'] for l in unit['lines']]; pred=[l['text'] for l in data['regions']]
        review=approval['reviews'].get(sid)
        human=review is None or bool(review['human_reviewed'])
        issues=[]
        if sid in approval['exceptions']:
            issues.append('v2에서 미전사/판독 불가를 빈 정답으로 수용; 원본 독립 전사·판독 가능성 재검수')
        empty=[l['line_id'] for l in unit['lines'] if not text_sequence([l['text']])]
        if empty:
            issues.append('빈 줄의 실제 무문자 여부 미확인: '+','.join(empty))
        if sid=='GS-02_002__sec_1_01':
            issues.append('L005·L006 좌표형 숫자 30자: 원본 근거 미확인, GT 오염 의심; 삭제/수정 미승인')
        if not unit['lines']:
            cohort='empty_image_pending_v5_confirmation'
        elif issues:
            cohort='scope_or_gt_pending'
        elif not human:
            cohort='ai_only_pending'
        else:
            cohort='prior_human_review_no_known_scope_issue'
        row=dict(id=sid,cohort=cohort,line_count=len(gt),empty_line_ids=empty,
                 image_path=str(image),image_sha256=unit['image']['sha256'],
                 human_reviewed=human,original_incomplete_exception=sid in approval['exceptions'],
                 issues=issues,prior_issues=approval['exceptions'].get(sid,{}),
                 v5_scope_approved=False,visual_transcription_rechecked=False,
                 required_action='원본 이미지 기준 전사·범위 재검수; OCR 출력으로 GT 보완 금지' if issues else 'v5 범위 승인 필요')
        r=score_lines(gt,pred)
        r.update(id=sid,cohort=cohort,status='candidate_diagnostic',scope_status='undetermined',official_eligible=False,
                 v4={k:old[k] for k in ('S','D','I','N','cer_percent')},output=out,image_sha256=unit['image']['sha256'])
        assert r['N']==old['N']
        audit.append(row); results.append(r)
        print(sid, r['errors'], flush=True)
    aggregate={}
    for cohort in ['all']+sorted({r['cohort'] for r in results}):
        aggregate[cohort]={}
        for gs in ['GS-01','GS-02','GS-03','all']:
            rr=[r for r in results if (cohort=='all' or r['cohort']==cohort) and (gs=='all' or r['id'].startswith(gs))]
            measured=[r for r in rr if r['N']]
            n=sum(r['N'] for r in measured)
            a={k:sum(r[k] for r in measured) for k in ['S','D','I','N','errors']}
            a.update(sections=len(rr),nonzero_gt_sections=len(measured),
                     v5_cer=100*a['errors']/n if n else None,
                     v4_errors=sum(sum(r['v4'][k] for k in ['S','D','I']) for r in measured))
            a['v4_cer']=100*a['v4_errors']/n if n else None
            aggregate[cohort][gs]=a
    for path,h in protected.items():
        if digest(path)!=h: raise ValueError('보호 파일 변경 '+path)
    payload=dict(status='candidate_not_official',unicode_version=unicodedata.unidata_version,
                 algorithm_sha256=digest(W/'tools/baseline/ocr_v5.py'),spec_sha256=digest(W/'docs/ai-baseline/protocols/v1/ocr-v5.md'),
                 truth_sha256=digest(truth/'text_units.jsonl'),protected_files=protected,
                 aggregate=aggregate,sections=results)
    args.out.mkdir(parents=True)
    for name,data in [('comparison.json',payload),('completeness-audit.json',audit)]:
        (args.out/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(aggregate,ensure_ascii=True),flush=True)

if __name__=='__main__': main()
