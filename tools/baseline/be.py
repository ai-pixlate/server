"""릴리즈 BE 연결. 이 모듈의 import는 DB·모델을 실행하지 않는다."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import platform
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from .core import IDS, SequentialDispatcher, append as _append, digest, now, read, write_new as _write_new


class RecordingStop(BaseException):
    """수집기 오류를 제품의 Exception 재시도/대체 처리에 전달하지 않는다."""


def initialize_native_runtime():
    """Windows Paddle→Torch DLL 충돌 방지. 승인 뒤 호출, 가중치/추론 없음."""
    started = time.monotonic()
    if sys.platform != 'win32':
        return {'import_order': [], 'seconds': 0}
    if 'paddle' in sys.modules and 'torch' not in sys.modules:
        raise ValueError('Paddle보다 Torch가 먼저 필요합니다. 새 격리 프로세스를 사용하세요')
    import torch
    return {'import_order': ['torch', 'paddle (이후 OCR 초기화 시)'],
            'torch': torch.__version__, 'seconds': time.monotonic() - started}


def recorded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        raise RecordingStop(f"증거 수집 실패: {type(e).__name__}: {e}") from e


def append(*args, **kwargs):
    return recorded(_append, *args, **kwargs)


def write_new(*args, **kwargs):
    return recorded(_write_new, *args, **kwargs)


class BudgetStop(BaseException):
    """제품의 일반 Exception 재시도로 삼키지 않는 측정 중단."""


class FileStore:
    """불변 해시 파일 + 객체 키 색인. 삭제 시에도 수집 증거 바이트는 보존한다."""
    def __init__(self, root, missing=KeyError):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = {}
        self.lock = threading.RLock()
        self.missing = missing

    def put(self, key, data, content_type=None):
        sha = hashlib.sha256(data).hexdigest()
        with self.lock:
            p = self.root / sha
            if not p.exists():
                def save():
                    with p.open("xb") as f:
                        f.write(data)
                recorded(save)
            self.index[key] = {"sha256": sha, "bytes": len(data), "content_type": content_type}
            append(self.root / "objects.jsonl", {"key": key, **self.index[key]})

    def put_if_absent(self, key, data, content_type=None):
        with self.lock:
            if key in self.index:
                return False
            self.put(key, data, content_type)
            return True

    def get(self, key):
        with self.lock:
            if key not in self.index:
                raise self.missing(key)
            return recorded((self.root / self.index[key]["sha256"]).read_bytes)

    def exists(self, key):
        return key in self.index

    def delete(self, key):
        with self.lock:
            self.index.pop(key, None)

    def delete_prefix(self, prefix):
        if not prefix or not prefix.endswith("/"):
            raise ValueError("빈 접두사 또는 잘못된 접두사")
        with self.lock:
            keys = [k for k in self.index if k.startswith(prefix)]
            for k in keys:
                self.delete(k)
            return len(keys)

    def presign_put(self, key, ttl, content_type=None):
        return "baseline://put/" + hashlib.sha256(key.encode()).hexdigest()

    def presign_get(self, key, ttl):
        return "baseline://get/" + hashlib.sha256(key.encode()).hexdigest()


def safe_record(value):
    """DB 실행 권한과 임시 서명 URL은 평가 자료에 보관하지 않는다."""
    if isinstance(value, dict):
        return {k: safe_record(v) for k, v in value.items()
                if not re.search(r"(^token$)|(^|_)(password|secret|authorization|credential|presigned)(_|$)|lease_token|access_token|api_key", k, re.I)}
    if isinstance(value, (list, tuple)):
        return [safe_record(v) for v in value]
    if isinstance(value, str) and re.search(r"X-Amz-|[?&](token|key)=", value, re.I):
        return "[임시 인증 주소 생략]"
    return value


def check_runtime(config):
    root = Path(config["product_root"]).resolve()
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if head != config["product_commit"]:
        raise ValueError("product_root의 전체 HEAD SHA가 지정한 릴리즈와 다릅니다")
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--",
                                    "app", "pipeline", "migrations", "requirements.txt"], text=True)
    if dirty.strip():
        raise ValueError("제품 실행 코드에 미커밋 변경이 있습니다")
    url = os.environ.get("BASELINE_DATABASE_URL", "")
    parsed = urlparse(url)
    if parsed.scheme != "postgresql+psycopg" or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or not re.fullmatch(r"/px_baseline_[a-z0-9_]+", parsed.path):
        raise ValueError("로컬 px_baseline_* PostgreSQL DB를 BASELINE_DATABASE_URL로 지정해야 합니다")
    if config.get("protocol") != "v1":
        raise ValueError("지원 평가 명세는 v1입니다")
    if any(config["products"][g].get("target_country") != "US" or config["products"][g].get("target_lang") != "en" for g in ("GS-01", "GS-02", "GS-03")):
        raise ValueError("고정 평가 조건은 미국·영어입니다")
    return root, url


class Meter:
    """SDK 요청별 사용량 기록과 보수적 예약 예산. 단가 상한의 타당성은 실행 전 확인한다."""
    def __init__(self, budget, record):
        self.budget, self.record, self.calls = budget, record, 0
        self.charged = 0.0
        self.stopped = False
        self.lock = threading.RLock()
        for k in ("max_calls", "max_amount", "reserve_per_call"):
            if type(budget.get(k)) not in (int, float) or not math.isfinite(budget[k]) or budget[k] <= 0:
                raise ValueError(f"budget.{k}: 양수 필요")
        if type(budget["max_calls"]) is not int:
            raise ValueError("budget.max_calls: 양의 정수 필요")
        if not budget.get("currency") or not budget.get("basis"):
            raise ValueError("예산 통화와 호출당 예약 상한 근거 필요")
        if budget.get('settle_usage'):
            if budget.get('sdk_attempts') != 1 or not budget.get('reservation_verified'):
                raise ValueError('사용량 정산 전 SDK 1시도·예약 상한 검증 필요')
            for k in ('input_per_million', 'output_per_million'):
                if type(budget.get(k)) not in (int, float) or not math.isfinite(budget[k]) or budget[k] < 0:
                    raise ValueError('정산 단가 필요: ' + k)
            if not budget.get('model'):
                raise ValueError('정산 모델 필요')

    def call(self, fn, *args, **kwargs):
        with self.lock:
            return self._call(fn, *args, **kwargs)

    def _call(self, fn, *args, **kwargs):
        reserve = self.budget['reserve_per_call']
        if self.stopped or self.calls + 1 > self.budget["max_calls"] or self.charged + reserve > self.budget["max_amount"]:
            raise BudgetStop("추가 요청의 예산을 확보할 수 없어 중단")
        settle = self.budget.get('settle_usage', False)
        if settle and kwargs.get('model') != self.budget['model']:
            raise BudgetStop('승인된 예산 모델 밖 요청')
        self.calls += 1
        self.charged += reserve
        record = {"event": "model_call", "call": self.calls, "model": kwargs.get("model"), "at": now(),
                  "usage": None, "cost": None, "cost_reason": "실제 청구액/단가 정산 전", "status": "failed"}
        if settle:
            # 예약 기록 실패 시 요청을 보내지 않는다. 중단·미사용량은 예약액 유지.
            try:
                self.record({**record, 'status': 'reserved', 'reserved_amount': reserve})
            except BaseException:
                self.stopped = True
                raise RecordingStop('예산 예약 기록 실패')
        try:
            response = fn(*args, **kwargs)
            um = recorded(getattr, response, "usage_metadata", None)
            record.update(status="done", usage=recorded(um.model_dump, mode="json") if hasattr(um, "model_dump") else None)
            usage = record['usage']
            if settle and isinstance(usage, dict):
                keys = ('prompt_token_count', 'candidates_token_count', 'thoughts_token_count')
                if all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys):
                    amount = (usage[keys[0]] * self.budget['input_per_million'] +
                              (usage[keys[1]] + usage[keys[2]]) * self.budget['output_per_million']) / 1_000_000
                    record.update(cost=amount, cost_reason='단가 기반 추정', currency=self.budget['currency'])
                    self.charged += amount - reserve
                    if amount > reserve:
                        self.stopped = True
                        record['status'] = 'reservation_exceeded'
                        raise BudgetStop('실제 사용량이 검증된 예약 상한을 초과했습니다')
            return response
        finally:
            record['budget_consumed'] = self.charged
            try:
                recorded(self.record, record)
            except BaseException:
                self.stopped = True
                raise


@contextmanager
def observe(out, current, budget, usage_sink=None, stage_sink=None):
    """함수의 입력·출력과 오류를 변경하지 않는 기록 래퍼."""
    import functools
    import importlib
    from google.genai.models import Models
    original = Models.generate_content
    def usage_record(e):
        event = {"source_id": current[0], "task_context": current[1] if len(current) > 1 else None, **e}
        append(out / "usage.jsonl", event)
        if usage_sink: usage_sink(event)
    meter = Meter(budget, usage_record)
    patches = []
    for name in ("section_split", "ocr", "merge", "judge", "policy", "label", "logo", "style", "translate", "text_check"):
        module = importlib.import_module("pipeline.stages." + name)
        attr = {"translate": "translate_section", "text_check": "check_block"}.get(name, "run")
        old = getattr(module, attr)

        def wrap(fn, stage):
            @functools.wraps(fn)
            def wrapped(*a, **kw):
                started = time.monotonic()
                event = {"source_id": current[0], "stage": stage, "at": now()}
                try:
                    result = fn(*a, **kw)
                    def encode(v):
                        import dataclasses
                        if hasattr(v, "model_dump"):
                            return v.model_dump(mode="json")
                        if dataclasses.is_dataclass(v):
                            return encode(dataclasses.asdict(v))
                        if isinstance(v, (list, tuple)):
                            return [encode(x) for x in v]
                        if isinstance(v, dict):
                            return {k: encode(x) for k, x in v.items()}
                        return v
                    event["result"] = recorded(encode, result)
                    if stage.endswith("_initialization") and a:
                        event["model_info"] = recorded(encode, getattr(a[0], "info", None))
                        if hasattr(a[0], "describe"):
                            event["model_info"] = recorded(a[0].describe)
                    return result
                except BaseException as e:
                    event["error_type"] = type(e).__name__
                    raise
                finally:
                    event["seconds"] = time.monotonic() - started
                    append(out / "stages.jsonl", recorded(safe_record, event))
                    if stage_sink: recorded(stage_sink, safe_record(event))
            return wrapped
        patches.append((module, attr, old, wrap(old, name)))
    # 캐시된 엔진 조회가 아니라 실제 생성자 구간을 별도로 기록한다.
    for module_name, class_name in (("ocr", "PaddleOcrEngine"), ("inpaint_proc", "SubprocessInpaintModel")):
        cls = getattr(importlib.import_module("pipeline.stages." + module_name), class_name)
        old = cls.__init__
        patches.append((cls, "__init__", old, wrap(old, module_name + "_initialization")))
    cls = getattr(importlib.import_module('pipeline.stages.inpaint_proc'), 'SubprocessInpaintModel')
    old = cls.inpaint
    patches.append((cls, 'inpaint', old, wrap(old, 'inpaint_inference')))
    try:
        Models.generate_content = lambda self, *a, **kw: meter.call(original, self, *a, **kw)
        for module, attr, old, wrapped in patches:
            setattr(module, attr, wrapped)
        yield meter
    finally:
        Models.generate_content = original
        for module, attr, old, wrapped in patches:
            setattr(module, attr, old)


def drive_job(jid, seller, session_factory, query, dispatch, analysis, common, downstream, final, record,
              checkpoint=None, human=None, operation=None):
    """사람 확인 전 N3/N5를 진행하지 않는다. 제품의 기존 전이 함수를 호출한다."""
    if human is None or checkpoint is None:
        from .interaction import HumanRequired
        raise HumanRequired('N3/N5 사람 검수와 체크포인트 수집 연결 필요')
    op = operation or (lambda *a: None)
    def timed(stage, action):
        op('stage_started', stage, {})
        started = time.monotonic()
        try:
            action()
            dispatch.drain()
        except BaseException:
            op('stage_stopped', stage, {})
            raise
        job = query("SELECT * FROM job WHERE id=:j", j=jid)[0]
        if stage == 'N6':
            with session_factory() as session: done = final.can_save(session, jid)
        else:
            done = job['current_step'] == {'N2': 'N3', 'N4': 'N5'}[stage] and job['status'] == 'review'
        op('stage_finished' if done else 'stage_stopped', stage, {'seconds': time.monotonic() - started})
        return done
    def analyze():
        with session_factory() as session: analysis.start_analysis(session, jid, seller)
    if not timed('N2', analyze): return 'failed'
    job = query("SELECT * FROM job WHERE id=:j", j=jid)[0]
    if job['current_step'] != 'N3' or job['status'] != 'review': return 'failed'
    human('N3', jid)
    with session_factory() as session:
        secs = common.visible_sections(session, jid)
    if not secs: return 'failed'
    if not any(s['bucket'] == 'include' for s in secs): return 'all_excluded'
    def proceed():
        with session_factory() as session: downstream.proceed(session, jid, seller)
    if not timed('N4', proceed): return 'failed'
    job = query("SELECT * FROM job WHERE id=:j", j=jid)[0]
    if job['current_step'] != 'N5' or job['status'] != 'review': return 'failed'
    checkpoint('machine_snapshot', jid)
    response = human('N5', jid)
    def confirm():
        with session_factory() as session:
            record(final.unresolved_warnings(session, jid))
            final.confirm(session, jid, seller, response['acknowledgedWarnings'])
        op('first_n5_confirmed', 'N5', {'original': response['original'], 'final': response['final']})
    if not timed('N6', confirm): return 'failed'
    with session_factory() as session:
        if not final.can_save(session, jid): return 'failed'
    checkpoint('service_final', jid)
    return 'completed'


def collect_terminal(started, collect):
    """제품 처리 종료 시각을 먼저 확정하고 사후 수집 시간을 분리한다."""
    ended = time.monotonic()
    evidence = collect()
    return evidence, ended - started, time.monotonic() - ended


@contextmanager
def isolated_connections(storage, execution, adapters, store):
    """사전 점검부터 실패/중단까지 기존 연결 객체를 그대로 복구한다."""
    previous = (storage._store, execution._dispatcher, adapters._adapters)
    try:
        storage.set_store(store)
        adapters.reset_adapters()
        yield
    finally:
        # 설정 객체만 복구한다. 이전 모델을 새로 만들거나 호출하지 않는다.
        storage._store, execution._dispatcher, adapters._adapters = previous


def run_prepared(out, execute=False):
    """제품 모듈과 환경은 별도 프로세스에만 로드한다."""
    if not execute:
        raise ValueError("실제 모델 호출은 별도 승인 후 --execute-models가 필요합니다")
    from .approval import require_approval
    require_approval(out)
    worker = Path(__file__).with_name("worker.py").resolve()
    return subprocess.run([sys.executable, "-I", "-B", str(worker), str(Path(out).resolve()), "--execute-models"],
                          cwd=worker.parents[2], check=True)


def verify_database_binding(db, expected_url, parse_url):
    """접속 전 엔진·세션 팩토리의 실제 연결 대상을 검증한다."""
    if db.engine.url != parse_url(expected_url) or db.SessionLocal.kw.get("bind") is not db.engine:
        raise ValueError("격리 DB와 실제 엔진/세션 연결이 다릅니다")


def _run_prepared(out, execute=False):
    """새 격리 DB의 참조 데이터를 사용. 실제 모델 실행은 명시적 옵션 뒤에서만 가능."""
    if not execute:
        raise ValueError("실제 모델 호출은 별도 승인 후 --execute-models가 필요합니다")
    from .approval import require_approval
    require_approval(out)
    if any(k == "app" or k.startswith("app.") or k == "pipeline" or k.startswith("pipeline.") for k in sys.modules):
        raise ValueError("제품 모듈이 이미 로드됐습니다. 새 측정 프로세스가 필요합니다")
    out = Path(out).resolve()
    from . import protocol
    from .interaction import Collector, HumanSession
    from .product_review import ProductReview
    manifest, data = protocol.load(out)
    if protocol.validate(out) or protocol.readiness(manifest, purpose='generation'):
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
    if [i['source_id'] for i in inputs] != list(IDS) or any(digest(i['path']) != i['sha256'] for i in inputs):
        raise ValueError('34장 입력 목록 또는 해시 불일치')
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
    write_new(out / "started.json", {"at": now(), "product_commit": config["product_commit"]})
    write_new(out / 'native-runtime.json', initialize_native_runtime())
    sys.path.insert(0, str(root))
    os.environ["DATABASE_URL"] = url
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["PIXLATE_GPU_WORK_DIR"] = str(out / "gpu-work")
    os.environ["PIXLATE_POLICY_RULES"] = str(root / "pipeline/data/policy_rules.json")
    os.environ["PIXLATE_STYLE_DEFAULTS"] = str(root / "pipeline/data/style_defaults.json")
    os.environ["PIXLATE_PIPELINE_CONFIG"] = str(root / "pipeline/config/default.toml")
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
                for case in ('GS-01','GS-02','GS-03'):
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
