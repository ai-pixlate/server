"""서버 HTML 렌더(app/html_render.py) — 브라우저 없이 보는 부분.

- 요청 허용 목록: FE 원점에만 토큰 헤더, 바탕은 이 프로세스 바이트, Pretendard 는 고정본(SHA), 그 밖은 막는다
- 열기 조건: 토큰 · FE revision 설정, Chromium 버전, FE 응답, FE revision 일치 — 하나라도 어긋나면 대체 촬영 없이 실패
- 결과 변환: 측정 overflow.any → 넘침, 줄 수, ink_visible, 측정 실패 · 촬영 크기 불일치는 실패
- 렌더 입력: /preview 와 서버 렌더가 같은 함수(render_input.build_block)
실제 Chromium · FE 와의 결합은 로컬 · dev 확인(plans: 실제 흐름 계획 §5)으로 본다 — CI 에는 FE 컨테이너가 없다.
"""
from __future__ import annotations

import io
import shutil

import pytest
from PIL import Image

from app import html_render, render_input, typeset
from tests.exec_fakes import png_bytes

TOKEN = "unit-test-token"  # 시험 값(비밀 아님)


class _Req:
    def __init__(self, url, headers=None):
        self.url, self.headers = url, dict(headers or {"accept": "*/*"})


class _Route:
    def __init__(self):
        self.calls = []

    def continue_(self, headers=None):
        self.calls.append(("continue", headers))

    def fulfill(self, status=200, body=b"", content_type=None, headers=None):
        self.calls.append(("fulfill", status, body, content_type, headers))

    def abort(self):
        self.calls.append(("abort",))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PIXLATE_INTERNAL_RENDER_TOKEN", TOKEN)
    monkeypatch.setenv("PIXLATE_FE_RENDER_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("PIXLATE_FE_INTERNAL_ORIGIN", "http://pixlate-fe:3000")


def _route(session, url, headers=None):
    r = _Route()
    session._route(r, _Req(url, headers))
    return r.calls


def test_route_token_only_to_fe_origin(env):
    s = html_render._Session()
    s.fonts = html_render.load_fonts()
    s.backgrounds = {"bg.png": b"PNG"}
    fe = _route(s, "http://pixlate-fe:3000/internal/typeset-render")
    assert fe[0][0] == "continue" and fe[0][1][html_render.TOKEN_HEADER] == TOKEN and fe[0][1]["accept"] == "*/*"
    assert _route(s, "http://pixlate-fe:3000/_next/static/chunks/a.js")[0][1][html_render.TOKEN_HEADER] == TOKEN
    # 비슷한 이름의 다른 원점에는 붙이지 않는다(접두어 속임)
    assert _route(s, "http://pixlate-fe:3000.evil.test/x") == [("abort",)]
    bg = _route(s, html_render.BG_PREFIX + "bg.png")
    assert bg == [("fulfill", 200, b"PNG", "image/png", None)]
    assert _route(s, html_render.BG_PREFIX + "other.png")[0][:2] == ("fulfill", 404)
    font = _route(s, html_render.FONT_PREFIX + "packages/pretendard/dist/web/static/woff2/Pretendard-Regular.woff2")
    assert font[0][0] == "fulfill" and font[0][3] == "font/woff2" and len(font[0][2]) == 765892
    # 고정본이 아닌 글꼴(다른 굵기 · woff) · 외부 주소는 막는다 — 토큰은 어디에도 실리지 않는다
    for url in (html_render.FONT_PREFIX + "packages/pretendard/dist/web/static/woff2/Pretendard-Bold.woff2",
                html_render.FONT_PREFIX + "packages/pretendard/dist/web/static/woff/Pretendard-Regular.woff",
                "https://example.com/x.js", "https://fonts.googleapis.com/css2"):
        assert _route(s, url) == [("abort",)]
    assert len(s.blocked) == 5 and all(TOKEN not in u for u in s.blocked)
    assert s.fonts_served == {"packages/pretendard/dist/web/static/woff2/Pretendard-Regular.woff2": html_render.FONT_FILES[
        "packages/pretendard/dist/web/static/woff2/Pretendard-Regular.woff2"][1]}


def test_fonts_pinned_by_sha(monkeypatch, tmp_path):
    assert set(html_render.load_fonts()) == set(html_render.FONT_FILES)
    shutil.copytree(html_render.FONT_DIR, tmp_path / "f")
    (tmp_path / "f" / "Pretendard-SemiBold.woff2").write_bytes(b"not the pinned font")
    monkeypatch.setattr(html_render, "FONT_DIR", tmp_path / "f")
    with pytest.raises(html_render.RenderUnavailable, match="SHA"):
        html_render.load_fonts()
    (tmp_path / "f" / "Pretendard-SemiBold.woff2").unlink()
    with pytest.raises(html_render.RenderUnavailable, match="없음"):
        html_render.load_fonts()


def test_open_requires_token_and_revision(monkeypatch):
    s = html_render._Session()
    monkeypatch.delenv("PIXLATE_INTERNAL_RENDER_TOKEN", raising=False)
    monkeypatch.setenv("PIXLATE_FE_RENDER_REVISION", "abc")
    with pytest.raises(html_render.RenderUnavailable, match="토큰"):
        s._open()
    monkeypatch.setenv("PIXLATE_INTERNAL_RENDER_TOKEN", TOKEN)
    monkeypatch.setenv("PIXLATE_FE_RENDER_REVISION", "")
    with pytest.raises(html_render.RenderUnavailable, match="revision"):
        s._open()
    for short in ("abcdef1", "0123456789ABCDEF0123456789ABCDEF01234567", "0123456789abcdef0123456789abcdef012345670"):  # 줄인 SHA · 대문자 · 41자는 bundleId 가 될 수 없다
        monkeypatch.setenv("PIXLATE_FE_RENDER_REVISION", short)
        with pytest.raises(html_render.RenderUnavailable, match="40자"):
            s._open()


class _FakePage:
    def __init__(self, status=200, rev="0123456789abcdef0123456789abcdef01234567", result=None, shot=None):
        self.status, self.rev, self.result, self.shot = status, rev, result, shot
        self.closed = False
        self.state, self.gotos = "ready", 0

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.gotos += 1
        return type("R", (), {"status": self.status})()

    def wait_for_function(self, js, timeout=None):
        return True

    def evaluate(self, js, arg=None):
        if "internalRender" in js and "feRevision" in js:  # 준비 확인(D.3)
            return [self.state, self.rev]
        if "feRevision" in js:
            return self.rev
        self.req = arg[0]
        return self.result

    def set_viewport_size(self, size):
        self.viewport = size

    def locator(self, sel):
        page = self

        class L:
            def screenshot(self, type=None, timeout=None):
                return page.shot

        return L()

    def close(self):
        self.closed = True


class _FakeCtx:
    def __init__(self, page):
        self.page = page

    def route(self, pattern, handler):
        self.handler = handler

    def new_page(self):
        return self.page

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, page, version=html_render.CHROMIUM_VERSION):
        self.page, self.version = page, version
        self.launch_args = None

    def new_context(self, **kw):
        self.ctx_kw = kw
        return _FakeCtx(self.page)

    def close(self):
        pass


def _session_with(monkeypatch, page, version=html_render.CHROMIUM_VERSION):
    s = html_render._Session()
    browser = _FakeBrowser(page, version)

    class PW:
        class chromium:
            @staticmethod
            def launch(args=None):
                browser.launch_args = args
                return browser

        def stop(self):
            pass

    s.pw = PW()
    return s, browser


def _blocks(*ids):
    return [render_input.RenderBlockInput(
        api={"blockId": i, "revision": 1, "text": "Soft and smooth skin", "box": {"x": 10, "y": 10, "w": 200, "h": 50},
             "style": {"fontFamily": "Pretendard", "fontWeight": 400, "fontSizePx": 28, "fontScale": 1, "fontColor": "#222222",
                       "align": "left", "letterSpacingEm": -0.03, "lineHeightRatio": 1.2}},
        applied={"font_color": "#222222", "est_font_px": 28, "align": "left"},
        source={"font_color": "role_default", "est_font_px": "role_default", "align": "role_default"}) for i in ids]


def _ok(*specs, font_px=28):
    return {"ok": True, "blocks": [{"blockId": i, "measure": {"ok": True, "fontPx": font_px, "lines": [{}] * n, "ink": ink,
                                                             "overflow": {"any": over}}} for i, n, over, ink in specs]}


def test_render_fails_when_font_size_is_not_drawn_as_specified(env, monkeypatch):
    """Chromium 은 글자 크기를 10000px 에서 자른다(실측). 지정 크기(기준 × 배율)와 다르게 그린 그림은 넘침으로 채택하지 않고 실패한다."""
    bg = png_bytes(300, 200)
    big = _blocks(1)
    big[0].api["style"]["fontScale"] = 1e3  # 지정 28000px
    s, _ = _session_with(monkeypatch, _FakePage(result=_ok((1, 1, True, None), font_px=10000), shot=bg))
    monkeypatch.setattr(html_render, "_SESSION", s)
    with pytest.raises(html_render.RenderUnavailable, match="지정대로"):
        html_render.render(bg, big)
    # 실측 경계(11-19, Chromium 159): 지정 10000px → 10000 그대로(채택), 지정 10000.01px → 10000 으로 잘림(실패)
    for want_px, drawn_px, ok in ((10000.0, 10000, True), (10000.01, 10000, False)):
        blk = _blocks(3)
        blk[0].api["style"].update(fontSizePx=want_px, fontScale=1)
        s3, _ = _session_with(monkeypatch, _FakePage(result=_ok((3, 1, True, None), font_px=drawn_px), shot=bg))
        monkeypatch.setattr(html_render, "_SESSION", s3)
        if ok:
            assert html_render.render(bg, blk).blocks[3].overflow is True
        else:
            with pytest.raises(html_render.RenderUnavailable, match="지정대로"):
                html_render.render(bg, blk)
    # 단정밀도 반올림(상대 약 6e-8)은 같은 크기로 본다
    near = _blocks(2)
    near[0].api["style"]["fontScale"] = 0.8  # 22.4px
    s2, _ = _session_with(monkeypatch, _FakePage(result=_ok((2, 1, False, None), font_px=22.399999618530273), shot=bg))
    monkeypatch.setattr(html_render, "_SESSION", s2)
    assert html_render.render(bg, near).blocks[2].overflow is False


def test_open_checks_chromium_version_fe_status_and_revision(env, monkeypatch):
    for page, version, msg in ((_FakePage(), "158.0.0.0", "Chromium 버전"), (_FakePage(status=404), html_render.CHROMIUM_VERSION, "응답 404"),
                               (_FakePage(rev="other"), html_render.CHROMIUM_VERSION, "revision 불일치")):
        s, browser = _session_with(monkeypatch, page, version)
        with pytest.raises(html_render.RenderUnavailable, match=msg):
            s._open()
    s, browser = _session_with(monkeypatch, _FakePage())
    s._open()
    assert browser.launch_args == ["--font-render-hinting=none"]
    assert browser.ctx_kw["device_scale_factor"] == html_render.OUTPUT_SCALE == 1  # 출력 배율 — 바꾸면 규칙판을 올린다
    assert s.page.url == "http://pixlate-fe:3000/internal/typeset-render"


def test_render_maps_measure_and_resets_on_failure(env, monkeypatch):
    bg = png_bytes(300, 200)
    page = _FakePage(result=_ok((1, 2, False, {"left": 10, "top": 10, "right": 150, "bottom": 70}),
                                (2, 1, True, {"left": 290, "top": 10, "right": 310, "bottom": 40}),
                                (3, 1, True, {"left": 320, "top": 10, "right": 400, "bottom": 40})), shot=bg)
    s, _ = _session_with(monkeypatch, page)
    monkeypatch.setattr(html_render, "_SESSION", s)
    res = html_render.render(bg, _blocks(1, 2, 3))
    assert (res.width, res.height, res.png) == (300, 200, bg)
    assert {k: (v.overflow, v.lines, v.ink_visible) for k, v in res.blocks.items()} == {1: (False, 2, True), 2: (True, 1, True), 3: (True, 1, False)}
    assert res.blocks[1].applied == {"font_color": "#222222", "est_font_px": 28, "align": "left"}
    # 입력은 renderInput 블록 그대로 · 바탕은 render.local 로만
    assert page.req["blocks"] == [b.api for b in _blocks(1, 2, 3)] and page.req["backgroundUrl"].startswith(html_render.BG_PREFIX)
    assert page.viewport == {"width": 300, "height": 200}

    # 측정 실패(글꼴 미준비 · 글리프 대체 등) → 대체 촬영 없이 실패, 페이지를 버린다
    page.result = {"ok": True, "blocks": [{"blockId": 1, "measure": {"ok": False, "reason": "glyph-fallback"}}]}
    with pytest.raises(html_render.RenderUnavailable, match="glyph-fallback"):
        html_render.render(bg, _blocks(1))
    assert s.page is None and page.closed

    # 내부 렌더 실패(시간 초과 · busy · 글꼴) → 실패, 다음 렌더는 새 페이지
    for result in ({"ok": False, "reason": "timeout", "detail": "20000ms"}, {"ok": False, "reason": "font-not-ready"}, None):
        p = _FakePage(result=result, shot=bg)
        s2, _ = _session_with(monkeypatch, p)
        monkeypatch.setattr(html_render, "_SESSION", s2)
        with pytest.raises(html_render.RenderUnavailable):
            html_render.render(bg, _blocks(1))
        assert s2.page is None

    # 촬영 크기가 바탕과 다르면 실패
    p = _FakePage(result=_ok((1, 1, False, None)), shot=png_bytes(299, 200))
    s3, _ = _session_with(monkeypatch, p)
    monkeypatch.setattr(html_render, "_SESSION", s3)
    with pytest.raises(html_render.RenderUnavailable, match="크기"):
        html_render.render(bg, _blocks(1))


def test_playwright_errors_become_render_unavailable(env, monkeypatch):
    class Boom(_FakePage):
        def evaluate(self, js, arg=None):
            if "feRevision" in js:
                return self.rev
            raise TimeoutError("page crashed")

    s, _ = _session_with(monkeypatch, Boom(shot=png_bytes(10, 10)))
    with pytest.raises(html_render.RenderUnavailable, match="TimeoutError") as e:
        s.render(png_bytes(10, 10), 10, 10, [])
    assert TOKEN not in str(e.value) and s.page is None


def test_ready_reuses_open_page_and_reopens_when_stale(env, monkeypatch):
    """D.3 준비 확인: 열린 페이지가 준비 표시 · revision 이 맞으면 다시 열지 않고, 상했으면 렌더와 같은 조건으로 다시 연다."""
    page = _FakePage()
    s, _ = _session_with(monkeypatch, page)
    assert s.ready() is None and page.gotos == 1
    assert s.ready() is None and page.gotos == 1  # 그대로 쓴다(네트워크 요청 없음)
    page.state = "loading"  # 페이지가 상함 → 다시 연다
    assert s.ready() is None and page.gotos == 2
    page.rev = "f" * 40  # FE 가 다른 빌드로 바뀜 → 다시 열어도 revision 불일치 → 사유, 페이지 버림
    why = s.ready()
    assert "revision 불일치" in why and s.page is None


def test_ready_reports_reason_without_token_and_closes_browser_on_crash(env, monkeypatch):
    s, _ = _session_with(monkeypatch, _FakePage(status=502))
    why = s.ready()
    assert "응답 502" in why and s.page is None and TOKEN not in why
    monkeypatch.delenv("PIXLATE_INTERNAL_RENDER_TOKEN")
    assert "토큰" in html_render._Session().ready()

    class Down(_FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            raise ConnectionError("connection refused")

    s, _ = _session_with(monkeypatch, Down())
    monkeypatch.setenv("PIXLATE_INTERNAL_RENDER_TOKEN", TOKEN)
    assert s.ready() == "내부 렌더 열기 오류: ConnectionError — connection refused"  # 첫 줄만 붙인다(운영 로그 원인 확인)
    assert s.browser is None and s.page is None  # 다음 확인에서 브라우저를 새로 띄운다


def test_ensure_ready_counts_consecutive_deferrals(html_render_session):
    html_render._DEFERRED["count"] = 0
    html_render_session.not_ready = "FE 내부 렌더 경로 응답 502"
    assert html_render.ensure_ready() == "FE 내부 렌더 경로 응답 502" and html_render._DEFERRED["count"] == 1
    assert html_render.ensure_ready() and html_render._DEFERRED["count"] == 2
    html_render_session.not_ready = None
    assert html_render.ensure_ready() is None and html_render._DEFERRED["count"] == 0


def test_renderer_version_and_render_input_are_shared():
    assert typeset.RENDERER_VERSION == html_render.RENDERER_VERSION
    # 합의 형식(11-21): rendererId 에 FE 빌드는 들어가지 않는다(그것은 bundleId)
    assert html_render.RULE_VERSION == 1
    assert html_render.RENDERER_VERSION == f"html-typeset/1+chromium-{html_render.CHROMIUM_VERSION}" == "html-typeset/1+chromium-159.0.8154.6"
    assert html_render.bundle_id_for("0123456789abcdef0123456789abcdef01234567") == "fe-0123456789abcdef0123456789abcdef01234567"
    assert [html_render.bundle_id_for(r) for r in ("", "abcdef1", "0123456789ABCDEF0123456789ABCDEF01234567", "0123456789abcdef0123456789abcdef0123456g")] == [None] * 4
    assert html_render.BUNDLE_ID is None  # 시험 환경은 FE revision 설정이 없다
    assert typeset.PILLOW_RENDERER_VERSION.startswith("pillow-typeset/3+pillow-")
    d = typeset.load_role_defaults().data
    common = dict(block_id=7, revision=3, role="body", is_excluded=False, source_ko="원문", text="Soft skin", style=None,
                  style_stage="failed", box={"x": 1.5, "y": 2, "w": 300.25, "h": 40}, section_width=860, defaults=d)
    b = render_input.build_block(font_scale=1.25, **common)
    assert b.api["box"] == {"x": 1.5, "y": 2, "w": 300.25, "h": 40} and b.api["style"]["fontScale"] == 1.25
    assert set(b.api["style"]) == {"fontFamily", "fontWeight", "fontSizePx", "fontScale", "fontColor", "align", "letterSpacingEm", "lineHeightRatio"}
    assert b.api["style"]["letterSpacingEm"] == pytest.approx(-0.03) and b.api["style"]["fontWeight"] == 400
    assert b.applied["font_scale"] == 1.25 and "font_scale" not in render_input.build_block(font_scale=1.0, **common).applied
    assert render_input.build_block(font_scale=1.0, **{**common, "text": "  "}) is None  # 빈 번역 = 그리지 않음


def test_ink_visible():
    assert html_render._ink_visible({"left": -5, "top": 0, "right": 1, "bottom": 5}, 10, 10)
    assert not html_render._ink_visible({"left": 10, "top": 0, "right": 20, "bottom": 5}, 10, 10)
    assert not html_render._ink_visible(None, 10, 10)


def test_fake_session_png_is_background(html_render_session):
    bg = png_bytes(40, 30)
    res = html_render.render(bg, _blocks(1))
    with Image.open(io.BytesIO(res.png)) as im:
        assert im.size == (40, 30)
    assert html_render_session.calls[0]["blocks"][0]["blockId"] == 1
