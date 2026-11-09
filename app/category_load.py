"""카테고리 마스터 v3(xlsx) 검증 · 트리 · 매핑 계획 — 전체 교체 CLI(app.dictionary_full_reload)가 한 트랜잭션에서 쓴다.

입력: 「02_표시카테고리」(display_category_code · level1~3 · display_name · full_path · glossary_category_code)와
「01_용어집카테고리」(G 코드 목록). 03(아마존 노드)은 적재하지 않는다(README: breadcrumb 기능이 없으면 비적재) — amazon_node NULL.
04(값 치환)는 옛 용어집 값용이며 최종 파일에는 대상이 없다.

저장(기존 열 + 0014 code 열, 새 표 없음)
- 노드: 대분류 → 중분류 → 소분류. 파일 행 순서대로, 처음 나온 경로에서 한 번 만든다(형제 순서 = id 순 = 파일 순 — FE 가 서버 배열 순서를 쓴다).
- name: 리프는 display_name, 상위 노드는 그 단계 이름. path: 루트부터 ' > ' 로 이은 경로(리프는 full_path 와 같아야 함).
- is_leaf: 파일 행(하위 없음)만 true. 소분류가 없는 중분류는 그 중분류가 리프다.
- code: 리프에만 display_category_code. 상위 노드는 NULL(파일에 코드가 없다).
- 매핑: 리프마다 category_mapping(category_id, glossary_category_code) 1행. 매핑 해석은 직접 매핑만 본다(app.category_mapping).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import openpyxl
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.glossary_ingest import Issue, _blank

DISPLAY_SHEET = "02_표시카테고리"
GLOSSARY_SHEET = "01_용어집카테고리"
DISPLAY_COLUMNS = ["display_category_code", "level1", "level2", "level3", "display_name", "full_path",
                   "glossary_category_code"]
CODE = re.compile(r"^[A-Z]{2}-\d{2}$")
SEP = " > "
LIMITS = {"name": 200, "path": 500, "code": 20}


@dataclass
class Node:
    path: str
    name: str
    parent_path: Optional[str]
    is_leaf: bool
    code: Optional[str]
    mapping: Optional[str] = None


@dataclass
class CategoryFile:
    path: str
    sha256: str = ""
    nodes: list[Node] = field(default_factory=list)  # 삽입 순서
    issues: list[Issue] = field(default_factory=list)

    @property
    def leaves(self) -> list[Node]:
        return [n for n in self.nodes if n.is_leaf]


def _s(v: Any) -> Optional[str]:
    return None if _blank(v) else str(v).strip()


def parse(path: str) -> CategoryFile:
    from app.dictionary_ingest import _sha256

    cf = CategoryFile(path)
    label = f"category({os.path.basename(path)})"
    try:
        cf.sha256 = _sha256(path)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        cf.issues.append(Issue(label, None, None, None, f"파일을 열 수 없음: {e}"))
        return cf
    try:
        missing = [s for s in (DISPLAY_SHEET, GLOSSARY_SHEET) if s not in wb.sheetnames]
        if missing:
            cf.issues.append(Issue(label, None, None, None, f"시트 없음 {missing}"))
            return cf
        g_rows = list(wb[GLOSSARY_SHEET].iter_rows(values_only=True))
        d_rows = list(wb[DISPLAY_SHEET].iter_rows(values_only=True))
    finally:
        wb.close()
    g_header = [_s(h) for h in g_rows[0]] if g_rows else []
    if "glossary_category_code" not in g_header:
        cf.issues.append(Issue(f"{label}/{GLOSSARY_SHEET}", 1, None, "glossary_category_code", "필수 열 없음"))
        return cf
    gi = g_header.index("glossary_category_code")
    g_codes = {_s(r[gi]) for r in g_rows[1:] if r and not _blank(r[gi])}
    header = [_s(h) for h in d_rows[0]] if d_rows else []
    idx = {}
    for col in DISPLAY_COLUMNS:
        pos = [i for i, h in enumerate(header) if h == col]
        if len(pos) != 1:
            cf.issues.append(Issue(f"{label}/{DISPLAY_SHEET}", 1, None, col, "필수 열 없음" if not pos else "같은 이름의 열이 여러 개"))
        else:
            idx[col] = pos[0]
    if len(idx) != len(DISPLAY_COLUMNS):
        return cf
    lbl = f"{label}/{DISPLAY_SHEET}"
    by_path: dict[str, Node] = {}
    codes: dict[str, int] = {}
    leaf_paths: list[str] = []
    for row_no, row in enumerate(d_rows[1:], start=2):
        cell = {c: (row[i] if i < len(row) else None) for c, i in idx.items()}
        if all(_blank(v) for v in cell.values()):
            continue
        code = _s(cell["display_category_code"])
        errs = []
        if code is None or not CODE.match(code):
            errs.append(("display_category_code", f"XX-00 형식이 아님({code!r})"))
        elif code in codes:
            errs.append(("display_category_code", f"코드 중복(첫 행 {codes[code]})"))
        levels = [_s(cell["level1"]), _s(cell["level2"]), _s(cell["level3"])]
        if levels[0] is None or levels[1] is None:
            errs.append(("level1", "대분류 · 중분류는 필수"))
        levels = [x for x in levels if x is not None]
        name, full = _s(cell["display_name"]), _s(cell["full_path"])
        if levels and name != levels[-1]:
            errs.append(("display_name", f"최하위 단계 이름과 다름({name!r} ≠ {levels[-1]!r})"))
        if levels and full != SEP.join(levels):
            errs.append(("full_path", f"단계를 ' > ' 로 이은 값과 다름({full!r})"))
        g = _s(cell["glossary_category_code"])
        if g not in g_codes:
            errs.append(("glossary_category_code", f"01 시트에 없는 용어집 카테고리({g!r})"))
        for k, v in (("name", name), ("path", full), ("code", code)):
            if isinstance(v, str) and len(v) > LIMITS[k]:
                errs.append((k, f"{LIMITS[k]}자 초과"))
        if errs:
            cf.issues.extend(Issue(lbl, row_no, code, c, m) for c, m in errs)
            continue
        codes[code] = row_no
        for depth in range(1, len(levels) + 1):
            p = SEP.join(levels[:depth])
            if p not in by_path:
                node = Node(p, levels[depth - 1], SEP.join(levels[:depth - 1]) or None, False, None)
                by_path[p] = node
                cf.nodes.append(node)
        leaf = by_path[full]
        if leaf.is_leaf:
            cf.issues.append(Issue(lbl, row_no, code, "full_path", "같은 경로의 행이 이미 있음"))
            continue
        leaf.is_leaf, leaf.code, leaf.mapping, leaf.name = True, code, g, name
        leaf_paths.append(full)
    inner = {n.parent_path for n in cf.nodes if n.parent_path}
    for n in cf.nodes:
        if n.is_leaf and n.path in inner:
            cf.issues.append(Issue(lbl, None, n.code, "full_path", f"파일 행 {n.path!r} 아래에 다른 행이 있음(리프가 아님)"))
        if not n.is_leaf and n.path not in inner:
            cf.issues.append(Issue(lbl, None, None, "full_path", f"상위 노드 {n.path!r} 아래에 행이 없음"))
    return cf


def apply(conn: Connection, cf: CategoryFile) -> dict[str, int]:
    """빈 표(호출자가 지운 뒤)에 노드를 파일 순서로 넣고 매핑을 넣는다. 경로 → 새 id."""
    ids: dict[str, int] = {}
    for n in cf.nodes:
        ids[n.path] = conn.execute(
            text("INSERT INTO category_master (parent_id, name, amazon_node, is_leaf, path, code) "
                 "VALUES (:p, :n, NULL, :l, :path, :c) RETURNING id"),
            {"p": ids[n.parent_path] if n.parent_path else None, "n": n.name, "l": n.is_leaf, "path": n.path, "c": n.code},
        ).scalar_one()
    rows = [{"c": ids[n.path], "g": n.mapping} for n in cf.leaves]
    if rows:
        conn.execute(text("INSERT INTO category_mapping (category_id, internal_category) VALUES (:c, :g)"), rows)
    return ids


def expected_rows(cf: CategoryFile) -> list[tuple]:
    return [(n.path, n.name, n.parent_path, n.is_leaf, n.code, None) for n in cf.nodes]


def verify(conn: Connection, cf: CategoryFile) -> list[Issue]:
    """커밋 전: 노드 전체 저장값 · 순서(id 순 = 파일 순) · 매핑이 파일과 같은지, 파일 밖 행이 없는지."""
    issues = []
    got = [tuple(r) for r in conn.execute(text(
        "SELECT c.path, c.name, p.path, c.is_leaf, c.code, c.amazon_node FROM category_master c "
        "LEFT JOIN category_master p ON p.id = c.parent_id ORDER BY c.id")).all()]
    if got != expected_rows(cf):
        issues.append(Issue("검증", None, None, None, f"카테고리 노드가 파일과 다름(DB {len(got)} · 파일 {len(cf.nodes)})"))
    m = sorted(tuple(r) for r in conn.execute(text(
        "SELECT c.code, m.internal_category FROM category_mapping m JOIN category_master c ON c.id = m.category_id")).all())
    if m != sorted((n.code, n.mapping) for n in cf.leaves):
        issues.append(Issue("검증", None, None, None, f"카테고리 매핑이 파일과 다름(DB {len(m)} · 파일 {len(cf.leaves)})"))
    return issues
