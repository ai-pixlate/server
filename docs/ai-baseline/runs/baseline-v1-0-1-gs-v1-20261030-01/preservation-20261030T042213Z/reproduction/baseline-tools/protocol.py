"""Baseline v1 회차 기록·집계. 제품/모델/DB 모듈을 import하지 않는다."""
from __future__ import annotations

import copy
import csv
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .core import IDS, REGIONS, append, digest, inspect_inputs, now, read, rows, write_new
from .scope import automatic_scope, automatic_only, H05_REASON

STATES = ('measured', 'not_applicable', 'undetermined', 'no_output', 'evaluation_failed')
CHECKPOINTS = ('machine_snapshot', 'service_final')
SEVERITIES = ('critical', 'major', 'minor')
ORIGINS = ('섹션 분해', 'OCR', '블록 병합·역할 분류', '규제·현지 판정·정책 적용',
           '제품 라벨·브랜드 로고 판정', '인페인팅', '번역·현지화', '렌더·배치', 'undetermined')
DEFECT_TYPES = ('content_missing', 'content_wrong', 'content_extra_or_duplicate', 'structure_or_order_wrong',
                'decision_wrong', 'protection_wrong', 'text_not_removed', 'protected_content_damaged',
                'background_artifact', 'translation_missing', 'translation_meaning_error', 'translation_fact_error',
                'translation_naturalness', 'render_missing_or_clipped', 'readability_or_layout', 'final_unusable')
FIELDS = {
 'reference_units': 'evaluation_run_id dataset_id case_id source_key reference_unit_id reference_version unit_type location expected_value applicability importance basis_refs verified_by verified_at verification_ref',
 'execution': 'evaluation_run_id record_id record_kind case_id source_keys job_id stage checkpoint started_at ended_at evidence_refs unavailable_reason',
 'evaluations': 'evaluation_run_id evaluation_id session_id case_id source_key checkpoint metric_id reference_unit_id reference_version artifact_ids evaluation_status observed_value expected_value reason evidence_refs defect_record_ids supersedes_evaluation_id evaluated_at',
 'defects': 'evaluation_run_id defect_record_id defect_id evaluation_id recorded_by case_id source_key checkpoint reference_unit_ids defect_type severity location observed expected evidence_refs origin_stage detected_stage root_defect_id resolution_status correction_evidence_refs',
 'human_review': 'evaluation_run_id review_id evaluation_id defect_record_id source_key checkpoint reference_unit_id review_trigger review_scope reviewer_id reviewed_at ai_value reviewed_value reason evidence_refs supersedes_review_id',
}
EXTRA = {
 'operation': 'event values',
 'attempt': 'logical_execution_id target_id attempt_number execution_status error_codes fallback_type is_final_attempt applied_to_result artifact_ids usage cost',
 'user_edit': 'source_key target_id original_value final_value change_count user_retry_count confirmed_at n3_intervention',
 'usage': 'session_id purpose usage cost',
}
ID_FIELD = dict(zip(FIELDS, ('reference_unit_id', 'record_id', 'evaluation_id', 'defect_record_id', 'review_id')))
METRIC_FIELDS = ('evaluation_run_id metric_id scope case_id source_key checkpoint breakdown numerator denominator value unit '
                 'applicable_count measured_count not_applicable_count undetermined_count no_output_count evaluation_failed_count '
                 'ai_only_count human_checked_count display_reason evidence_refs aggregation_basis include_in_presentation').split()


def source_key(sid):
    return sid.replace('_', '-IMG-')


def template():
    return {'evaluation_run_id': None, 'product_version': '1.0.1', 'product_commit': None,
            'measurement_scope': automatic_scope(),
            'protocol_version': 'v1', 'protocol_commit': None, 'input_root': None,
            'environment': None, 'generation_config': None, 'case_inputs': None,
            'reference_version': None, 'reference_freeze': None, 'evaluation_sessions': [],
            'cost_approval': {'pm_cost_agreed': True, 'user_execution': None}, 'comparison': None}


def csv_read(path):
    result = []
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            result.append({k: (json.loads(v) if k in ('ai_value', 'reviewed_value', 'evidence_refs') else v) if v else None for k, v in row.items()})
    return result


def csv_write(path, fields, data):
    with Path(path).open('x', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in data:
            w.writerow({k: cell(row.get(k)) for k in fields})


def cell(value):
    return '' if value is None else value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


def load(root):
    root = Path(root)
    return read(root / 'manifest.json'), {k: csv_read(root / (k + '.csv')) if k == 'human_review'
                                        else rows(root / (k + '.jsonl')) for k in FIELDS}


def prepare(config, output):
    """미확정 값은 null로 보존하고 입력 외의 결과를 만들지 않는다."""
    root = Path(output)
    if not config.get('evaluation_run_id') or not re.fullmatch(r'[A-Za-z0-9_-]+', config['evaluation_run_id']):
        raise ValueError('evaluation_run_id 필요(영문·숫자·밑줄·하이픈)')
    inputs, issues = inspect_inputs(config)
    if issues or len(inputs) != 34:
        raise ValueError('; '.join(issues) or '34장 필요')
    dataset = Path(config['input_root']) / 'manifest.json'
    original = read(dataset)
    expected = {x['source_id']: x for x in original['files']}
    if set(expected) != set(IDS) or len(original['files']) != 34:
        raise ValueError('평가셋 manifest 목록 불일치')
    if any(expected[i['source_id']]['sha256'] != i['sha256'] for i in inputs):
        raise ValueError('평가셋 manifest 해시 불일치')
    root.mkdir(parents=True, exist_ok=False)
    artifacts = root / 'artifacts'
    artifacts.mkdir()
    shutil.copyfile(dataset, artifacts / 'dataset-manifest.json')
    manifest = {k: copy.deepcopy(config.get(k)) for k in template() if k != 'input_root'}
    manifest.update(created_at=now(), dataset_id='gs-v1', country='US', language='en',
                    dataset_manifest_path='artifacts/dataset-manifest.json', dataset_manifest_sha256=digest(dataset),
                    source_keys=[source_key(i) for i in IDS], artifacts=[], files=[])
    # 실행권한과 원본 절대경로는 manifest에 추가하지 않는다. 원본도 해시 사본으로 보존한다.
    for item in inputs:
        dest = artifacts / (item['sha256'] + '.jpg')
        shutil.copyfile(item['path'], dest)
        manifest['artifacts'].append(dict(artifact_id=item['source_id'], case_id=item['source_id'][:5],
            source_keys=[source_key(item['source_id'])], kind='original', stage='N1', checkpoint=None,
            path=dest.relative_to(root).as_posix(), sha256=item['sha256'], media_type='image/jpeg',
            width=item['width'], height=item['height'], parent_artifact_id=None, crop_bbox=None,
            created_at=now(), unavailable_reason=None))
    for p in (Path(__file__).resolve().parents[2] / 'docs/ai-baseline/protocols/v1').glob('*.md'):
        dest = artifacts / ('protocol-' + p.name)
        shutil.copyfile(p, dest)
        manifest['files'].append({'path': dest.relative_to(root).as_posix(), 'sha256': digest(dest)})
    prompt = Path(__file__).with_name('evaluation-prompt.md')
    shutil.copyfile(prompt, artifacts / prompt.name)
    manifest['files'].append({'path': 'artifacts/' + prompt.name, 'sha256': digest(prompt)})
    write_new(root / 'manifest.json', manifest)
    for kind in FIELDS:
        if kind == 'human_review':
            csv_write(root / 'human_review.csv', FIELDS[kind].split(), [])
        else:
            (root / (kind + '.jsonl')).touch(exist_ok=False)
    csv_write(root / 'metrics.csv', METRIC_FIELDS, [])
    (root / 'summary.md').write_text('# 준비 묶음\n\n공식 실측 미실행 · 비용·실행 승인 보류\n', encoding='utf-8')
    return readiness(manifest)


def readiness(m, purpose='evaluation'):
    if purpose not in ('generation', 'evaluation'):
        raise ValueError('준비 단계 오류')
    pending = [] if ((m.get('cost_approval') or {}).get('user_execution') or {}).get('approved') is True else ['사용자 비용·실행 승인 보류']
    fields = ['product_commit', 'protocol_commit', 'environment', 'generation_config', 'case_inputs', 'cost_approval']
    if purpose == 'evaluation':
        fields += ['reference_version', 'reference_freeze', 'evaluation_sessions']
    return pending + [k + ': 확인 필요' for k in fields if not m.get(k)]


def require_frozen_reference(root, m):
    """채점 전 별도 동결본과 PM 확인 증거의 해시를 검증한다."""
    freeze = m.get('reference_freeze') or {}
    if not m.get('reference_version') or not freeze.get('verified_by'):
        raise ValueError('PM 기준 자료 동결 필요')
    timestamp(freeze.get('verified_at'))
    for key in ('evidence_ref', 'reference_file'):
        ref = freeze.get(key) or {}
        if not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not isinstance(ref.get('sha256'), str):
            raise ValueError('PM 동결 파일·근거 경로와 해시 필요')
        local_file(root, ref.get('path'), ref.get('sha256'))
    if freeze['reference_file']['path'] != 'reference_units.jsonl':
        raise ValueError('동결본은 회차 reference_units.jsonl에 연결해야 합니다')


def required(row, fields):
    missing = set(fields.split()) - row.keys()
    if missing:
        raise ValueError('필드 누락: ' + ', '.join(sorted(missing)))


def reviewed(original, patch):
    result = copy.deepcopy(original)
    for k, v in patch.items():
        if k == 'observed_value' and isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k].update(v)
        else:
            result[k] = v
    return result


def timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError('시간대 있는 시각 필요')


def local_file(root, relative, sha):
    p = Path(root) / relative
    if Path(relative).is_absolute() or not p.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('회차 밖 파일 경로')
    if not p.is_file() or digest(p) != sha:
        raise ValueError('근거 파일 없음 또는 해시 불일치: ' + relative)


def capture_artifact(root, metadata, source):
    """이미 존재하는 산출물을 불변 복사한다. 생성·렌더·추론은 하지 않는다."""
    root, source = Path(root), Path(source)
    m = read(root / 'manifest.json')
    required(metadata, 'artifact_id case_id source_keys kind stage checkpoint media_type width height parent_artifact_id crop_bbox created_at unavailable_reason')
    if metadata['checkpoint'] not in (*CHECKPOINTS, None) or not metadata['source_keys'] or not set(metadata['source_keys']) <= set(m['source_keys']):
        raise ValueError('산출물 원본·체크포인트 오류')
    if any(a['artifact_id'] == metadata['artifact_id'] for a in m['artifacts']):
        raise ValueError('산출물 ID 덮어쓰기 금지')
    sha = digest(source)
    dest = root / 'artifacts' / sha
    if not dest.exists():
        with dest.open('xb') as f:
            f.write(source.read_bytes())
    item = {**metadata, 'path': dest.relative_to(root).as_posix(), 'sha256': sha}
    m['artifacts'].append(item)
    temp = root / 'manifest.next.json'
    write_new(temp, m)
    temp.replace(root / 'manifest.json')
    return item


def check_row(root, m, kind, row):
    automatic = automatic_only(m)
    if automatic and kind in ('evaluations', 'defects', 'human_review'):
        require_frozen_reference(root, m)
    if automatic and kind == 'execution':
        if row.get('record_kind') == 'user_edit' and (
                row.get('original_value') != row.get('final_value') or
                row.get('change_count') != 0 or row.get('user_retry_count') != 0 or
                (row.get('stage') == 'N3' and row.get('n3_intervention') != 'confirmation_only')):
            raise ValueError('무수정 회차의 변경·재생성 기록')
        values = row.get('values')
        if row.get('event') == 'first_n5_confirmed' and (
                not isinstance(values, dict) or values.get('original') != values.get('final')):
            raise ValueError('무수정 회차의 N5 변경 기록')
    if automatic and kind == 'defects' and row.get('resolution_status') == 'corrected_by_user':
        raise ValueError('사용자 수정 회복은 이번 회차 측정 범위 밖')
    if kind not in FIELDS:
        raise ValueError('지원하지 않는 기록')
    required(row, FIELDS[kind])
    if row['evaluation_run_id'] != m['evaluation_run_id'] or not isinstance(row[ID_FIELD[kind]], str) or not row[ID_FIELD[kind]]:
        raise ValueError('회차 또는 기록 ID 오류')
    sources = row.get('source_keys', [row.get('source_key')])
    if not sources or any(s not in m['source_keys'] for s in sources):
        raise ValueError('원본 목록 밖 기록')
    if row.get('case_id') is not None and any(s[:5] != row['case_id'] for s in sources):
        raise ValueError('상품과 원본 불일치')
    if 'checkpoint' in row and row['checkpoint'] not in (*CHECKPOINTS, None):
        raise ValueError('체크포인트 오류')
    if kind in ('evaluations', 'defects', 'human_review') and row['checkpoint'] not in CHECKPOINTS:
        raise ValueError('평가 체크포인트 필요')
    for field in ('evidence_refs', 'basis_refs'):
        if field in row:
            if not row[field]:
                raise ValueError('근거 파일 참조 필요')
            for e in row[field]:
                local_file(root, e['path'], e['sha256'])
    if kind == 'reference_units':
        if row['dataset_id'] != m['dataset_id'] or row['reference_version'] != m['reference_version']:
            raise ValueError('기준 버전 불일치')
        if not row['verified_by'] or not row['verification_ref']:
            raise ValueError('사람 확인·동결 근거 필요')
        timestamp(row['verified_at'])
        vref = row['verification_ref']
        local_file(root, vref['path'], vref['sha256'])
        if 'H01' in row['applicability']:
            if type(row['applicability']['H01']) is not bool or row['unit_type'] != 'policy_expectation':
                raise ValueError('H01 적용 여부는 정책 기대 기준의 bool')
            if row['applicability']['H01'] is False and row['expected_value'].get('all_sections_excluded') is not True:
                raise ValueError('H01 제외에는 정답 기준 전체 섹션 제외 확인 필요')
        if row['unit_type'] == 'critical_fact':
            applicability = row['applicability'].get('H04', {})
            if any(type(applicability.get(cp)) is not bool for cp in CHECKPOINTS):
                raise ValueError('핵심 사실의 두 체크포인트 적용 여부 필요')
    if kind == 'evaluations':
        if row['metric_id'] not in ('H02', 'H03', 'H04'):
            raise ValueError('Codex는 H02/H03/H04만 기록; H01/H05는 실행 증거 사용')
        if row['metric_id'] in ('H02', 'H03') and row['checkpoint'] != CHECKPOINTS[int(row['metric_id'][-1]) - 2]:
            raise ValueError('지표 체크포인트 불일치')
        if row['evaluation_status'] not in STATES or not row['reason']:
            raise ValueError('평가 상태·이유 필요')
        if row['reference_version'] != m['reference_version']:
            raise ValueError('기준 버전 불일치')
        if row['metric_id'] == 'H04' and not row['reference_unit_id']:
            raise ValueError('H04 기준 단위 필요')
        if row['evaluation_status'] == 'measured':
            if not row['artifact_ids']:
                raise ValueError('정상 측정에는 산출물 필요')
            value = row['observed_value']
            if not isinstance(value, dict):
                raise ValueError('관찰값은 구조화 객체')
            keys = ('accurate', 'readable') if row['metric_id'] == 'H04' else ('coverage_complete',)
            if any(type(value.get(k)) is not bool for k in keys):
                raise ValueError('관찰값 bool 필드 필요')
            if row['metric_id'] != 'H04' and value['coverage_complete'] is not True:
                raise ValueError('전체 확인 전 정상 원본 평가 불가')
        timestamp(row['evaluated_at'])
    if kind == 'defects':
        if row['severity'] not in SEVERITIES or row['origin_stage'] not in ORIGINS or row['defect_type'] not in DEFECT_TYPES:
            raise ValueError('결함 유형·심각도·원인 단계 오류')
        if row['resolution_status'] not in ('unresolved', 'corrected_by_user', 'corrected_by_retry', 'corrected_by_system', 'not_reproduced', 'disputed'):
            raise ValueError('결함 해결 상태 오류')
        if not row['recorded_by'] or not row['observed'] or not row['expected']:
            raise ValueError('결함 관찰·기대·기록자 필요')
        if row['resolution_status'].startswith('corrected_') and not row['correction_evidence_refs']:
            raise ValueError('해결 판정에는 수정 근거 필요')
        for ref in row['correction_evidence_refs']:
            local_file(root, ref['path'], ref['sha256'])
    if kind == 'execution':
        if row['record_kind'] not in EXTRA:
            raise ValueError('실행 기록 종류 오류')
        required(row, EXTRA[row['record_kind']])
        for k in ('started_at', 'ended_at'):
            if row[k] is not None:
                timestamp(row[k])
        if row['record_kind'] == 'operation':
            from .operations import check
            check(row)
        if row['record_kind'] == 'attempt':
            if row['execution_status'] not in ('success', 'recovered', 'degraded', 'blocked', 'no_output'):
                raise ValueError('실행 상태 오류')
            if type(row['attempt_number']) is not int or row['attempt_number'] < 1:
                raise ValueError('시도 번호 오류')
            if any(type(row[k]) is not bool for k in ('is_final_attempt', 'applied_to_result')):
                raise ValueError('시도 완료·반영 bool 필요')
        if row['record_kind'] == 'usage' and row['cost'] is not None:
            import math
            cost=row['cost']
            if not isinstance(cost,dict) or type(cost.get('amount')) not in (int,float) or not math.isfinite(cost['amount']) or cost['amount'] < 0 or not isinstance(cost.get('currency'),str) or not cost['currency'] or not cost.get('basis'):
                raise ValueError('비용의 금액·통화·산정 근거 필요')
        if row['record_kind'] == 'user_edit':
            if row['stage'] not in ('N3', 'N5') or row['source_key'] not in sources:
                raise ValueError('N3/N5 수정 범위 오류')
            if not isinstance(row['original_value'], dict) or not isinstance(row['final_value'], dict):
                raise ValueError('대상 ID별 전체 최초/최종 값 객체 필요')
            if row['original_value'].keys() != row['final_value'].keys():
                raise ValueError('수정 대상 목록 불일치')
            values = [*row['original_value'].values(), *row['final_value'].values()]
            if row['stage'] == 'N3' and any(v not in ('include', 'exclude') for v in values):
                raise ValueError('N3 값은 include/exclude')
            if row['stage'] == 'N5' and any(not isinstance(v, str) for v in values):
                raise ValueError('N5 값은 실제 문구(빈 번역은 빈 문자열)')
            if type(row['user_retry_count']) is not int or row['user_retry_count'] < 0:
                raise ValueError('사용자 재시도 건수 필요')
            count = sum(v != row['final_value'][k] for k, v in row['original_value'].items())
            if row['change_count'] != count:
                raise ValueError('수정 건수와 최초/최종 값 불일치')
    if kind == 'human_review':
        if not row['reviewer_id'] or not row['reason'] or not row['review_trigger']:
            raise ValueError('검수자·사유·검수 계기 필요')
        timestamp(row['reviewed_at'])
        patch = row['reviewed_value']
        allowed = {'severity', 'origin_stage', 'valid'} if row['defect_record_id'] else {'evaluation_status', 'observed_value'}
        if not isinstance(patch, dict) or not patch or set(patch) - allowed:
            raise ValueError('검수한 평가/결함 필드만 수정 가능')
        if 'valid' in patch and type(patch['valid']) is not bool:
            raise ValueError('결함 유효 여부 bool 필요')
        if 'severity' in patch and patch['severity'] not in SEVERITIES:
            raise ValueError('검수 심각도 오류')
        if 'origin_stage' in patch and patch['origin_stage'] not in ORIGINS:
            raise ValueError('검수 원인 단계 오류')
        if 'evaluation_status' in patch and patch['evaluation_status'] not in STATES:
            raise ValueError('검수 평가 상태 오류')


def record(root, kind, event):
    root = Path(root)
    m, data = load(root)
    check_row(root, m, kind, event)
    if any(e[ID_FIELD[kind]] == event[ID_FIELD[kind]] for e in data[kind]):
        raise ValueError('기록 ID 중복')
    if kind == 'evaluations':
        key = lambda e: (e['source_key'], e['checkpoint'], e['metric_id'], e['reference_unit_id'])
        old = [e for e in data[kind] if key(e) == key(event)]
        expected = old[-1]['evaluation_id'] if old else None
        if event['supersedes_evaluation_id'] != expected:
            raise ValueError('직전 평가 ID 연결 필요')
    if kind == 'human_review':
        key = lambda e: (e['evaluation_id'], e['defect_record_id'], e['review_scope'])
        old = [e for e in data[kind] if key(e) == key(event)]
        if event['supersedes_review_id'] != (old[-1]['review_id'] if old else None):
            raise ValueError('직전 검수 ID 연결 필요')
        with (root / 'human_review.csv').open('a', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow([cell(event[k]) for k in FIELDS[kind].split()])
    else:
        append(root / (kind + '.jsonl'), event)


def validate(root):
    m, d = load(root)
    issues = []
    try:
        automatic_only(m)
        local_file(root, m['dataset_manifest_path'], m['dataset_manifest_sha256'])
        for f in [*m['files'], *m['artifacts']]:
            local_file(root, f['path'], f['sha256'])
    except (ValueError, KeyError, TypeError) as e:
        issues.append(str(e))
    maps = {k: {r[ID_FIELD[k]]: r for r in v} for k, v in d.items()}
    artifacts = {a['artifact_id']: a for a in m['artifacts']}
    sessions = {s['session_id']: s for s in m['evaluation_sessions']}
    for session in sessions.values():
        try:
            required(session, 'session_id channel started_at model_name model_settings model_evidence_ref instruction_path instruction_sha256 conversation_ref transcript_path transcript_sha256 unavailable_reason')
            timestamp(session['started_at'])
            if session['channel'] != 'Codex 채팅':
                raise ValueError('평가 채널은 Codex 채팅')
            if (not session['model_name'] or session['model_settings'] is None) and not session['unavailable_reason']:
                raise ValueError('실제 모델·설정 미확인 사유 필요')
            for prefix in ('instruction', 'transcript'):
                local_file(root, session[prefix + '_path'], session[prefix + '_sha256'])
        except (ValueError, KeyError, TypeError) as e:
            issues.append('평가 세션: ' + str(e))
    if len(artifacts) != len(m['artifacts']) or len(sessions) != len(m['evaluation_sessions']):
        issues.append('산출물 또는 세션 ID 중복')
    previous_evaluations, previous_reviews = {}, {}
    attempts = defaultdict(list)
    for kind, entries in d.items():
        if len(maps[kind]) != len(entries):
            issues.append(kind + ': 중복 ID')
        for row in entries:
            try:
                check_row(root, m, kind, row)
                if kind == 'evaluations':
                    key = tuple(row[k] for k in ('source_key', 'checkpoint', 'metric_id', 'reference_unit_id'))
                    if row['supersedes_evaluation_id'] != previous_evaluations.get(key):
                        raise ValueError('평가 재기록 연결 오류')
                    previous_evaluations[key] = row['evaluation_id']
                    if row['session_id'] not in sessions or sessions[row['session_id']]['channel'] != 'Codex 채팅':
                        raise ValueError('Codex 세션 연결 필요')
                    if row['reference_unit_id']:
                        ref = maps['reference_units'][row['reference_unit_id']]
                        if ref['source_key'] != row['source_key']:
                            raise ValueError('기준 원본 불일치')
                    for did in row['defect_record_ids']:
                        defect = maps['defects'][did]
                        if any(defect[k] != row[k] for k in ('source_key', 'checkpoint', 'evaluation_id')):
                            raise ValueError('결함과 평가 연결 불일치')
                if kind in ('defects', 'human_review'):
                    e = maps['evaluations'][row['evaluation_id']]
                    if any(e[k] != row[k] for k in ('source_key', 'checkpoint')):
                        raise ValueError('평가 원본·체크포인트 불일치')
                    if kind == 'human_review' and row['defect_record_id']:
                        de = maps['defects'][row['defect_record_id']]
                        if de['evaluation_id'] != e['evaluation_id']:
                            raise ValueError('검수 결함과 평가 불일치')
                    if kind == 'human_review':
                        key = tuple(row[k] for k in ('evaluation_id', 'defect_record_id', 'review_scope'))
                        if row['supersedes_review_id'] != previous_reviews.get(key):
                            raise ValueError('검수 재기록 연결 오류')
                        previous_reviews[key] = row['review_id']
                        if not row['defect_record_id']:
                            patched = reviewed(e, row['reviewed_value'])
                            check_row(root, m, 'evaluations', patched)
                    if kind == 'defects' and row['defect_record_id'] not in e['defect_record_ids']:
                        raise ValueError('평가에서 결함 기록을 연결해야 합니다')
                if kind == 'execution' and row['record_kind'] == 'attempt':
                    attempts[row['logical_execution_id']].append(row)
                for aid in row.get('artifact_ids', []):
                    a = artifacts[aid]
                    if row.get('source_key') and row['source_key'] not in a['source_keys']:
                        raise ValueError('산출물 원본 불일치')
                    if row.get('checkpoint') and a['checkpoint'] != row['checkpoint']:
                        raise ValueError('산출물 체크포인트 불일치')
                if kind == 'defects':
                    a = artifacts[row['location']['artifact_id']]
                    if row['source_key'] not in a['source_keys'] or a['checkpoint'] != row['checkpoint']:
                        raise ValueError('결함 위치 산출물 불일치')
                    box = row['location']['bbox']
                    if box is not None and not (len(box) == 4 and 0 <= box[0] < box[2] <= a['width'] and 0 <= box[1] < box[3] <= a['height']):
                        raise ValueError('결함 좌표 범위 오류')
            except (ValueError, KeyError, TypeError) as e:
                issues.append(f'{kind}/{row.get(ID_FIELD[kind])}: {e}')
    for key, aa in attempts.items():
        if len({a['attempt_number'] for a in aa}) != len(aa) or sum(a['is_final_attempt'] for a in aa) > 1:
            issues.append(key + ': 중복 시도 번호 또는 복수 최종 시도')
        if any(a['is_final_attempt'] and a['attempt_number'] != max(x['attempt_number'] for x in aa) for a in aa):
            issues.append(key + ': 마지막이 아닌 시도가 최종으로 표시됨')
        if len({(a['stage'], a['target_id'], tuple(a['source_keys'])) for a in aa}) != 1:
            issues.append(key + ': 논리 실행 대상 불일치')
    return issues


def adopted(data):
    es, ds = copy.deepcopy(data['evaluations']), copy.deepcopy(data['defects'])
    em = {e['evaluation_id']: e for e in es}
    dm = {d['defect_record_id']: d for d in ds}
    # 영역 검수는 원본 평가 상태를 덮어쓰지 않는다. 결함 검수는 그 결함에만 적용.
    for r in data['human_review']:
        if r['defect_record_id']:
            dm[r['defect_record_id']].update(r['reviewed_value'])
        elif r['review_scope'] == 'original' or (r['reference_unit_id'] and r['review_scope'] == r['reference_unit_id']):
            em[r['evaluation_id']].update(reviewed(em[r['evaluation_id']], r['reviewed_value']))
            em[r['evaluation_id']]['human_checked'] = True
    latest = {}
    for e in es:
        latest[(e['source_key'], e['checkpoint'], e['metric_id'], e['reference_unit_id'])] = e
    return latest, dm


def grade(defects):
    roots = {}
    for d in defects:
        if d.get('valid', True) is False or d['resolution_status'] == 'not_reproduced':
            continue
        if d['checkpoint'] == 'service_final' and d['resolution_status'].startswith('corrected_'):
            continue
        k = d['root_defect_id'] or d['defect_id']
        if k not in roots or SEVERITIES.index(d['severity']) < SEVERITIES.index(roots[k]['severity']):
            roots[k] = d
    counts = Counter(d['severity'] for d in roots.values())
    return ('C' if counts['critical'] else 'B' if counts['major'] else 'A'), list(roots.values())


def metric(m, metric_id, checkpoint, breakdown, statuses, numerator, denominator=None, unit='%', presentation=True):
    counts = Counter(statuses)
    applicable = len(statuses) - counts['not_applicable']
    denominator = counts['measured'] if denominator is None else denominator
    row = {k: None for k in METRIC_FIELDS}
    row.update(evaluation_run_id=m['evaluation_run_id'], metric_id=metric_id, scope='all', checkpoint=checkpoint,
               breakdown=breakdown, numerator=numerator, denominator=denominator, unit=unit,
               applicable_count=applicable, measured_count=counts['measured'],
               ai_only_count=0, human_checked_count=0, evidence_refs=[], aggregation_basis=[],
               include_in_presentation=presentation)
    row.update({s + '_count': counts[s] for s in STATES if s != 'measured'})
    row['value'] = (numerator / denominator * 100 if unit == '%' else numerator / denominator) if denominator else None
    row['display_reason'] = None if denominator else ('해당 대상 없음' if not applicable else '정상 측정 0건')
    return row


def aggregate(root):
    issues = validate(root)
    if issues:
        raise ValueError('; '.join(issues))
    m, data = load(root)
    if automatic_only(m):
        require_frozen_reference(root, m)
    result = aggregate_data(m, data)
    # 전체·상품·원본 결과를 같은 계산기로 만든다. 공통 사용량은 원본에 중복 배분하지 않는다.
    for group in [sorted(s for s in m['source_keys'] if s.startswith(c)) for c in ('GS-01', 'GS-02', 'GS-03')] + [[s] for s in m['source_keys']]:
        if not group:
            continue
        subset = {k: [r for r in v if (r.get('source_key') in group or (r.get('source_keys') and set(r['source_keys']) <= set(group)))] for k, v in data.items()}
        part = aggregate_data({**m, 'source_keys': group}, subset)
        for r in part['metrics']:
            r.update(scope='source' if len(group) == 1 else 'case', case_id=group[0][:5], source_key=group[0] if len(group) == 1 else None)
        result['metrics'].extend(part['metrics'])
    return result


def aggregate_data(m, data):
    automatic = automatic_only(m)
    latest, defects = adopted(data)
    result, review = [], []
    sources = m['source_keys']
    # H01 기대 대상은 검증된 기준에서만 읽는다. 실행의 all_excluded로 추론하지 않는다.
    h1, good = [], 0
    for s in sources:
        refs = [r for r in data['reference_units'] if r['source_key'] == s and 'H01' in r['applicability']]
        applicability = {r['applicability']['H01'] for r in refs}
        if applicability == {False}:
            h1.append('not_applicable')
        elif applicability != {True}:
            h1.append('undetermined')
        else:
            attempts = [a for a in data['execution'] if a['record_kind'] == 'attempt' and s in a['source_keys']
                        and a['checkpoint'] == 'service_final' and a['stage'] == 'N6' and a['is_final_attempt']]
            if len(attempts) == 1:
                a = attempts[0]
                ok = a['execution_status'] in ('success', 'recovered', 'degraded') and bool(a['artifact_ids']) and a['applied_to_result']
                h1.append('measured' if ok else 'no_output')
                good += ok
            else:
                h1.append('undetermined')
    h = metric(m, 'H01', 'service_final', 'deliverable', h1, good, len(sources) - h1.count('not_applicable'))
    if 'undetermined' in h1:
        h.update(value=None, display_reason='기대 대상 또는 최종 실행 기록 확인 필요')
    result.append(h)
    diagnostics = {}
    checkpoint_statuses = {}
    for mid, cp in (('H02', 'machine_snapshot'), ('H03', 'service_final')):
        statuses, grades, checked = [], Counter(), 0
        for s in sources:
            e = latest.get((s, cp, mid, None))
            status = e['evaluation_status'] if e else 'evaluation_failed'
            statuses.append(status)
            if not e:
                review.append({'source_key': s, 'checkpoint': cp, 'reason': '평가 기록 없음'})
                continue
            checked += status == 'measured' and bool(e.get('human_checked'))
            if status == 'measured':
                observed = [d for d in defects.values() if d['evaluation_id'] == e['evaluation_id']]
                g, roots = grade(observed)
                grades[g] += 1
                for d in roots:
                    diagnostics[(s, cp, d['root_defect_id'] or d['defect_id'])] = d
                    if d['severity'] in ('critical', 'major'):
                        review.append({'source_key': s, 'checkpoint': cp, 'defect_record_id': d['defect_record_id'], 'reason': d['severity']})
                if g == 'A':
                    review.append({'source_key': s, 'checkpoint': cp, 'reason': 'A등급 표본 후보(선정 수 미정)'})
            else:
                review.append({'source_key': s, 'checkpoint': cp, 'reason': status})
        for g in ('A', 'B', 'C'):
            r = metric(m, mid, cp, g, statuses, grades[g])
            r.update(human_checked_count=checked, ai_only_count=len([s for s in statuses if s == 'measured']) - checked)
            result.append(r)
        result.append(metric(m, mid, cp, '평가 가능률', statuses, statuses.count('measured'), len(statuses) - statuses.count('not_applicable')))
        checkpoint_statuses[cp] = statuses
        if mid == 'H03':
            for severity in ('critical', 'major'):
                dd = [d for (s, c, _), d in diagnostics.items() if c == cp and d['severity'] == severity]
                result.append(metric(m, mid, cp, severity, statuses, len(dd), int('measured' in statuses), 'count'))
            affected = {s for (s, c, _), d in diagnostics.items() if c == cp and d['severity'] in ('critical', 'major')}
            result.append(metric(m, mid, cp, '중대 결함 보유 원본', statuses, len(affected), int('measured' in statuses), 'count'))
    for cp in CHECKPOINTS:
        statuses, good, checked = [], 0, 0
        for r in data['reference_units']:
            if r['unit_type'] != 'critical_fact':
                continue
            applicable = r['applicability'].get('H04', {}).get(cp)
            e = latest.get((r['source_key'], cp, 'H04', r['reference_unit_id']))
            status = 'not_applicable' if applicable is False else (e['evaluation_status'] if e and applicable is True else 'undetermined')
            statuses.append(status)
            if status == 'measured':
                good += e['observed_value']['accurate'] is True and e['observed_value']['readable'] is True
                checked += bool(e.get('human_checked'))
        result.append(metric(m, 'H04', cp, 'preserved', statuses, good))
        result.append(metric(m, 'H04', cp, '평가 가능률', statuses, statuses.count('measured'), len(statuses) - statuses.count('not_applicable')))
        for r in result[-2:]:
            r.update(human_checked_count=checked, ai_only_count=statuses.count('measured') - checked)
        if not any(r['unit_type'] == 'critical_fact' for r in data['reference_units']):
            for r in result[-2:]:
                r.update(value=None, display_reason='핵심 사실 기준 자료 미확인', applicable_count=None, measured_count=None)
    if automatic:
        row = {k: None for k in METRIC_FIELDS}
        row.update(evaluation_run_id=m['evaluation_run_id'], metric_id='H05', scope='all',
                   breakdown='측정 범위 제외', display_reason=H05_REASON, evidence_refs=[],
                   aggregation_basis=[], include_in_presentation=False)
        result.append(row)
    else:
        edits = {}
        for e in data['execution']:
            if e['record_kind'] == 'user_edit':
                edits[e['source_key'], e['stage']] = e
        statuses, buckets, totals, modified = [], Counter(), Counter(), 0
        for s in sources:
            n3, n5 = edits.get((s, 'N3')), edits.get((s, 'N5'))
            if not n3 or not n5:
                statuses.append('undetermined')
                continue
            statuses.append('measured')
            c3, c5 = n3['change_count'], n5['change_count']
            n = c3 + c5
            buckets['0' if n == 0 else '1' if n == 1 else '2~3' if n < 4 else '4+'] += 1
            modified += n > 0
            totals.update(N3=c3, N5=c5, user_retry=n3['user_retry_count'] + n5['user_retry_count'])
        for bucket in ('0', '1', '2~3', '4+'):
            result.append(metric(m, 'H05', None, bucket, statuses, buckets[bucket]))
        for k in ('N3', 'N5', 'user_retry'):
            r = metric(m, 'H05', None, k, statuses, totals[k], 1, 'count')
            if not statuses.count('measured'):
                r.update(value=None, display_reason='수정 기록 미확인')
            result.append(r)
        for k in ('N3', 'N5'):
            r = metric(m, 'H05', None, k + '_수정발생이미지당평균', statuses, totals[k], modified, 'mean')
            if modified == 0 and statuses.count('measured'):
                r['display_reason'] = '해당 대상 없음'
            result.append(r)
    for cp in CHECKPOINTS:
        for origin in ORIGINS:
            for severity in SEVERITIES:
                matching = [d for (s, c, _), d in diagnostics.items() if c == cp and d['origin_stage'] == origin and d['severity'] == severity]
                result.append(metric(m, None, cp, {'origin_stage': origin, 'severity': severity, 'affected_sources': len({d['source_key'] for d in matching}) if 'measured' in checkpoint_statuses[cp] else None},
                                     checkpoint_statuses[cp], len(matching), int('measured' in checkpoint_statuses[cp]), 'count', False))
    # 관찰되지 않은 운영값은 0 또는 성공으로 대체하지 않는다.
    attempts = [e for e in data['execution'] if e['record_kind'] == 'attempt' and not e.get('result_only')]
    logical = defaultdict(list)
    for a in attempts:
        logical[a['logical_execution_id']].append(a)
    for status in ('success', 'recovered', 'degraded', 'blocked', 'no_output'):
        finals = [a for a in attempts if a['is_final_attempt']]
        state = ['measured'] * len(finals) + ['undetermined'] * (len(logical) - len(finals))
        result.append(metric(m, 'OP02', None, status, state, sum(a['execution_status'] == status for a in finals), len(logical)))
    known_retry = [v for v in logical.values() if all('retry_origin' in a for a in v)]
    retried = sum(any(a.get('retry_origin') == 'auto' for a in v) for v in known_retry)
    retry_metric = metric(m, 'OP03', None, '자동 재시도 발생률', ['measured'] * len(known_retry) + ['undetermined'] * (len(logical)-len(known_retry)), retried, len(logical))
    if len(known_retry) != len(logical): retry_metric.update(value=None, display_reason='재시도 원인 미확인')
    result.append(retry_metric)
    for stage in sorted({a['stage'] for a in attempts}):
        selected = [a for a in attempts if a['stage'] == stage]
        for label, count in (('자동 재시도 건수', sum(a.get('retry_origin') == 'auto' for a in selected)),
                             ('회복 수', sum(a['is_final_attempt'] and a['execution_status']=='recovered' for a in selected))):
            result.append(metric(m,'OP03',None,{'stage':stage,'result':label},['measured']*len(selected),count,1,'count',False))
    for code in sorted({code for a in attempts for code in a['error_codes']}):
        affected = {a['job_id'] for a in attempts if code in a['error_codes']}
        result.append(metric(m,'OP02',None,{'error_code':code,'unit':'job'},['measured']*len(affected),len(affected),1,'count',False))

    # 제품 사용량은 시도 행 또는 별도 usage 행 중 한 곳에만 기록해야 한다.
    usage_rows = [e for e in data['execution'] if (e['record_kind'] == 'attempt' and e.get('usage') is not None) or e.get('purpose') == 'product_generation']
    observed_usage = [e for e in usage_rows if e.get('usage') is not None]
    for key in ('calls', 'prompt_token_count', 'candidates_token_count', 'thoughts_token_count'):
        known = [e['usage'][key] for e in observed_usage if type(e['usage'].get(key)) is int and e['usage'][key] >= 0]
        r = metric(m, 'OP05', None, key, ['measured'] * len(known), sum(known), 1, 'count', False)
        if not usage_rows or len(known) != len(usage_rows):
            r.update(value=None, display_reason='사용량 일부 또는 전체 미확인')
        result.append(r)
    from .operations import aggregate as operation_metrics
    result.extend(operation_metrics(m, data['execution'], metric))
    for rid, sid, _ in REGIONS:
        if source_key(sid) not in sources:
            continue
        for cp in CHECKPOINTS:
            review.append({'source_key': source_key(sid), 'checkpoint': cp, 'reason': rid})
    # 모든 출력에 계산 근거 ID를 연결한다. 검수 이력은 원본 AI 파일을 변경하지 않는다.
    basis = {k: [r[ID_FIELD[k]] for r in v] for k, v in data.items()}
    for r in result:
        r['aggregation_basis'] = basis
    return {'formal_complete': False, 'approval_pending': True, 'metrics': result, 'review_required': review,
            'readiness_issues': readiness(m)}


def export(root, output):
    """원본 회차·이력을 덮어쓰지 않는 파생 집계 디렉터리."""
    result = aggregate(root)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    csv_write(out / 'metrics.csv', METRIC_FIELDS, result['metrics'])
    write_new(out / 'review-required.json', result['review_required'])
    write_new(out / 'validation.json', {'issues': [], 'readiness_issues': result['readiness_issues'],
                                     'formal_complete': False, 'approval_pending': True})
    lines = ['# Baseline 잠정 집계', '', '공식 완료 아님 · 비용·실행 승인 보류', '', '| 지표 | 체크포인트 | 구분 | 값 |', '|---|---|---|---|']
    for r in result['metrics']:
        if r['metric_id'] and r['metric_id'].startswith('H'):
            value = r['value'] if r['value'] is not None else '— / ' + r['display_reason']
            lines.append(f"| {r['metric_id']} | {r['checkpoint'] or ''} | {r['breakdown']} | {value} |")
    (out / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result
