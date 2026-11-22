"""⑨ 미리보기 · N6 최종 렌더 — FE 내부 렌더 경로를 서버 Chromium 으로 열어 N5 와 같은 HTML/CSS 조판으로 그리고 잰다.

계약 6.4: 미리보기와 서버 최종 촬영은 같은 HTML/CSS 틀 · 입력 · 적용값. 6.7(428행): 측정 함수 · 기대 사례는 AI, 공통 HTML/CSS 와
연결은 FE, Playwright · 폰트 준비 확인 · 함수 호출 · 채택 · 저장은 BE — 서버에서 별도 측정 알고리즘을 만들지 않는다.
방식 (가)(2026-11-19 결정): FE 앱 안의 내부 렌더 경로(`/internal/typeset-render`)를 같은 docker 네트워크의 FE 컨테이너에서 연다.

- 브라우저: worker 프로세스마다 Chromium headless shell 하나 · 페이지 하나(worker-cpu 는 `--pool=solo`). 처음 쓸 때 띄우고 재사용한다.
  `--font-render-hinting=none` — 없으면 리눅스가 글자 폭을 정수로 반올림해 N5(사용자 브라우저)와 줄바꿈이 갈린다(시제품 영어 26/29).
- 요청 허용 목록: FE 원점(토큰 헤더는 이 원점에만) · 바탕(이 프로세스가 가진 바이트를 `render.local` 로 응답) ·
  Pretendard 고정본(FE 가 jsDelivr 에서 읽는 같은 파일 3개 — 이미지 안 사본, SHA 대조). 그 밖은 모두 막는다(외부 CDN 의존 없음).
- 실패(브라우저 · FE · 버전 불일치 · 시간 초과 · 글꼴 미준비 · 측정 실패)는 대체 촬영 없이 `RenderUnavailable` — 호출자의 기존 실패 경로
  (`<STAGE>_UNEXPECTED`, retryable)로 간다. 새 오류 코드를 만들지 않는다. 실패하면 페이지를 버리고 다음 렌더에서 새로 연다.
- 넘침 = FE 측정 엔진의 `overflow.any`(계약 6.2 D-01 · D-02: 배치된 글리프 ink, 소수 유지, 같으면 정상 · 넘으면 넘침, 허용 오차 없음).
- 토큰 · 헤더는 로그 · 오류 메시지에 넣지 않는다.

식별자(계약 6.7 — FE · AI 합의 2026-11-21):
- `rendererId` = `html-typeset/<규칙판>+chromium-<Chromium 전체 버전>` — 서버 렌더 · 측정 실행 환경.
- `bundleId` = `fe-<FE 전체 SHA 40자>` — 서버가 연 FE 빌드(`PIXLATE_FE_RENDER_REVISION`).
- 두 값 모두 렌더 고정 입력(`renderer` · `bundle`)에 들어가 지문과 불일치 검사(`common.renderer_mismatch`)에 쓰인다.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLAYWRIGHT_VERSION = "1.65.2"
CHROMIUM_VERSION = "159.0.8154.6"  # playwright 1.65.2 의 chromium-headless-shell — 실행 시 대조(다르면 실패)
CHROMIUM_ARGS = ("--font-render-hinting=none",)
RENDER_PATH = "/internal/typeset-render"
TOKEN_HEADER = "x-pixlate-internal-render"
BG_PREFIX = "http://render.local/bg/"
FONT_PREFIX = "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/"
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts" / "pretendard-web-1.3.9"
# FE root layout 이 읽는 CDN 경로 → (이미지 안 파일, sha256, content-type). 조판은 400 · 600 만 쓴다(ROLE 기본값).
FONT_FILES: dict[str, tuple[str, str, str]] = {
    "dist/web/static/pretendard.css":
        ("pretendard.css", "479ddc1caf4fa5ef806598d7b7cec1b5f2d1993236eb9c82a42046bbe9c3275d", "text/css; charset=utf-8"),
    "packages/pretendard/dist/web/static/woff2/Pretendard-Regular.woff2":
        ("Pretendard-Regular.woff2", "fad853f7f47c6c8b103171e7193fa095708cdcd70850a71d93aa5379e8a61d63", "font/woff2"),
    "packages/pretendard/dist/web/static/woff2/Pretendard-SemiBold.woff2":
        ("Pretendard-SemiBold.woff2", "c863f76a7de5c1ddc1ed8b2fa794964530774592c4f31407a84e2a2ae93f17f0", "font/woff2"),
}


def fe_origin() -> str:
    return os.getenv("PIXLATE_FE_INTERNAL_ORIGIN", "http://pixlate-fe:3000").rstrip("/")


def fe_revision() -> str:
    """발표용으로 고정한 FE 빌드 revision(FE `NEXT_PUBLIC_PIXLATE_FE_REVISION`). 비어 있으면 렌더하지 않는다."""
    return os.getenv("PIXLATE_FE_RENDER_REVISION", "").strip()


def timeout_ms() -> int:
    return int(float(os.getenv("PIXLATE_RENDER_TIMEOUT_S", "20")) * 1000)


def fe_revision_valid(rev: str) -> bool:
    """bundleId 는 FE 전체 SHA 40자다(합의 11-21). 줄인 SHA · 태그 · 빈 값은 받지 않는다."""
    return len(rev) == 40 and all(c in "0123456789abcdef" for c in rev)


# 규칙판 — 아래 중 하나라도 바꾸면 올린다(/1 → /2). 바꾸면 전환 전 고정 입력은 INPUT_CHANGED(common.renderer_mismatch).
#   서버 고정본 글꼴(FONT_FILES 3파일 SHA) · 글꼴 힌팅 옵션(CHROMIUM_ARGS) · 내부 경로 입출력 형식 · 넘침 · 줄 수 변환 ·
#   글자 크기 대조 규칙 · 출력 배율(OUTPUT_SCALE — 촬영 PNG 의 CSS px 당 픽셀 수).
# Chromium 버전은 규칙판과 따로 rendererId 에 들어간다. FE 빌드는 rendererId 가 아니라 bundleId 다.
RULE_VERSION = 1
OUTPUT_SCALE = 1
RENDERER_VERSION = f"html-typeset/{RULE_VERSION}+chromium-{CHROMIUM_VERSION}"


def bundle_id_for(rev: str) -> str | None:
    return f"fe-{rev}" if fe_revision_valid(rev) else None


BUNDLE_ID = bundle_id_for(fe_revision())


class RenderUnavailable(RuntimeError):
    """서버 HTML 렌더를 할 수 없다 — 대체 촬영하지 않는다."""


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_fonts() -> dict[str, tuple[bytes, str]]:
    """고정본을 읽어 SHA 를 대조한다. 하나라도 다르면 렌더하지 않는다(FE 와 다른 글꼴로 그리지 않는다)."""
    out = {}
    for path, (name, want, ctype) in FONT_FILES.items():
        try:
            body = (FONT_DIR / name).read_bytes()
        except OSError as e:
            raise RenderUnavailable(f"글꼴 고정본 없음: {name}") from e
        if _sha256(body) != want:
            raise RenderUnavailable(f"글꼴 고정본 SHA 불일치: {name}")
        out[path] = (body, ctype)
    return out


class _Session:
    """프로세스 안 브라우저 · 페이지 하나. 렌더는 한 번에 하나(잠금)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pw = self.browser = self.ctx = self.page = None
        self.backgrounds: dict[str, bytes] = {}
        self.fonts: dict[str, tuple[bytes, str]] = {}
        self.fonts_served: dict[str, str] = {}  # 이번 페이지가 실제로 받은 글꼴 → sha256(렌더 기록용)
        self.blocked: list[str] = []

    # -- 네트워크 -------------------------------------------------------------------------------------------
    def _route(self, route, request) -> None:
        url = request.url
        if url.startswith(fe_origin() + "/"):
            headers = {**request.headers, TOKEN_HEADER: os.environ.get("PIXLATE_INTERNAL_RENDER_TOKEN", "")}
            route.continue_(headers=headers)
            return
        if url.startswith(BG_PREFIX):
            body = self.backgrounds.get(url[len(BG_PREFIX):])
            if body is None:
                route.fulfill(status=404, body=b"")
            else:
                route.fulfill(status=200, body=body, content_type="image/png")
            return
        if url.startswith(FONT_PREFIX) and url[len(FONT_PREFIX):] in self.fonts:
            body, ctype = self.fonts[url[len(FONT_PREFIX):]]
            self.fonts_served[url[len(FONT_PREFIX):]] = _sha256(body)
            route.fulfill(status=200, body=body, content_type=ctype, headers={"access-control-allow-origin": "*"})
            return
        self.blocked.append(url.split("?", 1)[0][:120])
        route.abort()

    # -- 열기 · 닫기 ----------------------------------------------------------------------------------------
    def _open(self) -> None:
        if not os.environ.get("PIXLATE_INTERNAL_RENDER_TOKEN"):
            raise RenderUnavailable("내부 렌더 토큰 설정 없음")
        if not fe_revision():
            raise RenderUnavailable("고정할 FE revision 설정 없음(PIXLATE_FE_RENDER_REVISION)")
        if not fe_revision_valid(fe_revision()):
            raise RenderUnavailable("PIXLATE_FE_RENDER_REVISION 이 FE 전체 SHA 40자(소문자 16진수)가 아니다")
        from playwright.sync_api import sync_playwright

        self.fonts = load_fonts()
        if self.pw is None:
            self.pw = sync_playwright().start()
        if self.browser is None:
            self.browser = self.pw.chromium.launch(args=list(CHROMIUM_ARGS))
            if self.browser.version != CHROMIUM_VERSION:
                raise RenderUnavailable(f"Chromium 버전 불일치: {self.browser.version} ≠ {CHROMIUM_VERSION}")
        self.ctx = self.browser.new_context(device_scale_factor=OUTPUT_SCALE, viewport={"width": 1000, "height": 1000})
        self.ctx.route("**/*", self._route)
        self.page = self.ctx.new_page()
        resp = self.page.goto(fe_origin() + RENDER_PATH, wait_until="load", timeout=timeout_ms())
        if resp is None or resp.status != 200:
            raise RenderUnavailable(f"FE 내부 렌더 경로 응답 {resp.status if resp else '없음'}")
        self.page.wait_for_function("document.documentElement.dataset.internalRender === 'ready'", timeout=timeout_ms())
        got = self.page.evaluate("document.documentElement.dataset.feRevision")
        if got != fe_revision():
            raise RenderUnavailable(f"FE revision 불일치: {str(got)[:40]} ≠ {fe_revision()[:40]}")

    def reset(self) -> None:
        """페이지 · context 를 버린다(브라우저는 둔다). 시간 초과 · 실패 뒤 다음 렌더가 새 페이지로 연다."""
        for obj in (self.page, self.ctx):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001 — 닫기 실패는 무시하고 새로 연다
                pass
        self.page = self.ctx = None
        self.backgrounds.clear()

    def close(self) -> None:
        self.reset()
        for obj, meth in ((self.browser, "close"), (self.pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self.browser = self.pw = None

    # -- 렌더 ----------------------------------------------------------------------------------------------
    def render(self, background_png: bytes, width: int, height: int, api_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            try:
                if self.page is None:
                    self._open()
                name = _sha256(background_png)[:32] + ".png"
                self.backgrounds = {name: background_png}  # 이전 작업 바탕을 남기지 않는다
                req = {"sectionId": 0, "width": width, "height": height, "backgroundUrl": BG_PREFIX + name, "blocks": api_blocks}
                self.page.set_viewport_size({"width": width, "height": height})
                res = self.page.evaluate(
                    "([req, ms]) => Promise.race([window.__pixlateRender.render(req),"
                    " new Promise((r) => setTimeout(() => r({ok: false, reason: 'timeout', detail: ms + 'ms'}), ms))])",
                    [req, timeout_ms()])
                if not isinstance(res, dict) or not res.get("ok"):
                    reason = res.get("reason") if isinstance(res, dict) else "응답 없음"
                    raise RenderUnavailable(f"내부 렌더 실패: {reason} {str((res or {}).get('detail', ''))[:120]}")
                png = self.page.locator("[data-render-section]").screenshot(type="png", timeout=timeout_ms())
                return {"png": png, "blocks": res["blocks"], "fonts": dict(self.fonts_served)}
            except RenderUnavailable:
                self.reset()
                raise
            except Exception as e:  # noqa: BLE001 — Playwright 오류 · 시간 초과 · 페이지 손상
                self.reset()
                raise RenderUnavailable(f"내부 렌더 오류: {e.__class__.__name__}") from e

    # -- 준비 확인(D.3) ------------------------------------------------------------------------------------
    def _page_ready(self) -> bool:
        """열린 페이지가 아직 쓸 수 있는지 — 준비 표시와 FE revision 만 본다(네트워크 요청 없음)."""
        try:
            got = self.page.evaluate("[document.documentElement.dataset.internalRender, document.documentElement.dataset.feRevision]")
        except Exception:  # noqa: BLE001 — 페이지 손상 · 브라우저 종료
            return False
        return got == ["ready", fe_revision()]

    def ready(self) -> str | None:
        """렌더 시도를 잡기 전에 FE 내부 렌더 경로가 준비됐는지 본다. 준비됐으면 None, 아니면 사유(토큰 · 값 없음).
        열린 페이지가 있으면 그대로 확인하고, 없거나 상했으면 렌더와 같은 조건(`_open`)으로 새로 연다."""
        with self.lock:
            if self.page is not None and self._page_ready():
                return None
            try:
                self.reset()
                self._open()
                return None
            except RenderUnavailable as e:
                self.reset()
                return str(e)
            except Exception as e:  # noqa: BLE001 — 브라우저 기동 실패 · 브라우저 종료 · FE 이름 풀이 실패 — 다음 확인에서 새로 띄운다
                self.close()
                # 운영 로그에서 원인을 알 수 있게 첫 줄만 붙인다(토큰은 요청 헤더에만 있어 이 메시지에 들어가지 않는다)
                first = (str(e).strip().splitlines() or [""])[0][:120]
                return f"내부 렌더 열기 오류: {e.__class__.__name__}" + (f" — {first}" if first else "")


_SESSION = _Session()
_DEFERRED = {"count": 0}  # 이 프로세스에서 연속으로 미룬 횟수(로그용) — 준비되면 0


def ensure_ready() -> str | None:
    """렌더 작업 진입점(app.tasks)이 시도를 임대하기 전에 부른다(D.3). None = 그려도 된다.
    사유가 있으면 호출자는 시도를 잡지 않고 끝낸다 — 시도는 pending 그대로(실패 기록 · 재시도 소모 · 이미지 변경 없음),
    복구 sweep(recovery._pending, PIXLATE_DISPATCH_STALE_S)이 다시 보낸다."""
    reason = _SESSION.ready()
    _DEFERRED["count"] = 0 if reason is None else _DEFERRED["count"] + 1
    return reason


def _ink_visible(ink: dict[str, float] | None, width: int, height: int) -> bool:
    """글자 ink 가 섹션 안에 조금이라도 있는가(기록용 — 판정에 쓰지 않는다, Pillow 기록과 같은 의미)."""
    if not ink:
        return False
    return ink["right"] > 0 and ink["bottom"] > 0 and ink["left"] < width and ink["top"] < height


def render(background_png: bytes, blocks: list[Any]) -> Any:
    """`typeset.render` 와 같은 반환 모양(RenderResult)으로 그린다. blocks = `render_input.RenderBlockInput` 목록."""
    from PIL import Image

    from app import typeset

    with Image.open(io.BytesIO(background_png)) as im:
        width, height = im.size
    out = typeset.RenderResult(png=b"", width=width, height=height)
    # 그릴 글자가 없어도 같은 경로로 촬영한다(바탕만) — Pillow 와 같은 크기 · 같은 결과 형태
    res = _SESSION.render(background_png, width, height, [b.api for b in blocks])
    with Image.open(io.BytesIO(res["png"])) as shot:
        if shot.size != (width, height):
            _SESSION.reset()
            raise RenderUnavailable(f"촬영 크기 불일치: {shot.size} ≠ {(width, height)}")
    by_id = {int(x["blockId"]): x for x in res["blocks"]}
    for b in blocks:
        bid = int(b.api["blockId"])
        m = (by_id.get(bid) or {}).get("measure") or {}
        if not m.get("ok"):
            _SESSION.reset()
            raise RenderUnavailable(f"블록 {bid} 측정 실패: {m.get('reason', '결과 없음')}")
        # 지정 크기(기준 크기 × 셀러 배율) 그대로 그렸는가 — Chromium 은 글자 크기를 10000px 에서 자른다(2026-11-19 실측: 배율 1e3 · 1e9 · 1e300
        # 모두 10000px). 적용값과 다른 크기로 그린 그림을 넘침으로 채택하지 않는다 — 그릴 수 없는 입력은 실패(Pillow 때와 같은 결과).
        want = float(b.api["style"]["fontSizePx"]) * float(b.api["style"]["fontScale"])
        got = m.get("fontPx")
        # 허용 차: 상대 1e-7(+1e-6px) — 단정밀도 반올림(상대 약 6e-8)만 허용한다. 실측(11-19): 0.1 · 9999.99 · 10000px 은 그대로,
        # 10000.01px 은 10000 으로 잘린다 → 0.01px 차이도 실패로 잡아야 하므로 이보다 넓히지 않는다
        if not isinstance(got, (int, float)) or abs(got - want) > want * 1e-7 + 1e-6:
            _SESSION.reset()
            raise RenderUnavailable(f"블록 {bid} 글자 크기를 지정대로 그릴 수 없다: 지정 {want:.6g}px · 실제 {got}")
        overflow = bool((m.get("overflow") or {}).get("any"))
        out.blocks[bid] = typeset.BlockRender(overflow=overflow, applied=b.applied, source=b.source, lines=len(m.get("lines") or []),
                                              ink_visible=_ink_visible(m.get("ink"), width, height) if overflow else True)
    out.png = res["png"]
    return out
