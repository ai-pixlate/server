"""GS-03 별도 실험 실행기. baseline/be.py@af2e716의 실행 흐름을 격리 복제.
원본 실행기는 변경하지 않는다. 제품 import 이전에 별도 승인과 해시를 검사한다.
"""
import os
import sys
import re
import platform
from pathlib import Path
from .be import (Meter, check_runtime, digest, write_new, now, initialize_native_runtime,
                 verify_database_binding, FileStore, isolated_connections, safe_record,
                 SequentialDispatcher, append, observe, drive_job, read)
from .gs03_supplement import GS03_IDS, validate, canonical, preflight

def _run_prepared(out, execute=False):
    """새 격리 DB의 참조 데이터를 사용. 실제 모델 실행은 명시적 옵션 뒤에서만 가능."""
    if not execute:
        raise ValueError("실제 모델 호출은 별도 승인 후 --execute-models가 필요합니다")
    from .gs03_supplement import require_approval
    require_approval(out)
    preflight(out)
    if any(k == "app" or k.startswith("app.") or k == "pipeline" or k.startswith("pipeline.") for k in sys.modules):
        raise ValueError("제품 모듈이 이미 로드됐습니다. 새 측정 프로세스가 필요합니다")
    out = Path(out).resolve()
    from . import protocol
    from .interaction import Collector, HumanSession
    from .product_review import ProductReview
    manifest, data = protocol.load(out)
    if validate(out):
        raise ValueError('생성 회차 명세·승인·입력 검증 필요(기준 자료 동결은 채점 전 필수)')
    generation = manifest['generation_config']
    from .preflight import verify_dependency_lock
    lock = (manifest.get('environment') or {}).get('dependency_lock')
    if not lock:
        raise ValueError('격리 환경 dependency_lock 경로·해시 필요')
    protocol.local_file(out, lock['path'], lock['sha256'])
    verify_dependency_lock(out / lock['path'])
    config = {'product_root': generation['product_root'], 'product_commit': manifest['product_commit'],
              'protocol': 'v1', 'products': manifest['case_inputs'], 'budget': generation['budget'],
              'environment': manifest['environment']}
    root, url = check_runtime(config)
    inputs = [dict(source_id=a['artifact_id'], path=str(out/a['path']), sha256=a['sha256'],
                   width=a['width'], height=a['height']) for a in manifest['artifacts'] if a['kind']=='original']
    if [i['source_id'] for i in inputs] != list(GS03_IDS) or any(digest(i['path']) != i['sha256'] for i in inputs):
        raise ValueError('GS-03 18장 입력 목록 또는 해시 불일치')
    Meter(config['budget'], lambda e: None)
    environment = config.get("environment", {})
    if any(not environment.get(k) for k in ("hardware", "resource_limits", "concurrent_work", "model_assets")):
        raise ValueError("장비·자원 제한·동시 작업·모델 파일 목록 필요")
    assets = []
    for asset in environment["model_assets"]:
        if not asset.get("model") or not asset.get("path") or not asset.get("sha256"):
            raise ValueError("모델별 실제 파일 경로와 SHA-256 필요")
        if digest(asset["path"]) != asset["sha256"]:
            raise ValueError("모델 파일 해시 불일치")
        assets.append(asset)
    # 동일 묶음 재실행을 금지한다. 중단 복구는 기존 기록을 검토한 별도 작업이다.
    native_runtime = initialize_native_runtime()
    sys.path.insert(0, str(root))
    os.environ["DATABASE_URL"] = url
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["PIXLATE_GPU_WORK_DIR"] = str(out / "gpu-work")
    os.environ["PIXLATE_POLICY_RULES"] = str(root / "pipeline/data/policy_rules.json")
    os.environ["PIXLATE_STYLE_DEFAULTS"] = str(root / "pipeline/data/style_defaults.json")
    os.environ["PIXLATE_PIPELINE_CONFIG"] = str(out / 'config-120.toml')
    from sqlalchemy import MetaData, Table, select, text
    from sqlalchemy.engine import make_url
    from app import db, execution, ai_adapters, storage
    from app.storage import ObjectMissing
    from app.flows import analysis, common, downstream, final
    from app.flows import gpu_worker
    if Path(db.__file__).resolve().parent != root / "app":
        raise ValueError("선택한 릴리즈 외부의 app 모듈이 로드됐습니다")
    verify_database_binding(db, url, make_url)
    store = FileStore(out / "objects", ObjectMissing)
    with isolated_connections(storage, execution, ai_adapters, store):
        metadata = MetaData()
        def table(name):
            return Table(name, metadata, autoload_with=db.engine, extend_existing=True)
        def insert(conn, name, values):
            t = table(name)
            return conn.execute(t.insert().values(**values).returning(t.c.id)).scalar_one()
        def query(sql, **kw):
            with db.engine.connect() as conn:
                return [dict(r) for r in conn.execute(text(sql), kw).mappings()]
        # 참조 데이터 DB는 사전 준비한다. 기존 업무 데이터가 있으면 쓰지 않는다.
        if any(query(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in ("job", "seller", "brand")):
            raise ValueError("업무 데이터가 없는 전용 DB가 필요합니다")
        reference_tables = ("alembic_version", "master_country", "master_language", "category_master", "category_mapping",
                            "expression_dictionary", "expression_dictionary_evidence", "glossary", "master_data_version")
        references = {t: query(f"SELECT * FROM {t}") for t in reference_tables}
        if not references["expression_dictionary"]:
            raise ValueError("실제 사전 적재 필요")
        if canonical(safe_record(references)) != canonical(read(out / 'expected-references.json')):
            raise ValueError('당시 사전·용어집 DB와 불일치')
        # 연결·스키마·사전 대조 성공 후 업무 쓰기 전에 배타적으로 시작 기록.
        write_new(out / "started.json", {"at": now(), "product_commit": config["product_commit"]})
        write_new(out / 'native-runtime.json', native_runtime)
        write_new(out / "references.json", safe_record(references))
        current = [None, None]
        from app import tasks
        import importlib.metadata
        def resolve(name):
            if name not in set(execution.TASK_BY_STAGE.values()) | {execution.ADOPT_TASK}:
                raise ValueError("알 수 없는 BE 작업")
            return getattr(tasks, name.rsplit(".", 1)[-1])
        def record_dispatch(e):
            if e["event"] == "task_started":
                current[1] = {"task": e["task"], "args": e["args"]}
            append(out / "dispatch.jsonl", {"source_id": current[0], **e})
            if e["event"] == "task_finished":
                current[1] = None
        dispatch = SequentialDispatcher(resolve, record_dispatch)
        execution.set_dispatcher(dispatch)
        write_new(out / "runtime.json", {"python": sys.version, "platform": platform.platform(),
                  "machine": platform.machine(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
                  "environment": environment, "model_assets": assets,
                  "environment_settings": {k: v for k, v in os.environ.items() if k.startswith(("PIXLATE_", "OMP_", "MKL_", "CUDA_VISIBLE_")) and not re.search("KEY|TOKEN|SECRET|PASSWORD|URL", k)},
                  "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata["Name"]},
                  "product_commit": config["product_commit"], "reference_sha256": digest(out / "references.json"),
                  "settings": {str(p.relative_to(root)): digest(p) for base in ("pipeline/config", "pipeline/prompts", "pipeline/data")
                               for p in (root / base).rglob("*") if p.is_file() and p.suffix in (".toml", ".json", ".md", ".txt", ".otf", ".ttf")},
                  "tool_sha256": {p.name: digest(p) for p in Path(__file__).parent.glob("*.py")}})
        def snapshot(sid, jid):
            data = {t: query(f"SELECT * FROM {t} WHERE job_id = :j", j=jid)
                    for t in ("source_image", "section", "job_async_task", "task_handoff", "task_artifact", "deliverable")}
            data["text_block"] = query("SELECT tb.* FROM text_block tb JOIN section s ON s.id=tb.section_id WHERE s.job_id=:j", j=jid)
            data["section_verdict"] = query("SELECT v.* FROM section_verdict v JOIN section s ON s.id=v.section_id WHERE s.job_id=:j", j=jid)
            data["deliverable_section"] = query("SELECT ds.* FROM deliverable_section ds JOIN deliverable d ON d.id=ds.deliverable_id WHERE d.job_id=:j", j=jid)
            data["job"] = query("SELECT * FROM job WHERE id=:j", j=jid)
            write_new(out / "evidence" / (sid + ".json"), safe_record(data))
            return data
        active = [None]
        def usage_sink(e):
            collector = active[0]
            if e['status'] == 'reserved' or collector is None: return
            evidence = collector.evidence(safe_record(e))
            usage = dict(e.get('usage') or {}, calls=1)
            cost = {'amount': e['cost'], 'currency': e.get('currency'), 'basis': '토큰 단가 추정; 실제 청구 아님'} if e.get('cost') is not None else None
            collector.event('usage', (e.get('task_context') or {}).get('task'), evidence,
                            session_id=None,purpose='product_generation',model=e.get('model'),usage=usage,cost=cost,
                            unavailable_reason=None if cost else '실패 또는 사용량 미확인; 예약 유지',call_id=e['call'])
        def stage_sink(e):
            collector=active[0]
            if collector:
                evidence=collector.evidence(e)
                collector.operation('resource',e['stage'],{'name':e['stage']+'_wall_time','amount':e['seconds'],'unit':'seconds'},evidence)
                if e['stage']=='inpaint_inference':
                    collector.operation('resource',e['stage'],{'name':'inpaint_inference_attempts','amount':1,'unit':'count'},evidence)
        try:
            with observe(out, current, config['budget'], usage_sink, stage_sink):
                for case in ('GS-03',):
                    items = [i for i in inputs if i['source_id'].startswith(case)]
                    current[0] = case
                    product = config['products'][case]
                    sources = [protocol.source_key(i['source_id']) for i in items]
                    # claim은 업무 DB 변경보다 먼저 생성한다. 중단 회차 자동 재개 금지.
                    collector = Collector(out, case, sources)
                    collector.claim()
                    with db.engine.begin() as conn:
                        seller = insert(conn, 'seller', {'cognito_sub':'baseline:'+case,'email':case+'@baseline.invalid'})
                        brand = insert(conn, 'brand', {'seller_id':seller,'name_ko':product['name_ko'],'name_en':product['name_en']})
                        jid = insert(conn, 'job', {'seller_id':seller,'brand_id':brand,
                                     **{k:product[k] for k in ('product_name','regulatory_class','internal_category','target_country','target_lang')}})
                        bindings = {}
                        for order,item in enumerate(items,1):
                            key='source/'+item['source_id']+'.jpg'
                            store.put(key,Path(item['path']).read_bytes(),'image/jpeg')
                            source = insert(conn,'source_image',{'job_id':jid,'upload_order':order,'file_url':key,
                                            'width':item['width'],'height':item['height'],'sha256':item['sha256']})
                            bindings[source]=protocol.source_key(item['source_id'])
                    collector.job_id=jid
                    active[0]=collector
                    collector.operation('job_created','N1',{'bindings':bindings})
                    adapter=ProductReview(jid,seller,db.SessionLocal,query,dispatch,store,bindings)
                    human_session=HumanSession(collector,adapter)
                    checkpoint_ids={}
                    def checkpoint(cp, job):
                        state=adapter.snapshot('N5')
                        checkpoint_ids[cp]=collector.checkpoint(cp,state,adapter.files(cp,state))
                    def human(stage, job):
                        if stage=='N3':
                            state=adapter.snapshot(stage)
                            adapter.regulatory_evidence(collector,state)
                            collector.operation('resource','N2',{'name':'n2_sections','amount':len(state['sections']),'unit':'count'})
                        return human_session.review(stage)
                    status='interrupted'
                    try:
                        status=drive_job(jid,seller,db.SessionLocal,query,dispatch,analysis,common,downstream,final,
                            lambda w: collector.evidence({'warnings':w}),checkpoint,human,collector.operation)
                        if status=='completed':
                            adapter.spec_evidence(collector)
                            saved=adapter.save()
                            collector.operation('job_saved','N6',saved)
                    finally:
                        terminal=safe_record(snapshot(case,jid))
                        evidence=collector.evidence(terminal)
                        from .collection import terminal_attempts
                        terminal_attempts(collector,terminal,bindings,evidence)
                        current_manifest=read(out/'manifest.json')
                        for source in sources:
                            arts=[a['artifact_id'] for a in current_manifest['artifacts'] if a['checkpoint']=='service_final' and source in a['source_keys']]
                            collector.event('attempt','N6',evidence,source_keys=[source],checkpoint='service_final',
                                logical_execution_id=f'{jid}:delivery:{source}',target_id=source,attempt_number=1,
                                execution_status='success' if arts else 'no_output',error_codes=[],fallback_type=None,
                                is_final_attempt=True,applied_to_result=bool(arts),artifact_ids=arts,usage=None,cost=None,
                                result_only=True,unavailable_reason='H01 산출물 보존 요약; 재시도는 원시 시도 기록 참조')
                        collector.evidence({'status':status,'checkpoint_ids':checkpoint_ids})
        finally:
            write_new(out / "object-index.json", store.index)
