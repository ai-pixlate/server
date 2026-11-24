"""⑨ 조판 자동 조정 · 경계 이탈 판정 — 실행 · 측정 오차 측정 · 검수 자료 생성(2026-11-24). 개발용, 운영 경로 아님.

운영 코드(`pipeline/handoff/typeset_adjust.py` · `typeset_boundary.py` · `typeset_geometry.py`)를 그대로 부르고, 측정기(SectionMeasurer)만
여기서 만든다. 측정 · 그림은 FE 내부 렌더 경로(`/internal/typeset-render` — 서버 HTML 렌더와 같은 FE 조판 · 측정 코드)를 연다.

    # 입력: 기존 넘침 측정 도구의 입력(베이스라인 v1.0.1 보존본 → renderInput 모양)
    python docs/ai-experiments/tools/typeset_browser_measure.py build-input --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 --out <run>/input.json
    # 측정 오차(엔진 글자 경계 ↔ 실제 픽셀) — 이웃 간격(GAP_PX)의 근거
    python docs/ai-experiments/tools/typeset_auto_adjust_run.py probe --input <run>/input.json --out <run>/<env> --backend local
    # 자동 조정 + 경계 판정 + 그림
    python docs/ai-experiments/tools/typeset_auto_adjust_run.py run --input <run>/input.json --out <run>/<env> --backend local

측정기(--backend):
- local: 이 PC의 Playwright Chromium + 로컬로 띄운 FE(`next start`, --fe-origin). 서버와 OS · Chromium 버전이 다르다 — 로컬 측정으로만 쓴다.
  글꼴 · 바탕 · 외부 요청 처리는 서버 렌더러(`app.html_render`)와 같은 규칙(같은 상수 · 고정본 글꼴)을 쓴다.
- server: BE 워커 환경에서 `app.html_render` 세션을 그대로 쓴다(서버 Chromium · 힌팅 옵션 · FE revision 대조 · 토큰 — 환경 변수는 deploy/README).
  섹션 밖으로 나간 글자 그림(전체 그림)은 서버 세션이 촬영하지 않아 잘린 출력 그림만 남긴다.
토큰 값은 출력 · 기록하지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from app import html_render as hr  # noqa: E402  상수 · 글꼴 고정본 · 서버 세션
from pipeline.handoff import typeset_adjust as adj  # noqa: E402
from pipeline.handoff import typeset_boundary as bd  # noqa: E402
from pipeline.handoff import typeset_geometry as geo  # noqa: E402

TOOL_VERSION = "typeset-auto-adjust-run/1"
MARGIN = 120  # 전체 그림의 섹션 밖 여백(px) — 섹션 밖으로 나간 글자를 보이게 한다
PIXEL_INK_LEVEL = 245  # 흰 바탕에서 이 밝기 미만이면 글자 픽셀(눈에 보이는 가장자리까지 포함)


def _png(width: int, height: int, color=(255, 255, 255)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


WHITE_1PX = _png(1, 1)


# ───────────────────────── 측정기 ─────────────────────────

class LocalFe:
    """로컬 Chromium 으로 FE 내부 렌더 경로를 연다 — 서버 렌더러와 같은 요청 처리 규칙."""

    def __init__(self, origin: str, revision: str, token: str):
        from playwright.sync_api import sync_playwright
        self.origin, self.revision, self.token = origin.rstrip("/"), revision, token
        self.fonts = hr.load_fonts()
        self.fonts_served: dict[str, str] = {}
        self.bg: dict[str, bytes] = {}
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(args=list(hr.CHROMIUM_ARGS))
        self.ctx = self.browser.new_context(device_scale_factor=hr.OUTPUT_SCALE, viewport={"width": 1400, "height": 1000})
        self.ctx.route("**/*", self._route)
        self.page = self.ctx.new_page()
        resp = self.page.goto(self.origin + hr.RENDER_PATH, wait_until="load", timeout=60000)
        if resp is None or resp.status != 200:
            raise SystemExit(f"FE 내부 렌더 경로 응답 {resp.status if resp else '없음'} — FE 를 토큰과 함께 띄웠는지 확인한다")
        self.page.wait_for_function("document.documentElement.dataset.internalRender === 'ready'", timeout=60000)
        got = self.page.evaluate("document.documentElement.dataset.feRevision")
        if got != revision:
            raise SystemExit(f"FE revision 불일치: {got} ≠ {revision}")
        self.page.evaluate(f"document.body.style.margin = '{MARGIN}px'")
        self.renders = 0

    def _route(self, route, request) -> None:
        url = request.url
        if url.startswith(self.origin + "/"):
            route.continue_(headers={**request.headers, hr.TOKEN_HEADER: self.token})
        elif url.startswith(hr.BG_PREFIX):
            body = self.bg.get(url[len(hr.BG_PREFIX):])
            route.fulfill(status=200 if body else 404, body=body or b"", content_type="image/png")
        elif url.startswith(hr.FONT_PREFIX) and url[len(hr.FONT_PREFIX):] in self.fonts:
            body, ctype = self.fonts[url[len(hr.FONT_PREFIX):]]
            self.fonts_served[url[len(hr.FONT_PREFIX):]] = hashlib.sha256(body).hexdigest()
            route.fulfill(status=200, body=body, content_type=ctype, headers={"access-control-allow-origin": "*"})
        else:
            route.abort()

    def env(self) -> dict[str, Any]:
        return {"backend": "local", "chromium": self.browser.version, "chromiumArgs": list(hr.CHROMIUM_ARGS), "outputScale": hr.OUTPUT_SCALE,
                "feRevision": self.revision, "platform": {"system": platform.system(), "release": platform.release(),
                                                         "version": platform.version(), "machine": platform.machine()},
                "playwright": _pkg_version("playwright"), "serverRendererVersion": hr.RENDERER_VERSION,
                "fontsServed": dict(self.fonts_served)}

    def render(self, width: int, height: int, bg: bytes, blocks: list[dict[str, Any]], shots: bool = False) -> dict[str, Any]:
        name = hashlib.sha256(bg).hexdigest()[:32] + ".png"
        self.bg = {name: bg}
        self.page.set_viewport_size({"width": int(width) + 2 * MARGIN + 40, "height": int(height) + 2 * MARGIN + 40})
        res = self.page.evaluate("(req) => window.__pixlateRender.render(req)",
                                 {"sectionId": 0, "width": width, "height": height, "backgroundUrl": hr.BG_PREFIX + name, "blocks": blocks})
        self.renders += 1
        if not res.get("ok"):
            raise RuntimeError(f"내부 렌더 실패: {res.get('reason')} {str(res.get('detail'))[:200]}")
        out = {"measures": {int(b["blockId"]): b["measure"] for b in res["blocks"]}}
        if shots:
            sec = self.page.locator("[data-render-section]")
            out["clip"] = sec.screenshot(type="png")
            bb = sec.bounding_box()
            self.page.evaluate("document.querySelector('[data-render-section]').style.overflow = 'visible'")
            out["full"] = self.page.screenshot(type="png", clip={"x": bb["x"] - MARGIN, "y": bb["y"] - MARGIN,
                                                                 "width": width + 2 * MARGIN, "height": height + 2 * MARGIN})
        return out

    def close(self) -> None:
        self.browser.close()
        self.pw.stop()


class ServerSession:
    """BE 워커 환경의 서버 HTML 렌더 세션(`app.html_render`) — 이 PC 에는 BE 환경이 없어 실행하지 않았다."""

    def __init__(self):
        reason = hr.ensure_ready()
        if reason:
            raise SystemExit(f"서버 HTML 렌더 준비 안 됨: {reason}")
        self.renders = 0

    def env(self) -> dict[str, Any]:
        return {"backend": "server", "rendererId": hr.RENDERER_VERSION, "bundleId": hr.BUNDLE_ID, "chromium": hr.CHROMIUM_VERSION,
                "chromiumArgs": list(hr.CHROMIUM_ARGS), "outputScale": hr.OUTPUT_SCALE,
                "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()}}

    def render(self, width: int, height: int, bg: bytes, blocks: list[dict[str, Any]], shots: bool = False) -> dict[str, Any]:
        if bg == WHITE_1PX:
            bg = _png(int(width), int(height))  # 서버 세션은 바탕 크기로 촬영 크기를 대조하지 않지만 실제 크기 바탕을 넣는다
        res = hr._SESSION.render(bg, int(width), int(height), blocks)
        self.renders += 1
        out = {"measures": {int(b["blockId"]): b["measure"] for b in res["blocks"]}}
        if shots:
            out["clip"] = res["png"]
        return out

    def close(self) -> None:
        hr._SESSION.close()


def _pkg_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:  # noqa: BLE001
        return None


def open_backend(args) -> Any:
    if args.backend == "server":
        return ServerSession()
    token = os.environ.get("PIXLATE_INTERNAL_RENDER_TOKEN")
    if not token:
        raise SystemExit("PIXLATE_INTERNAL_RENDER_TOKEN 이 없다 — 로컬 FE 를 띄운 것과 같은 값을 환경 변수로 준다(출력하지 않는다)")
    return LocalFe(args.fe_origin, args.fe_revision, token)


def render_tolerant(backend, width, height, bg, blocks, shots=False) -> dict[str, Any]:
    """내부 렌더는 블록 하나라도 측정에 실패하면 섹션 전체를 실패로 돌려준다(예: 글꼴에 없는 글자 — glyph-fallback).
    그럴 때 블록을 하나씩 다시 재 실패한 블록만 측정 실패로 남기고, 그림은 실패 블록을 뺀 입력으로 촬영한다(failedBlocks 에 기록).
    운영 서버 렌더는 이 경우 섹션 렌더 자체가 실패한다(`app.html_render.RenderUnavailable`)."""
    try:
        return backend.render(width, height, bg, blocks, shots)
    except RuntimeError:
        pass
    measures, failed = {}, {}
    for b in blocks:
        try:
            measures.update(backend.render(width, height, WHITE_1PX, [b])["measures"])
        except RuntimeError as e:
            failed[int(b["blockId"])] = str(e)[:200]
            measures[int(b["blockId"])] = {"ok": False, "reason": "render-failed", "detail": str(e)[:200]}
    out = {"measures": measures, "failedBlocks": failed}
    if shots:
        rest = [b for b in blocks if int(b["blockId"]) not in failed]
        out.update({k: v for k, v in backend.render(width, height, bg, rest, True).items() if k != "measures"})
    return out


def make_measurer(backend):
    """SectionMeasurer — 측정만 할 때는 1px 흰 바탕을 늘려 쓴다(측정은 바탕과 무관하고 그림을 촬영하지 않는다)."""
    def measure(section, blocks):
        return render_tolerant(backend, section["width"], section["height"], WHITE_1PX, [dict(b) for b in blocks])["measures"]
    return measure


# ───────────────────────── 입력 ─────────────────────────

def load_input(path: Path) -> dict[str, Any]:
    inp = json.loads(path.read_text(encoding="utf-8"))
    for s in inp["sections"]:
        s["_bg"] = (ROOT / s["background"]["path"]).read_bytes()
    return inp


def api_block(b: dict[str, Any]) -> dict[str, Any]:
    return {"blockId": b["blockId"], "revision": b["revision"], "text": b["text"], "box": dict(b["box"]), "style": dict(b["style"])}


def static_obstacles(inp: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """조판 대상이 아닌 블록(제외 · 라벨 · 로고 · 번역 없음)의 원래 박스 — 섹션별."""
    import typeset_adjust_experiment as ae  # PR #141 도구의 보존본 박스 해석(applied_layout)을 그대로 쓴다
    allobs = ae.load_obstacles(inp)
    typeset = {s["key"]: {b["blockId"] for b in s["blocks"]} for s in inp["sections"]}
    return {k: [o for o in v if o["blockId"] not in typeset.get(k, set())] for k, v in allobs.items()}


# ───────────────────────── probe: 측정 오차 ─────────────────────────

def pixel_bounds(png: bytes, ox: float, oy: float) -> dict[str, float] | None:
    """흰 바탕 검정 글자 그림에서 글자 픽셀의 바깥 경계(섹션 좌표 · 픽셀 가장자리)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("L").point(lambda v: 255 if v < PIXEL_INK_LEVEL else 0)
    bb = im.getbbox()
    if not bb:
        return None
    s = hr.OUTPUT_SCALE
    return {"left": bb[0] / s - ox, "top": bb[1] / s - oy, "right": bb[2] / s - ox, "bottom": bb[3] / s - oy}


def probe(args) -> None:
    inp = load_input(args.input)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    be = open_backend(args)
    if not isinstance(be, LocalFe):
        raise SystemExit("probe 는 전체 그림(섹션 밖 포함)이 필요해 local 에서만 돈다 — 서버 절차는 실험 기록 참고")
    rows = []
    t0 = time.perf_counter()
    for s in inp["sections"]:
        w, h = s["width"], s["height"]
        for b in s["blocks"]:
            blk = api_block(b)
            blk["style"]["fontColor"] = "#000000"
            try:
                r = be.render(w, h, WHITE_1PX, [blk], shots=True)
            except RuntimeError as e:
                rows.append({"key": f"{s['key']}/b{b['blockId']}", "ok": False, "reason": str(e)[:200]})
                continue
            m = r["measures"][b["blockId"]]
            if not m.get("ok") or not m.get("ink"):
                rows.append({"key": f"{s['key']}/b{b['blockId']}", "ok": False})
                continue
            px = pixel_bounds(r["full"], MARGIN, MARGIN)
            ink = m["ink"]
            # 바깥쪽(+) = 실제 픽셀이 엔진 글자 경계보다 바깥으로 나간 양
            out_d = None if px is None else {"left": ink["left"] - px["left"], "right": px["right"] - ink["right"],
                                             "top": ink["top"] - px["top"], "bottom": px["bottom"] - ink["bottom"]}
            rows.append({"key": f"{s['key']}/b{b['blockId']}", "ok": True, "ink": ink, "pixel": px, "outward": out_d})
    secs = time.perf_counter() - t0
    vals = collections.defaultdict(list)
    for r in rows:
        if r.get("outward"):
            for k, v in r["outward"].items():
                vals[k].append(v)
    allv = [v for vs in vals.values() for v in vs]

    def q(v):
        v = sorted(v)
        return {p: round(v[min(len(v) - 1, int(p / 100 * (len(v) - 1)))], 3) for p in (0, 50, 90, 99, 100)} if v else None
    summary = {"blocks": len(rows), "measured": sum(1 for r in rows if r.get("outward")), "perSide": {k: q(v) for k, v in vals.items()},
               "all": q(allv), "maxOutwardPx": round(max(allv), 3) if allv else None, "seconds": round(secs, 1),
               "pixelInkLevel": PIXEL_INK_LEVEL}
    (out / "probe.json").write_text(json.dumps({"tool": TOOL_VERSION, "env": be.env(), "summary": summary, "rows": rows},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    be.close()


# ───────────────────────── run: 조정 + 판정 + 그림 ─────────────────────────

def run(args) -> None:
    inp = load_input(args.input)
    out = args.out
    (out / "sections").mkdir(parents=True, exist_ok=True)
    be = open_backend(args)
    measure = make_measurer(be)
    obstacles = static_obstacles(inp)
    gap = args.gap
    t0 = time.perf_counter()
    sections_out = []
    judged = {"before": {}, "after": {}}
    for s in inp["sections"]:
        sec = {"key": s["key"], "width": s["width"], "height": s["height"]}
        blocks = [api_block(b) for b in s["blocks"]]
        r0 = be.renders
        ts = time.perf_counter()
        res = adj.adjust_section(sec, blocks, measure, obstacles=obstacles.get(s["key"], []), gap=gap, floor=args.floor)
        cost = {"renders": be.renders - r0, "seconds": round(time.perf_counter() - ts, 2)}
        # 판정 — 조정 없이 부르는 입구(judge_render)를 조정 전 · 후 렌더 입력에 똑같이 쓴다
        jb = bd.judge_render(sec, blocks, res["before"])
        ja = bd.judge_render(sec, res["renderBlocks"], res["after"])
        judged["before"][s["key"]], judged["after"][s["key"]] = jb, ja
        imgs = {}
        name = s["key"].replace("/", "_")
        failed_shots = {}
        for tag, blks in (("before", blocks), ("after", res["renderBlocks"])):
            r = render_tolerant(be, s["width"], s["height"], s["_bg"], blks, shots=True)
            if r.get("failedBlocks"):
                failed_shots[tag] = r["failedBlocks"]
            for kind in ("clip", "full"):
                if kind in r:
                    p = out / "sections" / f"{name}.{tag}.{kind}.png"
                    p.write_bytes(r[kind])
                    imgs[f"{tag}.{kind}"] = p.relative_to(out).as_posix()
        sections_out.append({"key": s["key"], "group": s["group"], "id": s["id"], "width": s["width"], "height": s["height"],
                             "margin": MARGIN if any(k.endswith(".full") for k in imgs) else 0, "images": imgs, "cost": cost,
                             "renderFailedBlocks": failed_shots,
                             "blocks": [{"blockId": b["blockId"], "role": b["role"], "text": b["text"], "box": b["box"],
                                         "style": b["style"], "srcLines": b["aux"].get("srcLines")} for b in s["blocks"]],
                             "obstacles": obstacles.get(s["key"], []), "adjust": {k: v for k, v in res.items() if k != "renderBlocks"},
                             "renderBlocks": res["renderBlocks"], "judge": {"before": jb, "after": ja}})
        print(f"{s['key']}: {collections.Counter(r['status'] for r in res['blocks'].values())} {cost}", flush=True)
    # 최종 출력(원본 이미지별) 경계
    by_id = {s["id"]: s for s in inp["sections"]}
    finals = {}
    for o in inp["outputs"]:
        for tag in ("before", "after"):
            secs = [{"id": x["id"], "order": x["sectionOrder"], "height": x["height"], "width": o["width"], "include": x["bucket"] == "include",
                     "blocks": judged[tag].get(by_id[x["id"]]["key"], {}) if x["id"] in by_id else {}} for x in o["sections"]]
            finals.setdefault(o["key"], {})[tag] = bd.judge_final(secs, width=o["width"])
    total = time.perf_counter() - t0
    C = collections.Counter
    status = C(r["status"] for s in sections_out for r in s["adjust"]["blocks"].values())
    jstat = {t: dict(C(j["status"] for jj in judged[t].values() for j in jj.values())) for t in ("before", "after")}
    meta = {"tool": TOOL_VERSION, "env": be.env(), "rules": {"adjust": adj.RULE_VERSION, "boundary": bd.RULE_VERSION},
            "params": {"gapPx": gap, "floor": args.floor, "widthPrecision": adj.WIDTH_PRECISION, "scalePrecision": adj.SCALE_PRECISION},
            "input": str(args.input), "inputSha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
            "seconds": round(total, 1), "renders": be.renders, "adjustStatus": dict(status), "judgeStatus": jstat}
    (out / "result.json").write_text(json.dumps({"meta": meta, "sections": sections_out, "finals": finals}, ensure_ascii=False),
                                     encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    be.close()


# ───────────────────────── audit: 실패 · 잘림 1차 검수(에이전트) ─────────────────────────

def _ink_pixels(be, width, height, blk) -> tuple[Any, dict[str, float] | None]:
    """블록 하나를 검정 · 흰 바탕으로 섹션 밖까지 그려 글자 픽셀 마스크(전체 그림 좌표)와 바깥 경계(섹션 좌표)."""
    from PIL import Image
    b = dict(blk)
    b["style"] = {**b["style"], "fontColor": "#000000"}
    r = be.render(width, height, WHITE_1PX, [b], shots=True)
    mask = Image.open(io.BytesIO(r["full"])).convert("L").point(lambda v: 255 if v < PIXEL_INK_LEVEL else 0)
    return mask, pixel_bounds(r["full"], MARGIN, MARGIN)


def _pixels_outside(mask, x0: float, y0: float, x1: float, y1: float) -> int:
    """마스크에서 사각형(섹션 좌표) 밖에 있는 글자 픽셀 수."""
    from PIL import ImageDraw
    m = mask.copy()
    ImageDraw.Draw(m).rectangle([round(x0 + MARGIN), round(y0 + MARGIN), round(x1 + MARGIN) - 1, round(y1 + MARGIN) - 1], fill=0)
    return m.histogram()[255]


def audit(args) -> None:
    """끝까지 실패한 블록과 섹션 밖 잘림 블록을 에이전트가 1차로 가린다(사람 검수 대상만 남기기 위해).
    실패: 남은 넘침이 실제 픽셀로 보이는가 · 높이 확장이면 풀리는가(아래 여백) · 몇 배율까지 줄이면 풀리는가.
    잘림: 조정 후 출력에서 섹션 밖에 글자 픽셀이 실제로 있는가(잘려 사라지는 픽셀 수 · 거리)."""
    inp = load_input(args.input)
    R = json.loads((args.out / "result.json").read_text(encoding="utf-8"))
    be = open_backend(args)
    if not isinstance(be, LocalFe):
        raise SystemExit("audit 는 섹션 밖 촬영이 필요해 local 에서만 돈다")
    measure = make_measurer(be)
    api_by = {s["key"]: {b["blockId"]: api_block(b) for b in s["blocks"]} for s in inp["sections"]}
    rows = []
    for s in R["sections"]:
        W, H = s["width"], s["height"]
        sec = {"key": s["key"], "width": W, "height": H}
        rb = {b["blockId"]: b for b in s["renderBlocks"]}
        after = {int(k): v for k, v in s["adjust"]["after"].items()}
        occ = {bid: geo.occupied(bid, rb[bid]["box"], (after[bid] or {}).get("ink") if after[bid].get("ok") else None) for bid in rb}
        for k, row in s["adjust"]["blocks"].items():
            bid = int(k)
            key = f"{s['key']}/b{bid}"
            jb, ja = s["judge"]["before"][k], s["judge"]["after"][k]
            base = api_by[s["key"]][bid]
            if row["status"] == "unresolved":
                mask, px = _ink_pixels(be, W, H, base)
                box = base["box"]
                vis = geo.excess(px, box) if px else None
                att = row.get("attempt") or {"box": box, "fontScale": 1.0, "lineBreakApplied": False}
                others = [r for b2, rs in occ.items() if b2 != bid for r in rs] + [dict(o) for o in s["obstacles"]]
                ignore = {bid} | {o["blockId"] for o in others if geo.intersects(o, box)}
                lim = geo.free_limits(att["box"], others, W, H, args.gap, ignore)
                height = []
                for sc in (1.0, args.floor):
                    st = {"box": att["box"], "fontScale": sc, "lineBreakApplied": att["lineBreakApplied"]}
                    m = geo.measure_blocks(measure, sec, [adj.apply_system(base, st)])[bid]
                    if not m.ok:
                        continue
                    h_ok = not any(m.overflow[x] for x in ("left", "right", "top"))
                    need = max(att["box"]["y"] + att["box"]["h"], m.ink["bottom"]) if m.ink else None
                    height.append({"scale": sc, "horizontalFit": h_ok, "needBottom": need, "limitBottom": lim["bottom"],
                                   "extraPx": None if need is None else round(need - (att["box"]["y"] + att["box"]["h"]), 2),
                                   "fits": bool(h_ok and need is not None and need <= lim["bottom"]), "limitedBy": lim["limitedBy"]["bottom"]})
                need_scale = None
                lo, hi = 0.30, args.floor
                st0 = {"box": att["box"], "lineBreakApplied": att["lineBreakApplied"]}
                m_lo = geo.measure_blocks(measure, sec, [adj.apply_system(base, {**st0, "fontScale": lo})])[bid]
                if m_lo.ok and not m_lo.over:
                    while hi - lo > 0.005:
                        mid = round((lo + hi) / 2, 4)
                        m = geo.measure_blocks(measure, sec, [adj.apply_system(base, {**st0, "fontScale": mid})])[bid]
                        lo, hi = (mid, hi) if m.ok and not m.over else (lo, mid)
                    need_scale = lo
                rows.append({"kind": "unresolved", "key": key, "visibleExcessPx": vis, "engineExcessPx": (row.get("remainingOverflow") or {}).get("excessPx"),
                             "causes": row.get("causes"), "height": height, "needScale": need_scale})
            if jb["status"] == "section_clip" or ja["status"] == "section_clip":
                mask, px = _ink_pixels(be, W, H, rb[bid])
                lost = _pixels_outside(mask, 0, 0, W, H)
                dist = geo.excess(px, geo.section_box(W, H)) if px else None
                rows.append({"kind": "clip", "key": key, "before": jb["status"], "after": ja["status"], "lostPixels": lost,
                             "pixelOutsidePx": dist, "engineClipPx": (ja.get("sectionClip") or {}).get("excessPx")})
        print(s["key"], flush=True)
    (args.out / "audit.json").write_text(json.dumps({"tool": TOOL_VERSION, "env": be.env(), "gapPx": args.gap, "rows": rows},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(collections.Counter(r["kind"] for r in rows), ensure_ascii=False))
    be.close()


EDGE_LEVEL, BUSY_RATIO = 32, 0.05  # 배경 경계 밀도 — PR #141 실험 분류용 후보값 그대로


def _busy(bg_png: bytes, rect: dict[str, float]) -> float | None:
    """바탕 그림에서 rect(섹션 좌표) 안 경계 픽셀 비율 — 사진 · 그래픽 · 무늬 위인지 어림한다(넓이 0이면 None)."""
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(bg_png)).convert("L")
    x0, y0 = max(0, int(rect["x"])), max(0, int(rect["y"]))
    x1, y1 = min(im.width, int(rect["x"] + rect["w"] + 0.999)), min(im.height, int(rect["y"] + rect["h"] + 0.999))
    if x1 <= x0 or y1 <= y0:
        return None
    e = im.crop((max(0, x0 - 1), max(0, y0 - 1), min(im.width, x1 + 1), min(im.height, y1 + 1))).filter(ImageFilter.FIND_EDGES)
    e = e.crop((1 if x0 > 0 else 0, 1 if y0 > 0 else 0, e.width - (1 if x1 < im.width else 0), e.height - (1 if y1 < im.height else 0)))
    hist = e.point(lambda v: 255 if v >= EDGE_LEVEL else 0).histogram()
    return hist[255] / max(1, sum(hist))


def followup(args) -> None:
    """끝까지 실패한 블록을 에이전트가 1차로 나눈다(audit 결과 사용). 사람 검수는 「높이를 더 쓰면 풀리지만 늘어나는 자리가 복잡한 바탕」만 남긴다.
    분류(우선순위 순):
      env_suspect   마지막 시도 상태의 글자 픽셀이 박스 밖에 없다 — 엔진 경계만 넘침(이 PC 정수화 의심, 서버 재측정 대상)
      edge_1_2px    남은 넘침 2px 이하 · 박스 밖 픽셀이 가장자리 몇 개 — 서버 재측정 대상
      height        마지막 시도(폭 확장 · 배율 1.0 또는 0.9)에서 아래로만 넘치고, 아래로 늘린 영역이 다른 블록 자리 · 섹션 밖과 겹치지 않는다
                    → 늘어나는 자리 바탕이 단색이면 에이전트 판정 「괜찮음」, 복잡하면 사람 검수
      shrink_more   −10%로 부족하지만 더 줄이면 풀린다(필요 배율 기록)
      text_change   0.30배까지 줄여도 안 풀린다 — 문구를 고쳐야 한다"""
    inp = load_input(args.input)
    R = json.loads((args.out / "result.json").read_text(encoding="utf-8"))
    au = {r["key"]: r for r in json.loads((args.out / "audit.json").read_text(encoding="utf-8"))["rows"] if r["kind"] == "unresolved"}
    be = open_backend(args)
    if not isinstance(be, LocalFe):
        raise SystemExit("followup 은 섹션 밖 촬영이 필요해 local 에서만 돈다")
    api_by = {s["key"]: {b["blockId"]: api_block(b) for b in s["blocks"]} for s in inp["sections"]}
    bg_by = {s["key"]: s["_bg"] for s in inp["sections"]}
    (args.out / "followup").mkdir(exist_ok=True)
    rows = []
    for s in R["sections"]:
        W, H = s["width"], s["height"]
        rb = {b["blockId"]: b for b in s["renderBlocks"]}
        after = {int(k): v for k, v in s["adjust"]["after"].items()}
        occ = {b: geo.occupied(b, rb[b]["box"], after[b].get("ink") if after[b].get("ok") else None) for b in rb}
        for k, row in s["adjust"]["blocks"].items():
            if row["status"] != "unresolved":
                continue
            bid, key = int(k), f"{s['key']}/b{k}"
            a, att = au[key], row["attempt"]
            base = api_by[s["key"]][bid]
            out: dict[str, Any] = {"key": key, "attempt": att, "needScale": a["needScale"], "causes": row.get("causes")}
            eng = (row.get("remainingOverflow") or {}).get("excessPx") or {}
            if eng.get("max", 99) <= 2:
                mask, px = _ink_pixels(be, W, H, adj.apply_system(base, att))
                b = att["box"]
                lost = _pixels_outside(mask, b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"])
                out.update(cls="env_suspect" if lost == 0 else "edge_1_2px", attemptPixelsOutsideBox=lost)
            elif a["visibleExcessPx"] and a["visibleExcessPx"]["max"] == 0:
                out.update(cls="env_suspect", attemptPixelsOutsideBox=0)
            else:
                others = [o for b2, rs in occ.items() if b2 != bid for o in rs] + [dict(o) for o in s["obstacles"]]
                for h in a["height"]:
                    if not h["fits"]:
                        continue
                    ext = {**att["box"], "h": h["needBottom"] - att["box"]["y"]}
                    if any(geo.intersects(ext, o) for o in others) or ext["y"] + ext["h"] > H:
                        continue
                    st = {**att, "fontScale": h["scale"], "box": ext}
                    blks = [adj.apply_system(base, st) if x["blockId"] == bid else x for x in s["renderBlocks"]]
                    r = render_tolerant(be, W, H, bg_by[s["key"]], blks, shots=True)
                    name = key.replace("/", "_")
                    for kind in ("clip", "full"):
                        (args.out / "followup" / f"{name}.height.{kind}.png").write_bytes(r[kind])
                    spill = {**att["box"], "y": att["box"]["y"] + att["box"]["h"], "h": ext["h"] - att["box"]["h"]}
                    busy = _busy(bg_by[s["key"]], spill)
                    out.update(cls="height", height={"scale": h["scale"], "box": ext, "extraPx": round(ext["h"] - att["box"]["h"], 2),
                                                     "spill": spill, "busyRatio": busy,
                                                     "agent": "ok_solid" if busy is not None and busy < BUSY_RATIO else "human"},
                               images={kind: f"followup/{name}.height.{kind}.png" for kind in ("clip", "full")})
                    break
                else:
                    out["cls"] = "shrink_more" if a["needScale"] is not None else "text_change"
            rows.append(out)
    summary = dict(collections.Counter(r["cls"] for r in rows))
    summary["height_human"] = sum(1 for r in rows if r["cls"] == "height" and r["height"]["agent"] == "human")
    (args.out / "followup.json").write_text(json.dumps({"tool": TOOL_VERSION, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    be.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("probe", "run", "audit", "followup"):
        a = sub.add_parser(name)
        a.add_argument("--input", type=Path, required=True)
        a.add_argument("--out", type=Path, required=True)
        a.add_argument("--backend", choices=("local", "server"), default="local")
        a.add_argument("--fe-origin", default="http://127.0.0.1:3217")
        a.add_argument("--fe-revision", default=os.environ.get("NEXT_PUBLIC_PIXLATE_FE_REVISION", ""))
        if name in ("run", "audit", "followup"):
            a.add_argument("--gap", type=float, default=adj.GAP_PX)
            a.add_argument("--floor", type=float, default=adj.FLOOR)
    args = ap.parse_args()
    args.input, args.out = args.input.resolve(), args.out.resolve()
    {"probe": probe, "run": run, "audit": audit, "followup": followup}[args.cmd](args)


if __name__ == "__main__":
    main()
