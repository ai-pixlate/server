"""서버 HTML 렌더(app/html_render.py)의 브라우저 세션 대역 — 시험 환경에는 FE 컨테이너 · Chromium 이 없다.

`_Session.render(바탕, 폭, 높이, renderInput 블록)` 과 같은 모양으로 돌려준다. 촬영 PNG = 바탕 그대로(크기 같음).
측정은 **흐름 시험용 근사**다(실제 판정 기준이 아니다 — 실제 줄바꿈 · ink 는 FE 측정 엔진, 로컬 · dev 대조로 본다).
근사값은 실제 모듈 측정(2026-11-19, 리눅스 Chromium 159 · Pretendard, 영어 29블록 — plans/html-renderer-proto-20261119
tools/fake_calibration.py · edge_cases_check.py)에서 가져왔다:
  - 그려지는 글자 크기 = fontSizePx × fontScale, 단 10000px 에서 잘린다(실측: 지정 28000 · 2.8e10 · 2.8e301px → 모두 10000px).
    서버(app/html_render.py)는 측정 글자 크기가 지정과 다르면 실패시킨다
  - 글자당 폭: 굵기 400 = 0.42em, 600 = 0.46em(실측 중앙값). 공백 단위로 박스 폭 안에서 줄을 나눈다(긴 낱말은 그대로 넘친다)
  - 줄 높이 = 글자 크기 × lineHeightRatio
  - ink: 윗변 = 블록 윗변 + 0.18em(실측 중앙값), 아랫변 = 줄 상자 합 − 0.06em, 가로 = 박스 왼쪽부터 가장 긴 줄 폭. 박스를 넘으면 넘침(방향별)
받은 입력은 `calls` 에 남겨 시험이 대조한다. `fail_with` 를 넣으면 그 예외를 낸다.
실제 브라우저 경로(요청 허용 목록 · 토큰 · 시간 초과 · 버전 확인)는 tests/test_html_render.py 가 Playwright 대역으로 따로 본다.
"""
from __future__ import annotations

from typing import Any

CHROMIUM_FONT_PX_MAX = 10000.0
EM_ADVANCE = {400: 0.42, 600: 0.46}
INK_TOP_EM = 0.18
INK_BOTTOM_INSET_EM = 0.06


def _measure(text: str, box: dict[str, float], style: dict[str, Any]) -> dict[str, Any]:
    want = float(style["fontSizePx"]) * float(style["fontScale"])
    px = min(want, CHROMIUM_FONT_PX_MAX)
    adv = EM_ADVANCE.get(int(style["fontWeight"]), 0.42) * px
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}" if cur else word
            if cur and len(trial) * adv > box["w"]:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
    width = max((len(line) * adv for line in lines), default=0.0)
    height = len(lines) * px * float(style["lineHeightRatio"])
    # 글자 ink 윗변은 줄 상자 윗변보다 아래다 — 섹션 맨 아래 1px 박스면 글자가 섹션 밖(실측: ink_visible false)
    ink = {"left": box["x"], "top": box["y"] + INK_TOP_EM * px, "right": box["x"] + width,
           "bottom": box["y"] + height - INK_BOTTOM_INSET_EM * px}
    sides = {"left": False, "top": False, "right": ink["right"] > box["x"] + box["w"], "bottom": ink["bottom"] > box["y"] + box["h"]}
    sides["any"] = any(sides.values())
    return {"ok": True, "fontPx": px, "lines": [{"text": line} for line in lines], "ink": ink, "overflow": sides,
            "blockRect": {"left": box["x"], "top": box["y"], "right": box["x"] + box["w"], "bottom": box["y"] + box["h"]}}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.not_ready: str | None = None  # 값이 있으면 준비 확인(ready)이 그 사유를 돌려준다 — FE 미기동 상황(D.3)
        self.ready_calls = 0

    def ready(self) -> str | None:
        self.ready_calls += 1
        return self.not_ready

    def render(self, background_png: bytes, width: int, height: int, api_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append({"width": width, "height": height, "blocks": api_blocks})
        if self.fail_with is not None:
            raise self.fail_with
        blocks = [{"blockId": int(b["blockId"]), "measure": _measure(b["text"], b["box"], b["style"])} for b in api_blocks]
        return {"png": background_png, "blocks": blocks, "fonts": {}}

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass
