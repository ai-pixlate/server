"""용어집 테스트용 xlsx 생성 도우미. 실제 파일과 같은 시트·열 이름으로 작은 파일을 만든다."""
from pathlib import Path

import openpyxl

from app.glossary_ingest import COMMON_COLUMNS, PHRASE_COLUMNS, SPECS

# 실제 파일처럼 검토용 열을 섞어 둔다(로더가 읽지 않아야 함).
EXTRA_COLUMN = "검토용_메모"


def base_row(kind: str, n: int, **over) -> dict:
    spec = SPECS[kind]
    row = {
        "구역": "A",
        "id": f"{spec.id_prefix}{n:03d}",
        "source_ko": f"{kind}-한국어-{n}",
        "target_text": f"{kind} english {n}",
        "term_kind": spec.term_kind,
        "enforcement": "참고" if spec.phrase else "강제",
        "target_lang": "en",
        "internal_category": "common",
        "example_sentence": None,
        "frequency": None,
        "corpus_size": None,
        "corpus_version": None,
        "verified_at": None,
        "source": "테스트 출처",
        "note": None,
        "version": 1,
        EXTRA_COLUMN: "적재 안 함",
    }
    if spec.phrase:
        row.update({"적재여부": "Y", "라벨": f"라벨{n}", "대표여부": "대표"})
    row.update(over)
    return row


def write_book(path: Path, kind: str, rows: list[dict], drop_columns=()) -> Path:
    spec = SPECS[kind]
    header = [EXTRA_COLUMN] + COMMON_COLUMNS + (PHRASE_COLUMNS if spec.phrase else [])
    header = [h for h in header if h not in drop_columns]
    wb = openpyxl.Workbook()
    wb.active.title = "00_읽는법"
    ws = wb.create_sheet(spec.sheet)
    ws.append(header)
    for r in rows:
        ws.append([r.get(h) for h in header])
    wb.save(path)
    return path
