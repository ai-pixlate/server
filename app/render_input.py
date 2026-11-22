"""렌더 입력 블록 한 곳 — `/preview` renderInput(N5 가 그리는 입력)과 서버 렌더(⑨ 미리보기 · N6 최종)가 같은 함수로 만든다.

계약 6.4: 미리보기와 서버 최종 촬영은 같은 HTML/CSS 틀 · 입력 · 적용값을 쓴다. N5 에 보내는 style 8키와 서버가 FE 내부 렌더 경로에
넣는 style 8키가 따로 만들어지면 그것만으로 N5 ↔ N6 이 갈릴 수 있으므로, 두 경로 모두 이 함수의 결과를 그대로 쓴다.
적용값 규칙은 승인 설정 자료의 `resolve_block`(6.3)이 정본이다 — 여기서 값을 새로 정하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.handoff import style_defaults as sd

DEFAULT_FONT_SCALE = 1.0  # 셀러 배율이 없으면 1.0(계약 fontScale)


@dataclass(frozen=True)
class RenderBlockInput:
    """renderInput 블록(API 모양 그대로)과 렌더 기록용 적용값 · 출처."""
    api: dict[str, Any]      # {blockId, revision, text, box{x,y,w,h}, style 8키} — /preview renderInput.blocks[] 와 같은 값
    applied: dict[str, Any]  # 렌더 기록(preview payload.render.blocks[].applied) — 기존 기록과 같은 키
    source: dict[str, str]   # 적용값 출처(measured | role_default)


def build_block(*, block_id: int, revision: int, role: str, is_excluded: bool | None, source_ko: str | None, text: str | None,
                style: Any, style_stage: str | None, box: dict[str, Any], font_scale: float, section_width: int,
                defaults: Any) -> RenderBlockInput | None:
    """한 블록의 렌더 입력. 조판 대상이 아니면(빈 원문 · 빈 번역 · 제외) None.
    defaults = 검증을 마친 승인 설정 자료(`typeset.RoleDefaults.data`). ⑦ 결과가 계약과 어긋나면 `sd.StyleInputInvalid` 를 그대로 올린다."""
    res = sd.resolve_block(sd.BlockInput(id=block_id, role=role, is_excluded=is_excluded, source_ko=source_ko, text=text,
                                         style=style, style_stage=style_stage),  # type: ignore[arg-type]
                           section_width, defaults)
    if not res.render:
        return None
    a, s = res.applied, res.source
    api = {
        "blockId": block_id, "revision": revision, "text": text, "box": dict(box),
        "style": {"fontFamily": a["font_family"], "fontWeight": a["font_weight"], "fontSizePx": a["font_size_px"],
                  "fontScale": font_scale, "fontColor": a["font_color"], "align": a["align"],
                  "letterSpacingEm": a["letter_spacing_em"], "lineHeightRatio": defaults.data.pipeline_mapping.line_height_ratio},
    }
    applied = {"font_color": a["font_color"], "est_font_px": a["font_size_px"], "align": a["align"]}
    if font_scale != 1:  # 배율이 없는 블록의 기록은 그대로(Pillow 렌더 기록과 같은 규칙)
        applied["font_scale"] = font_scale
    source = {"font_color": s["font_color"], "est_font_px": s["font_size_px"], "align": s["align"]}
    return RenderBlockInput(api=api, applied=applied, source=source)
