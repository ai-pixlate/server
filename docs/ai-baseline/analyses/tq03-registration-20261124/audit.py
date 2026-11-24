"""보존된 TQ03 실험의 오프라인 대조. 모델 실행·입력 수정·정책 변경 없음."""
import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

EXPECTED = {
    'full_runner.py': '7e124f4565a0686c2142da82dba3e03d7d99a780ae9b787730c4f17a11bec45a',
    'protocol.py': 'aa07c38c98e84e69d148bb8705becc6b22a6bc49b6c3743575fa0ec79f2f6575',
    'validated-run-config.json': '9b53506dc9dbee4307e9c57336fa91265e2195d27b5ef0e8eb9437be45300a20',
    'targets.jsonl': '53ed00b938c68514606357e218113f7fcfb9e9aa17ac0176989f28e27d551b25',
    'received-20261121/results.jsonl': '63bf17f66ce13a43a66a376181625d89138576b4ce5f32587a78b0e8b1465f0d',
    'received-20261121/summary.json': 'a13e46910f91dc8615bcb389492ffe9a00f4c6909b6f5415883ccc1719c3dace',
    'received-20261121/receipt-validation.json': '3e78d6071d69dbf66dde1a676cb5c075358f9ceab289a55e68163ae3fea3849e',
}

def audit(root):
    hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in EXPECTED}
    if hashes != EXPECTED:
        raise ValueError('보존 파일 해시 불일치: 다른 실험을 동일 근거로 집계하지 않습니다.')
    rows=[json.loads(s) for s in (root/'received-20261121/results.jsonl').read_text(encoding='utf-8').splitlines()]
    targets=[json.loads(s) for s in (root/'targets.jsonl').read_text(encoding='utf-8').splitlines()]
    target_by={r['evaluation_key']:r for r in targets}
    keys=[r['evaluation_key'] for r in rows]
    assert len(keys)==len(set(keys))==len(target_by)==491
    assert set(keys)==set(target_by)
    scores=[]; mismatches=[]; multiple=[]; decimals=[]; strict=0
    strict_pattern=r'\s*Feedback:\s*Reason:\s*(.+?)\s+Issues:\s*(.+?)\s+Direction:\s*(.+?)\s*\[RESULT\]\s*([1-5])\s*[.]?\s*'
    for r in rows:
        for k in ('source_ko','translation_en','product_id','image_id','section_id','block_id','source_artifact','source_pointer'):
            assert r[k]==target_by[r['evaluation_key']][k],(r['id'],k)
        raw=r.get('raw_output') or ''
        markers=re.findall(r'\[RESULT\]\s*([1-5])(?!\d)',raw)
        if len(markers)>1:multiple.append(r['id'])
        if re.search(r'\[RESULT\]\s*[1-5]\.\d',raw):decimals.append(r['id'])
        score=int(markers[0]) if len(markers)==1 and r['status'] in ('ok','format_error') else None
        # Compare aggregation eligibility, not a hypothetical rewrite of raw reported_score.
        stored=r.get('reported_score') if r['status'] in ('ok','format_error') else None
        if score!=stored:mismatches.append(r['id'])
        if score is not None:scores.append(score)
        if r['status']!='output_limit' and re.fullmatch(strict_pattern,raw,re.S):strict+=1
    assert not mismatches and len(scores)==449 and sum(scores)==970 and strict==0
    summary=json.loads((root/'received-20261121/summary.json').read_text(encoding='utf-8'))['overall']
    assert summary['marker_score_n']==len(scores) and summary['marker_mean']==sum(scores)/len(scores)
    counts={str(i):scores.count(i) for i in range(1,6)}
    assert counts==summary['marker_distribution']
    return {
        'checked_on':'2026-11-24',
        'mandatory_limitations':'단일 평가모델(M-Prometheus 7B) · 독립 검증 없음 · 출력 형식 완전 충족 0개 · 점수 확보 449/491개',
        'source_file_sha256':hashes,'unique_targets':len(keys),'source_texts_and_identifiers_match':True,
        'status_counts':dict(collections.Counter(r['status'] for r in rows)),
        'origin_counts':dict(collections.Counter(r['origin'] for r in rows)),
        'strict_format_count':strict,'marker_score_n':len(scores),'marker_score_sum':sum(scores),
        'marker_mean':sum(scores)/len(scores),'marker_distribution':counts,
        'score_recheck_mismatch_ids':mismatches,'multiple_valid_marker_ids':multiple,'decimal_marker_ids':decimals,
        'no_marker_untruncated':sum(r['status']=='format_error' and r.get('reported_score') is None for r in rows),
        'output_limit_ids':[r['id'] for r in rows if r['status']=='output_limit'],
        'legacy_body_only':{r['id']:r['body_only_score'] for r in rows if r.get('body_only_score') is not None},
        'model_calls':0,'inputs_modified':False,
        'note':'기존 통계 재현 확인이며 최종판 추출 경계·재시도·대상 대응 정책의 신규 확정이 아님',
    }

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('experiment_root',type=Path)
    print(json.dumps(audit(p.parse_args().experiment_root),ensure_ascii=False,indent=2))
