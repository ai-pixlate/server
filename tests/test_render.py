"""렌더 엔진 단위 테스트 — DB 불필요."""
from app import render

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_render_produces_png():
    blocks = [
        {"id": 1, "role": "title", "trans_1": "Deep Moisture Cream", "is_excluded": False},
        {"id": 2, "role": "body", "trans_1": "Hydrates all day.", "is_excluded": False},
    ]
    png, w, h = render.render_section_png(blocks)
    assert png[:8] == PNG_MAGIC
    assert w == render.CANVAS_WIDTH
    assert h > 0


def test_excluded_blocks_skipped():
    # 제품 라벨/브랜드 로고(is_excluded)는 렌더에서 제외 — 그래도 유효한 이미지
    blocks = [{"id": 1, "role": "body", "trans_1": "BRAND", "is_excluded": True}]
    png, w, h = render.render_section_png(blocks)
    assert png[:8] == PNG_MAGIC
    assert w == render.CANVAS_WIDTH


def test_validation_char_limit_is_warning():
    blocks = [{"id": 1, "role": "body", "trans_1": "a" * 80, "is_excluded": False, "char_limit": 50}]
    vr = render.validate_deliverable(blocks, render.CANVAS_WIDTH, 300, {"maxWidth": 1000, "charLimit": 50})
    limit_checks = [c for c in vr["checks"] if c["key"] == "char_limit"]
    assert len(limit_checks) == 1
    assert limit_checks[0]["passed"] is False
    assert limit_checks[0]["severity"] == "warning"
    # warning 은 전체 통과 여부(passed)에 영향 없음
    assert vr["passed"] is True


def test_validation_width_error_fails():
    vr = render.validate_deliverable([], 1200, 300, {"maxWidth": 1000})
    width_check = [c for c in vr["checks"] if c["key"] == "image_width"][0]
    assert width_check["passed"] is False
    assert width_check["severity"] == "error"
    assert vr["passed"] is False
