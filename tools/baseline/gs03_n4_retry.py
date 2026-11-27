"""GS-03 중단 자료의 JOB-06 후보 검토. 모델 실행/상태 우회 기능 없음."""
from pathlib import Path
from .core import read
from .gs03_recovery import budget_state, object_index


def retry_candidates(data):
    jobs = data['job']
    if len(jobs) != 1 or (jobs[0]['current_step'], jobs[0]['status']) != ('N4', 'failed'):
        raise ValueError('N4 failed 작업만 검토')
    tasks = {t['id']: t for t in data['job_async_task']}
    blocks = {b['id']: b for b in data['text_block']}
    candidates = []
    for task in tasks.values():
        if not (task.get('stage') == 'translate' and task['is_current'] and task['status'] == 'failed'):
            continue
        parent = tasks.get(task['parent_task_id'])
        if not parent or parent['status'] != 'running' or parent.get('cancelled_at') is not None:
            raise ValueError('대표 실행 중단/종료: JOB-06 불가')
        expected = task['input_manifest']['expected']
        targets = [b for b in expected if blocks[b]['block_status'] == 'machine' and blocks[b]['trans_1'] is None]
        preserved = [b for b in expected if blocks[b]['block_status'] == 'machine' and blocks[b]['trans_1'] is not None]
        candidates.append(dict(task_id=task['id'], section_id=task['unit_id'],
            blocking=task['error_code'] != 'TRANSLATE_PARTIAL',
            retry_count=task['retry_count'], max_retry=task['max_retry'],
            retries_remaining=task['max_retry']-task['retry_count'],
            failed_block_ids=sorted(targets), preserved_block_ids=sorted(preserved)))
    if not candidates or not any(c['blocking'] for c in candidates):
        raise ValueError('차단 번역 작업 없음: 다른 원인 확인 필요')
    return sorted(candidates, key=lambda c:(not c['blocking'], c['task_id']))


def inspect(root):
    root = Path(root)
    manifest = read(root/'manifest.json')
    if manifest['product_commit'] != 'bfa3b700144467f4adf7d6802b7b2e93791fe6de':
        raise ValueError('검토한 v1.0.1 제품 필요')
    data = read(root/'evidence/GS-03.json')
    candidates = retry_candidates(data)
    objects = object_index(root/'objects')
    seed = budget_state(root/'usage.jsonl')
    return {'source_run':manifest['evaluation_run_id'], 'candidates':candidates,
            'recommended_task_ids':[c['task_id'] for c in candidates if c['blocking'] and c['retries_remaining']>0],
            'budget_seed':seed, 'verified_objects':len(objects),
            'execution_approved':False, 'model_calls':0,
            'warning':'JOB-06 허용 검증과 번역 성공 보장은 별개. 기존 증거 보존을 위해 별도 복제본에서 실행.'}


def probe_job06(session_factory, job_id, seller_id, task_id):
    """테스트 전용 DB에서 실제 제품 JOB-06을 호출하되 dispatch를 캡처만 한다.

    호출자는 격리 복제 DB를 제공해야 한다. 원본 DB/일반 실행 URL 거부.
    제품 상태 전이는 endpoint만 수행한다. 워커·GPU·모델은 호출하지 않는다.
    """
    from unittest.mock import patch
    from sqlalchemy import text
    from app import execution
    from app.routers.jobs import retry_task
    with session_factory() as db:
        url = db.get_bind().url
        if url.host not in ('localhost','127.0.0.1') or not url.database.startswith('px_baseline_gs03_n4probe_'):
            raise ValueError('전용 로컬 비과금 probe DB 필요')
        def all_rows(table):
            return [dict(r) for r in db.execute(text('SELECT * FROM '+table+' ORDER BY id')).mappings()]
        before = {t:all_rows(t) for t in ('text_block','section','task_artifact','task_handoff','job_async_task')}
        old = next(t for t in before['job_async_task'] if t['id']==task_id)
        with patch.object(execution,'dispatch') as dispatch:
            response = retry_task(job_id, task_id, db=db, seller_id=seller_id)
            dispatch.assert_called_once_with([response['taskId']])
        after = {t:all_rows(t) for t in before}
        for table in ('text_block','section','task_artifact','task_handoff'):
            if before[table] != after[table]:raise AssertionError(table+' 기존 결과 변경')
        new = next(t for t in after['job_async_task'] if t['id']==response['taskId'])
        if len(after['job_async_task']) != len(before['job_async_task'])+1:raise AssertionError('추가 작업 수')
        if new['retry_origin']!='user' or new['retry_count']!=old['retry_count']+1:raise AssertionError('사용자 재시도 이력')
        if new['stage']!='translate' or new['status']!='pending':raise AssertionError('번역 작업 외 실행')
        expected = set(old['input_manifest']['expected'])
        blocks = {b['id']:b for b in before['text_block']}
        failed = {b for b in expected if blocks[b]['block_status']=='machine' and blocks[b]['trans_1'] is None}
        actual = {b['block_id'] for b in new['input_manifest']['targets']}
        if actual != failed:raise AssertionError('실패 블록 외 재계산')
        successful = {b for b in expected if blocks[b]['block_status']=='machine' and blocks[b]['trans_1'] is not None}
        if {b['block_id'] for b in new['input_manifest']['preserved']} != successful:raise AssertionError('성공분 참조 누락')
        job = dict(db.execute(text('SELECT * FROM job WHERE id=:j'),{'j':job_id}).mappings().one())
        if (job['current_step'],job['status']) != ('N4','processing'):raise AssertionError('제품 상태 전이')
        return {'task_id':task_id,'new_task_id':new['id'],'target_ids':sorted(actual),
                'preserved_ids':sorted(successful),'retry_count':new['retry_count'],'max_retry':new['max_retry'],
                'dispatch_captured_not_executed':True,'existing_results_unchanged':True,'model_calls':0}


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.source), ensure_ascii=False, indent=2))
