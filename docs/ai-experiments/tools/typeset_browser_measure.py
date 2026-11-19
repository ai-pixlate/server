"""⑨ 조판 — 브라우저 넘침 측정 실험 도구(2026-11-16). 개발용 로컬 실험이며 운영 경로가 아니다. 운영 코드 · config는 바꾸지 않는다.

    # 1) 입력 만들기 — 베이스라인 보존본 → 섹션별 렌더 입력(/preview renderInput 모양) JSON
    python docs/ai-experiments/tools/typeset_browser_measure.py build-input \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 --out <폴더>/input.json
    # 2) 측정 — FE 공통 조판 모듈을 Playwright Chromium에서 실행
    python docs/ai-experiments/tools/typeset_browser_measure.py measure --input <폴더>/input.json --fe <FE 체크아웃> --out <폴더>/before
    # 3) (조정 실험) 같은 입력에서 블록 값만 바꾼 변형 입력 → 측정 → 전후 비교
    python docs/ai-experiments/tools/typeset_browser_measure.py derive --input <폴더>/input.json --out <폴더>/after.json \
        --set GS-01/s3/b40 fontScale=0.9 --set GS-01/s3/b41 box.h=120
    python docs/ai-experiments/tools/typeset_browser_measure.py compare --before <폴더>/before --after <폴더>/after-run --out <폴더>/compare
    # 합의 사례 자체 점검(경계와 같음 · +0.115px · 한 줄 줄 상자 · 긴 단어 · 하강부 · 글리프 대체 등)
    python docs/ai-experiments/tools/typeset_browser_measure.py selfcheck --fe <FE 체크아웃> --out <폴더>/selfcheck

무엇을 재사용하나(재구현하지 않는다):
- 렌더 입력: `/preview` `renderInput` 구성과 같은 함수 — 적용 박스 `app.layout_adjust`(보존본 해석은 `typeset_overflow_probe.applied_layout`),
  style 8키 `pipeline.handoff.style_defaults.resolve_block` + 행간 비율(`app/flows/preview_view.section_input`과 같은 대응),
  배경 = ⑥ 완료면 인페인팅 이미지 · 아니면 원본(`downstream._render_inputs`와 같은 규칙), 섹션 박스 검사(`preview_view._box_ok`).
- 표시 · 측정 · 판정: FE 공통 모듈 원본(`lib/n5/typeset-rules.ts` · `typeset-measurement.ts` · `typeset-final-output.ts` · `layout-edit.ts`)을
  타입만 지워(`typeset_browser_measure/strip_fe_modules.mjs`) 그대로 부른다 — `typesetBlockStyle`(표시 CSS) · `openTypesetMeasurer`
  (DOM 줄바꿈 · DOM baseline · Canvas TextMetrics ink · 경계와 같으면 정상 · 허용 오차 없음, 조판 D-01 · D-02) · `evaluateTypesetBlock`(Q-07).
- 폰트: 승인 설정 자료의 Pretendard face 파일(`sd.font_path`, SHA-256 검증 `t.load_role_defaults`) — 서버 Pillow와 같은 파일.

무엇이 서비스와 다른가(이 결과를 서비스와 같은 결과라고 보지 않는다):
- FE 앱 셸(Next · React · 전역 CSS)이 아니라 최소 페이지(`typeset_browser_measure/page.html`)에서 모듈을 부른다. FE 측정기는 블록 CSS의
  줄바꿈 · 글꼴 속성을 모두 명시하지만 앱 전역 CSS와의 차이를 확인한 것은 아니다.
- FE는 Pretendard v1.3.9 웹폰트(CDN static CSS)를, 이 도구는 같은 릴리스의 OTF를 쓴다(파일 동일성 미확인).
- 서버 Playwright 공통 렌더는 아직 없다(develop 서버 렌더는 Pillow `pillow-typeset/3`). 이 도구의 섹션 합성 이미지는 확인 자료이며
  서버 최종 촬영이 아니다. 렌더 입력은 보존본(v1.0.1 DB 행)에 현행 develop의 구성 함수를 적용한 것이다.

판정과 측정 오류 구분(블록당 judgement 1개 — 판정은 FE 엔진 결과이고, 픽셀은 엔진 측정의 교차 확인이다):
- 픽셀 대조: 측정한 블록 요소를 그대로 두고 글자색만 검정으로 바꿔 흰 바탕에서 DPR(기본 2)로 촬영, min(R,G,B) < 230인 픽셀의 경계를
  섹션 로컬 px로 환산한다(FE e2e `n5-typeset-measurement.spec.ts`와 같은 방법). TOL = 0.5 CSS px(DPR 2의 디바이스 픽셀 1개)는
  촬영 양자화 · 안티앨리어싱 때문의 **검증 허용치**이며 넘침 판정 허용 오차가 아니다(판정은 허용 오차 없음).
- fit                  : 엔진 넘침 아님, 픽셀도 영역 밖으로 TOL 넘게 나가지 않음
- overflow_confirmed   : 엔진 넘침(최대 초과 > TOL)이고 엔진이 넘친 방향에서 픽셀도 TOL 넘게 영역 밖 — 실제 넘침
- overflow_subpixel    : 엔진 넘침이나 최대 초과 ≤ TOL — 합의상 넘침(D-02)이며 픽셀로는 구분할 수 없는 범위(측정 오류로 보지 않는다)
- overflow_quantized   : 엔진 넘침(TOL < 최대 초과 ≤ QUANT_ALLOW)이고 픽셀은 그 방향에서 영역 안이며, 이 브라우저의 Canvas TextMetrics
                         글리프 경계가 정수 px로 떨어지는 환경(meta.metricsProbe.quantized) — 측정 한계로 생겼을 수 있는 넘침.
                         판정은 합의대로 넘침으로 둔다(픽셀은 정답이 아니다). QUANT_ALLOW는 실험 분류용 값(자체 점검에서 관측한
                         엔진 − 픽셀 최대 차 1.5px + TOL)이며 판정 규칙이 아니다
- overflow_pixel_inside: 엔진은 넘침인데 픽셀은 그 방향에서 영역 안이고 위 정수화 범위로 설명되지 않음 — 측정 오류 후보(사람 확인 필요)
- fit_pixel_outside    : 엔진은 정상인데 픽셀은 TOL 넘게 영역 밖 — 측정 오류 후보(사람 확인 필요)
- measure_failed       : FE 측정기가 측정을 거절(글리프 대체 · baseline 탐침 변화 등) — 넘침이 아니다. 사유를 남긴다
보조 표시(flags): ink_mismatch(엔진 ink와 픽셀 경계 차 > TOL) · dpr_sensitive(보조 DPR 측정과 넘침 여부가 다름) ·
line_box_only(한 줄 · 엔진 정상인데 DOM 줄 상자는 영역 밖 — 줄 상자 기준이면 오판이 됐을 블록) ·
section_clip(ink가 섹션 사각형 밖 — 섹션 PNG crop 손실 후보, 6.6) · final_image(ink가 최종 출력 이미지 밖 — Q-07, 6.6).
블록 넘침 · 섹션 crop · 최종 이미지 경계는 서로 독립이며 합치지 않는다.

출력(`--out`, git 제외 폴더 권장): measure.json(조건 · 블록별 값) · summary.json · review/index.html(블록별 그림 · 값) ·
sections/*.clean.png(섹션 밖을 자른 합성) · sections/*.annotated.png(섹션 밖까지, 빨강 = 배치 영역 · 굵은 빨강 = 넘침 블록 ·
파랑 = 측정 ink · 초록 점선 = DOM 줄 조각 · 회색 점선 = 섹션 경계) · blocks/*.pixel.png(픽셀 대조: 빨강 영역 · 파랑 엔진 ink · 초록 픽셀 경계).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import html
import importlib.util
import io
import json
import math
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
ASSETS = TOOLS / "typeset_browser_measure"
sys.path.insert(0, str(ROOT))

from app import typeset as t  # noqa: E402
from pipeline.handoff import style_defaults as sd  # noqa: E402

TOOL_VERSION = "typeset-browser-measure/1"
INPUT_SCHEMA = "typeset-measure-input/1"
ORIGIN = "http://tbm.local"
SIDES = ("left", "right", "top", "bottom")
TOL = 0.5  # 검증 허용치(CSS px) — 판정 허용 오차가 아니다(머리말)
QUANT_ALLOW = 2.0  # 정수화 환경의 분류용 값(머리말 overflow_quantized) — 판정 규칙이 아니다
PIXEL_DARK = 230  # 흰 바탕 검정 글자에서 글자 픽셀로 보는 기준(FE e2e와 같음)
PROBE_PAD = 8
GROUPS = ("GS-01", "GS-02")


# ───────────────────────── 공통 ─────────────────────────

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canon(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def input_sha256(inp: dict[str, Any]) -> str:
    """측정 입력의 지문(measure.json meta.inputSha256) — 실행 중에 붙는 내부 필드(_*, 섹션 _bg)는 뺀다."""
    return sha256_bytes(canon({k: v for k, v in inp.items() if not k.startswith("_")}
                              | {"sections": [{k: v for k, v in s.items() if k != "_bg"} for s in inp["sections"]]}).encode())


def block_input_sha(b: dict[str, Any]) -> str:
    """측정 결과에 영향을 주는 블록 입력(문구 · 적용 박스 · style 8키 · 역할)의 지문. 전후 비교에서 무엇이 바뀌었는지 가린다."""
    return sha256_bytes(canon({k: b[k] for k in ("role", "text", "box", "style")}).encode())


def git_info(path: Path) -> dict[str, Any]:
    def run(*a: str) -> str:
        return subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    try:
        return {"commit": run("rev-parse", "HEAD"), "describe": run("log", "-1", "--format=%cd %s", "--date=iso"),
                "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"commit": None}


def load_probe():
    spec = importlib.util.spec_from_file_location("typeset_overflow_probe", TOOLS / "typeset_overflow_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def box_ok(box: dict[str, Any], width: Any, height: Any) -> bool:
    """`app/flows/preview_view._box_ok`과 같은 조건(그 모듈은 DB 의존성을 끌어와 옮겨 적었다) — 섹션 안의 유한한 양의 크기인가.
    실패면 /preview는 RENDER_INPUT_INVALID."""
    def fin(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if not all(fin(box.get(k)) for k in ("x", "y", "w", "h")) or not (fin(width) and fin(height)):
        return False
    x, y, w, h = (box[k] for k in ("x", "y", "w", "h"))
    return w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= width and y + h <= height


# ───────────────────────── build-input ─────────────────────────

def build_input(run: Path, groups: tuple[str, ...]) -> dict[str, Any]:
    run = run.resolve()
    probe = load_probe()
    defaults = t.load_role_defaults()
    data = defaults.data
    lh = data.data.pipeline_mapping.line_height_ratio
    idx = json.loads((run / "object-index.json").read_text(encoding="utf-8"))
    sections, outputs, skipped, errors = [], [], collections.Counter(), []
    for g in groups:
        d = json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))
        cur = {(a["stage"], a["unit_id"]): a["status"] for a in d["job_async_task"] if a["is_current"]}
        stack = {r["section_id"]: r["stack_offset"] for r in d["deliverable_section"]}
        secs = sorted(d["section"], key=lambda s: (s["source_image_id"], s["section_order"]))
        for src in d["source_image"]:
            ss = [s for s in secs if s["source_image_id"] == src["id"]]
            outputs.append({"key": f"{g}/img{src['id']}", "group": g, "sourceImageId": src["id"], "width": src["width"],
                            "sections": [{"id": s["id"], "sectionOrder": s["section_order"], "height": s["height"],
                                          "bucket": s["bucket"]} for s in ss],
                            "recordedStackOffset": {str(s["id"]): stack.get(s["id"]) for s in ss}})
        by_sec = collections.defaultdict(list)
        for b in d["text_block"]:
            by_sec[b["section_id"]].append(b)
        for s in secs:
            sid, key = s["id"], f"{g}/s{s['id']}"
            if s["bucket"] != "include":
                skipped["section_not_include"] += 1
                continue
            stage = cur.get(("style", sid))
            if stage not in ("done", "failed"):
                skipped[f"section_style_stage_{stage}"] += 1
                continue
            width, height = s["bbox"]["w"], s["height"]
            bg_key = s["inpaint_image_url"] if cur.get(("inpaint", sid)) == "done" and s["inpaint_image_url"] else s["image_key"]
            obj = idx[bg_key]
            bg_path = run / "objects" / obj["sha256"]
            blocks, bad_box, bad_style, rblocks = [], [], [], []
            for b in sorted(by_sec[sid], key=lambda r: r["block_order"]):
                if b["is_excluded"] is not False or not (b["source_ko"] or "").strip() or not (b["trans_1"] or "").strip():
                    skipped["block_not_typeset"] += 1
                    continue
                box, scale = probe.applied_layout(b)
                if not box_ok(box, width, height):
                    bad_box.append(b["id"])
                    continue
                try:
                    res = sd.resolve_block(sd.BlockInput(id=b["id"], role=b["role"], is_excluded=b["is_excluded"],
                                                         source_ko=b["source_ko"], text=b["trans_1"], style=b["style"],
                                                         style_stage=stage), width, data)
                except sd.StyleInputInvalid:
                    bad_style.append(b["id"])
                    continue
                a = res.applied
                blocks.append({
                    "blockId": b["id"], "revision": b["revision"], "role": b["role"], "text": b["trans_1"], "box": dict(box),
                    "style": {"fontFamily": a["font_family"], "fontWeight": a["font_weight"], "fontSizePx": a["font_size_px"],
                              "fontScale": scale, "fontColor": a["font_color"], "align": a["align"],
                              "letterSpacingEm": a["letter_spacing_em"], "lineHeightRatio": lh},
                    "aux": {"sourceKo": b["source_ko"], "srcLines": len(b["source_lines"] or []),
                            "recordedOverflow": b["overflow"], "styleSource": res.source},
                })
                rblocks.append(t.RenderBlock(id=b["id"], role=b["role"], text=b["trans_1"], box=dict(box), style=b["style"],
                                             style_stage=stage, source_ko=b["source_ko"], font_scale=scale))
            if bad_box or bad_style:
                code = "RENDER_INPUT_INVALID" if bad_box else "STYLE_RESULT_INVALID"
                errors.append({"section": key, "code": code, "blockIds": sorted(bad_box or bad_style)})
                continue
            if not blocks:
                skipped["section_no_typeset_block"] += 1
                continue
            # 참고값: 같은 행을 현행 develop 서버 Pillow 렌더(판정 = 글리프 잉크, PR #138)로 그린 넘침. 브라우저 판정의 정답이 아니다
            pil = t.render(bg_path.read_bytes(), rblocks, defaults)
            for blk in blocks:
                br = pil.blocks.get(blk["blockId"])
                blk["aux"]["pillowOverflow"] = br.overflow if br else None
                blk["aux"]["pillowLines"] = br.lines if br else None
            sections.append({"key": key, "group": g, "id": sid, "sourceImageId": s["source_image_id"],
                             "sectionOrder": s["section_order"], "width": width, "height": height,
                             "background": {"path": bg_path.relative_to(ROOT).as_posix(), "sha256": obj["sha256"],
                                            "source": "inpaint" if bg_key != s["image_key"] else "original", "key": bg_key},
                             "blocks": blocks})
    return {"schema": INPUT_SCHEMA, "variant": "baseline",
            "origin": {"run": run.resolve().relative_to(ROOT).as_posix(), "groups": list(groups), "server": git_info(ROOT),
                       "styleDefaults": {"version": defaults.version, "sha256": defaults.sha256},
                       "pillowRenderer": t.RENDERER_VERSION,
                       "note": "보존본(v1.0.1 DB 행)에 현행 develop의 렌더 입력 구성 함수를 적용했다. 운영 /preview 응답 원본이 아니다."},
            "skipped": dict(skipped), "inputErrors": errors, "outputs": outputs, "sections": sections}


# ───────────────────────── derive ─────────────────────────

def derive(inp: dict[str, Any], sets: list[tuple[str, str]], label: str) -> dict[str, Any]:
    """같은 입력에서 지정한 블록의 박스 · 배율 값만 바꾼 변형. 문구 · 역할 · 측정 style은 바꾸지 않는다(문구 축약 없음 — 6.4)."""
    out = json.loads(json.dumps(inp))
    out["variant"] = label
    out.setdefault("derivedFrom", []).append({"variant": inp.get("variant"), "sha256": sha256_bytes(canon(inp).encode()),
                                              "sets": [f"{k} {v}" for k, v in sets]})
    blocks = {f"{s['key']}/b{b['blockId']}": (s, b) for s in out["sections"] for b in s["blocks"]}
    for key, assign in sets:
        if key not in blocks:
            raise SystemExit(f"블록 {key}가 입력에 없다")
        s, b = blocks[key]
        field, _, raw = assign.partition("=")
        if field not in ("fontScale", "box.x", "box.y", "box.w", "box.h"):
            raise SystemExit(f"{key}: 바꿀 수 있는 값은 fontScale · box.x · box.y · box.w · box.h(문구 · 역할 · style은 바꾸지 않는다)")
        try:
            val = float(raw)
        except ValueError:
            raise SystemExit(f"{key} {assign}: 수가 아니다") from None
        if not math.isfinite(val):
            raise SystemExit(f"{key} {assign}: 유한한 수가 아니다")
        if field == "fontScale":
            if val <= 0:
                raise SystemExit(f"{key}: fontScale은 양수(6.5)")
            b["style"]["fontScale"] = val
        elif field in ("box.x", "box.y", "box.w", "box.h"):
            b["box"][field[4:]] = val
        else:
            raise SystemExit(f"{key}: 바꿀 수 있는 값은 fontScale · box.x · box.y · box.w · box.h")
        if not box_ok(b["box"], s["width"], s["height"]):
            raise SystemExit(f"{key}: 박스가 섹션 밖이거나 크기가 0 이하 — 렌더 입력 오류(RENDER_INPUT_INVALID)가 되는 값")
    return out


# ───────────────────────── 브라우저 ─────────────────────────

class Bundle:
    """페이지 · 실행기 · FE 모듈 · 폰트 · 배경을 가상 주소(ORIGIN)로 넘긴다 — 로컬 서버 프로세스 없이, 네트워크 없이."""

    def __init__(self, fe_root: Path, work: Path):
        fe_out = work / "_bundle" / "fe"
        r = subprocess.run(["node", str(ASSETS / "strip_fe_modules.mjs"), str(fe_root), str(fe_out)],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise SystemExit(f"FE 모듈 변환 실패: {r.stderr}")
        self.fe_manifest = json.loads((fe_out / "manifest.json").read_text(encoding="utf-8"))
        defaults = t.load_role_defaults()  # 폰트 SHA-256 검증 포함
        faces = defaults.data.data.fonts.faces
        self.files: dict[str, tuple[bytes, str]] = {
            "/index.html": ((ASSETS / "page.html").read_bytes(), "text/html; charset=utf-8"),
            "/runner.js": ((ASSETS / "runner.js").read_bytes(), "text/javascript"),
        }
        for p in fe_out.glob("*.js"):
            self.files[f"/fe/{p.name}"] = (p.read_bytes(), "text/javascript")
        self.fonts = {}
        for name, f in faces.items():
            path = sd.font_path(defaults.data, name)
            self.files[f"/fonts/{f.weight}.otf"] = (path.read_bytes(), "font/otf")
            self.fonts[str(f.weight)] = {"face": name, "file": path.relative_to(ROOT).as_posix(), "sha256": f.sha256,
                                         "postscript": f.postscript_name, "version": f.version}
        self.font_package = {"name": defaults.data.data.fonts.package.name, "release": defaults.data.data.fonts.package.release}
        self.fe_git = git_info(fe_root)
        self.assets = {n: sha256_bytes((ASSETS / n).read_bytes()) for n in ("page.html", "runner.js", "strip_fe_modules.mjs")}

    def add_background(self, path: Path) -> str:
        url = f"/bg/{sha256_bytes(path.read_bytes())}"
        if url not in self.files:
            ctype = "image/png" if path.read_bytes()[:4] == b"\x89PNG" else "image/jpeg"
            self.files[url] = (path.read_bytes(), ctype)
        return ORIGIN + url

    def route(self, route) -> None:
        path = route.request.url[len(ORIGIN):].split("?")[0]
        if path in self.files:
            body, ctype = self.files[path]
            route.fulfill(status=200, body=body, headers={"content-type": ctype, "cache-control": "no-store"})
        else:
            route.fulfill(status=404, body=b"")


def open_page(browser, bundle: Bundle, dpr: float, problems: list[str]):
    ctx = browser.new_context(device_scale_factor=dpr, viewport={"width": 1400, "height": 1000})
    page = ctx.new_page()
    page.on("console", lambda m: problems.append(f"[dpr {dpr}] console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"[dpr {dpr}] pageerror: {e}"))
    page.route(f"{ORIGIN}/**", bundle.route)
    page.goto(f"{ORIGIN}/index.html")
    page.wait_for_function("document.getElementById('status')?.dataset.ready === 'true'")
    opened = page.evaluate("window.__tbm.open()")
    if not opened.get("ok"):
        raise SystemExit(f"측정기를 열지 못했다(폰트 미준비면 대체 폰트로 측정하지 않는다): {opened}")
    return ctx, page, opened


def excess(ink: dict[str, float] | None, box: dict[str, float]) -> dict[str, float]:
    if not ink:
        return {**{s: 0.0 for s in SIDES}, "max": 0.0}
    e = {"left": box["x"] - ink["left"], "right": ink["right"] - (box["x"] + box["w"]),
         "top": box["y"] - ink["top"], "bottom": ink["bottom"] - (box["y"] + box["h"])}
    e = {k: max(0.0, v) for k, v in e.items()}
    e["max"] = max(e.values())
    return e


def union(*rs: dict[str, float] | None) -> dict[str, float]:
    rs = [r for r in rs if r]
    return {"left": min(r["left"] for r in rs), "top": min(r["top"] for r in rs),
            "right": max(r["right"] for r in rs), "bottom": max(r["bottom"] for r in rs)}


def box_rect(box: dict[str, float]) -> dict[str, float]:
    return {"left": box["x"], "top": box["y"], "right": box["x"] + box["w"], "bottom": box["y"] + box["h"]}


def pixel_probe(page, res: dict[str, Any], box: dict[str, float], dpr: float, out_png: Path | None) -> dict[str, Any] | None:
    """측정한 블록 요소를 옮겨 촬영하고 글자 픽셀 경계를 섹션 로컬 px로 돌려준다(엔진 측정의 교차 확인)."""
    u = union(box_rect(box), res["ink"], res["domBounds"])
    dx, dy = PROBE_PAD - math.floor(u["left"]), PROBE_PAD - math.floor(u["top"])
    w, h = math.ceil(u["right"]) + dx + PROBE_PAD, math.ceil(u["bottom"]) + dy + PROBE_PAD
    page.evaluate("([dx, dy, w, h]) => window.__tbm.placeForPixel(dx, dy, w, h)", [dx, dy, w, h])
    png = page.screenshot(clip={"x": 0, "y": 0, "width": w, "height": h}, full_page=True, scale="device", animations="disabled")
    im = Image.open(io.BytesIO(png)).convert("RGB")
    # min(R,G,B) < 230 ⇔ 세 채널 중 하나라도 < 230 — 채널별 마스크 경계의 합집합
    boxes = [im.getchannel(c).point(lambda v: 255 if v < PIXEL_DARK else 0).getbbox() for c in "RGB"]
    boxes = [x for x in boxes if x]
    bb = (min(x[0] for x in boxes), min(x[1] for x in boxes), max(x[2] for x in boxes), max(x[3] for x in boxes)) if boxes else None
    if not bb:
        return None
    ink = {"left": bb[0] / dpr - dx, "top": bb[1] / dpr - dy, "right": bb[2] / dpr - dx, "bottom": bb[3] / dpr - dy}
    out = {"ink": ink, "excess": excess(ink, box)}
    if res["ink"]:
        diff = {s: ink[s] - res["ink"][s] for s in SIDES}
        out["diffVsEngine"] = diff
        out["maxAbsDiff"] = max(abs(v) for v in diff.values())
    if out_png is not None:
        # 사람 검수용: 덧그림 없는 촬영 + 배치 영역을 바깥쪽 1 디바이스 픽셀에 빨간 선으로만(영역 안 픽셀을 가리지 않는다)
        rv = im.copy()
        outline_outside(rv, box, dx, dy, dpr)
        rv.save(out_png.with_name(out_png.name.replace(".pixel.png", ".review.png")))
        d = ImageDraw.Draw(im)

        def draw(r, color):
            d.rectangle([(r["left"] + dx) * dpr, (r["top"] + dy) * dpr, (r["right"] + dx) * dpr - 1, (r["bottom"] + dy) * dpr - 1],
                        outline=color, width=1)
        draw(box_rect(box), (230, 0, 0))
        if res["ink"]:
            draw(res["ink"], (0, 90, 255))
        draw(ink, (0, 170, 60))
        im.save(out_png)
    return out


def outline_outside(im: Image.Image, box: dict[str, float], dx: float, dy: float, scale: float) -> None:
    """배치 영역을 영역 바깥쪽 1px 선으로 그린다 — 소수 경계는 바깥으로 올려 선이 영역 안 픽셀을 덮지 않게 한다."""
    x0 = math.floor((box["x"] + dx) * scale) - 1
    y0 = math.floor((box["y"] + dy) * scale) - 1
    x1 = math.ceil((box["x"] + box["w"] + dx) * scale)
    y1 = math.ceil((box["y"] + box["h"] + dy) * scale)
    ImageDraw.Draw(im).rectangle([x0, y0, x1, y1], outline=(230, 0, 0), width=1)


def judge(primary: dict[str, Any], pixel: dict[str, Any] | None, quantized: bool) -> str:
    """머리말 「판정과 측정 오류 구분」. 판정(넘침 여부)은 엔진 결과 그대로이고, 이 값은 해석 분류다."""
    if not primary["ok"]:
        return "measure_failed"
    over = primary["overflow"]["any"]
    if pixel is None:
        return "overflow_unchecked" if over else "fit_unchecked"
    if not over:
        return "fit_pixel_outside" if pixel["excess"]["max"] > TOL else "fit"
    if primary["excess"]["max"] <= TOL:
        return "overflow_subpixel"
    sides = [s for s in SIDES if primary["overflow"][s]]
    if any(pixel["excess"][s] > TOL for s in sides):
        return "overflow_confirmed"
    if quantized and all(primary["excess"][s] <= QUANT_ALLOW for s in sides):
        return "overflow_quantized"
    return "overflow_pixel_inside"


def slim(res: dict[str, Any], box: dict[str, float]) -> dict[str, Any]:
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason"), "detail": res.get("detail"), "fallbackGraphemes": res.get("fallbackGraphemes")}
    return {"ok": True, "fontPx": res["fontPx"], "fontWeight": res["fontWeight"], "letterSpacingPx": res["letterSpacingPx"],
            "blockRect": res["blockRect"], "domBounds": res["domBounds"], "ink": res["ink"],
            "lines": [{"text": l["text"], "rect": l["rect"], "baselineY": l["baselineY"], "ink": l["ink"],
                       "canvasAdvanceWidth": l["canvasAdvanceWidth"]} for l in res["lines"]],
            "overflow": res["overflow"], "excess": excess(res["ink"], box)}


def measure_sections(inp: dict[str, Any], fe_root: Path, out: Path, dpr: float, check_dpr: float | None,
                     render: bool) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    (out / "blocks").mkdir(exist_ok=True)
    (out / "sections").mkdir(exist_ok=True)
    bundle = Bundle(fe_root, out)
    for s in inp["sections"]:
        s["_bg"] = bundle.add_background(ROOT / s["background"]["path"])
    problems: list[str] = []
    rows: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx, page, opened = open_page(browser, bundle, dpr, problems)
        meta_browser = {"name": "chromium", "version": browser.version, "userAgent": opened["userAgent"], "headless": True,
                        "playwright": _playwright_version()}
        metrics = page.evaluate("window.__tbm.probeMetrics()")
        quantized = metrics["quantized"]
        primary: dict[str, dict[str, Any]] = {}
        for s in inp["sections"]:
            for b in s["blocks"]:
                key = f"{s['key']}/b{b['blockId']}"
                res = page.evaluate("([w, b]) => window.__tbm.measure(w, b)", [s["width"], b])
                pr = slim(res, b["box"])
                px = None
                if pr["ok"] and pr["ink"]:
                    name = key.replace("/", "_")
                    px = pixel_probe(page, res, b["box"], dpr, out / "blocks" / f"{name}.pixel.png")
                primary[key] = res
                rows.append({"key": key, "group": s["group"], "sectionKey": s["key"], "sectionId": s["id"],
                             "sectionSize": {"width": s["width"], "height": s["height"]}, "blockId": b["blockId"],
                             "role": b["role"], "text": b["text"], "box": b["box"], "style": b["style"],
                             "inputSha256": block_input_sha(b), "aux": b.get("aux", {}),
                             "primary": {"dpr": dpr, **pr}, "pixel": px})
        # 최종 출력 이미지 경계(Q-07) — 블록 넘침과 별개. 측정에 성공한 블록만
        by_key = {r["key"]: r for r in rows}
        final_eval = {}
        for o in inp["outputs"]:
            items = [{"key": r["key"], "sectionId": r["sectionId"],
                      "measured": {"bbox": {"y": r["box"]["y"]}, "ink": r["primary"]["ink"], "overflow": r["primary"]["overflow"]}}
                     for r in rows if r["primary"]["ok"] and any(x["id"] == r["sectionId"] and r["group"] == o["group"]
                                                                  for x in o["sections"])]
            if not items:
                continue
            ev = page.evaluate("([o, items]) => window.__tbm.evaluateFinal(o, items)", [o, items])
            final_eval[o["key"]] = {"image": ev["image"], "offsets": ev["offsets"], "recordedStackOffset": o["recordedStackOffset"]}
            for k, e in ev["blocks"].items():
                by_key[k]["finalImage"] = {"output": o["key"], "violation": e["imageBoundaryViolation"],
                                           "finalBlockY": e["finalBlockY"], "inkInImage": e["inkInImage"]}
        ctx.close()

        check: dict[str, dict[str, Any]] = {}
        if check_dpr is not None:
            ctx2, page2, _ = open_page(browser, bundle, check_dpr, problems)
            for s in inp["sections"]:
                for b in s["blocks"]:
                    key = f"{s['key']}/b{b['blockId']}"
                    check[key] = page2.evaluate("([w, b]) => window.__tbm.measure(w, b)", [s["width"], b])
            if render:
                render_sections(page2, inp, check, out, check_dpr)
            ctx2.close()
        elif render:
            ctx3, page3, _ = open_page(browser, bundle, dpr, problems)
            render_sections(page3, inp, primary, out, dpr)
            ctx3.close()
        browser.close()

    for r in rows:
        c = check.get(r["key"])
        if c is not None:
            cs = slim(c, r["box"])
            r["check"] = {"dpr": check_dpr, "ok": cs["ok"], "overflow": cs.get("overflow"), "ink": cs.get("ink"),
                          "lines": len(cs["lines"]) if cs["ok"] else None, "reason": cs.get("reason")}
    classify(rows, quantized, out)

    meta = {"tool": TOOL_VERSION, "inputVariant": inp.get("variant"), "inputSha256": input_sha256(inp),
            "inputOrigin": inp.get("origin"), "derivedFrom": inp.get("derivedFrom"),
            "fe": {"git": bundle.fe_git, "modules": bundle.fe_manifest}, "toolAssets": bundle.assets,
            "fonts": {"package": bundle.font_package, "faces": bundle.fonts,
                      "note": "FE 화면은 같은 릴리스의 CDN 웹폰트(static CSS)를 쓴다 — 파일 동일성 미확인"},
            "browser": meta_browser, "platform": platform_info(), "metricsProbe": metrics, "quantAllowPx": QUANT_ALLOW, "dpr": dpr, "checkDpr": check_dpr, "renderDpr": check_dpr if check_dpr is not None else dpr,
            "viewport": {"width": 1400, "height": 1000}, "verifyTolerancePx": TOL, "pixelDarkThreshold": PIXEL_DARK,
            "server": git_info(ROOT), "consoleProblems": problems,
            "path": "minimal-page: FE 공통 조판 모듈(타입만 제거) + 최소 HTML. FE 앱 셸 · 서버 Playwright 공통 렌더가 아니다"}
    for s in inp["sections"]:
        s.pop("_bg", None)
    return {"meta": meta, "finalOutputs": final_eval, "blocks": rows}


def classify(rows: list[dict[str, Any]], quantized: bool, out: Path) -> None:
    """측정값(엔진 · 픽셀 · 보조 DPR)에서 judgement · flags를 다시 계산한다(브라우저 불필요 — `rejudge`도 이 함수를 쓴다)."""
    for r in rows:
        pr = r["primary"]
        px = r.get("pixel")
        flags = []
        if px and px.get("maxAbsDiff", 0) > TOL:
            flags.append("ink_mismatch")
        c = r.get("check")
        if c is not None:
            if c["ok"] and pr["ok"]:
                c["maxInkDiff"] = max(abs(c["ink"][k] - pr["ink"][k]) for k in SIDES) if c["ink"] and pr["ink"] else 0.0
                if c["overflow"]["any"] != pr["overflow"]["any"] or c["lines"] != len(pr["lines"]):
                    flags.append("dpr_sensitive")
            elif c["ok"] != pr["ok"]:
                flags.append("dpr_sensitive")
        if pr["ok"]:
            # 엔진이 넘쳤다고 본 방향 중 픽셀로는 영역 안인 방향 — 방향(축)별 상세를 쓸 때의 측정 오류
            if px:
                for side in SIDES:
                    if pr["overflow"][side] and px["excess"][side] <= TOL:
                        flags.append(f"side_pixel_inside:{side}")
            if len(pr["lines"]) == 1 and not pr["overflow"]["any"]:
                lr, bx = pr["lines"][0]["rect"], r["box"]
                if lr["top"] < bx["y"] or lr["bottom"] > bx["y"] + bx["h"]:
                    flags.append("line_box_only")
            ink, size = pr["ink"], r.get("sectionSize")
            if ink and size:
                clip = {"left": ink["left"] < 0, "top": ink["top"] < 0, "right": ink["right"] > size["width"],
                        "bottom": ink["bottom"] > size["height"]}
                clip["any"] = any(clip.values())
                r["sectionClip"] = clip
                if clip["any"]:
                    flags.append("section_clip")
            if (r.get("finalImage") or {}).get("violation", {}).get("any"):
                flags.append("final_image")
        r["judgement"] = judge(pr, px, quantized)
        r["flags"] = flags
        name = r["key"].replace("/", "_")
        r["images"] = {k: f"blocks/{name}.{k}.png" for k in ("pixel", "context", "review", "scene") if (out / "blocks" / f"{name}.{k}.png").exists()}


def platform_info() -> dict[str, str]:
    """측정을 실행한 OS · 아키텍처. 글꼴 래스터라이저 · TextMetrics 특성이 OS마다 다를 수 있어 비교 조건에 쓴다."""
    return {"system": platform.system(), "release": platform.release(), "version": platform.version(), "machine": platform.machine()}


def _playwright_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception:  # noqa: BLE001
        return None


def render_sections(page, inp: dict[str, Any], measured: dict[str, dict[str, Any]], out: Path, dpr: float) -> None:
    """섹션 합성 확인 자료 — clean(섹션 밖 잘림) · annotated(섹션 밖까지 + 덧그림) · 블록별 주변 crop."""
    for s in inp["sections"]:
        sec = {"width": s["width"], "height": s["height"], "backgroundUrl": s["_bg"], "blocks": s["blocks"]}
        mres = {str(b["blockId"]): measured.get(f"{s['key']}/b{b['blockId']}") for b in s["blocks"]}
        reach = 0.0
        for r in mres.values():
            if r and r.get("ok") and r.get("ink"):
                i = r["ink"]
                reach = max(reach, -i["left"], -i["top"], i["right"] - s["width"], i["bottom"] - s["height"])
        margin = int(min(600, max(0, math.ceil(reach))) + 24)
        name = s["key"].replace("/", "_")
        page.evaluate("([s, m]) => window.__tbm.renderSection(s, m, 'clean', 0)", [sec, mres])
        page.locator("#stage").screenshot(path=str(out / "sections" / f"{name}.clean.png"), scale="device", animations="disabled")
        page.evaluate("([s, m, g]) => window.__tbm.renderSection(s, m, 'plain', g)", [sec, mres, margin])
        plain_path = out / "sections" / f"{name}.plain.png"
        page.locator("#stage").screenshot(path=str(plain_path), scale="device", animations="disabled")
        page.evaluate("([s, m, g]) => window.__tbm.renderSection(s, m, 'annotated', g)", [sec, mres, margin])
        ann_path = out / "sections" / f"{name}.annotated.png"
        page.locator("#stage").screenshot(path=str(ann_path), scale="device", animations="disabled")
        page.evaluate("window.__tbm.hideStage()")
        ann = Image.open(ann_path).convert("RGB")
        plain = Image.open(plain_path).convert("RGB")
        for b in s["blocks"]:
            r = mres[str(b["blockId"])]
            bx = box_rect(b["box"])
            u = union(bx, r["ink"] if r and r.get("ok") else None)
            pad = max(16.0, b["box"]["h"] * 0.5)
            c = [(u["left"] - pad + margin) * dpr, (u["top"] - pad + margin) * dpr, (u["right"] + pad + margin) * dpr,
                 (u["bottom"] + pad + margin) * dpr]
            c = [max(0, int(c[0])), max(0, int(c[1])), min(ann.width, math.ceil(c[2])), min(ann.height, math.ceil(c[3]))]
            crop = ann.crop(c)
            crop.thumbnail((900, 600))
            crop.save(out / "blocks" / f"{s['key'].replace('/', '_')}_b{b['blockId']}.context.png")
            # 사람 검수용 장면: 실제 배경 · 글자색 그대로(덧그림 없음) + 배치 영역만 바깥쪽 빨간 선. 축소하지 않는다
            scene = plain.crop(c)
            outline_outside(scene, b["box"], margin - c[0] / dpr, margin - c[1] / dpr, dpr)
            scene.save(out / "blocks" / f"{s['key'].replace('/', '_')}_b{b['blockId']}.scene.png")


# ───────────────────────── 집계 · 보고 ─────────────────────────

def summarize(m: dict[str, Any]) -> dict[str, Any]:
    C = collections.Counter
    rows = m["blocks"]
    ok = [r for r in rows if r["primary"]["ok"]]
    one = [r for r in ok if len(r["primary"]["lines"]) == 1]
    flags = C(f for r in rows for f in r["flags"])
    s = {"blocks": len(rows), "judgement": dict(C(r["judgement"] for r in rows)), "flags": dict(flags),
         "measureFailed": dict(C(r["primary"].get("reason") for r in rows if not r["primary"]["ok"])),
         "byLines": {"1": dict(C(r["judgement"] for r in one)), "2+": dict(C(r["judgement"] for r in ok if len(r["primary"]["lines"]) >= 2))},
         "overflowSides": dict(C(sd_ for r in ok for sd_ in SIDES if r["primary"]["overflow"][sd_])),
         # 방향별: 엔진이 넘쳤다고 본 방향 중 픽셀로도 TOL 넘게 밖인 방향(confirmed) · 픽셀로는 안인 방향(pixelInside)
         "overflowSidesPixel": {sd_: {"confirmed": sum(r["primary"]["overflow"][sd_] and r["pixel"]["excess"][sd_] > TOL for r in ok if r.get("pixel")),
                                      "pixelInside": sum(r["primary"]["overflow"][sd_] and r["pixel"]["excess"][sd_] <= TOL for r in ok if r.get("pixel"))}
                                for sd_ in SIDES},
         "byAlign": {al: dict(C(r["judgement"] for r in ok if r["style"]["align"] == al)) for al in ("left", "center", "right")},
         "rightPixelInsideByAlign": dict(C(r["style"]["align"] for r in ok if "side_pixel_inside:right" in r["flags"])),
         "oneLineConfirmedSides": dict(C(sd_ for r in one if r["judgement"] == "overflow_confirmed" for sd_ in SIDES
                                         if r["primary"]["overflow"][sd_] and r["pixel"]["excess"][sd_] > TOL)),
         "sectionClipSides": dict(C(sd_ for r in ok if (r.get("sectionClip") or {}).get("any") for sd_ in SIDES if r["sectionClip"][sd_])),
         "finalImageSides": dict(C(sd_ for r in ok if (r.get("finalImage") or {}).get("violation", {}).get("any")
                                   for sd_ in SIDES if r["finalImage"]["violation"][sd_])),
         "pixelMaxAbsDiff": _quantiles([r["pixel"]["maxAbsDiff"] for r in rows if r.get("pixel") and "maxAbsDiff" in r["pixel"]]),
         "overflowExcessPx": _quantiles([r["primary"]["excess"]["max"] for r in ok if r["primary"]["overflow"]["any"]]),
         "independent": {"blockOverflow": sum(r["primary"]["overflow"]["any"] for r in ok),
                         "sectionClip": flags.get("section_clip", 0), "finalImage": flags.get("final_image", 0),
                         "sectionClipWithoutBlockOverflow": sum("section_clip" in r["flags"] and not r["primary"]["overflow"]["any"] for r in ok)}}
    ref = [r for r in ok if r["aux"].get("pillowOverflow") is not None]
    if ref:
        s["reference"] = {
            "note": "참고값 — Pillow는 브라우저 판정의 정답이 아니다. recorded = 보존본 기록(v1.0.1, 줄 수 × 행간 판정), pillow = 현행 develop 서버 렌더 판정",
            "browser_vs_pillow": {f"browser={a}/pillow={b}": n for (a, b), n in
                                  C((r["primary"]["overflow"]["any"], r["aux"]["pillowOverflow"]) for r in ref).items()},
            "browser_vs_recorded": {f"browser={a}/recorded={b}": n for (a, b), n in
                                    C((r["primary"]["overflow"]["any"], r["aux"]["recordedOverflow"]) for r in ref).items()},
            "lines_browser_vs_pillow": {"same": sum(len(r["primary"]["lines"]) == r["aux"]["pillowLines"] for r in ref),
                                        "different": sum(len(r["primary"]["lines"]) != r["aux"]["pillowLines"] for r in ref)},
        }
    return s


def _quantiles(v: list[float]) -> list[float]:
    v = sorted(v)
    return [round(v[int(q * (len(v) - 1))], 4) for q in (0, .25, .5, .75, .9, 1)] if v else []


JUDGE_KO = {"fit": "정상", "overflow_confirmed": "실제 넘침", "overflow_subpixel": "넘침(픽셀 구분 불가 범위)",
            "overflow_quantized": "넘침 — 측정 한계 가능(TextMetrics 정수화 범위 · 픽셀 안)",
            "overflow_pixel_inside": "측정 오류 후보 — 엔진 넘침 · 픽셀 안", "fit_pixel_outside": "측정 오류 후보 — 엔진 정상 · 픽셀 밖",
            "measure_failed": "측정 실패(넘침 아님)", "fit_unchecked": "정상(픽셀 대조 없음)", "overflow_unchecked": "넘침(픽셀 대조 없음)"}


def fmt_r(r: dict[str, float] | None) -> str:
    return "-" if not r else f"L {r['left']:.3f} · T {r['top']:.3f} · R {r['right']:.3f} · B {r['bottom']:.3f}"


def write_review(m: dict[str, Any], s: dict[str, Any], out: Path) -> None:
    rows = sorted(m["blocks"], key=lambda r: (["overflow_pixel_inside", "fit_pixel_outside", "measure_failed", "overflow_quantized", "overflow_subpixel",
                                               "overflow_confirmed", "fit"].index(r["judgement"]) if r["judgement"] in JUDGE_KO else 9, r["key"]))
    opts = "".join(f'<option value="{k}">{html.escape(v)} ({s["judgement"].get(k, 0)})</option>' for k, v in JUDGE_KO.items()
                   if s["judgement"].get(k))
    fopts = "".join(f'<option value="f:{k}">flag {k} ({n})</option>' for k, n in s["flags"].items())
    items = []
    for r in rows:
        pr = r["primary"]
        imgs = "".join(f'<img loading="lazy" src="../{html.escape(p)}" alt="{k}">' for k, p in r["images"].items())
        if pr["ok"]:
            ov = ", ".join(x for x in SIDES if pr["overflow"][x]) or "none"
            detail = (f"줄 {len(pr['lines'])} · 글자 {pr['fontPx']:.3f}px · 넘친 방향 {ov} · 최대 초과 {pr['excess']['max']:.3f}px<br>"
                      f"배치 영역 {fmt_r(box_rect(r['box']))}<br>측정 ink {fmt_r(pr['ink'])}<br>"
                      f"픽셀 경계 {fmt_r((r.get('pixel') or {}).get('ink'))} · 엔진 대비 최대 차 "
                      f"{(r.get('pixel') or {}).get('maxAbsDiff', float('nan')):.3f}px<br>"
                      f"줄 텍스트 {html.escape(json.dumps([l['text'] for l in pr['lines']], ensure_ascii=False))}")
        else:
            detail = f"측정 실패: {html.escape(str(pr.get('reason')))} — {html.escape(str(pr.get('detail')))}"
        ref = r["aux"]
        items.append(
            f'<article data-j="{r["judgement"]}" data-f="{" ".join(r["flags"])}"><h3>{html.escape(r["key"])} · {r["role"]} · '
            f'{html.escape(JUDGE_KO.get(r["judgement"], r["judgement"]))}</h3>'
            f'<p class="flags">{" ".join(r["flags"]) or "&nbsp;"}</p><p class="t">{html.escape(r["text"])}</p>'
            f'<div class="imgs">{imgs}</div><p class="d">{detail}<br><span class="ref">참고(정답 아님): 보존본 기록 '
            f'{ref.get("recordedOverflow")} · 현행 Pillow {ref.get("pillowOverflow")}</span></p></article>')
    meta = m["meta"]
    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>조판 넘침 측정 결과 목록</title><style>
body{{font:14px/1.5 "Malgun Gothic",sans-serif;margin:16px;color:#1d2b33;background:#f4f6f7}}article{{background:#fff;border:1px solid #d5dde0;
border-radius:8px;padding:10px 14px;margin:10px 0}}h3{{margin:0;font-size:14px}}.flags{{color:#a05000;margin:2px 0}}.t{{white-space:pre-wrap;
background:#f0f3f4;padding:4px 8px}}.imgs{{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start}}.imgs img{{max-width:440px;
border:1px solid #ccd}}.d{{font:12px/1.6 Consolas,"Malgun Gothic",monospace}}.ref{{color:#777}}header{{position:sticky;top:0;background:#f4f6f7;
padding:8px 0}}</style></head><body><header><b>측정 결과 전체 목록(열람용)</b> — 사람이 판단할 사례만 모은 화면은
<a href="../check/index.html">사람 확인 페이지</a>(`typeset_browser_review.py page`로 만든다)<br>{html.escape(meta['path'])}<br>
입력 {html.escape(str(meta['inputVariant']))} · FE {html.escape(str(meta['fe']['git'].get('commit'))[:10])} · Chromium {html.escape(meta['browser']['version'])}
· DPR {meta['dpr']} (확인 {meta['checkDpr']}) · 검증 허용치 {TOL}px(판정 허용 오차 아님)<br>
그림: pixel = 흰 바탕 검정 글자 촬영(빨강 영역 · 파랑 엔진 ink · 초록 픽셀 경계) / context = 섹션 합성(빨강 영역 · 굵은 빨강 넘침 · 파랑 ink · 초록 점선 줄 · 회색 점선 섹션 경계)<br>
<select id="f"><option value="">전체 ({len(rows)})</option>{opts}{fopts}</select></header>
{''.join(items)}
<script>document.getElementById('f').onchange=e=>{{const v=e.target.value;for(const a of document.querySelectorAll('article')){{
a.hidden=v&&(v.startsWith('f:')?!a.dataset.f.split(' ').includes(v.slice(2)):a.dataset.j!==v)}}}}</script></body></html>"""
    (out / "review").mkdir(exist_ok=True)
    (out / "review" / "index.html").write_text(doc, encoding="utf-8")


# ───────────────────────── compare ─────────────────────────

# 전후 비교가 같은 측정 조건인지 — 측정 코드 · 폰트 · 브라우저 · 배율 · 실행기 · OS(래스터라이저) · 이 환경의 TextMetrics 특성.
# OS 빌드 번호(platform.version)는 기록만 하고 비교하지 않는다. 환경 기록(platform · metricsProbe)이 없는 결과는 비교를 거절한다
REQUIRED_ENV = (("platform",), ("metricsProbe",))
CONDITION_KEYS = (("fe", "modules"), ("fonts", "faces"), ("browser", "version"), ("dpr",), ("checkDpr",), ("toolAssets",), ("tool",),
                  ("platform", "system"), ("platform", "release"), ("platform", "machine"), ("metricsProbe",))


def _get(d: dict[str, Any], path: tuple[str, ...]) -> Any:
    for k in path:
        d = (d or {}).get(k)
    if path == ("fe", "modules"):
        d = {k: v["source_sha256"] for k, v in (d or {}).get("modules", {}).items()}
    return d


def condition_problems(ma: dict[str, Any], mb: dict[str, Any], keys=CONDITION_KEYS, required=REQUIRED_ENV) -> tuple[list[str], list[str]]:
    """두 측정 기록이 같은 조건인지 — (환경 기록이 빠진 항목, 값이 다른 항목)."""
    missing = [f"{side}:{'.'.join(p)}" for side, M in (("before", ma), ("after", mb)) for p in required if _get(M, p) is None]
    return missing, [".".join(p) for p in keys if _get(ma, p) != _get(mb, p)]


def compare(before_dir: Path, after_dir: Path, out: Path, allow_condition_diff: bool) -> dict[str, Any]:
    A = json.loads((before_dir / "measure.json").read_text(encoding="utf-8"))
    B = json.loads((after_dir / "measure.json").read_text(encoding="utf-8"))
    missing, diffs = condition_problems(A["meta"], B["meta"])
    if missing and not allow_condition_diff:
        raise SystemExit(f"측정 환경 기록이 없다 — 최신 도구로 다시 측정하거나 --allow-condition-diff: {missing}")
    if diffs and not allow_condition_diff:
        raise SystemExit(f"측정 조건이 다르다 — 같은 조건에서 다시 측정하거나 --allow-condition-diff: {diffs}")
    a, b = {r["key"]: r for r in A["blocks"]}, {r["key"]: r for r in B["blocks"]}
    rows = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if x is None or y is None:
            rows.append({"key": k, "status": "only_before" if y is None else "only_after"})
            continue
        # 줄바꿈 위치만 바꾼 변형(공백 · 개행만 다름)은 같은 문구로 본다 — 글자는 하나도 바뀌지 않는다(자동 축약 없음, 6.4)
        same_words = x["text"].split() == y["text"].split()
        if not same_words or x["role"] != y["role"]:
            rows.append({"key": k, "status": "text_or_role_changed"})  # 같은 입력의 전후가 아니다
            continue
        px, py = x["primary"], y["primary"]
        changed = [f for f in ("box", "style") if x[f] != y[f]] + (["lineBreak"] if x["text"] != y["text"] else [])
        row = {"key": k, "status": "changed_input" if changed else "same_input", "changedFields": changed,
               "box": [x["box"], y["box"]], "fontScale": [x["style"]["fontScale"], y["style"]["fontScale"]],
               "fontPx": [px.get("fontPx"), py.get("fontPx")], "lines": [len(px.get("lines") or []), len(py.get("lines") or [])],
               "overflow": [px.get("overflow", {}).get("any") if px["ok"] else None, py.get("overflow", {}).get("any") if py["ok"] else None],
               "maxExcess": [px.get("excess", {}).get("max"), py.get("excess", {}).get("max")],
               "judgement": [x["judgement"], y["judgement"]], "flags": [x["flags"], y["flags"]],
               "images": [{k2: os.path.relpath(before_dir / p, out / "review").replace("\\", "/") for k2, p in x["images"].items()},
                          {k2: os.path.relpath(after_dir / p, out / "review").replace("\\", "/") for k2, p in y["images"].items()}]}
        if not changed and (row["overflow"][0] != row["overflow"][1] or px.get("ink") != py.get("ink")):
            row["status"] = "same_input_result_differs"  # 측정 재현성 문제 — 조정 효과로 읽지 않는다
        rows.append(row)
    C = collections.Counter
    summary = {"conditionDiffs": diffs, "missingEnvironment": missing, "status": dict(C(r["status"] for r in rows)),
               "transitions_changed": dict(C(f"{r['judgement'][0]} -> {r['judgement'][1]}" for r in rows if r["status"] == "changed_input")),
               "overflow_changed": dict(C(f"{r['overflow'][0]} -> {r['overflow'][1]}" for r in rows if r["status"] == "changed_input"))}
    res = {"before": {"dir": str(before_dir), "variant": A["meta"]["inputVariant"], "inputSha256": A["meta"]["inputSha256"]},
           "after": {"dir": str(after_dir), "variant": B["meta"]["inputVariant"], "inputSha256": B["meta"]["inputSha256"]},
           "summary": summary, "blocks": rows}
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    cards = []
    for r in rows:
        if r["status"] not in ("changed_input", "same_input_result_differs"):
            continue
        im = "".join(f'<figure><figcaption>{side}</figcaption>{"".join(f"<img src={chr(34)}{html.escape(p)}{chr(34)}>" for p in imgs.values())}</figure>'
                     for side, imgs in zip(("전", "후"), r["images"]))
        cards.append(f"<article><h3>{html.escape(r['key'])} · {r['status']}</h3><p>판정 {r['judgement'][0]} → {r['judgement'][1]} · "
                     f"넘침 {r['overflow'][0]} → {r['overflow'][1]} · 최대 초과 {r['maxExcess'][0]} → {r['maxExcess'][1]} px · "
                     f"줄 {r['lines'][0]} → {r['lines'][1]} · 글자 {r['fontPx'][0]} → {r['fontPx'][1]} px · 배율 {r['fontScale'][0]} → "
                     f"{r['fontScale'][1]}<br>영역 {html.escape(canon(r['box'][0]))} → {html.escape(canon(r['box'][1]))}</p>"
                     f"<div style='display:flex;gap:12px'>{im}</div></article>")
    (out / "review").mkdir(exist_ok=True)
    (out / "review" / "index.html").write_text(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>조정 전후 비교</title><style>body{font:14px/1.5 "
        "'Malgun Gothic',sans-serif;margin:16px}article{border:1px solid #ccd;border-radius:8px;padding:8px 12px;margin:8px 0}"
        "img{max-width:420px;border:1px solid #ccd;display:block;margin:4px 0}figure{margin:0}</style></head><body>"
        f"<h2>조정 전후 비교</h2><pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=1))}</pre>{''.join(cards)}</body></html>",
        encoding="utf-8")
    return res


# ───────────────────────── selfcheck ─────────────────────────

def selfcheck(fe_root: Path, out: Path, dpr: float) -> dict[str, Any]:
    """합의 사례(D-01 · D-02 · 6.4)를 이 도구의 경로(입력 변환 · 폰트 · 최소 페이지)로 다시 확인한다. FE e2e 사례를 본떴다."""
    from playwright.sync_api import sync_playwright

    defaults = t.load_role_defaults()
    m = defaults.data.data.pipeline_mapping

    def blk(text, box, role="body", align="left", size=16.0, weight=400, ls=-0.03):
        return {"blockId": 0, "role": role, "text": text, "box": box,
                "style": {"fontFamily": m.font_family, "fontWeight": weight, "fontSizePx": size, "fontScale": 1.0,
                          "fontColor": "#222222", "align": align, "letterSpacingEm": ls, "lineHeightRatio": m.line_height_ratio}}

    out.mkdir(parents=True, exist_ok=True)
    bundle = Bundle(fe_root, out)
    problems: list[str] = []
    cases = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx, page, _ = open_page(browser, bundle, dpr, problems)
        metrics = page.evaluate("window.__tbm.probeMetrics()")

        def run(name, b, expect, note=""):
            r = page.evaluate("([w, b]) => window.__tbm.measure(w, b)", [375, b])
            pr = slim(r, b["box"])
            px = pixel_probe(page, r, b["box"], dpr, out / f"{name}.pixel.png") if pr["ok"] and pr["ink"] else None
            got = {"ok": pr["ok"], "reason": pr.get("reason"), "lines": len(pr["lines"]) if pr["ok"] else None,
                   "overflow": {k: pr["overflow"][k] for k in (*SIDES, "any")} if pr["ok"] else None,
                   "judgement": judge(pr, px, metrics["quantized"]), "pixelMaxAbsDiff": (px or {}).get("maxAbsDiff"),
                   "engineInk": pr.get("ink"), "pixelInk": (px or {}).get("ink"), "excess": pr.get("excess"),
                   "lineBoxOutside": bool(pr["ok"] and len(pr["lines"]) == 1 and (pr["lines"][0]["rect"]["top"] < b["box"]["y"]
                                                                                 or pr["lines"][0]["rect"]["bottom"] > b["box"]["y"] + b["box"]["h"]))}
            passed = all((got["overflow"] or {}).get(k) == v if k in (*SIDES, "any") else got.get(k) == v for k, v in expect.items())
            cases.append({"name": name, "input": b, "expect": expect, "got": got, "pass": passed, "note": note})
            return pr

        base = run("한줄_넓은영역", blk("Pixlate", {"x": 20, "y": 20, "w": 400, "h": 60}), {"any": False, "lines": 1, "judgement": "fit"})
        ink = base["ink"]
        edge = {"x": 20, "y": 20, "w": ink["right"] - 20, "h": ink["bottom"] - 20}
        run("경계와_같음", blk("Pixlate", edge), {"any": False, "lineBoxOutside": True})
        run("오른쪽_0.115px", blk("Pixlate", {**edge, "w": edge["w"] - 0.115}), {"right": True, "bottom": False, "left": False, "top": False,
                                                                              "judgement": "overflow_subpixel"})
        run("아래_0.115px", blk("Pixlate", {**edge, "h": edge["h"] - 0.115}), {"bottom": True, "right": False, "judgement": "overflow_subpixel"})
        # 한 줄 블록: 글자 크기 = 영역 높이 × 0.9(현행 em_ratio 잠정값과 같은 비)일 때 줄 상자(1.08배)는 넘지만 글자는 안 — 정상이어야 한다
        run("한줄_줄상자만_초과", blk("Recommended for:", {"x": 20, "y": 20, "w": 300, "h": 30}, size=27.0), {"any": False, "lineBoxOutside": True})
        run("긴단어_폭초과", blk("averyveryveryverylongword", {"x": 20, "y": 20, "w": 60, "h": 60}), {"right": True, "lines": 1,
                                                                                                 "judgement": "overflow_confirmed"})
        run("여러줄_높이초과", blk("The browser decides where each line breaks", {"x": 20, "y": 20, "w": 120, "h": 20}),
            {"bottom": True, "judgement": "overflow_confirmed"})
        run("하강부_j_왼쪽", blk("jump", {"x": 20, "y": 20, "w": 120, "h": 60}), {"left": True},
            "j의 음수 왼쪽 여백 — 합의상 넘침. 픽셀 초과량과 엔진 초과량의 차이는 judgement로 본다")
        run("가운데정렬_정상", blk("Before", {"x": 20, "y": 20, "w": 200, "h": 40}, align="center"), {"any": False, "judgement": "fit"})
        # 오른쪽 정렬 + 음수 자간: CSS letter-spacing 은 마지막 글자 뒤에도 붙어 줄 끝(영역 오른쪽)이 글리프 advance 끝보다 안쪽이 된다.
        # 끝 자간 보정 없음(D-05) · 허용 오차 없음(D-02)이면 넘침이 합의 기준의 결과다 — 자간 0 대조와 함께 남긴다(협의 사항)
        run("오른쪽정렬_음수자간", blk("POINT 02", {"x": 20, "y": 20, "w": 200, "h": 40}, align="right"), {"right": True, "left": False},
            "합의 기준(D-02 · D-05)의 결과. 넉넉한 영역의 오른쪽 정렬 블록도 넘침이 될 수 있다 — 협의 필요")
        run("오른쪽정렬_자간0", blk("POINT 02", {"x": 20, "y": 20, "w": 200, "h": 40}, align="right", ls=0.0), {"any": False},
            "같은 문구 · 자간 0 대조")
        run("title600", blk("Sun Care", {"x": 20, "y": 20, "w": 300, "h": 50}, role="title", size=36.0, weight=600, ls=-0.015),
            {"any": False})
        run("글리프대체_한자", blk("実際", {"x": 20, "y": 20, "w": 200, "h": 40}), {"ok": False, "reason": "glyph-fallback",
                                                                               "judgement": "measure_failed"})
        # 폰트 미준비 경로 — 없는 family 로 측정기를 열면 대체 폰트로 측정하지 않고 거절해야 한다
        missing = page.evaluate("""async () => { const m = await import('/fe/typeset-measurement.js');
            const r = await m.openTypesetMeasurer({ fontFamily: 'PixlateNoSuchTypesetFont' }); return { ok: r.ok, reason: r.reason }; }""")
        cases.append({"name": "폰트_미준비", "expect": {"ok": False, "reason": "font-not-ready"}, "got": missing,
                      "pass": missing == {"ok": False, "reason": "font-not-ready"}})
        version = browser.version
        ctx.close()
        browser.close()
    res = {"tool": TOOL_VERSION, "dpr": dpr, "browser": version, "metricsProbe": metrics, "fe": {"git": bundle.fe_git, "modules": bundle.fe_manifest},
           "fonts": bundle.fonts, "consoleProblems": problems, "passed": sum(c["pass"] for c in cases), "total": len(cases), "cases": cases}
    (out / "selfcheck.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ───────────────────────── CLI ─────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("build-input")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--group", action="append", help="기본 GS-01 · GS-02(GS-03은 렌더 없음)")
    a = sub.add_parser("measure")
    a.add_argument("--input", type=Path, required=True)
    a.add_argument("--fe", type=Path, required=True, help="FE 저장소 체크아웃(lib/n5/typeset-*.ts)")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--dpr", type=float, default=2.0, help="판정 · 픽셀 대조 DPR(FE e2e와 같은 2)")
    a.add_argument("--check-dpr", type=float, default=1.0, help="보조 측정 · 섹션 합성 DPR. 0이면 생략")
    a.add_argument("--no-render", action="store_true", help="섹션 합성 그림 생략")
    a.add_argument("--only", action="append", help="섹션 키(GS-01/s3) — 일부만 측정")
    a = sub.add_parser("rejudge", help="measure.json에서 분류 · 집계 · 검토 페이지만 다시 만든다(브라우저 불필요)")
    a.add_argument("--run", type=Path, required=True)
    a = sub.add_parser("derive")
    a.add_argument("--input", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--label", default="derived")
    a.add_argument("--set", nargs=2, action="append", metavar=("BLOCK", "FIELD=VALUE"), required=True)
    a = sub.add_parser("compare")
    a.add_argument("--before", type=Path, required=True)
    a.add_argument("--after", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--allow-condition-diff", action="store_true")
    a = sub.add_parser("selfcheck")
    a.add_argument("--fe", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--dpr", type=float, default=2.0)
    args = ap.parse_args()

    if args.cmd == "build-input":
        inp = build_input(args.run, tuple(args.group or GROUPS))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(inp, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({"sections": len(inp["sections"]), "blocks": sum(len(s["blocks"]) for s in inp["sections"]),
                          "skipped": inp["skipped"], "inputErrors": inp["inputErrors"]}, ensure_ascii=False))
    elif args.cmd == "measure":
        inp = json.loads(args.input.read_text(encoding="utf-8"))
        if inp.get("schema") != INPUT_SCHEMA:
            raise SystemExit(f"입력 형식이 {INPUT_SCHEMA}가 아니다")
        if args.only:
            inp["sections"] = [s for s in inp["sections"] if s["key"] in args.only]
        m = measure_sections(inp, args.fe, args.out, args.dpr, args.check_dpr or None, not args.no_render)
        m["meta"]["inputFile"] = str(args.input)
        m["meta"]["only"] = args.only
        s = summarize(m)
        (args.out / "measure.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        (args.out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        write_review(m, s, args.out)
        print(json.dumps(s, ensure_ascii=False, indent=1))
    elif args.cmd == "rejudge":
        m = json.loads((args.run / "measure.json").read_text(encoding="utf-8"))
        m["meta"]["quantAllowPx"] = QUANT_ALLOW
        m["meta"]["verifyTolerancePx"] = TOL
        classify(m["blocks"], m["meta"]["metricsProbe"]["quantized"], args.run)
        s = summarize(m)
        (args.run / "measure.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        (args.run / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        write_review(m, s, args.run)
        print(json.dumps(s, ensure_ascii=False, indent=1))
    elif args.cmd == "derive":
        inp = json.loads(args.input.read_text(encoding="utf-8"))
        out = derive(inp, [tuple(x) for x in args.set], args.label)
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{args.out}: {len(args.set)}개 값 변경")
    elif args.cmd == "compare":
        r = compare(args.before, args.after, args.out, args.allow_condition_diff)
        print(json.dumps(r["summary"], ensure_ascii=False, indent=1))
    elif args.cmd == "selfcheck":
        r = selfcheck(args.fe, args.out, args.dpr)
        for c in r["cases"]:
            print(("PASS " if c["pass"] else "FAIL ") + c["name"], json.dumps(c["got"], ensure_ascii=False))
        print(f"{r['passed']}/{r['total']}")


if __name__ == "__main__":
    main()
