"""동결 자료를 별도 평가 입력으로 연결한다. 생성·채점·사람 승인은 수행하지 않는다."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

from . import protocol
from .core import REGIONS


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def remap(value, run_id):
    """원시 기록을 보존하고 파생 사본의 경로와 회차 ID만 변환한다."""
    if isinstance(value, list):
        return [remap(x, run_id) for x in value]
    if isinstance(value, dict):
        return {k: (run_id if k == 'evaluation_run_id' else
                    'generation/' + v if k in ('path', 'dataset_manifest_path') and isinstance(v, str) else
                    remap(v, run_id)) for k, v in value.items()}
    return value


def source_plan(repository, path, expected, commit):
    path = Path(path).as_posix()
    if Path(path).is_absolute() or '..' in Path(path).parts:
        raise ValueError('저장소 상대 경로 필요')
    local = repository / path
    data = local.read_bytes() if local.is_file() else b''
    if checksum(data) != expected:
        data = subprocess.check_output(['git', 'show', f'{commit}:{path}'], cwd=repository)
    if checksum(data) != expected:
        raise ValueError('근거 해시 불일치: ' + path)
    return data


def exception_plan(rows, manifest):
    result = []
    for source in manifest['source_keys']:
        rr = [r for r in rows if r['source_key'] == source]
        applicability = {r['applicability']['H01'] for r in rr if 'H01' in r['applicability']}
        if len(applicability) != 1:
            raise ValueError('원본별 H01 기대값 누락·충돌: ' + source)
        applies = applicability.pop()
        images = {cp: [a['artifact_id'] for a in manifest['artifacts']
                       if source in a['source_keys'] and a['checkpoint'] == cp]
                  for cp in protocol.CHECKPOINTS}
        if not applies and any(images.values()):
            raise ValueError('전체 제외 기대와 실제 산출물 충돌: 별도 평가 필요 ' + source)
        result.append({'source_key': source, 'h01_applicable': applies,
                       'h01_expected_denominator_member': applies,
                       'checkpoints': {cp: {'artifact_ids': images[cp],
                                           'planned_status': ('not_applicable' if not applies else
                                                              'pending_visual_evaluation' if images[cp] else 'no_output')}
                                       for cp in protocol.CHECKPOINTS},
                       'h04': [{'reference_unit_id': r['reference_unit_id'],
                                'planned_status': {cp: ('not_applicable' if not r['applicability']['H04'][cp] else
                                                       'pending_visual_evaluation' if images[cp] else 'no_output')
                                                   for cp in protocol.CHECKPOINTS}}
                               for r in rr if r['unit_type'] == 'critical_fact'],
                       'execution_failure_group': 'GS-03-job-3-N2' if source.startswith('GS-03') else None})
    return result


def build(repository, output, source_run, reference_set, run_id):
    repository, output = Path(repository).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('새 출력 폴더만 허용')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    run = repository / source_run
    index_path = repository / reference_set
    index = read(index_path)
    files, blobs = {}, {}
    def connect(path, expected=None, destination=None, pinned=commit):
        path = Path(path)
        relative = path.relative_to(repository).as_posix()
        expected = expected or checksum(path.read_bytes())
        data = source_plan(repository, relative, expected, pinned)
        destination = destination or 'evidence/' + expected + path.suffix
        entry = {'path': relative, 'commit': pinned, 'sha256': expected, 'bytes': len(data)}
        if destination in files and files[destination]['sha256'] != expected:
            raise ValueError('대상 경로 충돌')
        files[destination] = entry
        return {'path': destination, 'sha256': expected}

    # 최초 회차 전체를 색인으로 참조하되 실제 이미지·로그를 새 PR에 중복 저장하지 않는다.
    inv = read(run / 'github-file-index.json')['files']
    for name, meta in inv.items():
        connect(run / name, meta['sha256'], 'generation/' + name)
    connect(run / 'github-file-index.json', destination='generation/github-file-index.json')
    m = remap(read(run / 'manifest.json'), run_id)
    m['reference_version'] = index['version']  # 승인된 두 버전의 통합 색인 식별자
    m['evaluation_sessions'] = []
    preservation = read(repository / 'docs/ai-baseline/evaluations/baseline-v1-0-1-gs01-20261103/preservation-index.json')
    mappings = {r['destination']: r for r in preservation['files']}
    refs, origins, approvals = [], [], []
    connect(index_path)
    for bundle in index['bundles']:
        original = index_path.parent / bundle['reference_file']['path']
        raw = source_plan(repository, original.relative_to(repository), bundle['reference_file']['sha256'], commit)
        connect(original, bundle['reference_file']['sha256'])
        approval_file = index_path.parent / bundle['approval']['path']
        approval_ref = connect(approval_file, bundle['approval']['sha256'])
        frozen_manifest = index_path.parent / bundle['manifest']['path']
        fm = read(frozen_manifest)
        connect(frozen_manifest, bundle['manifest']['sha256'])
        approvals.append({'version': bundle['version'], 'verified_by': fm['verified_by'],
                          'verified_at': fm['verified_at'], 'evidence_ref': approval_ref,
                          'reference_sha256': bundle['reference_file']['sha256']})
        for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
            old = json.loads(line)
            row = copy.deepcopy(old)
            row.update(evaluation_run_id=run_id, reference_version=index['version'], verification_ref=approval_ref)
            for r in row['basis_refs']:
                path = (original.parent / r['path']).resolve()
                pinned = commit
                if not path.is_file() or checksum(path.read_bytes()) != r['sha256']:
                    entry = mappings[path.relative_to(repository).as_posix()]
                    assert entry['sha256'] == r['sha256']
                    path = repository / entry['source']['path']
                    pinned = commit if entry['source']['commit'] == 'PR_HEAD' else entry['source']['commit']
                r.update(connect(path, r['sha256'], pinned=pinned))
            refs.append(row)
            origins.append({'reference_unit_id': row['reference_unit_id'], 'source_file': original.relative_to(repository).as_posix(),
                            'source_sha256': bundle['reference_file']['sha256'], 'source_line': number,
                            'source_reference_version': old['reference_version'], 'source_evaluation_run_id': old['evaluation_run_id'],
                            'source_row_sha256': checksum(encoded(old)), 'derived_row_sha256': checksum(encoded(row))})
    if len(refs) != 339 or len({r['reference_unit_id'] for r in refs}) != 339:
        raise ValueError('339개 고유 기준 단위 필요')
    reference_bytes = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in refs).encode('utf-8')
    blobs['reference_units.jsonl'] = reference_bytes
    blobs['reference-origins.json'] = encoded({'transformations': ['evaluation_run_id', 'reference_version', 'basis_refs.path', 'verification_ref.path'],
                                               'semantic_fields_unchanged': True,
                                               'row_hash_encoding': 'UTF-8 json.dumps(ensure_ascii=False, indent=2) + LF; 파일 해시는 원시 바이트',
                                               'rows': origins})
    blobs['reference-approval.json'] = encoded({'kind': '기존 승인 연결 — 새 승인 아님', 'bundles': approvals,
                                              'gs02_gs03_approval': '사용자 예람이 전달한 PM 구두 승인; 실제 구두 승인 시각 미확인',
                                              'source_map_sha256': checksum(blobs['reference-origins.json'])})
    m['reference_freeze'] = {'verified_by': '기존 PM 승인 2건 연결(reference-approval.json 참조)',
                             'verified_at': max(a['verified_at'] for a in approvals),
                             'evidence_ref': {'path': 'reference-approval.json', 'sha256': checksum(blobs['reference-approval.json'])},
                             'reference_file': {'path': 'reference_units.jsonl', 'sha256': checksum(reference_bytes)}}
    for name in ['evaluation.md', 'aggregation.md', 'dataset.md']:
        blobs['instructions/' + name] = (repository / 'docs/ai-baseline/protocols/v1' / name).read_bytes()
    blobs['instructions/evaluation-prompt.md'] = (repository / 'tools/baseline/evaluation-prompt.md').read_bytes()
    m['files'] += [{'path': name, 'sha256': x['sha256']} for name, x in files.items()
                   if name.startswith(('instructions/', 'evidence/')) or name == 'generation/manifest.json']
    m['files'] += [{'path': k, 'sha256': checksum(v)} for k, v in blobs.items() if k != 'reference_units.jsonl']
    m['source_generation_run_id'] = read(run / 'manifest.json')['evaluation_run_id']
    m['preparation_only'] = True
    m['protocol_commit'] = commit
    blobs['manifest.json'] = encoded(m)
    plan = exception_plan(refs, m)
    blobs['exception-plan.json'] = encoded({'not_evaluation_records': True, 'sources': plan})
    blobs['review-checklist.json'] = encoded({'reviewer': 'PM', 'time_limit': None, 'completion': '필요한 확인·수정 완료 시',
        'status': 'not_started', 'core_regions': [{'id': k, 'source_key': protocol.source_key(s), 'bbox': b,
            'status': 'not_started', 'checkpoints': list(protocol.CHECKPOINTS)} for k, s, b in REGIONS],
        'required_categories': ['critical/major 전건', '불확실 판단', '예외 상태와 원인', 'H04 판단 근거'],
        'a_sample': {'status': 'proposal_not_adopted', 'proposal': '상품×체크포인트별 A 1장, 최대 6쌍; 명세 SHA-256 정렬 추천안', 'selected': []},
        'human_reviews': [], 'pilot': 'GS-01 Pilot 점수·결함 기록을 공식 평가로 복사하지 않음', 'H05': '미측정(별도 서비스 평가 예정)'})
    counts = {cp: Counter(v['planned_status'][cp] for s in plan for v in s['h04']) for cp in protocol.CHECKPOINTS}
    blobs['preflight.json'] = encoded({'status': 'prepared_not_scored', 'source_commit': commit,
        'originals': len(plan), 'references': len(refs), 'H01_applicable': sum(x['h01_applicable'] for x in plan),
        'H04_planned_states': counts, 'H02_H03_planned_states': {cp: Counter(x['checkpoints'][cp]['planned_status'] for x in plan) for cp in protocol.CHECKPOINTS},
        'pending': ['Codex 실제 평가 세션·지침·응답 증거 기록', '실제 이미지 확인과 평가 상태 기록', 'PM 평가 검수 및 A 표본 방식 채택'],
        'policy_changes': [], 'model_calls': 0, 'scores_computed': False})
    blobs['file-plan.json'] = encoded({'repository_paths': True, 'files': files,
                                      'execution_transform': 'generation/execution.jsonl의 evaluation_run_id와 path만 변환',
                                      'source_run': str(source_run), 'run_id': run_id})
    output.mkdir(parents=True)
    for name, data in blobs.items():
        (output / name).parent.mkdir(parents=True, exist_ok=True)
        (output / name).write_bytes(data)
    return read(output / 'preflight.json')


def materialize(repository, preparation, output):
    repository, preparation, output = Path(repository).resolve(), Path(preparation).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('새 출력 폴더만 허용')
    plan = read(preparation / 'file-plan.json')
    output.mkdir(parents=True)
    for name, item in plan['files'].items():
        dest = (output / name).resolve()
        if not dest.is_relative_to(output):
            raise ValueError('회차 밖 대상 경로')
        data = source_plan(repository, item['path'], item['sha256'], item['commit'])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    for name in ('manifest.json', 'reference_units.jsonl', 'reference-origins.json', 'reference-approval.json'):
        (output / name).write_bytes((preparation / name).read_bytes())
    for path in (preparation / 'instructions').iterdir():
        (output / 'instructions').mkdir(exist_ok=True)
        (output / 'instructions' / path.name).write_bytes(path.read_bytes())
    lines = [remap(json.loads(s), plan['run_id']) for s in (output / 'generation/execution.jsonl').read_text(encoding='utf-8').splitlines()]
    (output / 'execution.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in lines), encoding='utf-8')
    for kind in ('evaluations', 'defects'):
        (output / (kind + '.jsonl')).touch()
    protocol.csv_write(output / 'human_review.csv', protocol.FIELDS['human_review'].split(), [])
    issues = protocol.validate(output)
    protocol.require_frozen_reference(output, read(output / 'manifest.json'))
    if issues:
        raise ValueError('; '.join(issues))
    return {'validation_issues': issues, 'scores_computed': False, 'evaluation_records': 0}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', type=Path, default=Path('.'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--materialize', type=Path, help='준비 입력을 로컬에 복원한다. 채점하지 않음')
    p.add_argument('--source-run', default='docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01')
    p.add_argument('--reference-set', default='docs/ai-baseline/references/gs-v1/reference-set-20261124.json')
    p.add_argument('--run-id', default='baseline-v1-0-1-official-h01-h04-20261124')
    a = p.parse_args()
    print(json.dumps(materialize(a.repository, a.materialize, a.output) if a.materialize else
                     build(a.repository, a.output, a.source_run, a.reference_set, a.run_id), ensure_ascii=False, indent=2))
