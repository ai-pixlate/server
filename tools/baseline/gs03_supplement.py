"""GS-03 보완 실험의 비과금 준비. 실행은 별도 승인 기록과 명시 옵션이 필요하다."""
import argparse
import copy
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path
from . import protocol
from .core import read, digest, write_new, IDS
from .approval import TARGET

GS03_IDS = tuple(x for x in IDS if x.startswith('GS-03'))
SOURCES = [protocol.source_key(x) for x in GS03_IDS]
OVERRIDE = {'merge.llm_timeout_s': 120}
BUDGET = dict(max_calls=500, max_amount=25, reserve_per_call=1.04, currency='USD',
              settle_usage=True, sdk_attempts=1, reservation_verified=True,
              model='gemini-3.8-flash', input_per_million=.75, output_per_million=3.75,
              basis='2026-11-26 공식 Standard 요금; 입력1048576/출력65536 한도: $1.032192; 예약 $1.04; 2026-12-31까지')

def canonical(value):
    if isinstance(value, list):
        return sorted((canonical(x) for x in value), key=lambda x: json.dumps(x,sort_keys=True,ensure_ascii=False))
    if isinstance(value, dict): return {k:canonical(v) for k,v in value.items()}
    return value

def override_config(raw):
    original=tomllib.loads(raw)
    if original['merge']['llm_timeout_s'] != 60: raise ValueError('원래 60초 조건 필요')
    lines=raw.splitlines(keepends=True); table=None; count=0
    for i,line in enumerate(lines):
        if line.startswith('['): table=line.split(']',1)[0][1:]
        if table=='merge' and re.match(r'^llm_timeout_s\s*=',line):
            lines[i]=re.sub(r'=\s*60\b','= 120',line,count=1);count+=1
    if count != 1: raise ValueError('병합 제한 키 불일치')
    changed=''.join(lines); expected=copy.deepcopy(original);expected['merge']['llm_timeout_s']=120
    if tomllib.loads(changed)!=expected: raise ValueError('허용 외 설정 변경')
    return changed

def tree_hashes(root):
    return {str(p.relative_to(root)).replace('\\','/'):digest(p) for p in sorted(root.rglob('*')) if p.is_file()}

def validate(out):
    out=Path(out).resolve();m=read(out/'manifest.json');issues=[]
    if m['source_keys']!=SOURCES or m['product_commit']!=TARGET or m['product_version']!='1.0.1': issues.append('범위/버전')
    if m.get('supplement',{}).get('override')!=OVERRIDE:issues.append('override')
    if m.get('measurement_scope')!={'measured_quality_metrics':['H01','H02','H03','H04'],'human_intervention':'confirmation_only'}:issues.append('사람 확인 범위')
    originals=[a for a in m['artifacts'] if a['kind']=='original']
    if [a['artifact_id'] for a in originals]!=list(GS03_IDS):issues.append('원본 목록')
    for a in originals:
        protocol.local_file(out,a['path'],a['sha256'])
    for f in m['files']:protocol.local_file(out,f['path'],f['sha256'])
    baseline=Path(m['supplement']['baseline_root'])
    old=read(baseline/'manifest.json')
    if m['case_inputs']!=old['case_inputs']:issues.append('상품 입력 변경')
    old_env=copy.deepcopy(old['environment']);old_env['dependency_lock']=m['environment']['dependency_lock']
    if m['environment']!=old_env:issues.append('환경 기록 변경')
    if m['generation_config']['budget']!=BUDGET:issues.append('검토된 예산 설정 변경')
    if canonical(read(out/'expected-references.json'))!=canonical(read(baseline/'references.json')):issues.append('당시 사전 변경')
    for path,sha in m['supplement']['baseline_hashes'].items():
        if digest(baseline/path)!=sha:issues.append('기존 자료 변경: '+path)
    root=Path(m['generation_config']['product_root'])
    for path,sha in m['supplement']['product_hashes'].items():
        if digest(root/path)!=sha:issues.append('제품 파일 변경: '+path)
    original=(root/'pipeline/config/default.toml').read_text(encoding='utf-8')
    if (out/'config-120.toml').read_text(encoding='utf-8')!=override_config(original):issues.append('설정 차이')
    for a in m['environment']['model_assets']:
        if digest(a['path'])!=a['sha256']:issues.append('모델 자산 변경')
    return issues + protocol.validate(out)

def prepare(baseline, product, out):
    baseline=Path(baseline).resolve();product=Path(product).resolve();out=Path(out).resolve()
    if out==baseline or baseline in out.parents or out in baseline.parents:raise ValueError('기존 회차와 출력 분리 필요')
    old=read(baseline/'manifest.json')
    if old['product_commit']!=TARGET:raise ValueError('v1.0.1 아님')
    head=subprocess.check_output(['git','-C',str(product),'rev-parse','HEAD'],text=True).strip()
    if head!=TARGET:raise ValueError('제품 HEAD 불일치')
    tracked=subprocess.check_output(['git','-C',str(product),'ls-files','app','pipeline','migrations','requirements.txt'],text=True).splitlines()
    hashes={}
    for name in tracked:
        p=product/name
        expected=subprocess.check_output(['git','-C',str(product),'show',TARGET+':'+name])
        actual=p.read_bytes()
        if actual.replace(b'\r\n',b'\n')!=expected.replace(b'\r\n',b'\n'):raise ValueError('제품 변경: '+name)
        hashes[name]=digest(p)
    out.mkdir(parents=True,exist_ok=False);(out/'artifacts').mkdir()
    m=copy.deepcopy(old)
    m.update(evaluation_run_id=out.name,source_keys=SOURCES,artifacts=[],files=[],
             reference_version=None,reference_freeze=None,evaluation_sessions=[],comparison=None,
             cost_approval={'pm_cost_agreed':False,'user_execution':None})
    m['generation_config'].update(product_root=str(product),budget=copy.deepcopy(BUDGET))
    m['supplement']={'baseline_root':str(baseline),'baseline_hashes':tree_hashes(baseline),
                    'product_hashes':hashes,'override':OVERRIDE,'official_baseline':False,
                    'same_conditions':False,'database_name':'px_baseline_gs03_supplement_20261126'}
    for a in old['artifacts']:
        if a['kind']=='original' and a['case_id']=='GS-03':
            protocol.local_file(baseline,a['path'],a['sha256'])
            shutil.copyfile(baseline/a['path'],out/a['path']);m['artifacts'].append(a)
    protocol.local_file(baseline,old['dataset_manifest_path'],old['dataset_manifest_sha256'])
    destination=out/old['dataset_manifest_path']
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(baseline/old['dataset_manifest_path'],destination)
    for source,dest in [(baseline/'references.json',out/'expected-references.json'),
                        (baseline/old['environment']['dependency_lock']['path'],out/'requirements.lock')]:
        shutil.copyfile(source,dest)
    m['environment']['dependency_lock']={'path':'requirements.lock','sha256':digest(out/'requirements.lock')}
    (out/'config-120.toml').write_text(override_config((product/'pipeline/config/default.toml').read_text(encoding='utf-8')),encoding='utf-8')
    for name in ['expected-references.json','requirements.lock','config-120.toml']:
        m['files'].append({'path':name,'sha256':digest(out/name)})
    for kind,fields in protocol.FIELDS.items():
        if kind=='human_review':
            with (out/'human_review.csv').open('w',encoding='utf-8',newline='') as f:csv.writer(f).writerow(fields.split())
        else:(out/(kind+'.jsonl')).touch(exist_ok=False)
    write_new(out/'manifest.json',m)
    problems=validate(out)
    if problems:raise ValueError(problems)
    return {'sources':len(SOURCES),'approved':False,'executed':False,'validation':problems}

def database_preflight(out, url):
    """제품 import·쓰기·모델 호출 없이 전용 DB를 읽기 전용 트랜잭션에서 확인."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from .be import safe_record
    out=Path(out);m=read(out/'manifest.json');parsed=make_url(url)
    if (parsed.drivername!='postgresql+psycopg' or parsed.host not in ('localhost','127.0.0.1','::1')
            or parsed.database!=m['supplement']['database_name']):
        raise ValueError('GS-03 전용 로컬 DB 필요')
    expected=read(out/'expected-references.json')
    tables=('alembic_version','master_country','master_language','category_master','category_mapping',
            'expression_dictionary','expression_dictionary_evidence','glossary','master_data_version')
    engine=create_engine(url,connect_args={'connect_timeout':3})
    try:
        with engine.connect() as conn:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            for name in ('job','seller','brand','source_image','section','job_async_task','task_handoff',
                         'task_artifact','deliverable','text_block','section_verdict','deliverable_section'):
                if conn.execute(text('SELECT count(*) FROM '+name)).scalar_one():
                    raise ValueError('업무 데이터가 없는 전용 DB 필요')
            actual={t:[dict(r) for r in conn.execute(text('SELECT * FROM '+t)).mappings()] for t in tables}
            if not actual['expression_dictionary'] or canonical(safe_record(actual))!=canonical(expected):
                raise ValueError('DB 스키마 버전 또는 당시 사전·용어집 불일치')
    finally:
        engine.dispose()
    return {'database_checked':True,'read_only':True,'executed':False}

def preflight(out):
    """실행 직전과 수동 점검에서 공유. 승인·시작 기록을 만들지 않는다."""
    out=Path(out)
    issues=validate(out)
    if issues:raise ValueError(issues)
    if (out/'started.json').exists():raise ValueError('중단 회차 자동 재실행 금지')
    m=read(out/'manifest.json')
    from .preflight import verify_dependency_lock
    from .be import check_runtime
    verify_dependency_lock(out/m['environment']['dependency_lock']['path'])
    _,url=check_runtime({'product_root':m['generation_config']['product_root'],'product_commit':TARGET,
                        'protocol':'v1','products':m['case_inputs']})
    return database_preflight(out,url)


def require_approval(out):
    out=Path(out);m=read(out/'manifest.json');u=(m.get('cost_approval') or {}).get('user_execution') or {}
    if u.get('approved') is not True:raise ValueError('GS-03 별도 비용·실행 승인 보류')
    issues=validate(out)
    if issues:raise ValueError(issues)
    if date.today()>date(2026,12,31):raise ValueError('요금 재확인 필요')
    for k,v in {'source_keys':SOURCES,'product_commit':TARGET,'override':OVERRIDE,
                'human_confirmation_actor':'예람','human_intervention_scope':'confirmation_only',
                'evaluation_run_id':m['evaluation_run_id'],'user_regeneration_allowed':False}.items():
        if u.get(k)!=v:raise ValueError('승인 조건 불일치: '+k)
    if u.get('budget')!=m['generation_config']['budget'] or u['budget']!=BUDGET:raise ValueError('예산 승인 불일치')
    if not u.get('approved_by'):raise ValueError('승인자 필요')
    protocol.timestamp(u.get('approved_at'))
    ref=u.get('evidence_ref') or {};protocol.local_file(out,ref.get('path'),ref.get('sha256'))
    from .be import check_runtime
    _,url=check_runtime({'product_root':m['generation_config']['product_root'],'product_commit':TARGET,
                        'protocol':'v1','products':m['case_inputs']})
    from urllib.parse import urlparse
    if urlparse(url).path!='/'+m['supplement']['database_name']:raise ValueError('GS-03 전용 신규 DB 필요')
    if (out/'started.json').exists():raise ValueError('중단 회차 자동 재실행 금지')
    return m

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('baseline');a.add_argument('product');a.add_argument('output')
    a=sub.add_parser('validate');a.add_argument('output')
    a=sub.add_parser('preflight');a.add_argument('output')
    a=sub.add_parser('execute');a.add_argument('output');a.add_argument('--execute-models',action='store_true')
    args=p.parse_args()
    if args.command=='prepare':print(json.dumps(prepare(args.baseline,args.product,args.output)))
    elif args.command=='preflight':print(json.dumps(preflight(args.output)))
    elif args.command=='validate':
        issues=validate(args.output);print(json.dumps(issues));sys.exit(bool(issues))
    else:
        if not args.execute_models:raise ValueError('명시적 실행 옵션 필요')
        require_approval(args.output)
        from .gs03_runner import _run_prepared
        _run_prepared(args.output,True)
if __name__=='__main__':main()
