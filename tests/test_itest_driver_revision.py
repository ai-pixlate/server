"""deploy/itest/driver_n2.py — 배포 버전 마이그레이션 head ↔ 실제 DB revision 점검(고정 revision 없이).

순수 함수(code_head·revision_problem)와 precheck 관문은 DB 없이 시험하고, 실제 alembic_version 표는
PIXLATE_TEST_DATABASE_URL 이 있을 때 일회용 DB 로 시험한다.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.exec_db import fresh_database, requires_db

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ITEST_EV", tempfile.mkdtemp())
_spec = importlib.util.spec_from_file_location("driver_n2_under_test", ROOT / "deploy" / "itest" / "driver_n2.py")
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)
MIGRATIONS = str(ROOT / "migrations")


def test_code_head_is_single_head_of_repo_migrations():
    head, bad = d.code_head(MIGRATIONS)
    assert bad is None and head and head == sorted(p.name.split("_")[0] for p in (ROOT / "migrations" / "versions").glob("[0-9]*.py"))[-1]


def test_code_head_fails_on_two_heads_and_unreadable(tmp_path):
    loc = tmp_path / "migrations"
    shutil.copytree(ROOT / "migrations", loc)
    # 0007 에서 갈라지는 두 번째 마이그레이션 → head 2개(두 PR 이 같은 부모를 쓴 경우)
    (loc / "versions" / "9999_branch.py").write_text('revision = "9999"\ndown_revision = "0007"\n', encoding="utf-8")
    head, bad = d.code_head(str(loc))
    assert head is None and "head 2개" in bad
    head2, bad2 = d.code_head(str(tmp_path / "없음"))
    assert head2 is None and bad2


@pytest.mark.parametrize("revs,expect", [
    (["0010"], None),
    ([], "DB 버전 행 0개"),
    (["0010", "0009"], "DB 버전 행 2개"),
    (["0008"], "DB 0008 ≠ 코드 head 0010"),
])
def test_revision_problem(revs, expect):
    got = d.revision_problem("0010", revs)
    assert (got is None) if expect is None else (expect in got)
    assert d.revision_problem(None, ["0010"]) == "코드 head 없음"


class _FakeDb:
    def __init__(self, revs):
        self.revs, self.sql = revs, []

    def schema_problems(self):
        return []

    def queues(self):
        return {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}

    def q(self, sql, **p):
        self.sql.append(sql)
        if "alembic_version" in sql:
            return [{"v": v} for v in self.revs]
        if "count(*)" in sql:
            return [{"n": 0}]
        raise RuntimeError("관문 뒤 조회")  # 관문을 지나면 여기로 온다


@pytest.mark.parametrize("revs,head", [
    (["0008"], ("0010", None)),           # 불일치
    (["0010", "0010"], ("0010", None)),   # 행 2개
    ([], ("0010", None)),                 # 행 0개
    (["0010"], (None, "코드 마이그레이션 head 2개 ['0009', '0010']")),  # 대상 코드 head 2개
])
def test_precheck_stops_before_further_checks(revs, head, capsys):
    db = _FakeDb(revs)
    assert d.precheck(db, api=None, head_of=lambda: head) is False
    assert not any("seller" in s for s in db.sql)
    assert "배포 버전과 DB revision 불일치" in capsys.readouterr().out


def test_precheck_passes_revision_gate_when_single_and_equal():
    db = _FakeDb(["0010"])
    with pytest.raises(RuntimeError, match="관문 뒤 조회"):
        d.precheck(db, api=None, head_of=lambda: ("0010", None))


@requires_db
def test_real_db_revision_against_code_head():
    """일회용 DB 를 head 까지 올린 뒤 실제 alembic_version 행을 읽어 비교 — 행 0·2개·다른 값이면 문제로 잡는다."""
    head, bad = d.code_head(MIGRATIONS)
    assert bad is None
    with fresh_database() as u:
        eng = create_engine(u)
        try:
            def revs():
                with eng.connect() as c:
                    return [r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))]
            assert d.revision_problem(head, revs()) is None
            with eng.begin() as c:
                c.execute(text("INSERT INTO alembic_version (version_num) VALUES ('extra')"))
            assert "2개" in d.revision_problem(head, revs())
            with eng.begin() as c:
                c.execute(text("DELETE FROM alembic_version"))
            assert "0개" in d.revision_problem(head, revs())
            with eng.begin() as c:
                c.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0008')"))
            assert "≠ 코드 head" in d.revision_problem(head, revs())
        finally:
            eng.dispose()
