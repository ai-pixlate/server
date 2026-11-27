"""새 GS-03 DB에만 v1.0.1 스키마·보존 참조 자료를 복원한다. 모델 호출 없음."""
import argparse
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from .core import read
from .gs03_supplement import TARGET, validate, preflight, DB_NAME, dedicated_database_name

def database_url(path, expected_name=DB_NAME):
    dedicated_database_name(expected_name)
    from sqlalchemy.engine import make_url
    url=make_url(Path(path).read_text(encoding='utf-8').strip())
    if url.drivername!='postgresql+psycopg' or url.host not in ('localhost','127.0.0.1','::1') or url.database!=expected_name:
        raise ValueError('GS-03 전용 로컬 DB만 준비 가능')
    return url

def restore(out, url_file):
    from sqlalchemy import create_engine, text, MetaData, Table
    from .preflight import verify_dependency_lock
    out=Path(out).resolve();issues=validate(out)
    if issues:raise ValueError(issues)
    manifest=read(out/'manifest.json');product=Path(manifest['generation_config']['product_root'])
    if (out/'started.json').exists():raise ValueError('실행한 회차의 DB 복원 금지')
    verify_dependency_lock(out/manifest['environment']['dependency_lock']['path'])
    if subprocess.check_output(['git','-C',str(product),'rev-parse','HEAD'],text=True).strip()!=TARGET:
        raise ValueError('v1.0.1 checkout 불일치')
    name=dedicated_database_name(manifest['supplement']['database_name'])
    url=database_url(url_file,name)
    expected=read(out/'expected-references.json')
    if expected['alembic_version']!=[{'version_num':'0008'}]:raise ValueError('당시 스키마 0008 필요')
    admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT',connect_args={'connect_timeout':3})
    try:
        with admin.connect() as conn:
            if conn.execute(text('SELECT 1 FROM pg_database WHERE datname=:name'),{'name':name}).scalar():
                raise ValueError('기존 DB는 덮어쓰지 않음; preflight로 상태 확인 필요')
            conn.execute(text('CREATE DATABASE '+name))
    finally:admin.dispose()
    env={**os.environ,'DATABASE_URL':url.render_as_string(hide_password=False)}
    subprocess.run([sys.executable,'-B','-m','alembic','upgrade','0008'],cwd=product,env=env,check=True)
    engine=create_engine(url,connect_args={'connect_timeout':3});metadata=MetaData()
    try:
        with engine.begin() as conn:
            tables=[Table(name,metadata,autoload_with=conn) for name in expected if name!='alembic_version']
            for table in metadata.sorted_tables:
                if table not in tables:continue
                if conn.execute(text('SELECT count(*) FROM '+table.name)).scalar_one():
                    raise ValueError('새 DB 참조 테이블이 비어 있지 않음: '+table.name)
                rows=[]
                for original in expected[table.name]:
                    row=dict(original)
                    for column in table.columns:
                        value=row.get(column.name)
                        if isinstance(value,str):
                            kind=column.type.python_type
                            if kind is datetime:row[column.name]=datetime.fromisoformat(value)
                            elif kind is date:row[column.name]=date.fromisoformat(value)
                    rows.append(row)
                for start in range(0,len(rows),500):conn.execute(table.insert(),rows[start:start+500])
                # 명시 ID 복원 후 새 삽입의 시퀀스 충돌을 방지한다. 행 내용은 바꾸지 않는다.
                if 'id' in table.c:
                    seq=conn.execute(text('SELECT pg_get_serial_sequence(:name,\'id\')'),{'name':table.name}).scalar()
                    if seq and rows:
                        conn.execute(text('SELECT setval(CAST(:seq AS regclass), :value, true)'),{'seq':seq,'value':max(r['id'] for r in rows)})
    finally:engine.dispose()
    previous=os.environ.get('BASELINE_DATABASE_URL')
    try:
        os.environ['BASELINE_DATABASE_URL']=url.render_as_string(hide_password=False)
        return preflight(out)
    finally:
        if previous is None:os.environ.pop('BASELINE_DATABASE_URL',None)
        else:os.environ['BASELINE_DATABASE_URL']=previous

def main():
    p=argparse.ArgumentParser();p.add_argument('output');p.add_argument('--url-file',required=True)
    args=p.parse_args()
    import json
    print(json.dumps(restore(args.output,args.url_file)))
if __name__=='__main__':main()
