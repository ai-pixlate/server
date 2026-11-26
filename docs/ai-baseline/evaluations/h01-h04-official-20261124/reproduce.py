"""기존 준비 묶음으로 복원한 로컬 입력에 이번 평가 기록을 연결하고 재집계한다.

모델/API 호출 및 이미지 생성 없음. 기존 Baseline·동결 자료를 수정하지 않는다.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys
from collections import Counter

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parents[3]
sys.path.insert(0, str(REPO))
from tools.baseline import protocol


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    if path.name == 'metrics.json':
        text = '[\n' + ',\n'.join(json.dumps(r, ensure_ascii=False) for r in value) + '\n]\n'
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    path.write_text(text, encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='evaluation_prepare로 복원한 새 로컬 디렉터리')
    parser.add_argument('--output', type=Path, required=True, help='존재하지 않는 파생 집계 디렉터리')
    args = parser.parse_args()
    root, out = args.input.resolve(), args.output.resolve()
    if out.exists():
        raise SystemExit('기존 파생 결과를 덮어쓰지 않습니다. 새 --output 경로를 지정하세요.')
    for protected in ('docs', 'pipeline', 'tools'):
        if root == REPO / protected or (REPO / protected) in root.parents:
            raise SystemExit('추적 자료 폴더를 --input으로 사용할 수 없습니다. 새 로컬 복원본을 사용하세요.')
    manifest = read(root / 'manifest.json')
    if manifest['evaluation_run_id'] != 'baseline-v1-0-1-official-h01-h04-20261124':
        raise SystemExit('다른 평가 회차에는 연결할 수 없습니다.')
    # 입력 복원본·세션에 이미 다른 평가가 있으면 덮어쓰지 않는다.
    for name in ('evaluations.jsonl', 'defects.jsonl', 'human_review.csv'):
        dest = root / name
        current = dest.read_bytes()
        if current.strip() and current != (PACKAGE / name).read_bytes():
            if name != 'human_review.csv' or len(current.splitlines()) > 1:
                raise SystemExit(f'{name}: 기존 기록이 있어 중단합니다.')
    before = {f['path']: digest(root / f['path']) for f in [*manifest['files'], *manifest['artifacts']] if not f['path'].startswith('scoring/')}
    for f in [*manifest['files'], *manifest['artifacts']]:
        if not f['path'].startswith('scoring/') and before[f['path']] != f['sha256']:
            raise SystemExit('보존 입력 해시 불일치: ' + f['path'])
    for name in ('reference_units.jsonl', 'execution.jsonl', 'reference-approval.json', 'reference-origins.json'):
        before[name] = digest(root / name)
    provenance = read(PACKAGE / 'provenance.json')
    for f in provenance['fixed_inputs']:
        if digest(root / f['path']) != f['sha256']:
            raise SystemExit('고정 입력 불일치: ' + f['path'])
    for f in provenance['evaluation_files']:
        if digest(PACKAGE / f['path']) != f['sha256']:
            raise SystemExit('최초 평가 파일 해시 불일치: ' + f['path'])
    scoring = root / 'scoring'
    scoring.mkdir(exist_ok=True)
    for name in ('instruction.md', 'response-record.md', 'visual-observations.json', 'visual-coverage.json'):
        shutil.copyfile(PACKAGE / name, scoring / name)
    session = read(PACKAGE / 'evaluation-session.json')
    for prefix in ('instruction', 'transcript'):
        session[prefix + '_sha256'] = digest(root / session[prefix + '_path'])
    if manifest['evaluation_sessions'] and manifest['evaluation_sessions'] != [session]:
        raise SystemExit('기존 평가 세션을 덮어쓰지 않습니다.')
    manifest['evaluation_sessions'] = [session]
    manifest['preparation_only'] = False
    manifest['files'] = [f for f in manifest['files'] if not f['path'].startswith('scoring/')]
    manifest['files'].extend({'path': 'scoring/' + p.name, 'sha256': digest(p)} for p in sorted(scoring.iterdir()) if p.is_file())
    for name in ('evaluations.jsonl', 'defects.jsonl', 'human_review.csv'):
        shutil.copyfile(PACKAGE / name, root / name)
    write(root / 'manifest.json', manifest)
    assert all(digest(root / p) == h for p, h in before.items()), '기존 입력 변경 감지'
    # aggregate 내부에서 전체 스키마·참조·해시 validate를 수행한다.
    result = protocol.aggregate(root)
    _, data = protocol.load(root)
    evaluations, defects = data['evaluations'], data['defects']
    assert len(evaluations) == 466
    assert len({(e['source_key'], e['checkpoint'], e['metric_id'], e['reference_unit_id']) for e in evaluations}) == 466
    assert len(data['human_review']) == 0
    assert len(data['reference_units']) == 339
    coverage = read(PACKAGE / 'visual-coverage.json')
    assert len(coverage) == 13 and sum(len(x['preview_artifact_ids']) for x in coverage) == 67
    for x in coverage:
        spans = x['viewed_y_ranges']
        assert spans[0][0] == 0 and spans[-1][1] == x['height']
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    # 공식 도구의 계산값 유지. 반복되는 근거 ID는 별도 파일로 한 번만 보관.
    metrics = [r.copy() for r in result['metrics'] if r['metric_id'] in ('H01', 'H02', 'H03', 'H04') or r['metric_id'] is None]
    bases = {}
    for r in metrics:
        basis = r.pop('aggregation_basis')
        key = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
        bases[key] = basis
        r['aggregation_basis_ref'] = 'aggregation-basis.json#' + key
    out.mkdir(parents=True)
    write(out / 'manifest.json', manifest)
    write(out / 'metrics.json', metrics)
    write(out / 'aggregation-basis.json', bases)
    fields = ['metric_id', 'scope', 'case_id', 'source_key', 'checkpoint', 'breakdown', 'numerator', 'denominator', 'value', 'unit', 'applicable_count', 'measured_count', 'not_applicable_count', 'no_output_count', 'undetermined_count', 'evaluation_failed_count', 'ai_only_count', 'human_checked_count', 'display_reason']
    with (out / 'metrics.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        for r in metrics:
            writer.writerow({**r, 'breakdown': json.dumps(r['breakdown'], ensure_ascii=False) if isinstance(r['breakdown'], dict) else r['breakdown']})
    write(out / 'review-required.json', result['review_required'])
    roots = {d['root_defect_id']: d for d in defects}
    # 공식 집계가 미판정 원본을 제외하는 것과 구분해, 이미 확인한 모든 결함의 영향도 보조 표기.
    write(out / 'observed-defect-inventory.json', {
        'basis': '확인된 전체 결함 관찰. undetermined 원본의 major도 포함; 공식 measured 분모 집계와 구별.',
        'observation_records': len(defects), 'unique_roots_across_checkpoints': len(roots),
        'by_severity': dict(Counter(d['severity'] for d in roots.values())),
        'by_origin': dict(Counter(d['origin_stage'] for d in roots.values())),
        'affected_sources': sorted({d['source_key'] for d in roots.values()}),
        'roots': [{k: d[k] for k in ('root_defect_id', 'source_key', 'severity', 'origin_stage', 'defect_type', 'observed')} for d in roots.values()]})
    write(out / 'validation.json', {
        'issues': [], 'formal_complete': False, 'pm_review_pending': True,
        'source_count': len(manifest['source_keys']), 'reference_count': len(data['reference_units']),
        'evaluation_count': len(evaluations), 'defect_record_count': len(defects),
        'human_review_count': 0, 'protected_input_hash_count': len(before),
        'protected_inputs_unchanged': True, 'visual_originals': 13, 'visual_previews': 67, 'visual_finals': 13,
        'readiness_issues': result['readiness_issues'],
        'limitations': ['PM 검수 전', '채팅 모델 식별자·설정 미확인', '전체 채팅 내보내기 미보존; 평가 응답 기록 보존'],
        'tool_commit': provenance['scoring_tool_commit']})
    print(json.dumps([r for r in metrics if r['scope'] == 'all' and r['metric_id'] in ('H01', 'H02', 'H03', 'H04')], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
