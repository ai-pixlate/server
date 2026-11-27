"""내용(JSON) → HTML → Playwright 스크린샷(PNG) + 보고서."""
import json
import os
import tempfile
from pathlib import Path

# Chromium 을 한글이 없는 경로(C:\ms-playwright)에 설치했다. 그 폴더가 있으면 그것을 쓴다.
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path(r"C:\ms-playwright").is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = r"C:\ms-playwright"

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from PIL import Image

from . import layout
from .spec import H, META, ROOT, STYLE_DEFAULT, W, ContentError, load_spec, validate

TEMPLATES = ROOT / "templates"
# [이 레포에서 수정] 글꼴은 조판 계약과 같은 Pretendard — 레포의 pipeline/data/fonts/pretendard 파일을 쓴다(사본에 있던 Noto Sans 는 뺐다).
FONT_DIR = ROOT.parents[3] / "pipeline" / "data" / "fonts" / "pretendard"
FONT = FONT_DIR / "Pretendard-Regular.otf"
FONT_BOLD = FONT_DIR / "Pretendard-SemiBold.otf"


def _font_uri(path=FONT):
    if path.is_file():
        return path.as_uri()
    return ""  # 폰트 파일이 없으면 CSS 의 대체 글꼴(Arial)로 그린다


class Renderer:
    """여러 장을 그릴 때 브라우저를 한 번만 띄우도록 with 문으로 쓴다.

        with Renderer(scale=1) as r:
            report = r.render_file("samples/content/2A.json", "out.png")
    """

    def __init__(self, scale=1, spec_path=None):
        self.scale = scale
        self.spec = load_spec(spec_path) if spec_path else load_spec()
        self.env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                               autoescape=select_autoescape(["html"]),
                               undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
        self.base_css = (TEMPLATES / "base.css").read_text(encoding="utf-8")
        self.fit_js = (Path(__file__).parent / "fit.js").read_text(encoding="utf-8")
        self._pw = self._browser = self._page = None

    # -------------------------------------------------- 브라우저
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        ctx = self._browser.new_context(viewport={"width": W, "height": H},
                                        device_scale_factor=self.scale)
        self._page = ctx.new_page()
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # -------------------------------------------------- HTML 만들기
    def build_html(self, raw, base_dir="."):
        c = validate(raw, base_dir)
        ctx = layout.build(self.spec, c)
        spec_json = json.dumps(ctx["spec_map"], ensure_ascii=True).replace("</", "<\\/")
        html = self.env.get_template(f"{c['template']}.html").render(
            c=c, tid=c["template"], font_uri=_font_uri(), font_bold_uri=_font_uri(FONT_BOLD), base_css=self.base_css,
            fit_js=self.fit_js, spec_json=spec_json, **ctx)
        return html, c, ctx

    # -------------------------------------------------- 그리기
    def render(self, raw, out_png, base_dir=".", html_out=None):
        html, c, ctx = self.build_html(raw, base_dir)
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        if html_out:
            html_path = Path(html_out)
            html_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = None
        else:
            fd, tmp = tempfile.mkstemp(suffix=".html")
            os.close(fd)
            html_path = Path(tmp)
        html_path.write_text(html, encoding="utf-8")
        try:
            page = self._page
            page.goto(html_path.resolve().as_uri())
            page.wait_for_function("window.__aplusReport !== undefined", timeout=20000)
            res = page.evaluate("window.__aplusReport")
            page.screenshot(path=str(out_png))
        finally:
            if tmp:
                os.remove(tmp)
        with Image.open(out_png) as im:
            size = list(im.size)
        want = [W * self.scale, H * self.scale]
        return make_report(c, ctx, res, out_png, size, want, html_out)

    def render_file(self, json_path, out_png, html_out=None):
        json_path = Path(json_path)
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        rep = self.render(raw, out_png, base_dir=json_path.parent, html_out=html_out)
        rep["input"] = str(json_path)
        return rep


def make_report(c, ctx, res, out_png, size, want, html_out):
    tid = c["template"]
    slot, frame, module = META[tid]
    warnings = []
    info = [f"[밀림] {p['name']} 이(가) 길어서 아래 글이 {p['px']}px 내려감" for p in res.get("pushes", [])]
    for t in res["texts"]:
        if t["overflow"] and t.get("flow"):
            warnings.append(f"[넘침] 글 칸 전체({t['name']}): 아래 끝을 {t['height_over']}px "
                            f"(약 {t['overflow_lines']}줄) 넘침 — 본문·제목을 최소 크기까지 줄인 뒤에도 넘침")
            continue
        if t["overflow"]:
            why = []
            if t.get("overflow_lines"):
                why.append(f"{t['overflow_lines']}줄 넘침")
            if t.get("width_over"):
                why.append(f"가로 {t['width_over']}px 넘침(긴 단어)")
            if t.get("height_over") and not t.get("overflow_lines"):
                why.append(f"세로 {t['height_over']}px 넘침")
            warnings.append(f"[넘침] {t['name']}: {', '.join(why) or '넘침'} "
                            f"(칸에 들어가는 줄 {t.get('allowed_lines', t['max_lines'])}줄, "
                            f"{t['base']}→{t['min']}px까지 줄여도 {t['lines']}줄)")
    over_names = {t["name"] for t in res["texts"] if t["overflow"]}
    for o in res["outside"]:
        if o["name"] in over_names:
            continue  # 넘친 글이 구역 밖으로 나간 것은 위 [넘침] 경고와 같은 내용

        warnings.append(f"[위치] {o['name']} 이(가) 치수 구역 '{o['area']}' 밖으로 {o['px']}px 나감 ({', '.join(o['sides'])})")
    for h in res.get("deco_hits", []):
        warnings.append(f"[장식] {h['deco']}이(가) '{h['hits']}' 을(를) 가림")
    if res.get("vshift"):
        info.append(f"[세로 가운데] 글 덩어리를 {res['vshift']:+g}px 옮김")
    if res.get("margin_deco") == 0:
        info.append("[여백 장식] 글·이미지를 가리지 않는 빈 모서리가 없어 넣지 않음")
    for b in res.get("broken_images", []):
        warnings.append(f"[이미지] 불러오지 못함: {b}")
    if size != want:
        warnings.append(f"[크기] {size[0]}×{size[1]} (기대 {want[0]}×{want[1]})")
    return {
        "template": tid, "slot": slot, "frame": frame, "module": module,
        "mirror": c["mirror"], "mode": c["mode"], "items": len(c["items"]),
        "layout": c["layout"],
        "style": {k: v for k, v in c["style"].items() if v != STYLE_DEFAULT[k]},
        "output": str(out_png), "html": str(html_out) if html_out else None,
        "size": size, "size_ok": size == want,
        "overflow": any(t["overflow"] for t in res["texts"]),
        "texts": res["texts"], "outside": res["outside"],
        "notes": ctx.get("notes", []),
        "info": info,
        "warnings": warnings,
        "spec_boxes": ctx["spec_boxes"],
    }


__all__ = ["Renderer", "ContentError"]
