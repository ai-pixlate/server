"""중단 N4의 격리 복제와 공식 JOB-07/SEC-04 재실행 준비. 준비는 비과금."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from .core import read, write_new, digest, now
from .gs03_recovery import (database_snapshot, snapshot_hash, object_index, budget_state,
    POLICY, REFERENCE_TABLES, drive_from_n3)
from .gs03_supplement import prepare as prepare_base, validate, tree_hashes, canonical, TARGET, BUDGET, SOURCES
from .gs03_database import database_url

MODE='gs03_n4_restart_clone'
SOURCE_DB='px_baseline_gs03_supplement_20261126_r3_recovery2'
CLONE_DB='px_baseline_gs03_supplement_20261126_r3_recovery3'
PROBE_DB='px_baseline_gs03_supplement_20261126_r3_recovery5'
SEED={'calls':164,'occupied_usd':10.25462925}
SOURCE_HASH='9370ca612293492fce222b034469b53f823578e1f798b7e59069274415836f57'


def n2_signature(data):
    aid=data['job'][0]['current_analysis_task_id']
    tasks=[t for t in data['job_async_task'] if t['id']==aid or t['parent_task_id']==aid]
    ids={t['id'] for t in tasks}
    handoffs=[h for h in data['task_handoff'] if h['task_id'] in ids]
    hids={h['id'] for h in handoffs}
    sec_keys=('id','source_image_id','section_order','top_offset','height','bbox','category','bucket','image_key','analysis_task_id','original_verdict')
    # is_excluded는 0007의 N4 라벨/로고 파생 GENERATED 값이므로 N2 원문에 포함하지 않는다.
    block_keys=('id','section_id','block_order','source_ko','source_lines','bbox','origin_block_ids','ocr_confidence')
    return snapshot_hash({'analysis_id':aid,'tasks':tasks,'handoffs':handoffs,
        'artifacts':[a for a in data['task_artifact'] if a['handoff_id'] in hids],
        'sources':data['source_image'], 'verdicts':data['section_verdict'],
        'sections':[{k:s[k] for k in sec_keys} for s in data['section']],
        'blocks':[{k:b[k] for k in block_keys} for b in data['text_block']],
        'refs':{t:data[t] for t in REFERENCE_TABLES}})


def check_source(data):
    if snapshot_hash(data)!=SOURCE_HASH:raise ValueError('검토한 Task 265 중단 DB와 다름')
    t=next(t for t in data['job_async_task'] if t['id']==265)
    if (t['status'],t['retry_count'],t['max_retry'])!=('running',2,2):raise ValueError('Task 265 상태 변경')
    if any(h['task_id']==265 for h in data['task_handoff']):raise ValueError('신규 인계 존재')
    if len(data['source_image'])!=18 or data['deliverable']:raise ValueError('GS03 범위 변경')


def restore_new(dump,url,pg_bin):
    from sqlalchemy import create_engine,text
    admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            if c.execute(text('SELECT 1 FROM pg_database WHERE datname=:n'),{'n':url.database}).scalar():raise ValueError('기존 DB 덮어쓰기 금지')
            c.execute(text('CREATE DATABASE '+url.database))
    finally:admin.dispose()
    env={**os.environ,'PGPASSWORD':url.password or ''}
    args=[str(Path(pg_bin)/'pg_restore.exe'),'-h',url.host,'-p',str(url.port),'-U',url.username,'-d',url.database,'--exit-on-error','--no-owner','--no-privileges',str(dump)]
    result=subprocess.run(args,env=env,capture_output=True)
    if result.returncode:raise ValueError('새 DB 복원 실패; 보존 후 중단')


def prepare(source,out,source_url_file,url_file,pg_bin):
    source=Path(source).resolve();out=Path(out).resolve()
    if source==out or source in out.parents or out in source.parents:raise ValueError('별도 경로 필요')
    src=database_url(source_url_file,SOURCE_DB);dst=database_url(url_file,CLONE_DB)
    before=database_snapshot(src);check_source(before)
    if budget_state(source/'usage.jsonl')!=SEED:raise ValueError('원장 불일치')
    hashes=tree_hashes(source);m=read(source/'manifest.json')
    prepare_base(m['supplement']['baseline_root'],m['generation_config']['product_root'],out,CLONE_DB)
    shutil.copytree(source,out/'parent-interrupted')
    shutil.copytree(source/'objects',out/'objects')
    shutil.copyfile(source/'usage.jsonl',out/'usage.jsonl')
    dump=out/'source-interrupted.dump'
    result=subprocess.run([str(Path(pg_bin)/'pg_dump.exe'),'-h',src.host,'-p',str(src.port),'-U',src.username,'-d',src.database,'-Fc','-f',str(dump)],env={**os.environ,'PGPASSWORD':src.password or ''},capture_output=True)
    if result.returncode:raise ValueError('원본 읽기 전용 덤프 실패')
    restore_new(dump,dst,pg_bin)
    if snapshot_hash(database_snapshot(dst))!=SOURCE_HASH:raise ValueError('복원 불일치')
    if snapshot_hash(database_snapshot(src))!=SOURCE_HASH or tree_hashes(source)!=hashes:raise ValueError('원본 변경')
    fresh=read(out/'manifest.json');fresh['measurement_scope']['human_intervention']='preauthorized_no_review'
    fresh['recovery']={'mode':MODE,'source_path':str(source),'source_hashes':hashes,'database_sha256':SOURCE_HASH,
        'dump_sha256':digest(dump),'seed':SEED,'policy':POLICY,'job_id':before['job'][0]['id'],
        'seller_id':before['seller'][0]['id'],'n2_signature':n2_signature(before),
        'bindings':{str(s['id']):SOURCES[s['upload_order']-1] for s in before['source_image']},
        'execution_approval':None,'official_baseline':False,'formal_complete':False}
    (out/'manifest.json').write_text(json.dumps(fresh,ensure_ascii=False,indent=2),'utf-8')
    write_new(out/'clone-preparation.json',{'at':now(),'model_calls':0,'source_unchanged':True,'seed':SEED})
    return preflight(out,url_file)


def preflight(out,url_file):
    from .preflight import verify_dependency_lock
    out=Path(out);m=read(out/'manifest.json');r=m['recovery']
    if r['mode']!=MODE or r['policy']!=POLICY or m['supplement']['database_name']!=CLONE_DB:raise ValueError('범위 불일치')
    if (out/'started.json').exists() or (out/'generation-claims').exists():raise ValueError('중복 실행 차단')
    if validate(out):raise ValueError('준비 묶음 검증 실패')
    verify_dependency_lock(out/m['environment']['dependency_lock']['path'])
    source=Path(r['source_path'])
    if tree_hashes(source)!=r['source_hashes'] or tree_hashes(out/'parent-interrupted')!=r['source_hashes']:raise ValueError('중단 증거 변경')
    if digest(out/'usage.jsonl')!=digest(source/'usage.jsonl') or budget_state(out/'usage.jsonl')!=SEED:raise ValueError('원장 변경')
    idx=object_index(out/'objects')
    if idx!=object_index(source/'objects'):raise ValueError('객체 변경')
    if digest(out/'source-interrupted.dump')!=r['dump_sha256']:raise ValueError('덤프 변경')
    data=database_snapshot(database_url(url_file,CLONE_DB));check_source(data)
    if n2_signature(data)!=r['n2_signature']:raise ValueError('N2 변경')
    from .gs03_n4_execution import verify_artifact
    for a in data['task_artifact']:verify_artifact(a,idx)
    return {'model_calls':0,'seed':SEED,'database_unchanged':True,'n2_preserved':True,'objects':len(idx),'execution_approved':False}


def require_execution(out):
    from .protocol import local_file,timestamp
    from datetime import date
    out=Path(out);m=read(out/'manifest.json');a=m['recovery'].get('execution_approval') or {}
    expected={'approved':True,'evaluation_run_id':m['evaluation_run_id'],'product_commit':TARGET,
        'mode':MODE,'seed':SEED,'budget':BUDGET,'policy':POLICY,'n2_rerun':False}
    if any(a.get(k)!=v for k,v in expected.items()) or not a.get('approved_by'):raise ValueError('N4 전체 신규 실행 별도 비용 승인 필요')
    timestamp(a.get('approved_at'));ref=a.get('evidence_ref') or {};local_file(out,ref.get('path'),ref.get('sha256'))
    if date.today()>date(2026,12,31):raise ValueError('요금 재확인 필요')


def official_abort(db,jid,seller):
    from app.routers.jobs import abort_job
    return abort_job(jid,db=db,seller_id=seller)


def official_proceed(db,jid,seller):
    from fastapi import Response
    from app.routers.sections import proceed
    return proceed(jid,response=Response(),db=db,seller_id=seller)


def drive(jid,seller,sessions,query,dispatch,analysis,common,downstream,final,record,checkpoint,human,operation):
    from types import SimpleNamespace
    with sessions() as db:response=official_abort(db,jid,seller)
    if response['returnTo']!='N3':raise ValueError('JOB07 N3 복귀 실패')
    record({'job07_response':response,'human_reviewed':False,'new_n4_run':True})
    return drive_from_n3(jid,seller,sessions,query,dispatch,analysis,common,
        SimpleNamespace(proceed=official_proceed),final,record,checkpoint,human,operation)


def probe(out,url_file,evidence):
    """별도 probe DB만 공식 전이한다. dispatch/모델은 차단하고 실행용 복제 DB는 보존."""
    import sys
    from unittest.mock import patch
    from sqlalchemy import text
    out=Path(out);evidence=Path(evidence)
    url=database_url(url_file,PROBE_DB)
    m=read(out/'manifest.json');root=Path(m['generation_config']['product_root'])
    if any(k=='app' or k.startswith('app.') for k in sys.modules):raise ValueError('새 probe 프로세스 필요')
    before=database_snapshot(url);check_source(before)
    sys.path.insert(0,str(root));os.environ['DATABASE_URL']=url.render_as_string(hide_password=False)
    os.environ['AWS_EC2_METADATA_DISABLED']='true'
    os.environ['PIXLATE_PIPELINE_CONFIG']=str((out/'config-120.toml').resolve())
    from app import db,execution,storage,ai_adapters
    from app.storage import ObjectMissing
    from .be import FileStore,isolated_connections,verify_database_binding
    from sqlalchemy.engine import make_url
    verify_database_binding(db,url.render_as_string(hide_password=False),make_url)
    if Path(db.__file__).resolve().parent!=root/'app':raise ValueError('제품 코드 경로')
    store=FileStore(out/'objects',ObjectMissing);store.index=object_index(out/'objects')
    object_before=tree_hashes(out/'objects');jid=m['recovery']['job_id'];seller=m['recovery']['seller_id']
    oldrun=next(t for t in before['job_async_task'] if t['run_kind']=='downstream' and t['parent_task_id'] is None and t['status']=='running')
    from google.genai.models import Models
    with isolated_connections(storage,execution,ai_adapters,store), patch.object(execution,'dispatch') as dispatch, patch.object(Models,'generate_content',side_effect=AssertionError('모델 호출 금지')) as model:
        with db.SessionLocal() as session:aborted=official_abort(session,jid,seller)
        after_abort=database_snapshot(url)
        if aborted!={'returnTo':'N3','jobId':jid} or after_abort['job'][0]['status']!='review':raise AssertionError('JOB07 전이')
        if n2_signature(before)!=n2_signature(after_abort):raise AssertionError('N2 변경')
        cancelled=next(t for t in after_abort['job_async_task'] if t['id']==265)
        if cancelled['status']!='cancelled' or cancelled['retry_count']!=2:raise AssertionError('기존 작업 취소/횟수')
        with db.SessionLocal() as session:first=official_proceed(session,jid,seller)
        after=database_snapshot(url)
        with db.SessionLocal() as session:second=official_proceed(session,jid,seller)
        repeated=database_snapshot(url)
        if first!=second or first['taskId']==oldrun['id']:raise AssertionError('새 실행/중복 차단')
        if snapshot_hash(after)!=snapshot_hash(repeated):raise AssertionError('중복 SEC04 변경')
        dispatch.assert_called_once();model.assert_not_called()
        if n2_signature(before)!=n2_signature(after):raise AssertionError('N2 원문/분할/분석 근거 변경')
        if (after['job'][0]['current_step'],after['job'][0]['status'])!=('N4','processing'):raise AssertionError('N4 전이')
        newtasks=[t for t in after['job_async_task'] if t['parent_task_id']==first['taskId']]
        if not newtasks or any(t['stage']!='label' or t['status']!='pending' for t in newtasks):raise AssertionError('신규 label 작업')
        if set(dispatch.call_args.args[0])!={t['id'] for t in newtasks}:raise AssertionError('dispatch 범위')
        if any(b['trans_1'] is not None for b in after['text_block']):raise AssertionError('기존 번역 혼합')
        if any(s['render_image_key'] is not None or s['inpaint_image_url'] is not None for s in after['section']):raise AssertionError('이전 이미지 혼합')
        for table in ('task_artifact','task_handoff'):
            if before[table]!=after[table]:raise AssertionError('기존 채택 증거 변경')
        old={t['id']:t for t in before['job_async_task']}
        for t in after['job_async_task']:
            if t['id'] in old and old[t['id']]['status'] in ('done','failed') and t!=old[t['id']]:raise AssertionError('완료/실패 이력 변경')
    if tree_hashes(out/'objects')!=object_before or budget_state(out/'usage.jsonl')!=SEED:raise AssertionError('객체/원장 변경')
    result={'model_calls':0,'job07':aborted,'sec04':first,'duplicate_sec04':second,
        'old_run':oldrun['id'],'new_label_tasks':len(newtasks),'n2_preserved':True,
        'task265_cancelled_by_product':True,'retry_count_preserved':True,
        'old_evidence_preserved':True,'derived_outputs_cleared_by_product':True,
        'dispatch_captured_not_executed':True,'duplicate_dispatch_blocked':True,'seed':SEED}
    write_new(evidence,result)
    db.engine.dispose()
    return result


def estimate(source):
    """기존 r3 N2(1~89), N4(90~163), 단일 JOB06(164)을 분리한 경험값."""
    source=Path(source)
    if budget_state(source/'usage.jsonl')!=SEED:raise ValueError('산정 원장 불일치')
    last={}
    for line in (source/'usage.jsonl').read_text('utf-8').splitlines():
        row=json.loads(line);last[row['call']]=row
    groups={}
    for number,row in last.items():
        if not 90<=number<=163:continue
        task=row['task_context']['task']
        g=groups.setdefault(task,{'calls':0,'known_token_estimate_usd':0.0,'unsettled_calls':0})
        g['calls']+=1;g['known_token_estimate_usd']+=row.get('cost') or 0
        g['unsettled_calls']+=row.get('cost') is None
    known=sum(g['known_token_estimate_usd'] for g in groups.values())
    unknown=sum(g['unsettled_calls'] for g in groups.values())
    empirical=known+unknown*BUDGET['reserve_per_call']
    return {'historical_n4_calls':74,'groups':groups,'historical_n4_occupied_usd':empirical,
        'historical_job06_call164_occupied_usd':BUDGET['reserve_per_call'],
        'expected_calls_basis':'기존 최초 라벨 36 + 최초 번역 36 + 자동 재시도 2; 재현 보장/상한 아님',
        'remaining_calls':BUDGET['max_calls']-SEED['calls'],
        'remaining_occupied_usd':BUDGET['max_amount']-SEED['occupied_usd'],
        'empirical_cumulative_calls':SEED['calls']+74,
        'empirical_cumulative_occupied_usd':SEED['occupied_usd']+empirical,
        'actual_bill':'unknown','model_calls_performed':0}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='command',required=True)
    a=s.add_parser('prepare');a.add_argument('source');a.add_argument('output');a.add_argument('--source-url-file',required=True);a.add_argument('--url-file',required=True);a.add_argument('--pg-bin',required=True)
    a=s.add_parser('preflight');a.add_argument('output');a.add_argument('--url-file',required=True)
    a=s.add_parser('probe');a.add_argument('output');a.add_argument('--url-file',required=True);a.add_argument('--evidence',required=True)
    a=p.parse_args()
    if a.command=='prepare':result=prepare(a.source,a.output,a.source_url_file,a.url_file,a.pg_bin)
    elif a.command=='probe':result=probe(a.output,a.url_file,a.evidence)
    else:result=preflight(a.output,a.url_file)
    print(json.dumps(result,ensure_ascii=False))
