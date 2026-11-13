"""빈 원문 블록 제외(계약 blockList 「빈 원문 블록은 목록에 없다」 · exportCreate 「content.csv 행」) — 실제 PostgreSQL + 메모리 저장소.

빈 원문 = source_ko 가 null 이거나 공백(줄바꿈·탭·전각 공백 포함)을 지우면 빈 문자열. 렌더 대상 판정과 같은 기준이다.
N5 블록 목록과 content.csv 에서만 뺀다. DB 행은 그대로 두고, 블록 수정(PATCH)은 지금 동작 그대로다.
빈 원문 블록은 분석 뒤 DB 값을 바꿔 만든다(합성 표본에는 빈 원문 블록이 없다).
"""
from __future__ import annotations

import csv
import io

import pytest

from app.flows.common import has_source
from tests.exec_db import requires_db
from tests.test_exec_downstream import _pvs, _q, _to_n5, dburl, env  # noqa: F401

pytestmark = requires_db

BLANKS = [None, "", "   ", "\n", "\t\r\n", "　"]  # null · 빈 문자열 · 공백 · 줄바꿈 · 탭 · 전각 공백


def _job_blocks(job):
    return _q("SELECT tb.id, tb.section_id, tb.block_order FROM text_block tb JOIN section s ON s.id = tb.section_id "
              "WHERE s.job_id = :j AND s.bucket = 'include' ORDER BY tb.section_id, tb.block_order, tb.id", j=job)


def _blank(bid, value):
    _q("UPDATE text_block SET source_ko = :v WHERE id = :b", v=value, b=bid)


def _unfiltered(job, **kw):
    """수정 전 동작(빼지 않은 목록) — 같은 계산으로 만든 기준값. 응답과 비교하도록 JSON 값으로 바꾼다."""
    from fastapi.encoders import jsonable_encoder

    from app import db as app_db
    from app.routers import cfm

    with app_db.SessionLocal() as db:
        return jsonable_encoder(cfm._blocks(db, job, **kw))


def _setup_blanks(env):
    """첫 섹션의 블록 일부와 둘째 섹션의 블록 전부를 빈 원문으로 만든다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    rows = _job_blocks(job)
    secs = sorted({r["section_id"] for r in rows})
    assert len(secs) >= 2
    first = [r for r in rows if r["section_id"] == secs[0]]
    second = [r for r in rows if r["section_id"] == secs[1]]
    assert len(first) >= 3
    blanked = {}
    for r, v in zip(first[1:], BLANKS):  # 첫 블록은 원문을 남긴다
        _blank(r["id"], v)
        blanked[r["id"]] = v
    for r, v in zip(second, BLANKS[::-1]):
        _blank(r["id"], v)
        blanked[r["id"]] = v
    return c, job, secs, blanked


def test_has_source_matches_render_target_rule():
    for v in BLANKS:
        assert has_source(v) is False, repr(v)
        assert not (v or "").strip()  # downstream._render_inputs 와 같은 판정
    for v in ("a", " 원문 ", "\n가\n", "　x"):
        assert has_source(v) is True


def test_block_list_drops_blank_source_and_keeps_others_as_before(env):
    c, job, secs, blanked = _setup_blanks(env)
    listed = c.get(f"/v1/jobs/{job}/blocks")
    assert listed.status_code == 200, listed.text
    listed = listed.json()
    ids = {b["id"] for b in listed}
    assert not ids & set(blanked)  # null · "" · 공백 · 줄바꿈 · 탭 · 전각 공백 모두 없다
    assert all(has_source(b["sourceKo"]) for b in listed)
    # 남은 블록은 빼기 전 계산과 같은 값(sectionInputFingerprint 포함) · 같은 순서
    before = [b for b in _unfiltered(job) if b["id"] not in blanked]
    assert listed == before
    kept = [b for b in listed if b["sectionId"] == secs[0]]
    assert kept and all(b["sectionInputFingerprint"] == kept[0]["sectionInputFingerprint"] for b in kept)
    assert kept[0]["sectionInputFingerprint"] == _pvs(c, job, secs[0])["inputFingerprint"]  # 지문 A = B
    # DB 행은 그대로다
    assert {r["id"] for r in _job_blocks(job)} >= set(blanked)


def test_section_with_only_blank_blocks_has_no_rows(env):
    c, job, secs, _ = _setup_blanks(env)
    assert not [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["sectionId"] == secs[1]]
    r = c.get(f"/v1/jobs/{job}/blocks", params={"sectionId": secs[1]})
    assert r.status_code == 200 and r.json() == []
    r = c.get(f"/v1/jobs/{job}/blocks", params={"sectionId": secs[0]})
    assert r.status_code == 200 and r.json() == [b for b in _unfiltered(job, section_id=secs[0]) if has_source(b["sourceKo"])]


@pytest.mark.parametrize("value", BLANKS)
def test_patch_blank_source_block_is_unchanged(env, value):
    """빈 원문 블록을 id 로 PATCH — 응답 block · 충돌 details.current 에 그 블록이 실린다(목록에서 빠져도 500 이 아니다)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    bid = _job_blocks(job)[1]["id"]
    _blank(bid, value)
    cur = _unfiltered(job, block_id=bid)[0]
    assert cur["sourceKo"] == value
    # 같은 값 저장 = no-op, 전체 TextBlock 그대로
    r = c.patch(f"/v1/jobs/{job}/blocks/{bid}", json={"trans1": cur["trans1"] or "", "revision": cur["revision"]})
    assert r.status_code == 200, r.text
    if cur["trans1"] is not None:
        assert r.json() == {"block": cur, "rerenderTaskId": None}
    # 번역문 변경 — 지금처럼 저장된다
    rev = r.json()["block"]["revision"]
    r = c.patch(f"/v1/jobs/{job}/blocks/{bid}", json={"trans1": "Edited", "revision": rev})
    assert r.status_code == 200, r.text
    nb = r.json()["block"]
    assert (nb["id"], nb["sourceKo"], nb["trans1"], nb["blockStatus"], nb["revision"]) == (bid, value, "Edited", "edited", rev + 1)
    assert nb == _unfiltered(job, block_id=bid)[0]
    # 낡은 revision — 409 REVISION_CONFLICT, details.current 는 그 블록
    r = c.patch(f"/v1/jobs/{job}/blocks/{bid}", json={"trans1": "Again", "revision": rev})
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "REVISION_CONFLICT" and err["details"]["current"]["id"] == bid
    assert err["details"]["current"]["sourceKo"] == value
    # 배치 저장 — 렌더 대상이 아니라 지금처럼 409 not_render_target
    r = c.patch(f"/v1/jobs/{job}/blocks/{bid}", json={"trans1": "Edited", "revision": rev + 1, "layoutAdjust": {"fontScale": 1.1}})
    assert r.status_code == 409 and r.json()["error"]["details"] == {"reason": "not_render_target"}, r.text
    # 목록에는 여전히 없다
    assert bid not in {b["id"] for b in c.get(f"/v1/jobs/{job}/blocks").json()}


def test_content_csv_has_no_blank_source_rows(env, monkeypatch):
    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    c, job, secs, blanked = _setup_blanks(env)
    r = c.post(f"/v1/jobs/{job}/export", json={"components": ["content.csv"]})
    assert r.status_code == 201, r.text
    key = _q("SELECT file_url FROM export_artifact WHERE job_id = :j AND artifact_type = 'csv'", j=job)[0]["file_url"]
    rows = list(csv.DictReader(io.StringIO(store.get(key).decode("utf-8-sig"))))
    assert rows and all(x["sourceKo"].strip() for x in rows)
    got = [(int(x["sectionId"]), int(x["blockOrder"])) for x in rows]
    want = [(b["section_id"], b["block_order"]) for b in _job_blocks(job) if b["id"] not in blanked]
    assert got == want  # 원문이 있는 블록은 지금과 같은 순서로 모두 나온다
    assert secs[1] not in {s for s, _ in got}
