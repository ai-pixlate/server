"""소셜 계정 B 의 세션 토큰으로 A 의 자원을 변경·확정·취소할 수 없다(PM 11-10 요청) — 실제 라우터·PostgreSQL.

- A = 간이 로그인 판매자, 작업을 N5(검수)까지 실제 흐름으로 진행
- B = 카카오 소셜 판매자(이메일은 A 와 같게) — 소셜 로그인 API 가 아직 없어(C) 세션을 직접 만든다
- 기대: 존재 은닉 계약대로 404 NOT_FOUND, 응답에 A 의 자원 정보가 없음
- 확인 대상: A 의 작업·섹션·블록·실행 시도·인계·원본 이미지·브랜드·로고 행, 실행 큐에 보낸 메시지, 저장소 객체, B 의 행
  (B 가 남의 자원에 접근할 때 허용된 부수 효과는 없다 — 아래에서 DB 전체 지문도 함께 비교해 실제로 없었음을 기록한다)
멱등 키 범위(판매자별)는 기존 시험(test_n1_job_contract·test_exec_downstream)이 본다.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.exec_db import requires_db
from tests.exec_fakes import sample_source_png as sample_png
from tests.test_exec_downstream import _q, _text_block, _to_n5, dburl, env  # noqa: F401

pytestmark = requires_db

_A_SCOPED = [  # (이름, A 의 작업·판매자에 딸린 행만 고르는 질의)
    ("job", "SELECT j.*::text AS r FROM job j WHERE j.id = :job"),
    ("section", "SELECT s.*::text AS r FROM section s WHERE s.job_id = :job"),
    ("text_block", "SELECT tb.*::text AS r FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :job"),
    ("job_async_task", "SELECT t.*::text AS r FROM job_async_task t WHERE t.job_id = :job"),
    ("task_handoff", "SELECT h.*::text AS r FROM task_handoff h WHERE h.job_id = :job"),
    ("source_image", "SELECT i.*::text AS r FROM source_image i WHERE i.job_id = :job"),
    ("brand", "SELECT b.*::text AS r FROM brand b WHERE b.seller_id = :seller"),
    ("brand_logo", "SELECT l.*::text AS r FROM brand_logo l JOIN brand b ON b.id = l.brand_id WHERE b.seller_id = :seller"),
    ("job_keyword", "SELECT k.*::text AS r FROM job_keyword k WHERE k.job_id = :job"),
]


def _a_state(job: int, seller: int) -> dict:
    return {name: sorted(r["r"] for r in _q(sql, job=job, seller=seller)) for name, sql in _A_SCOPED}


def _db_fingerprint() -> dict:
    tables = [r["t"] for r in _q("SELECT table_name AS t FROM information_schema.tables WHERE table_schema = 'public' "
                                 "AND table_type = 'BASE TABLE' ORDER BY 1")]
    return {t: _q(f'SELECT count(*) AS n, md5(coalesce(string_agg(x::text, \'|\' ORDER BY x::text), \'\')) AS h FROM "{t}" x')[0]
            for t in tables}


def _social_b(email: str) -> tuple[int, TestClient]:
    from app import auth_sessions
    from app.main import app
    from app.security import issue_access

    sid = _q("INSERT INTO seller (auth_provider, provider_subject, email) VALUES ('kakao', :s, :e) RETURNING id",
             s=str(uuid.uuid4().int)[:12], e=email)[0]["id"]
    session, _, _ = auth_sessions.create(sid)
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer " + issue_access(session, email)
    return sid, c


def _calls(ids: dict) -> list[tuple[str, str, str, dict]]:
    job, sec, blk, task, img, brand, logo = (ids[k] for k in ("job", "section", "block", "task", "image", "brand", "logo"))
    png = sample_png()
    return [
        ("작업 수정", "patch", f"/v1/jobs/{job}", {"json": {"productName": "B가 바꿈"}}),
        ("작업 취소", "delete", f"/v1/jobs/{job}", {}),
        ("분석 시작", "post", f"/v1/jobs/{job}/analyze", {}),
        ("중단", "post", f"/v1/jobs/{job}/abort", {}),
        ("시도 재시도", "post", f"/v1/jobs/{job}/tasks/{task}/retry", {}),
        ("섹션 제외", "patch", f"/v1/jobs/{job}/sections/{sec}", {"json": {"action": "exclude"}}),
        ("N3 진행", "post", f"/v1/jobs/{job}/sections/proceed", {}),
        ("블록 저장", "patch", f"/v1/jobs/{job}/blocks/{blk}", {"json": {"trans1": "B가 바꿈", "revision": 0}}),
        ("검수 확정", "post", f"/v1/jobs/{job}/confirm", {"json": {"acknowledgedWarnings": []}}),
        ("렌더", "post", f"/v1/jobs/{job}/render", {}),
        ("내보내기", "post", f"/v1/jobs/{job}/export", {"json": {"components": ["content.csv"]}}),
        ("저장", "post", f"/v1/jobs/{job}/save", {}),
        ("원본 추가", "post", f"/v1/jobs/{job}/source-images", {"files": [("files", ("b.png", png, "image/png"))]}),
        ("원본 순서", "patch", f"/v1/jobs/{job}/source-images/reorder", {"json": {"order": [img]}}),
        ("원본 삭제", "delete", f"/v1/jobs/{job}/source-images/{img}", {}),
        ("브랜드 수정", "patch", f"/v1/brands/{brand}", {"json": {"overview": "B가 바꿈"}}),
        ("로고 추가", "post", f"/v1/brands/{brand}/logos", {"files": {"file": ("l.png", png, "image/png")}}),
        ("로고 삭제", "delete", f"/v1/brands/{brand}/logos/{logo}", {}),
        ("A 브랜드로 작업 생성", "post", "/v1/jobs", {"json": {"brandId": brand, "productName": "x", "targetCountry": "US",
                                                           "targetLang": "en", "regulatoryClass": "cosmetic"}}),
    ]


def test_social_b_token_cannot_change_confirm_or_cancel_a_resources(env):
    ids, _ = _to_n5(env)
    a, job = ids["c"], ids["job"]
    seller_a = _q("SELECT seller_id FROM job WHERE id = :j", j=job)[0]["seller_id"]
    email_a = _q("SELECT email FROM seller WHERE id = :s", s=seller_a)[0]["email"]
    brand = a.get(f"/v1/jobs/{job}").json()["brandId"]
    logo = a.post(f"/v1/brands/{brand}/logos", files={"file": ("a.png", sample_png(), "image/png")})
    assert logo.status_code == 201, logo.text
    blk = _text_block(a, job)
    ids.update(section=blk["sectionId"], block=blk["id"], brand=brand, logo=logo.json()["id"],
               task=_q("SELECT id FROM job_async_task WHERE job_id = :j ORDER BY id LIMIT 1", j=job)[0]["id"],
               image=_q("SELECT id FROM source_image WHERE job_id = :j ORDER BY id LIMIT 1", j=job)[0]["id"])
    env["q"].drain()

    seller_b, b = _social_b(email_a)  # 같은 이메일의 다른 제공자 계정
    before_a, before_db = _a_state(job, seller_a), _db_fingerprint()
    before_q, before_store = list(env["q"].messages), dict(env["store"].objects)

    for label, method, url, kw in _calls(ids):
        r = getattr(b, method)(url, **kw)
        assert r.status_code == 404, (label, r.status_code, r.text)
        e = r.json()["error"]
        assert e["code"] == "NOT_FOUND", label
        assert email_a not in r.text and "B가 바꿈" not in r.text, label  # A 의 정보·요청 값을 되돌려주지 않는다

    assert _a_state(job, seller_a) == before_a  # 대상 데이터
    assert env["q"].messages == before_q  # 실행 큐에 보낸 것 없음
    assert env["store"].objects == before_store  # 저장소 객체 그대로
    assert _q("SELECT count(*) AS n FROM job WHERE seller_id = :s", s=seller_b)[0]["n"] == 0
    assert _q("SELECT count(*) AS n FROM brand WHERE seller_id = :s", s=seller_b)[0]["n"] == 0
    assert _db_fingerprint() == before_db  # 허용된 부수 효과도 없었다(관찰 기록)

    # A 는 그대로 쓸 수 있다(B 의 시도가 A 의 세션·상태에 영향 없음)
    assert a.get(f"/v1/jobs/{job}").status_code == 200
