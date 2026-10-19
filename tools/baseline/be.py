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
        for k in ("max_calls", "max_amount", "reserve_per_call"):
            if type(budget.get(k)) not in (int, float) or not math.isfinite(budget[k]) or budget[k] <= 0:
                raise ValueError(f"budget.{k}: 양수 필요")
        if type(budget["max_calls"]) is not int:
            raise ValueError("budget.max_calls: 양의 정수 필요")
        if not budget.get("currency") or not budget.get("basis"):
            raise ValueError("예산 통화와 호출당 예약 상한 근거 필요")

    def call(self, fn, *args, **kwargs):
        if self.calls + 1 > self.budget["max_calls"] or (self.calls + 1) * self.budget["reserve_per_call"] > self.budget["max_amount"]:
            raise BudgetStop("추가 요청의 예산을 확보할 수 없어 중단")
        self.calls += 1
        record = {"event": "model_call", "call": self.calls, "model": kwargs.get("model"), "at": now(),
                  "usage": None, "cost": None, "cost_reason": "실제 청구액/단가 정산 전", "status": "failed"}
        try:
            response = fn(*args, **kwargs)
            um = recorded(getattr, response, "usage_metadata", None)
            record.update(status="done", usage=recorded(um.model_dump, mode="json") if hasattr(um, "model_dump") else None)
            return response
        finally:
            recorded(self.record, record)


@contextmanager
def observe(out, current, budget):
    """함수의 입력·출력과 오류를 변경하지 않는 기록 래퍼."""
    import functools
    import importlib
    from google.genai.models import Models
    original = Models.generate_content
    meter = Meter(budget, lambda e: append(out / "usage.jsonl", {"source_id": current[0],
                   "task_context": current[1] if len(current) > 1 else None, **e}))
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
            return wrapped
        patches.append((module, attr, old, wrap(old, name)))
    # 캐시된 엔진 조회가 아니라 실제 생성자 구간을 별도로 기록한다.
    for module_name, class_name in (("ocr", "PaddleOcrEngine"), ("inpaint_proc", "SubprocessInpaintModel")):
        cls = getattr(importlib.import_module("pipeline.stages." + module_name), class_name)
        old = cls.__init__
        patches.append((cls, "__init__", old, wrap(old, module_name + "_initialization")))
    try:
        Models.generate_content = lambda self, *a, **kw: meter.call(original, self, *a, **kw)
        for module, attr, old, wrapped in patches:
            setattr(module, attr, wrapped)
        yield meter
    finally:
        Models.generate_content = original
        for module, attr, old, wrapped in patches:
            setattr(module, attr, old)


def drive_job(jid, seller, session_factory, query, dispatch, analysis, common, downstream, final, record):
    """단계 이동은 기존 BE의 상태·확정 함수를 사용한다. 결과 보존은 호출자가 담당한다."""
    with session_factory() as session:
        analysis.start_analysis(session, jid, seller)
    dispatch.drain()
    with session_factory() as session:
        secs = common.visible_sections(session, jid)
        job = query("SELECT * FROM job WHERE id=:j", j=jid)[0]
        if job["current_step"] != "N3" or job["status"] != "review":
            return "failed"
        if secs and not any(s["bucket"] == "include" for s in secs):
            return "all_excluded"
        if not secs:
            return "failed"
        downstream.proceed(session, jid, seller)
    dispatch.drain()
    job = query("SELECT * FROM job WHERE id=:j", j=jid)[0]
    if job["current_step"] == "N5" and job["status"] == "review":
        with session_factory() as session:
            warnings = final.unresolved_warnings(session, jid)
            record(warnings)
            final.confirm(session, jid, seller, warnings)
        dispatch.drain()
        with session_factory() as session:
            if final.can_save(session, jid):
                return "completed"
    return "failed"


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
    if any(k == "app" or k.startswith("app.") or k == "pipeline" or k.startswith("pipeline.") for k in sys.modules):
        raise ValueError("제품 모듈이 이미 로드됐습니다. 새 측정 프로세스가 필요합니다")
    out = Path(out).resolve()
    config = read(out / "config.json")
    prep = read(out / "preparation.json")
    if prep["missing"]:
        raise ValueError("준비 자료가 부족합니다. 채운 설정으로 새 준비 묶음을 만드세요")
    if digest(out / "config.json") != prep["config_sha256"] or digest(out / "inputs.json") != prep["inputs_sha256"]:
        raise ValueError("준비 이후 설정·입력 목록이 변경됐습니다")
    root, url = check_runtime(config)
    inputs = read(out / "inputs.json")
    frozen = read(out / "frozen.json")
    if any(digest(out / v["file"]) != v["sha256"] for v in frozen.values()):
        raise ValueError("동결된 평가 자료가 변경됐습니다")
    if [i["source_id"] for i in inputs] != list(IDS) or any(digest(i["path"]) != i["sha256"] for i in inputs):
        raise ValueError("34장 입력 목록 또는 해시가 바뀌었습니다")
    Meter(config["budget"], lambda e: None)  # DB 변경 전에 예산 형식 검증
    from .audit import policy_regions
    if policy_regions(out) is None:
        raise ValueError("구조화된 정책 기준 영역 필요")
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
            append(out / "execution.jsonl", {"source_id": current[0], **e})
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
        try:
            with observe(out, current, config["budget"]):
                for item in inputs:
                    sid = current[0] = item["source_id"]
                    product = config["products"][sid[:5]]
                    key = "source/" + sid + ".jpg"
                    store.put(key, Path(item["path"]).read_bytes(), "image/jpeg")
                    with db.engine.begin() as conn:
                        seller = insert(conn, "seller", {"cognito_sub": "baseline:" + sid, "email": sid + "@baseline.invalid"})
                        brand = insert(conn, "brand", {"seller_id": seller, "name_ko": product["name_ko"], "name_en": product["name_en"]})
                        jid = insert(conn, "job", {"seller_id": seller, "brand_id": brand,
                                     **{k: product[k] for k in ("product_name", "regulatory_class", "internal_category", "target_country", "target_lang")}})
                        source = insert(conn, "source_image", {"job_id": jid, "upload_order": 1, "file_url": key,
                                       "width": item["width"], "height": item["height"], "sha256": item["sha256"]})
                    append(out / "bindings.jsonl", {"source_id": sid, "job_id": jid, "db_source_id": source})
                    started = time.monotonic()
                    status = "failed"
                    try:
                        status = drive_job(jid, seller, db.SessionLocal, query, dispatch, analysis, common, downstream, final,
                                           lambda w: append(out / "warnings.jsonl", {"source_id": sid, "warnings": w}))
                    except (BudgetStop, RecordingStop):
                        status = "interrupted"
                        raise
                    finally:
                        evidence, processing_seconds, collection_seconds = collect_terminal(started, lambda: snapshot(sid, jid))
                        failed = [t for t in evidence["job_async_task"] if t.get("is_current") and t["status"] == "failed"]
                        pending = [t for t in evidence["job_async_task"] if t.get("is_current") and t["status"] in ("pending", "running") and t.get("parent_task_id")]
                        if status in ("completed", "all_excluded") and (failed or pending):
                            status = "partial_failure"
                        append(out / "execution.jsonl", {"event": "finished", "source_id": sid, "status": status,
                             "seconds": processing_seconds, "collection_seconds": collection_seconds,
                             "first_execution": sid == IDS[0],
                             "retry_count": sum(t.get("retry_count", 0) for t in evidence["job_async_task"] if t.get("is_current")),
                             "failed_attempts": [t["id"] for t in failed]})
        finally:
            write_new(out / "object-index.json", store.index)
