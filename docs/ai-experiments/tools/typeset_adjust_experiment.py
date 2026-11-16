"""⑨ 조판 자동 조정 실험 — 조정 후보 탐색 · 집계(2026-11-16). 개발용 로컬 실험이며 운영 경로가 아니다. 운영 코드 · config는 바꾸지 않는다.

    # 1) 조정 후보 탐색 — 넘친 블록마다 후보를 적용해 조정 입력(after-input.json)과 탐색 기록을 만든다
    python docs/ai-experiments/tools/typeset_adjust_experiment.py search --input <input.json> --fe <FE 체크아웃> --out <폴더> \
        [--criterion quant_aware|engine] [--strategy S90 --strategy BES90 ...]
    # 2) 조정 입력을 기존 측정 도구로 측정 · 비교(그림 · 픽셀 대조 · 판정은 typeset_browser_measure.py 그대로)
    python docs/ai-experiments/tools/typeset_browser_measure.py measure --input <폴더>/<기준>/<후보>/after-input.json --fe <FE> --out <폴더>/<기준>/<후보>/measure
    python docs/ai-experiments/tools/typeset_browser_measure.py compare --before <조정 전 measure> --after <폴더>/<기준>/<후보>/measure --out <...>/compare
    # 3) 집계
    python docs/ai-experiments/tools/typeset_adjust_experiment.py report --out <폴더> --before <조정 전 measure 폴더>

재사용(새로 구현하지 않는다): 측정 · 판정은 `typeset_browser_measure`(FE 공통 측정 엔진 실행 · 초과량 · 픽셀 대조 · 박스 검사),
렌더 입력은 그 도구의 `input.json`(/preview renderInput 모양), 보존본 적용 박스 해석은 `typeset_overflow_probe.applied_layout`.
이 파일이 더하는 것은 조정량 탐색(아래 수단)과 집계뿐이다.

조정 수단 — PRD v3.5.10 F-TRN-04 ⑦: 폰트 축소 · 줄바꿈 위치 · 겹치지 않는 여백 안의 배치 영역 확장. 줄 추가 · 자간 조정 · 문구 변경은 하지 않는다.
- B 줄바꿈 위치: 번역문의 명시적 개행을 풀어 브라우저 자연 줄바꿈에 맡긴다(글자는 그대로). 줄 수가 늘면 기각, 넘침이 줄 때만 채택.
  계약의 `line_break_applied`가 자연 줄바꿈인지 강제 개행인지는 미정이다(archive 5.13) — 이 실험의 해석이다.
- E 영역 확장: 같은 섹션의 다른 text_block 박스(제외 · 라벨 · 로고 포함 — 화면에 남거나 다른 번역문이 놓이는 자리)와 섹션 경계에
  닿기 전까지만 넓힌다(간격 0, 겹침 0). 가로 넘침이면 정렬 방향으로 폭을(left → 오른쪽, right → 왼쪽, center → 양쪽), 세로 넘침이면
  아래(위)로 높이를 늘린다. 원래 박스와 이미 겹친 장애물은 무시한다. 사진 · 그래픽 같은 이미지 요소와의 겹침은 보지 못한다.
  **저장 계약의 시스템 조정(`layout_adjust.system`)에는 박스 필드가 없다** — 계약 공백이며 실험 후보로만 쓴다.
- S 폰트 축소: 시스템 배율(`font_scale`)을 하한(floor)까지 내려 넘침이 없어지는 가장 큰 배율을 이분 탐색(정밀도 0.005)한다.
  하한 후보 0.90(PRD −10% 잠정) · 0.80(PoC V6 비교용). 사용자 배율과의 합성 규칙은 미정이며 이 표본(v1.0.1)에는 사용자 조정이 없다.
후보(STRATEGIES): 단독 S90 · S80 · B · E, 조합 BES90(PRD 「축소는 최후 수단」 순서) · BSE90, 진단 NEED(하한 0.30까지 필요 배율만 잰다).
하한 · 한도 · 간격 · 순서는 모두 실험용 후보값이며 정책이 아니다. 블록마다 「해소될 때만 적용」한다(후보 규칙) — 끝까지 넘치면
조정 입력에는 원래 값을 두고, 시도한 최종 상태와 남은 원인은 decisions.jsonl에만 남긴다. NEED는 진단이라 측정하지 않는다.

탐색 기준(--criterion):
- engine: 합의 기준 그대로(FE 엔진 ink가 영역을 넘으면 넘침, 허용 오차 없음).
- quant_aware(기본): 이 PC처럼 Canvas TextMetrics가 정수화되는 환경(metricsProbe.quantized)에서, 엔진 초과가 QUANT_ALLOW(2px) 이하인
  방향만 픽셀 대조로 다시 확인해 픽셀도 TOL(0.5px) 넘게 밖일 때만 넘침으로 본다. 정수화가 없는 환경이면 engine과 같다.
  정수화 오차 때문에 축소가 끝없이 이어지는 편향을 피하려는 실험 기준이며, 최종 판정은 measure 결과(합의 기준 + 픽셀 해석)로 보고한다.

출력(--out/<기준>/<후보>/): after-input.json(조정 입력, measure 입력 형식 그대로) · decisions.jsonl(블록별 단계 · 측정 횟수 · 시간 ·
남은 원인) · meta.json. --out/obstacles.json(섹션별 장애물). report는 --out/report.json · report.md.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import typeset_browser_measure as tbm  # noqa: E402

ROOT = tbm.ROOT
SIDES = tbm.SIDES
H_SIDES, V_SIDES = ("left", "right"), ("top", "bottom")
PRECISION = 0.005  # 축소 이분 탐색 정밀도(배율)
WIDTH_PRECISION = 0.5  # 폭 확장 이분 탐색 정밀도(px)
STRATEGIES: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "S90": [("shrink", {"floor": 0.90})],
    "S80": [("shrink", {"floor": 0.80})],
    "B": [("linebreak", {})],
    "E": [("expand", {})],
    "BES90": [("linebreak", {}), ("expand", {}), ("shrink", {"floor": 0.90})],
    "BSE90": [("linebreak", {}), ("shrink", {"floor": 0.90}), ("expand", {})],
    "NEED": [("shrink", {"floor": 0.30})],
}


# ───────────────────────── 장애물 · 여백 ─────────────────────────

def load_obstacles(inp: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    """섹션별 장애물 = 그 섹션의 모든 text_block 적용 박스(조판 대상이 아닌 블록 포함). 키는 input의 섹션 키."""
    probe = tbm.load_probe()
    run = ROOT / inp["origin"]["run"]
    out: dict[str, list[dict[str, float]]] = {}
    for g in inp["origin"]["groups"]:
        d = json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))
        for b in d["text_block"]:
            key = f"{g}/s{b['section_id']}"
            try:
                box, _ = probe.applied_layout(b)
            except ValueError:
                continue
            out.setdefault(key, []).append({"blockId": b["id"], **{k: float(box[k]) for k in ("x", "y", "w", "h")},
                                            "excluded": b["is_excluded"] is not False, "typeset": bool((b["trans_1"] or "").strip())})
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def intersects(a: dict[str, float], b: dict[str, float]) -> bool:
    return _overlap(a["x"], a["x"] + a["w"], b["x"], b["x"] + b["w"]) and _overlap(a["y"], a["y"] + a["h"], b["y"], b["y"] + b["h"])


def free_limits(box: dict[str, float], obstacles: list[dict[str, float]], sec_w: float, sec_h: float,
                ignore: set[int]) -> dict[str, float]:
    """박스를 각 방향으로 넓힐 수 있는 한계 좌표(장애물 · 섹션 경계에 닿기 전까지). 가로는 현재 세로 범위, 세로는 현재 가로 범위와
    겹치는 장애물만 본다."""
    x0, x1, y0, y1 = box["x"], box["x"] + box["w"], box["y"], box["y"] + box["h"]
    lim = {"left": 0.0, "right": float(sec_w), "top": 0.0, "bottom": float(sec_h)}
    for o in obstacles:
        if o["blockId"] in ignore:
            continue
        ox0, ox1, oy0, oy1 = o["x"], o["x"] + o["w"], o["y"], o["y"] + o["h"]
        if _overlap(y0, y1, oy0, oy1):
            if ox0 >= x1:
                lim["right"] = min(lim["right"], ox0)
            if ox1 <= x0:
                lim["left"] = max(lim["left"], ox1)
        if _overlap(x0, x1, ox0, ox1):
            if oy0 >= y1:
                lim["bottom"] = min(lim["bottom"], oy0)
            if oy1 <= y0:
                lim["top"] = max(lim["top"], oy1)
    return lim


# ───────────────────────── 측정 · 기준 ─────────────────────────

class Evaluator:
    """블록 상태(문구 · 박스 · 배율)를 FE 측정 엔진으로 재고 탐색 기준으로 넘침을 정한다. 측정 · 픽셀 대조 횟수를 센다."""

    def __init__(self, page, dpr: float, quantized: bool, criterion: str):
        self.page, self.dpr, self.quantized, self.criterion = page, dpr, quantized, criterion
        self.measures = 0
        self.probes = 0

    def __call__(self, sec: dict[str, Any], blk: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        b = {**blk, "text": st["text"], "box": st["box"], "style": {**blk["style"], "fontScale": st["scale"]}}
        res = self.page.evaluate("([w, b]) => window.__tbm.measure(w, b)", [sec["width"], b])
        self.measures += 1
        pr = tbm.slim(res, st["box"])
        if not pr["ok"]:
            return {"ok": False, "over": True, "sides": [], "res": res, "pr": pr, "reason": pr.get("reason")}
        sides = [s for s in SIDES if pr["overflow"][s]]
        real = sides
        if self.criterion == "quant_aware" and self.quantized:
            small = [s for s in sides if pr["excess"][s] <= tbm.QUANT_ALLOW]
            real = [s for s in sides if pr["excess"][s] > tbm.QUANT_ALLOW]
            if small:
                px = tbm.pixel_probe(self.page, res, st["box"], self.dpr, None)
                self.probes += 1
                real += [s for s in small if px is not None and px["excess"][s] > tbm.TOL]
        return {"ok": True, "over": bool(real), "sides": real, "engine_sides": sides, "res": res, "pr": pr,
                "lines": len(pr["lines"]), "excess": pr["excess"]}


def _max_excess(ev: dict[str, Any], sides: tuple[str, ...] = SIDES) -> float:
    return max([ev["excess"][s] for s in sides if s in ev.get("sides", [])] or [0.0]) if ev["ok"] else math.inf


# ───────────────────────── 수단 ─────────────────────────

def step_linebreak(E, sec, blk, st, cur, _p, _ctx):
    if not cur["over"] or "\n" not in st["text"]:
        return st, cur, {"applied": False, "why": "no_newline" if cur["over"] else "fit"}
    alt = {**st, "text": " ".join(st["text"].split())}
    ev = E(sec, blk, alt)
    if not ev["ok"] or ev["lines"] > cur["lines"]:
        return st, cur, {"applied": False, "why": "more_lines" if ev["ok"] else "measure_failed"}
    if not ev["over"] or _max_excess(ev) < _max_excess(cur):
        return alt, ev, {"applied": True, "linesBefore": cur["lines"], "linesAfter": ev["lines"]}
    return st, cur, {"applied": False, "why": "no_gain"}


def step_shrink(E, sec, blk, st, cur, p, _ctx):
    floor = p["floor"]
    if not cur["over"]:
        return st, cur, {"applied": False, "why": "fit"}
    hi = st["scale"]
    at_floor = {**st, "scale": floor}
    ev_floor = E(sec, blk, at_floor)
    if ev_floor["over"]:
        keep = ev_floor if ev_floor["ok"] and _max_excess(ev_floor) < _max_excess(cur) else None
        info = {"applied": keep is not None, "scale": floor if keep else hi, "need_below_floor": True}
        return (at_floor, ev_floor, info) if keep else (st, cur, info)
    lo, best, best_ev = floor, floor, ev_floor  # lo: 들어감, hi: 넘침
    while hi - lo > PRECISION:
        mid = round((lo + hi) / 2, 4)
        ev = E(sec, blk, {**st, "scale": mid})
        if ev["over"]:
            hi = mid
        else:
            lo, best, best_ev = mid, mid, ev
    return {**st, "scale": best}, best_ev, {"applied": True, "scale": best, "need_below_floor": False}


def step_expand(E, sec, blk, st, cur, _p, ctx):
    if not cur["over"]:
        return st, cur, {"applied": False, "why": "fit"}
    obstacles, ignore = ctx["obstacles"], ctx["ignore"]
    box0 = st["box"]
    info: dict[str, Any] = {"applied": False, "widthLimited": False, "heightLimited": False}
    # 가로: 정렬 방향으로 폭을 넓힌다(폭은 줄바꿈을 바꾸므로 이분 탐색)
    if any(s in cur["sides"] for s in H_SIDES):
        lim = free_limits(box0, obstacles, sec["width"], sec["height"], ignore)
        al = blk["style"]["align"]
        x0, w0 = box0["x"], box0["w"]
        if al == "left":
            wmax = lim["right"] - x0
        elif al == "right":
            wmax = (x0 + w0) - lim["left"]
        else:
            half = max(0.0, min(x0 - lim["left"], lim["right"] - (x0 + w0)))
            wmax = w0 + 2 * half

        def boxw(w):
            x = x0 if al == "left" else (x0 + w0 - w) if al == "right" else x0 - (w - w0) / 2
            x = max(lim["left"], x)  # 부동소수 오차로 한계를 넘지 않게
            return {**box0, "x": x, "w": min(w, lim["right"] - x)}

        if wmax > w0:
            ev_max = E(sec, blk, {**st, "box": boxw(wmax)})
            h_fit = ev_max["ok"] and not any(s in ev_max["sides"] for s in H_SIDES)
            if h_fit:
                lo, hi, best_ev = w0, wmax, ev_max
                while hi - lo > WIDTH_PRECISION:
                    mid = (lo + hi) / 2
                    ev = E(sec, blk, {**st, "box": boxw(mid)})
                    if ev["ok"] and not any(s in ev["sides"] for s in H_SIDES):
                        hi, best_ev = mid, ev
                    else:
                        lo = mid
                st, cur = {**st, "box": boxw(hi)}, best_ev
                info.update(applied=True, width=[w0, hi])
            elif ev_max["ok"] and _max_excess(ev_max, H_SIDES) < _max_excess(cur, H_SIDES):
                st, cur = {**st, "box": boxw(wmax)}, ev_max
                info.update(applied=True, width=[w0, wmax], widthLimited=True)
            else:
                info["widthLimited"] = True
        else:
            info["widthLimited"] = True
    # 세로: 높이는 줄바꿈을 바꾸지 않는다 — 엔진 ink 경계까지 늘리고 다시 잰다
    if cur["ok"] and any(s in cur["sides"] for s in V_SIDES):
        b = st["box"]
        lim = free_limits(b, obstacles, sec["width"], sec["height"], ignore)
        ink = cur["pr"]["ink"]
        top, bottom = b["y"], b["y"] + b["h"]
        if "bottom" in cur["sides"]:
            bottom = min(max(bottom, ink["bottom"]), lim["bottom"])
        if "top" in cur["sides"]:
            top = max(min(top, ink["top"]), lim["top"])
        if (top, bottom) != (b["y"], b["y"] + b["h"]):
            nb = {**b, "y": top, "h": bottom - top}
            ev = E(sec, blk, {**st, "box": nb})
            st, cur = {**st, "box": nb}, ev
            info.update(applied=True, height=[b["h"], bottom - top])
        if cur["ok"] and any(s in cur["sides"] for s in V_SIDES):
            info["heightLimited"] = True
    if info["applied"] and not tbm.box_ok(st["box"], sec["width"], sec["height"]):
        raise AssertionError(f"확장 박스가 섹션 밖이다: {blk['blockId']} {st['box']}")
    return st, cur, info


STEPS = {"linebreak": step_linebreak, "shrink": step_shrink, "expand": step_expand}


def unresolved_causes(sec, blk, st, ev, steps_info) -> list[str]:
    """조정 뒤에도 넘치는 원인(복수 가능). 측정 실패 · 한 단어 폭 초과 · 축소 하한 · 여백 부족 · 수단 없음."""
    if not ev["ok"]:
        return [f"measure_failed:{ev.get('reason')}"]
    out = []
    box = st["box"]
    if any(s in ev["sides"] for s in H_SIDES):
        for ln in ev["pr"]["lines"]:
            if " " not in ln["text"].strip() and ln["ink"]["right"] - ln["ink"]["left"] > box["w"]:
                out.append("long_word")
                break
    for op, info in steps_info:
        if op == "shrink" and info.get("need_below_floor"):
            out.append("shrink_floor")
        if op == "expand" and (info.get("widthLimited") or info.get("heightLimited")):
            out.append("space_blocked")
    return out or ["no_applicable_means"]


# ───────────────────────── search ─────────────────────────

def search(inp_path: Path, fe: Path, out: Path, criterion: str, strategies: list[str], dpr: float) -> None:
    from playwright.sync_api import sync_playwright

    inp = json.loads(inp_path.read_text(encoding="utf-8"))
    obstacles = load_obstacles(inp)
    out.mkdir(parents=True, exist_ok=True)
    (out / "obstacles.json").write_text(json.dumps(obstacles, ensure_ascii=False, indent=1), encoding="utf-8")
    bundle = tbm.Bundle(fe, out)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        _ctx, page, opened = tbm.open_page(browser, bundle, dpr, problems)
        metrics = page.evaluate("window.__tbm.probeMetrics()")
        # 측정 1회 비용(브라우저 안, 왕복 제외) — 대표 블록 하나를 50회 잰다
        bench = page.evaluate("""([w, b]) => { const t0 = performance.now(); for (let i = 0; i < 50; i++) window.__tbm.measure(w, b);
            return (performance.now() - t0) / 50; }""", [inp["sections"][0]["width"], inp["sections"][0]["blocks"][0]])
        for name in strategies:
            plan = STRATEGIES[name]
            E = Evaluator(page, dpr, metrics["quantized"], criterion)
            after = copy.deepcopy(inp)
            after["variant"] = f"adjust:{criterion}:{name}"
            after.setdefault("derivedFrom", []).append({"variant": inp.get("variant"), "sha256": tbm.sha256_bytes(tbm.canon(inp).encode()),
                                                        "adjust": {"criterion": criterion, "strategy": name, "plan": plan}})
            sdir = out / criterion / name
            sdir.mkdir(parents=True, exist_ok=True)
            rows = []
            t_all = time.perf_counter()
            for s_in, s_out in zip(inp["sections"], after["sections"]):
                for blk, blk_out in zip(s_in["blocks"], s_out["blocks"]):
                    m0, p0, t0 = E.measures, E.probes, time.perf_counter()
                    st = {"text": blk["text"], "box": dict(blk["box"]), "scale": blk["style"]["fontScale"]}
                    cur = E(s_in, blk, st)
                    row = {"key": f"{s_in['key']}/b{blk['blockId']}", "before": {"over": cur["over"], "sides": cur.get("sides"),
                                                                                "ok": cur["ok"]}}
                    if not cur["ok"]:
                        row.update(status="measure_failed", reason=cur.get("reason"))
                    elif not cur["over"]:
                        row.update(status="not_needed")
                    else:
                        ctx = {"obstacles": obstacles.get(s_in["key"], []),
                               "ignore": {blk["blockId"]} | {o["blockId"] for o in obstacles.get(s_in["key"], []) if intersects(o, blk["box"])}}
                        info_steps = []
                        for op, params in plan:
                            st, cur, info = STEPS[op](E, s_in, blk, st, cur, params, ctx)
                            info_steps.append((op, info))
                        row.update(status="resolved" if not cur["over"] else "unresolved", steps=info_steps,
                                   final={"scale": st["scale"], "box": st["box"], "lineBreakApplied": st["text"] != blk["text"],
                                          "sides": cur.get("sides"), "lines": cur.get("lines")})
                        if cur["over"]:  # 해소될 때만 적용 — 못 고친 조정은 원래 입력으로 둔다(시도한 상태는 기록만)
                            row["causes"] = unresolved_causes(s_in, blk, st, cur, info_steps)
                            row.update(measures=E.measures - m0, probes=E.probes - p0, ms=round((time.perf_counter() - t0) * 1000, 2))
                            rows.append(row)
                            continue
                        blk_out["text"], blk_out["box"] = st["text"], st["box"]
                        blk_out["style"] = {**blk_out["style"], "fontScale": st["scale"]}
                        blk_out.setdefault("aux", {})["adjust"] = {"strategy": name, "criterion": criterion,
                                                                   "systemFontScale": st["scale"],
                                                                   "lineBreakApplied": st["text"] != blk["text"],
                                                                   "originalBox": blk["box"]}
                    row.update(measures=E.measures - m0, probes=E.probes - p0, ms=round((time.perf_counter() - t0) * 1000, 2))
                    rows.append(row)
            total = time.perf_counter() - t_all
            (sdir / "after-input.json").write_text(json.dumps(after, ensure_ascii=False, indent=1), encoding="utf-8")
            with (sdir / "decisions.jsonl").open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            meta = {"tool": "typeset-adjust-experiment/1", "criterion": criterion, "strategy": name, "plan": plan,
                    "candidateValues": {"precisionScale": PRECISION, "precisionWidthPx": WIDTH_PRECISION, "gapPx": 0,
                                        "quantAllowPx": tbm.QUANT_ALLOW, "verifyTolerancePx": tbm.TOL},
                    "input": str(inp_path), "inputSha256": tbm.sha256_bytes(inp_path.read_bytes()),
                    "browser": browser.version, "platform": tbm.platform_info(), "metricsProbe": metrics, "dpr": dpr,
                    "fe": {"git": bundle.fe_git, "modules": bundle.fe_manifest}, "measureMsInPage": round(bench, 3),
                    "totalSeconds": round(total, 2), "measures": E.measures, "probes": E.probes, "consoleProblems": problems,
                    "status": dict(collections.Counter(r["status"] for r in rows))}
            (sdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            print(json.dumps({"strategy": name, **{k: meta[k] for k in ("status", "measures", "probes", "totalSeconds")}}, ensure_ascii=False))
        browser.close()


# ───────────────────────── report ─────────────────────────

def _q(v: list[float], qs=(0, .1, .5, .9, 1)) -> list[float] | None:
    v = sorted(x for x in v if x is not None)
    return [round(v[int(q * (len(v) - 1))], 4) for q in qs] if v else None


def _ink_hits(ink: dict[str, float] | None, obstacles: list[dict[str, float]], ignore: set[int]) -> int:
    if not ink:
        return 0
    r = {"x": ink["left"], "y": ink["top"], "w": ink["right"] - ink["left"], "h": ink["bottom"] - ink["top"]}
    return sum(1 for o in obstacles if o["blockId"] not in ignore and intersects(r, o))


EDGE_LEVEL = 32  # 배경 경계 강도(0~255) — 실험 분류용 후보값
BUSY_RATIO = 0.05  # 원래 영역 밖 글자 자리의 경계 픽셀 비율이 이 이상이면 「무늬 · 사진 위」로 본다 — 실험 분류용 후보값


class Backgrounds:
    """섹션 배경(렌더 입력 배경 = 인페인팅 결과 또는 원본)의 경계 지도. 글자가 원래 영역 밖에 놓인 자리가 단색 여백인지
    사진 · 그래픽 위인지 어림한다(장애물 판정은 text_block 박스만 보므로 이미지 요소와의 겹침을 보완하는 근사)."""

    def __init__(self, inp: dict[str, Any]):
        self.paths = {s["key"]: ROOT / s["background"]["path"] for s in inp["sections"]}
        self.cache: dict[str, Any] = {}

    def edges(self, key: str):
        if key not in self.cache:
            from PIL import Image, ImageFilter
            im = Image.open(self.paths[key]).convert("L").filter(ImageFilter.FIND_EDGES)
            self.cache = {key: im.point(lambda v: 255 if v >= EDGE_LEVEL else 0)}
        return self.cache[key]

    def spill_busy(self, key: str, ink: dict[str, float] | None, box: dict[str, float]) -> float | None:
        """글자 ink 중 원래 박스 밖 부분의 경계 픽셀 비율. 밖으로 나간 글자가 없으면 None."""
        if not ink:
            return None
        e = self.edges(key)
        x0, y0 = max(0, math.floor(ink["left"])), max(0, math.floor(ink["top"]))
        x1, y1 = min(e.width, math.ceil(ink["right"])), min(e.height, math.ceil(ink["bottom"]))
        if x1 <= x0 or y1 <= y0:
            return None
        crop = e.crop((x0, y0, x1, y1)).copy()
        from PIL import ImageDraw
        bx0, by0 = box["x"] - x0, box["y"] - y0
        ImageDraw.Draw(crop).rectangle([math.floor(bx0), math.floor(by0), math.ceil(bx0 + box["w"]) - 1, math.ceil(by0 + box["h"]) - 1],
                                       fill=128)  # 원래 박스 안은 셈에서 뺀다
        hist = crop.histogram()
        outside = sum(hist) - hist[128]
        return None if outside <= 0 else hist[255] / outside


def report(out: Path, before_dir: Path) -> dict[str, Any]:
    B = {r["key"]: r for r in json.loads((before_dir / "measure.json").read_text(encoding="utf-8"))["blocks"]}
    obstacles = json.loads((out / "obstacles.json").read_text(encoding="utf-8"))
    bgs = Backgrounds(json.loads((out.parent / "input.json").read_text(encoding="utf-8")))
    C = collections.Counter
    res: dict[str, Any] = {"before": str(before_dir), "strategies": {}}
    for meta_path in sorted(out.glob("*/*/meta.json")):
        sdir = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dec = {r["key"]: r for r in map(json.loads, (sdir / "decisions.jsonl").read_text(encoding="utf-8").splitlines())}
        mpath = sdir / "measure" / "measure.json"
        A = {r["key"]: r for r in json.loads(mpath.read_text(encoding="utf-8"))["blocks"]} if mpath.exists() else {}
        target = [k for k, r in B.items() if r["primary"]["ok"] and r["primary"]["overflow"]["any"]]
        tried = [k for k in target if dec.get(k, {}).get("status") in ("resolved", "unresolved")]
        applied = [k for k in tried if dec[k]["status"] == "resolved"]  # 해소될 때만 적용(못 고친 블록은 원래 입력)
        s: dict[str, Any] = {"criterion": meta["criterion"], "plan": meta["plan"], "searchStatus": meta["status"],
                             "baselineEngineOverflow": len(target), "tried": len(tried), "applied": len(applied)}
        # 탐색 비용
        costs = [dec[k] for k in tried]
        s["cost"] = {"measuresPerBlock": _q([c["measures"] for c in costs]), "probesPerBlock": _q([c["probes"] for c in costs]),
                     "msPerBlock": _q([c["ms"] for c in costs]), "totalSeconds": meta["totalSeconds"],
                     "measureMsInPage": meta["measureMsInPage"], "allMeasures": meta["measures"]}
        s["causes"] = dict(C(c for k in tried if dec[k]["status"] == "unresolved" for c in dec[k].get("causes", [])))
        steps = C()
        for k in tried:
            for op, info in dec[k].get("steps", []):
                if info.get("applied"):
                    steps[op] += 1
        s["stepsApplied"] = dict(steps)
        if A:
            def eng_fit(k):
                return A[k]["primary"]["ok"] and not A[k]["primary"]["overflow"]["any"]
            s["after"] = {
                "engineFit": sum(eng_fit(k) for k in target),
                "judgement": dict(C(A[k]["judgement"] for k in target)),
                "pixelInsideOrFit": sum(A[k]["judgement"] in ("fit", "overflow_quantized", "overflow_subpixel") for k in target),
                "untouchedChanged": sum(1 for k, r in B.items() if k not in target and r["primary"]["ok"]
                                        and A.get(k, {}).get("primary", {}).get("ink") != r["primary"]["ink"]),
            }
            # 부작용 — 글자 크기(원본 px · 375 기준 환산), 확장량, 겹침, 섹션 · 최종 이미지 경계, 줄 수, 내용 보존
            # 부작용은 실제로 적용된(해소된) 블록만 본다
            sc = [A[k]["style"]["fontScale"] for k in applied]
            px375 = [A[k]["primary"]["fontPx"] * 375 / A[k].get("sectionSize", {}).get("width", 1000)
                     for k in applied if A[k]["primary"]["ok"]]
            grow_w = [A[k]["box"]["w"] / B[k]["box"]["w"] for k in applied]
            grow_h = [A[k]["box"]["h"] / B[k]["box"]["h"] for k in applied]

            def hits(rows, k):
                sec = rows[k]["sectionKey"]
                ign = {rows[k]["blockId"]} | {o["blockId"] for o in obstacles.get(sec, []) if intersects(o, B[k]["box"])}
                return _ink_hits(rows[k]["primary"].get("ink"), obstacles.get(sec, []), ign)

            strip = lambda t: "".join(t.split())  # noqa: E731
            s["sideEffects"] = {
                "fontScale": _q(sc), "scaledBlocks": sum(x < 1 for x in sc),
                "fontPx375": _q(px375), "below375px": {f"<{t}px": sum(v < t for v in px375) for t in (10, 12, 14)},
                "widthGrowth": _q([g for g in grow_w if g > 1]), "heightGrowth": _q([g for g in grow_h if g > 1]),
                "expanded": sum(1 for g, h in zip(grow_w, grow_h) if g > 1 or h > 1),
                "lineBreakApplied": sum(A[k]["text"] != B[k]["text"] for k in applied),
                "inkOverlapsOtherBox": {"before": sum(hits(B, k) > 0 for k in applied), "after": sum(hits(A, k) > 0 for k in applied)},
                "sectionClip": {"before": sum("section_clip" in B[k]["flags"] for k in applied), "after": sum("section_clip" in A[k]["flags"] for k in applied)},
                "finalImage": {"before": sum("final_image" in B[k]["flags"] for k in applied), "after": sum("final_image" in A[k]["flags"] for k in applied)},
                "linesIncreased": sum(1 for k in applied if A[k]["primary"]["ok"] and B[k]["primary"]["ok"]
                                      and len(A[k]["primary"]["lines"]) > len(B[k]["primary"]["lines"])),
                "contentMismatch": sum(1 for k in applied if A[k]["primary"]["ok"]
                                       and strip("".join(l["text"] for l in A[k]["primary"]["lines"])) != strip(A[k]["text"])),
                "measureFailedAfter": sum(1 for k in applied if not A[k]["primary"]["ok"]),
            }
            # 원래 박스 밖에 놓인 글자 자리의 배경(단색 여백인가, 사진 · 그래픽 위인가) — 높이 확장은 그림을 바꾸지 않고 판정 영역만 넓힌다
            spill = {k: bgs.spill_busy(A[k]["sectionKey"], A[k]["primary"].get("ink"), B[k]["box"]) for k in applied if A[k]["primary"]["ok"]}
            sv = [v for v in spill.values() if v is not None]
            s["sideEffects"]["spill"] = {"outsideOriginalBox": len(sv), "overTextured": sum(v >= BUSY_RATIO for v in sv),
                                         "busyRatio": _q(sv), "edgeLevel": EDGE_LEVEL, "busyRatioCut": BUSY_RATIO,
                                         "texturedKeys": sorted(k for k, v in spill.items() if v is not None and v >= BUSY_RATIO)}
        res["strategies"][f"{meta['criterion']}/{meta['strategy']}"] = s
    for crit in ("quant_aware", "engine"):
        p = out / crit / "NEED" / "decisions.jsonl"
        if p.exists():
            rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
            vals = [r["final"]["scale"] if r["status"] == "resolved" else None for r in rows if r["status"] in ("resolved", "unresolved")]
            n = len(vals)
            res[f"needScale:{crit}"] = {"blocks": n, "cumulativeFit": {f">={t}": round(sum(1 for v in vals if v is not None and v >= t) / n, 3)
                                                                         for t in (0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5)} if n else {},
                                        "notFitAt0.30": sum(1 for v in vals if v is None), "quantiles": _q([v for v in vals if v is not None])}
    (out / "report.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("search")
    a.add_argument("--input", type=Path, required=True)
    a.add_argument("--fe", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--criterion", choices=("quant_aware", "engine"), default="quant_aware")
    a.add_argument("--strategy", action="append", choices=sorted(STRATEGIES))
    a.add_argument("--dpr", type=float, default=2.0)
    a = sub.add_parser("report")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--before", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "search":
        search(args.input, args.fe, args.out, args.criterion, args.strategy or list(STRATEGIES), args.dpr)
    else:
        print(json.dumps(report(args.out, args.before), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
