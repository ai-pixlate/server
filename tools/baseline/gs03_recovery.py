"""r3 N3 정지점의 별도 복제본 준비·검증. 실행은 별도 승인, 원본 재개 금지."""
import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path
from .core import read, write_new, digest, now
from .gs03_supplement import prepare as prepare_base, validate, canonical, BUDGET, TARGET, SOURCES
from .gs03_database import database_url
from .be import Meter

MODE = 'gs03_r3_n3_clone'
POLICY = {'human_review': False, 'edit': False, 'wait_for_input': False,
          'acknowledge_translation_failed': True, 'preserve_all_warnings': True}
SOURCE_RUN = 'gs03-supplement-20261126-r3'
SOURCE_DB = 'px_baseline_gs03_supplement_20261126_r3'
CLONE_DB = SOURCE_DB + '_recovery1'
REFERENCE_TABLES = ('alembic_version','master_country','master_language','category_master','category_mapping',
                    'expression_dictionary','expression_dictionary_evidence','glossary','master_data_version')


def budget_state(path):
    last = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        row = json.loads(line); call = row['call']
        if type(call) is not int or call < 1 or row.get('model') != BUDGET['model']:
            raise ValueError('호출 기록 불일치')
        if row['status'] not in ('reserved', 'done', 'failed'):
            raise ValueError('정상 상한 내에서 중단한 회차만 복구 가능')
        cost = row.get('cost')
        if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0 or cost > BUDGET['reserve_per_call']):
            raise ValueError('비용 기록 불일치')
        last[call] = row
    if sorted(last) != list(range(1, len(last)+1)):
        raise ValueError('호출 번호 누락')
    amount = sum(r['cost'] if r.get('cost') is not None else BUDGET['reserve_per_call'] for r in last.values())
    if len(last) > BUDGET['max_calls'] or amount > BUDGET['max_amount']:
        raise ValueError('기존 상한 초과')
    return {'calls': len(last), 'occupied_usd': amount}


class CumulativeMeter(Meter):
    def __init__(self, budget, record, *, history):
        super().__init__(budget, record)
        state = budget_state(history)
        self.calls, self.charged = state['calls'], state['occupied_usd']


def object_index(root):
    root = Path(root); index = {}
    for line in (root/'objects.jsonl').read_text(encoding='utf-8').splitlines():
        row=json.loads(line); sha=row['sha256']
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('객체 해시 형식')
        if digest(root/sha)!=sha:raise ValueError('객체 손상')
        index[row['key']]={k:row[k] for k in ('sha256','bytes','content_type')}
    return index


def database_snapshot(url):
    from sqlalchemy import create_engine, text
    engine=create_engine(url,connect_args={'connect_timeout':3})
    try:
        with engine.connect() as conn:
            conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            names=conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars().all()
            return {name:[dict(r) for r in conn.execute(text('SELECT * FROM "'+name.replace('"','""')+'"')).mappings()] for name in names}
    finally:engine.dispose()


def snapshot_hash(data):
    normalized=canonical(json.loads(json.dumps(data,default=str)))
    return hashlib.sha256(json.dumps(normalized,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def check_n3(data):
    jobs=data['job']
    if len(jobs)!=1 or jobs[0]['current_step']!='N3' or jobs[0]['status']!='review':raise ValueError('N3 검수 정지점 필요')
    tasks=data['job_async_task']
    if not tasks or any(t['status']!='done' for t in tasks):raise ValueError('미완료 작업 존재')
    if any(t.get('stage') not in (None,'analyze','judge') for t in tasks):raise ValueError('N4 이후 작업 존재')
    if any(h['state']!='adopted' for h in data['task_handoff']):raise ValueError('미채택 인계 존재')
    if data['deliverable'] or data['deliverable_section']:raise ValueError('최종 산출물 존재')
    if len(data['source_image'])!=18 or len(data['seller'])!=1:raise ValueError('GS-03 단독 작업 필요')


def prepare(source, out, source_url_file, target_url_file, pg_bin):
    source=Path(source).resolve();out=Path(out).resolve();pg_bin=Path(pg_bin)
    if source.name!=SOURCE_RUN or source==out or source in out.parents or out in source.parents:
        raise ValueError('r3와 분리된 새 복제 경로 필요')
    if validate(source):raise ValueError('r3 보존 검증 실패')
    m=read(source/'manifest.json')
    src=database_url(source_url_file,SOURCE_DB);dst=database_url(target_url_file,CLONE_DB)
    before=database_snapshot(src);check_n3(before)
    index=object_index(source/'objects');seed=budget_state(source/'usage.jsonl')
    if seed['calls']!=89 or abs(seed['occupied_usd']-3.69037875)>1e-9:raise ValueError('검토한 r3 호출 기록과 다름')
    from .gs03_supplement import tree_hashes
    hashes=tree_hashes(source)
    # 실패 시 생성한 준비물을 덮어쓰거나 기존 DB를 삭제하지 않는다.
    prepare_base(m['supplement']['baseline_root'],m['generation_config']['product_root'],out,CLONE_DB)
    shutil.copytree(source,out/'parent-r3')
    shutil.copytree(source/'objects',out/'objects')
    shutil.copyfile(source/'usage.jsonl',out/'usage.jsonl')
    from sqlalchemy import create_engine,text
    admin=create_engine(dst.set(database='postgres'),isolation_level='AUTOCOMMIT',connect_args={'connect_timeout':3})
    try:
        with admin.connect() as conn:
            if conn.execute(text('SELECT 1 FROM pg_database WHERE datname=:n'),{'n':CLONE_DB}).scalar():raise ValueError('복제 DB 이미 존재; 덮어쓰기 금지')
            conn.execute(text('CREATE DATABASE '+CLONE_DB))
    finally:admin.dispose()
    dump=out/'parent-r3/evidence/dedicated-db-interrupted.dump'
    expected=read(source/'interruption-summary.json')['preserved_db_dump_sha256']
    if digest(dump)!=expected:raise ValueError('보존 덤프 해시 불일치')
    env={**os.environ,'PGPASSWORD':dst.password or ''}
    cmd=[str(pg_bin/'pg_restore.exe'),'-h',dst.host,'-p',str(dst.port),'-U',dst.username,'-d',dst.database,'--exit-on-error','--no-owner','--no-privileges',str(dump)]
    result=subprocess.run(cmd,env=env,capture_output=True,text=True)
    if result.returncode:raise ValueError('새 DB 복원 실패; 기존 DB 변경 없음')
    clone=database_snapshot(dst);check_n3(clone)
    if snapshot_hash(before)!=snapshot_hash(clone):raise ValueError('복제 DB 내용 불일치')
    if snapshot_hash(database_snapshot(src))!=snapshot_hash(before) or tree_hashes(source)!=hashes:raise ValueError('원본 변경 감지')
    fresh=read(out/'manifest.json')
    fresh['measurement_scope']['human_intervention']='preauthorized_no_review'
    fresh['recovery']={'mode':MODE,'source_path':str(source),'source_run':SOURCE_RUN,
        'source_hashes':hashes,'database_sha256':snapshot_hash(clone),'seed':seed,'policy':POLICY,
        'job_id':before['job'][0]['id'],'seller_id':before['seller'][0]['id'],
        'bindings':{str(s['id']):SOURCES[s['upload_order']-1] for s in before['source_image']},
        'execution_approval':None,'formal_complete':False,'official_baseline':False}
    (out/'manifest.json').write_text(json.dumps(fresh,ensure_ascii=False,indent=2),encoding='utf-8')
    write_new(out/'clone-preparation.json',{'at':now(),'r3_unchanged':True,'objects':len(index),'seed':seed,'executed':False})
    return preflight(out,target_url_file)


def preflight(out,url_file):
    out=Path(out).resolve();m=read(out/'manifest.json');r=m.get('recovery') or {}
    if r.get('mode')!=MODE or r.get('policy')!=POLICY or m['supplement']['database_name']!=CLONE_DB:raise ValueError('복구 범위 불일치')
    if (out/'started.json').exists() or (out/'generation-claims').exists():raise ValueError('복제 회차 재실행 금지')
    if validate(out):raise ValueError('복제 자료 검증 실패')
    from .preflight import verify_dependency_lock
    verify_dependency_lock(out/m['environment']['dependency_lock']['path'])
    from .gs03_supplement import tree_hashes
    if tree_hashes(Path(r['source_path']))!=r['source_hashes'] or tree_hashes(out/'parent-r3')!=r['source_hashes']:raise ValueError('r3 증거 변경')
    if budget_state(out/'usage.jsonl')!=r['seed']:raise ValueError('누적 예산 기록 변경')
    if digest(out/'usage.jsonl')!=digest(out/'parent-r3/usage.jsonl'):raise ValueError('호출 증거 변경')
    idx=object_index(out/'objects')
    if idx!=object_index(out/'parent-r3/objects'):raise ValueError('객체 색인 불일치')
    url=database_url(url_file,CLONE_DB);data=database_snapshot(url);check_n3(data)
    if snapshot_hash(data)!=r['database_sha256']:raise ValueError('복제 DB 변경')
    if sorted(s['upload_order'] for s in data['source_image'])!=list(range(1,19)):raise ValueError('원본 순서 불일치')
    bindings={str(s['id']):SOURCES[s['upload_order']-1] for s in data['source_image']}
    if bindings!=r['bindings'] or data['job'][0]['id']!=r['job_id'] or data['seller'][0]['id']!=r['seller_id']:
        raise ValueError('복구 작업 연결 불일치')
    for source in data['source_image']:
        if idx.get(source['file_url'],{}).get('sha256')!=source['sha256']:raise ValueError('DB 원본 객체 불일치')
    for artifact in data['task_artifact']:
        if artifact['state']!='adopted' or idx.get(artifact['verified_key'],{}).get('sha256')!=artifact['sha256']:
            raise ValueError('DB 채택 객체 불일치')
    if canonical(json.loads(json.dumps({t:data[t] for t in REFERENCE_TABLES},default=str)))!=canonical(read(out/'expected-references.json')):raise ValueError('참조 자료 불일치')
    return {'read_only':True,'database_checked':True,'objects':len(idx),'seed':r['seed'],'executed':False}


def require_execution(out):
    out=Path(out);m=read(out/'manifest.json');r=m['recovery'];a=r.get('execution_approval') or {}
    expected={'approved':True,'evaluation_run_id':m['evaluation_run_id'],'product_commit':TARGET,
              'source_run':SOURCE_RUN,'budget':BUDGET,'seed':r['seed'],'policy':POLICY}
    if any(a.get(k)!=v for k,v in expected.items()) or not a.get('approved_by'):raise ValueError('복제본 무대기 유료 실행 승인 필요')
    from .protocol import timestamp,local_file
    timestamp(a.get('approved_at'));ref=a.get('evidence_ref') or {};local_file(out,ref.get('path'),ref.get('sha256'))
    if date.today()>date(2026,12,31):raise ValueError('요금 재확인 필요')


class NoWaitSession:
    def __init__(self,collector,adapter):self.c,self.adapter=collector,adapter
    def review(self,stage):
        state=copy.deepcopy(self.adapter.snapshot(stage));warnings=state.get('warnings',[])
        if stage not in ('N3','N5'):raise ValueError('알 수 없는 체크포인트')
        if any(w.get('code')!='translation_failed' for w in warnings):raise ValueError('승인 정책 밖 필수 경고')
        evidence=self.c.evidence({'stage':stage,'state':state,'policy':POLICY,'actor':'experiment_automation',
                                  'human_reviewed':False,'acknowledgedWarnings':warnings if stage=='N5' else []})
        if self.adapter.snapshot(stage)!=state:raise ValueError('확정 중 상태 변경')
        # 독립 실험의 사전 승인 증거다. 공식 운영 이벤트나 사람 검수로 기록하지 않는다.
        return {'original':state['values'],'final':state['values'],'acknowledgedWarnings':warnings if stage=='N5' else []}


def record_recovery_binding(collector, bindings):
    return collector.evidence({'kind':'recovery_bound','stage':'N3','bindings':bindings,
                               'actor':'experiment_automation','human_reviewed':False})


def drive_from_n3(jid,seller,sessions,query,dispatch,analysis,common,downstream,final,record,checkpoint,human,operation):
    """N2 재호출 없이 현재 N3부터. 제품의 proceed/confirm만 사용한다."""
    import time
    if query('SELECT * FROM job WHERE id=:j',j=jid)[0]['current_step']!='N3':raise ValueError('N3 필요')
    human('N3',jid)
    def run(stage, action):
        start=time.monotonic();operation('stage_started',stage,{})
        try:
            with sessions() as db:action(db)
            dispatch.drain()
        except BaseException:
            operation('stage_stopped',stage,{});raise
        job=query('SELECT * FROM job WHERE id=:j',j=jid)[0]
        if stage=='N4':done=job['current_step']=='N5' and job['status']=='review'
        else:
            with sessions() as db:done=final.can_save(db,jid)
        operation('stage_finished' if done else 'stage_stopped',stage,{'seconds':time.monotonic()-start})
        return done
    run('N4',lambda db:downstream.proceed(db,jid,seller))
    job=query('SELECT * FROM job WHERE id=:j',j=jid)[0]
    if job['current_step']!='N5' or job['status']!='review':return 'failed'
    checkpoint('machine_snapshot',jid)
    response=human('N5',jid)
    def confirm(db):
        record(final.unresolved_warnings(db,jid))
        final.confirm(db,jid,seller,response['acknowledgedWarnings'])
    run('N6',confirm)
    with sessions() as db:
        if not final.can_save(db,jid):return 'failed'
    checkpoint('service_final',jid)
    return 'completed'


def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='command',required=True)
    a=s.add_parser('prepare');a.add_argument('source');a.add_argument('output');a.add_argument('--source-url-file',required=True);a.add_argument('--url-file',required=True);a.add_argument('--pg-bin',required=True)
    a=s.add_parser('preflight');a.add_argument('output');a.add_argument('--url-file',required=True)
    args=p.parse_args()
    result=prepare(args.source,args.output,args.source_url_file,args.url_file,args.pg_bin) if args.command=='prepare' else preflight(args.output,args.url_file)
    print(json.dumps(result))


if __name__=='__main__':main()
