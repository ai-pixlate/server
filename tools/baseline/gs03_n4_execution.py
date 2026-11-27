"""승인된 JOB-06 한 번과 N5/N6 연결. 제품 상태는 공개 제품 함수만 변경한다."""
from pathlib import Path
from .core import read, digest
from .gs03_recovery import (CumulativeMeter, budget_state, object_index, database_snapshot,
                            snapshot_hash, POLICY, REFERENCE_TABLES)
from .gs03_supplement import validate, tree_hashes, canonical, BUDGET, TARGET
from .gs03_database import database_url
from .gs03_n4_retry import retry_candidates
from .be import BudgetStop

MODE='gs03_n4_job06_clone'
CLONE_DB='px_baseline_gs03_supplement_20261126_r3_recovery2'
SEED={'calls':163,'occupied_usd':9.21462925}


def verify_artifact(a,idx):
    ref=a.get('source_ref') or {}
    key=a.get('verified_key') or ref.get('key')
    if a['state']!='adopted' or not key or idx.get(key,{}).get('sha256')!=a['sha256']:
        raise ValueError('채택 객체 불일치')
    if not a.get('verified_key') and ref.get('sha256')!=a['sha256']:
        raise ValueError('재사용 원본 해시 불일치')


class SingleRetryMeter(CumulativeMeter):
    def __init__(self,budget,record,*,history,allowed):
        super().__init__(budget,record,history=history)
        if self.calls!=163 or abs(self.charged-SEED['occupied_usd'])>1e-9:raise ValueError('승계 원장 불일치')
        self.allowed=allowed
    def _call(self,fn,*args,**kwargs):
        if self.calls>=164 or not self.allowed():raise BudgetStop('승인한 block 232 단일 요청 범위 밖')
        return super()._call(fn,*args,**kwargs)


def preflight(out,url_file):
    import json
    from .preflight import verify_dependency_lock
    out=Path(out);m=read(out/'manifest.json');r=m['recovery']
    if r['mode']!=MODE or r['policy']!=POLICY or m['supplement']['database_name']!=CLONE_DB:raise ValueError('N4 복제 범위')
    if (out/'started.json').exists() or (out/'generation-claims').exists():raise ValueError('중복 실행 차단')
    if validate(out):raise ValueError('준비 묶음 검증 실패')
    verify_dependency_lock(out/m['environment']['dependency_lock']['path'])
    source=Path(r['source_path'])
    if tree_hashes(source)!=r['source_hashes']:raise ValueError('중단 증거 변경')
    if budget_state(out/'usage.jsonl')!=SEED or digest(out/'usage.jsonl')!=digest(source/'usage.jsonl'):raise ValueError('예산 원장 변경')
    idx=object_index(out/'objects')
    if idx!=object_index(source/'objects'):raise ValueError('객체 복제 불일치')
    data=database_snapshot(database_url(url_file,CLONE_DB))
    if snapshot_hash(data)!=r['database_sha256']:raise ValueError('복제 DB 불일치')
    candidates=retry_candidates(data)
    blockers=[c for c in candidates if c['blocking']]
    if len(blockers)!=1 or blockers[0]['task_id']!=227 or blockers[0]['failed_block_ids']!=[232] or blockers[0]['retries_remaining']!=1:
        raise ValueError('승인된 재시도 대상 변경')
    if any(t['parent_task_id'] and t['is_current'] and t['status'] in ('running','pending') for t in data['job_async_task']):raise ValueError('진행 중 하위 작업')
    if data['deliverable'] or len(data['source_image'])!=18:raise ValueError('작업 범위 변경')
    for a in data['task_artifact']:
        verify_artifact(a,idx)
    refs=json.loads(json.dumps({t:data[t] for t in REFERENCE_TABLES},default=str))
    if canonical(refs)!=canonical(read(out/'expected-references.json')):raise ValueError('참조 DB 변경')
    return {'executed':False,'seed':SEED,'objects':len(idx),'task_id':227,'block_ids':[232]}


def require_execution(out):
    from .protocol import local_file, timestamp
    from datetime import date
    out=Path(out);m=read(out/'manifest.json');a=m['recovery'].get('execution_approval') or {}
    expected={'approved':True,'evaluation_run_id':m['evaluation_run_id'],'product_commit':TARGET,
              'task_id':227,'block_ids':[232],'additional_calls':1,'additional_reservation_usd':1.04,
              'seed':SEED,'budget':BUDGET,'policy':POLICY}
    if any(a.get(k)!=v for k,v in expected.items()) or not a.get('approved_by'):raise ValueError('JOB-06 단일 실행 승인 필요')
    timestamp(a.get('approved_at'));ref=a.get('evidence_ref') or {};local_file(out,ref.get('path'),ref.get('sha256'))
    if date.today()>date(2026,12,31):raise ValueError('요금 재확인 필요')


def drive(jid,seller,sessions,query,dispatch,analysis,common,downstream,final,record,checkpoint,human,operation):
    from app.routers.jobs import retry_task
    return drive_with_retry(retry_task,jid,seller,sessions,query,dispatch,final,record,checkpoint,human,operation)


def drive_with_retry(retry,jid,seller,sessions,query,dispatch,final,record,checkpoint,human,operation):
    operation('stage_started','N4',{})
    with sessions() as db:response=retry(jid,227,db=db,seller_id=seller)
    task=query('SELECT * FROM job_async_task WHERE id=:t',t=response['taskId'])[0]
    if task['retry_count']!=2 or task['max_retry']!=2 or task['retry_origin']!='user' or task['input_manifest']['targets']!=[{'block_id':232,'revision':0}]:
        raise ValueError('생성된 재시도 범위 불일치; dispatch 미실행')
    record({'job06_response':response,'human_reviewed':False})
    dispatch.drain()
    job=query('SELECT * FROM job WHERE id=:j',j=jid)[0]
    if (job['current_step'],job['status'])!=('N5','review'):
        operation('stage_stopped','N4',{});return 'failed'
    operation('stage_finished','N4',{})
    checkpoint('machine_snapshot',jid)
    response=human('N5',jid)
    operation('stage_started','N6',{})
    with sessions() as db:
        record(final.unresolved_warnings(db,jid))
        final.confirm(db,jid,seller,response['acknowledgedWarnings'])
    dispatch.drain()
    with sessions() as db:done=final.can_save(db,jid)
    operation('stage_finished' if done else 'stage_stopped','N6',{})
    if not done:return 'failed'
    checkpoint('service_final',jid)
    return 'completed'
