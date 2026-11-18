"""⑨ 조판 자동 조정 실험 — 육안 검수 페이지 생성 · 결과 집계(2026-11-18). 개발용, 운영 경로 아님.

    # 검토 페이지(<run>/check/index.html + 확대 그림 check/img/). 회차 폴더를 정적 서버로 열어 /check/ 로 본다
    python docs/ai-experiments/tools/typeset_adjust_review.py page --run docs/ai-experiments/runs/2026-11-16_09-typeset_auto-adjust [--main quant_aware/BWES90D]
    # 내보낸 TSV 집계(검증에 실패하면 결과를 만들지 않고 종료 코드 1)
    python docs/ai-experiments/tools/typeset_adjust_review.py aggregate --run <같은 회차> --tsv <내보낸 TSV> [--main <페이지를 만든 후보>]

입력: run_adjust.sh · run_fit.sh · run_widen.sh 결과(input.json · before/ · search/<기준>/<후보>/). 읽기만 하고 <run>/check/에 쓴다.
페이지를 만들기 전에 typeset_adjust_experiment.check_provenance로 입력 연결 · 측정 조건을 확인한다(끊기면 만들지 않는다).

묶음(기준 후보 --main, 기본 BWES90D = 줄바꿈 → 폭 우선 → 확장 → 축소 −10%, 갱신 장애물 · 해소될 때만 적용):
1 spill      원래 영역 밖에 놓인 글자 중 배경 경계가 많은(사진 · 무늬 위로 어림한) 블록 전수 + 단색 여백으로 어림한 대조 일부
2 widen      폭을 넓힌 블록 전수
3 shrink     글자를 축소한 블록 전수
4 linebreak  줄바꿈 위치를 바꾼 블록 전수
5 unresolved 끝내 해소하지 못한 블록 전수(원래 입력 그대로 — 조정 전 그림만)
6 fit        박스 자동 맞춤(FIT): 맞춘 블록 전수 + 글자 위치가 달라져 기각된 블록(제안 박스만 표시)
7 overlap    기준 후보에서 블록끼리 새로 겹친 블록(최종 자리 기준 자동 집계)
그림은 섹션 합성(배경 · 글자색 그대로, 덧그림 없음, renderDpr 배율)에서 조정 전후를 같은 자리로 잘라 낸 것이다. 선(원래 영역 · 조정 뒤
영역 · 이웃 블록)은 그림에 굽지 않고 페이지에서 겹쳐 그려 끌 수 있다. 묶음 안 순서는 키의 SHA-256 순 — 앞에서부터 일부만 보아도
무작위 표본이 된다. 사람 판단은 본 사례에 대한 것이다(전수 · 다른 상품군으로 일반화하지 않는다).
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import json
import sys
from pathlib import Path
from typing import Any, Callable

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import typeset_adjust_experiment as ae  # noqa: E402

tbm = ae.tbm
MAIN, FIT = "quant_aware/BWES90D", "quant_aware/FIT"
SOLID_CONTROL = 10  # 단색 여백으로 어림한 대조 수(키 순 같은 간격)
GENERIC = (("ok", "1"), ("minor", "2"), ("bad", "3"), ("unsure", "4"))
GROUPS: dict[str, dict[str, Any]] = {
    "spill": {
        "title": "사진 · 무늬 위 글자", "question": "원래 영역(빨간 선) 밖으로 나온 글자가 배경 위에서 문제없이 읽히나요?",
        "why": "높이 확장은 그림을 바꾸지 않고 판정 영역만 넓힌다 — 원래 넘친 글자가 그대로 배경 위에 남는다. "
               "도구는 배경 경계 밀도로 「사진 · 무늬 위」를 어림했다(후보값). 단색 여백으로 어림한 대조가 섞여 있다.",
        "labels": ("문제없음", "거슬리지만 읽힘", "읽기 어렵거나 그림을 가림", "판단 불가"),
        "memo": "예: 제품 사진 위 · 아래 문단과 붙어 보임"},
    "widen": {
        "title": "폭 확장", "question": "폭을 넓힌 결과(오른쪽)가 디자인상 괜찮나요?",
        "why": "높이보다 폭을 먼저 넓혀 줄 수를 줄였다(겹치지 않는 여백 안 · 폭 한도 없음). 너무 넓어져 섹션 구성과 어긋나는지, "
               "원문 줄 구성(예: 두 줄 제목이 한 줄)이 바뀐 게 괜찮은지 본다. 섹션 전체 그림을 함께 본다.",
        "labels": ("괜찮음", "넓어진 게 보이지만 괜찮음", "너무 넓거나 구성이 깨짐", "판단 불가"),
        "memo": "예: 옆 블록과 열이 어긋남 · 제목이 한 줄로 길어짐"},
    "shrink": {
        "title": "글자 축소", "question": "글자를 줄인 결과(오른쪽)가 원래(왼쪽)와 비교해 괜찮나요?",
        "why": "축소 하한 −10%는 잠정값이다. 크기 차이가 눈에 띄는지, 같은 섹션의 위계(제목 > 본문)가 깨지는지 본다. 섹션 전체 그림을 함께 본다.",
        "labels": ("괜찮음", "작아진 게 보이지만 괜찮음", "너무 작거나 위계가 깨짐", "판단 불가"),
        "memo": "예: 옆 캡션보다 작아짐 · 모바일에서 안 읽힐 듯"},
    "linebreak": {
        "title": "줄바꿈 위치", "question": "줄을 바꾼 위치(오른쪽)가 자연스럽나요?",
        "why": "줄 수는 늘리지 않고 끊는 위치만 바꿨다(글자는 그대로). 의미 단위가 깨지는지, 한 단어만 남는 줄 · 해시태그 · 숫자 · 단위가 쪼개지는지 본다.",
        "labels": ("자연스러움", "어색하지만 괜찮음", "고쳐야 함", "판단 불가"),
        "memo": "예: 'SPF 50+'가 쪼개짐 · 마지막 줄 한 단어"},
    "unresolved": {
        "title": "못 고친 넘침", "question": "조정하지 못한 이 넘침이 실제 화면에서 문제인가요?",
        "why": "줄바꿈 · 확장 · 축소(−10%까지)로도 해소되지 않아 원래 입력 그대로 둔 블록이다(그림은 조정 전). "
               "실제로 얼마나 망가졌는지와, 어떤 처리가 맞을지(메모)를 본다.",
        "labels": ("그대로 둬도 됨", "거슬림", "고쳐야 함(잘림 · 겹침 · 읽기 어려움)", "판단 불가"),
        "memo": "어떤 처리가 맞을지 — 예: 더 축소 · 옆으로 확장 · 사용자 수정 요청"},
    "fit": {
        "title": "박스 자동 맞춤", "question": "맞춘 박스(파란 점선)가 글자에 알맞나요?",
        "why": "넘치지 않는 블록의 박스를 글자에 맞게 줄였다(그림 · 판정 불변을 자동 확인). 편집할 때 박스가 너무 꽉 끼거나 엉뚱한지 본다. "
               "「기각」 표시는 글자 위치가 달라져 적용하지 않은 제안 박스다.",
        "labels": ("알맞음", "꽉 끼거나 여유가 크지만 괜찮음", "글자를 벗어나거나 잘못 맞춤", "판단 불가"),
        "memo": "예: 아래 여백이 너무 없음 · 가운데 정렬인데 한쪽으로 치우침"},
    "overlap": {
        "title": "블록 간 겹침", "question": "조정 뒤(오른쪽) 글자가 이웃 블록(주황 선)과 겹쳐 보이나요?",
        "why": "조정 뒤 글자가 다른 블록의 자리(박스 ∪ 글자)와 새로 겹친다고 자동 집계된 블록이다(겹침 깊이는 도구 값). 실제로 붙거나 겹쳐 보이는지 본다.",
        "labels": ("안 겹쳐 보임", "닿아 보이지만 괜찮음", "겹쳐 보임", "판단 불가"),
        "memo": "예: 아래 블록 첫 줄과 붙음"},
}
ORDER = tuple(GROUPS)


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _rows(path: Path) -> dict[str, dict[str, Any]]:
    return {r["key"]: r for r in json.loads(path.read_text(encoding="utf-8"))["blocks"]}


def _dec(path: Path) -> dict[str, dict[str, Any]]:
    return {r["key"]: r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())}


def load(run: Path, main: str = MAIN) -> dict[str, Any]:
    out = run / "search"
    ae.check_provenance(out, run / "before", False)
    L: dict[str, Any] = {"run": run, "inp": json.loads((run / "input.json").read_text(encoding="utf-8")),
                         "B": _rows(run / "before" / "measure.json"), "obstacles": json.loads((out / "obstacles.json").read_text(encoding="utf-8")),
                         "meta": {"before": json.loads((run / "before" / "measure.json").read_text(encoding="utf-8"))["meta"]}, "V": {},
                         "main": main}
    for v in (main, FIT):
        d = out / v
        if not (d / "measure" / "measure.json").exists():
            raise SystemExit(f"{v}의 재측정이 없다 — run_adjust.sh · run_fit.sh · run_widen.sh를 먼저 실행한다")
        L["V"][v] = {"A": _rows(d / "measure" / "measure.json"), "dec": _dec(d / "decisions.jsonl")}
        L["meta"][v] = json.loads((d / "measure" / "measure.json").read_text(encoding="utf-8"))["meta"]
    return L


def _even(keys: list[str], n: int) -> list[str]:
    keys = sorted(keys)
    k = min(n, len(keys))
    return [keys[int(i * len(keys) / k)] for i in range(k)] if k else []


def neighbors(A: dict[str, dict[str, Any]], obstacles, key: str) -> list[dict[str, Any]]:
    """블록 글자(ink)와 맞닿지 않고 겹치는 다른 블록의 차지 자리(박스 ∪ ink, 측정 결과 기준)."""
    r = A[key]
    if not r["primary"]["ok"] or not r["primary"].get("ink"):
        return []
    ink = ae.ink_rect(r["primary"]["ink"])
    occ = ae._occ_rows(A, obstacles)[r["sectionKey"]]
    return [o for o in occ if o["blockId"] != r["blockId"] and ae.intersects(ink, o)]


def pick(L: dict[str, Any], spill: Callable[[str, dict | None, dict], float | None]) -> list[dict[str, Any]]:
    """묶음별 사례. 각 사례: group · key · left/right(그림을 가져올 측정: 'before' 또는 후보 이름, 없으면 None) ·
    rects(그림 위 선: kind orig/adj/prop/nb, side L/R) · info(답하기 전에 보여 줄 사실) · tool(답한 뒤 펼치는 도구 값)."""
    B, obs, main = L["B"], L["obstacles"], L["main"]
    A, dec = L["V"][main]["A"], L["V"][main]["dec"]
    applied = sorted(k for k, d in dec.items() if d["status"] == "resolved")
    cases: list[dict[str, Any]] = []

    def px375(r):
        return r["primary"]["fontPx"] * 375 / r["sectionSize"]["width"] if r["primary"]["ok"] else None

    def base(g, k, left, right, rects, info, tool, **extra):
        cases.append({"group": g, "key": k, "left": left, "right": right, "rects": rects, "info": info, "tool": tool, **extra})

    def lr(k, right_rows, adj_side="R"):
        out = [{"kind": "orig", "side": "L", **B[k]["box"]}]
        if right_rows is not None:
            out.append({"kind": "orig", "side": "R", **B[k]["box"]})
            if right_rows[k]["box"] != B[k]["box"]:
                out.append({"kind": "adj", "side": adj_side, **right_rows[k]["box"]})
        return out

    def size(b):
        return f"{b['w']:.0f}×{b['h']:.0f}"

    def judge(k, rows):
        return f"{B[k]['judgement']} → {rows[k]['judgement']}"

    # 1 spill
    sp = {k: spill(A[k]["sectionKey"], A[k]["primary"].get("ink"), B[k]["box"]) for k in applied if A[k]["primary"]["ok"]}
    busy = [k for k, v in sp.items() if v is not None and v >= ae.BUSY_RATIO]
    solid = _even([k for k, v in sp.items() if v is not None and v < ae.BUSY_RATIO], SOLID_CONTROL)
    for k in busy + solid:
        grow = "높이" if A[k]["box"]["h"] > B[k]["box"]["h"] else ("폭" if A[k]["box"]["w"] > B[k]["box"]["w"] else "확장 없음")
        base("spill", k, "before", main, lr(k, A), f"영역 {size(B[k]['box'])} → {size(A[k]['box'])}({grow}) · 배율 {A[k]['style']['fontScale']:.3f}",
             f"도구 어림: {'사진 · 무늬 위' if k in busy else '단색 여백(대조)'} · 경계 비율 {sp[k]:.3f}(기준 {ae.BUSY_RATIO}) · 판정 {judge(k, A)}",
             control=k in solid, thumb=main)
    # 2 widen
    for k in applied:
        if A[k]["box"]["w"] > B[k]["box"]["w"] + 1e-6:
            src = B[k].get("aux", {}).get("srcLines")
            base("widen", k, "before", main, lr(k, A),
                 f"폭 {B[k]['box']['w']:.0f} → {A[k]['box']['w']:.0f}px({A[k]['box']['w'] / B[k]['box']['w']:.2f}배) · 높이 {B[k]['box']['h']:.0f} → "
                 f"{A[k]['box']['h']:.0f}px · 줄 수 원문 {src} → 조정 뒤 {len(A[k]['primary']['lines'])} · 정렬 {B[k]['style']['align']}",
                 f"판정 {judge(k, A)} · 배율 {A[k]['style']['fontScale']:.3f}", thumb=main)
    # 3 shrink
    for k in applied:
        if A[k]["style"]["fontScale"] < B[k]["style"]["fontScale"] - 1e-9:
            base("shrink", k, "before", main, lr(k, A),
                 f"배율 {B[k]['style']['fontScale']:.3f} → {A[k]['style']['fontScale']:.3f} · 글자 {B[k]['primary']['fontPx']:.1f} → "
                 f"{A[k]['primary']['fontPx']:.1f}px(375 기준 {px375(B[k]):.1f} → {px375(A[k]):.1f}px)",
                 f"판정 {judge(k, A)} · 영역 {size(B[k]['box'])} → {size(A[k]['box'])}", thumb=main)
    # 4 linebreak
    for k in applied:
        if A[k]["text"] != B[k]["text"]:
            base("linebreak", k, "before", main, lr(k, A), "", f"판정 {judge(k, A)} · 배율 {A[k]['style']['fontScale']:.3f}",
                 lines=[[ln["text"] for ln in B[k]["primary"]["lines"]], [ln["text"] for ln in A[k]["primary"]["lines"]]])
    # 5 unresolved
    for k in sorted(k for k, d in dec.items() if d["status"] == "unresolved"):
        d = dec[k]
        base("unresolved", k, "before", None, lr(k, None),
             f"초과 최대 {B[k]['primary']['excess']['max']:.0f}px · 방향 {'·'.join(s for s in tbm.SIDES if B[k]['primary']['overflow'][s])} · 줄 {len(B[k]['primary']['lines'])}",
             f"원인 {', '.join(d.get('causes', []))} · 마지막 시도: 배율 {d['final']['scale']:.3f} · 영역 {size(d['final']['box'])}", thumb="before")
    # 6 fit
    Af, decf = L["V"][FIT]["A"], L["V"][FIT]["dec"]
    for k in sorted(decf):
        f = decf[k].get("fit") or {}
        if f.get("applied"):
            base("fit", k, "before", FIT, lr(k, Af), f"영역 {size(B[k]['box'])} → {size(Af[k]['box'])} · 면적 {f['areaRatio']:.2f}배",
                 f"판정 {judge(k, Af)} · 정렬 {B[k]['style']['align']}")
        elif "sameLayout" in f and not f["sameLayout"]:
            base("fit", k, "before", None, lr(k, None) + [{"kind": "prop", "side": "L", **f["box"]}],
                 f"기각 — 적용 안 됨 · 제안 영역 {size(B[k]['box'])} → {size(f['box'])} · 면적 {f['areaRatio']:.2f}배",
                 f"글자 위치가 달라져 기각 · 정렬 {B[k]['style']['align']}", rejected=True)
    # 7 overlap
    for k in applied:
        ov = ae.new_overlaps(A, B, obs, [k])
        if ov["blocks"]:
            rects = lr(k, A) + [{"kind": "nb", "side": "R", **{c: o[c] for c in "xywh"}} for o in neighbors(A, obs, k)]
            base("overlap", k, "before", main, rects, "", f"새 겹침 깊이 {ov['depthPx'][-1]:.2f}px · 정수화 범위 {tbm.QUANT_ALLOW}px")
    order = {g: i for i, g in enumerate(ORDER)}
    return sorted(cases, key=lambda c: (order[c["group"]], hashlib.sha256(f"{c['group']}:{c['key']}".encode()).hexdigest()))


# ───────────────────────── 그림 ─────────────────────────

def _section_png(run: Path, variant: str | None, sec_key: str, kind: str) -> Path:
    name = sec_key.replace("/", "_")
    d = run / "before" if variant == "before" else run / "search" / variant / "measure"
    return d / "sections" / f"{name}.{kind}.png"


def crop_region(c: dict[str, Any], L: dict[str, Any], sec: dict[str, Any], margin: float) -> tuple[float, float, float, float]:
    """사례 그림의 공통 자리(CSS px, 섹션 좌표) — 선 · 양쪽 측정의 ink를 모두 담고 여백을 둔다. 섹션 밖은 합성 여백(margin)까지."""
    xs, ys = [], []
    for r in c["rects"]:
        xs += [r["x"], r["x"] + r["w"]]
        ys += [r["y"], r["y"] + r["h"]]
    for v in (c["left"], c["right"]):
        rows = L["B"] if v == "before" else (L["V"][v]["A"] if v else None)
        ink = rows[c["key"]]["primary"].get("ink") if rows and rows[c["key"]]["primary"]["ok"] else None
        if ink:
            xs += [ink["left"], ink["right"]]
            ys += [ink["top"], ink["bottom"]]
    h = max(r["h"] for r in c["rects"])
    pad = max(24.0, h * 0.35)
    return (max(-margin, min(xs) - pad), max(-margin, min(ys) - pad),
            min(sec["width"] + margin, max(xs) + pad), min(sec["height"] + margin, max(ys) + pad))


def make_images(L: dict[str, Any], cases: list[dict[str, Any]], out: Path) -> None:
    from PIL import Image
    run = L["run"]
    secs = {s["key"]: s for s in L["inp"]["sections"]}
    rdpr = L["meta"]["before"]["renderDpr"]
    for v in L["V"]:
        if L["meta"][v]["renderDpr"] != rdpr:
            raise SystemExit(f"그림 배율이 다르다: before {rdpr} · {v} {L['meta'][v]['renderDpr']}")
    (out / "img").mkdir(parents=True, exist_ok=True)
    cache: dict[Path, Any] = {}

    def im(p: Path):
        if p not in cache:
            if len(cache) > 6:
                cache.clear()
            cache[p] = Image.open(p).convert("RGB")
        return cache[p]

    for c in cases:
        sk = L["B"][c["key"]]["sectionKey"]
        sec = secs[sk]
        margin = min((im(_section_png(run, v, sk, "plain")).width / rdpr - sec["width"]) / 2 for v in (c["left"], c["right"]) if v)
        x0, y0, x1, y1 = crop_region(c, L, sec, margin)
        c["crop"] = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "dpr": rdpr}
        c["img"] = {}
        for side, v in (("L", c["left"]), ("R", c["right"])):
            if not v:
                continue
            src = im(_section_png(run, v, sk, "plain"))
            g = (src.width / rdpr - sec["width"]) / 2
            box = tuple(round((a + g) * rdpr) for a in (x0, y0, x1, y1))
            name = f"img/{c['group']}_{c['key'].replace('/', '_')}.{side}.png"
            src.crop(box).save(out / name)
            c["img"][side] = name
        if c.get("thumb"):
            p = _section_png(run, c["thumb"], sk, "clean")
            c["thumbImg"] = {"src": Path("..", p.relative_to(run)).as_posix(), "w": sec["width"], "h": sec["height"]}


# ───────────────────────── 지문 · 페이지 ─────────────────────────

ENV_KEYS = ("tool", "inputSha256", "browser", "platform", "metricsProbe", "dpr", "renderDpr", "fonts", "toolAssets")


def fingerprint(L: dict[str, Any], cases: list[dict[str, Any]], out: Path | None) -> str:
    """검토 지문 — 사용한 측정들의 환경 · 입력 지문, 사례(묶음 · 키 · 선 · 보여 준 사실), 확대 그림 바이트. 하나라도 바뀌면 이전 TSV를 거절한다."""
    env = {v: {k: m.get(k) for k in ENV_KEYS} for v, m in sorted(L["meta"].items())}
    items = []
    for c in cases:
        imgs = {s: hashlib.sha256((out / p).read_bytes()).hexdigest() for s, p in sorted(c.get("img", {}).items())} if out else None
        items.append([c["group"], c["key"], c["left"], c["right"], c["rects"], c["info"], c.get("lines"), imgs])
    raw = json.dumps({"scheme": 1, "env": env, "cases": items}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def case_id(c: dict[str, Any]) -> str:
    return f"{c['group']}:{c['key']}"


def build_page(run: Path, main: str = MAIN) -> Path:
    L = load(run, main)
    bg = ae.Backgrounds(L["inp"])
    cases = pick(L, bg.spill_busy)
    out = run / "check"
    make_images(L, cases, out)
    fp = fingerprint(L, cases, out)
    n_in: collections.Counter = collections.Counter()
    data = []
    for c in cases:
        n_in[c["group"]] += 1
        b = L["B"][c["key"]]
        data.append({"id": case_id(c), "g": c["group"], "n": n_in[c["group"]], "key": c["key"], "text": b["text"], "role": b["role"],
                     "img": c.get("img", {}), "crop": c["crop"], "rects": c["rects"], "thumb": c.get("thumbImg"),
                     "info": c["info"], "tool": c["tool"], "lines": c.get("lines"), "control": c.get("control", False),
                     "rejected": c.get("rejected", False),
                     "labels": {"L": "조정 전" if c["left"] == "before" else c["left"].split("/")[1],
                                "R": (c["right"].split("/")[1] if c["right"] else None)}})
    groups = [{"id": g, **{k: v for k, v in GROUPS[g].items()}, "codes": [x for x, _ in GENERIC], "n": n_in[g]} for g in ORDER]
    page = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")) \
        .replace("__GROUPS__", json.dumps(groups, ensure_ascii=False).replace("</", "<\\/")) \
        .replace("__KEY__", json.dumps(f"typeset-adjust-review:{fp}")).replace("__FP__", json.dumps(fp)) \
        .replace("__META__", esc(f"기준 후보 {main} · Chromium {L['meta']['before']['browser']['version']} · "
                                 f"그림 배율 {L['meta']['before']['renderDpr']} · 지문 {fp}"))
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "cases.json").write_text(json.dumps({"fingerprint": fp, "main": main, "cases": [
        {k: c[k] for k in ("group", "key", "left", "right", "info", "tool")} | {"control": c.get("control", False), "rejected": c.get("rejected", False)}
        for c in cases]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out / "index.html"


# ───────────────────────── 집계 ─────────────────────────

TSV_COLUMNS = ("case_no", "group", "key", "verdict", "note", "review_fingerprint")


def load_review(run: Path, tsv: Path, main: str = MAIN) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], str]:
    """TSV를 검증해 읽는다 — 열 · 지문 · 사례 · 판단값 · 중복 중 하나라도 어기면 종료. 지문은 페이지를 만들 때 쓴 그림까지 다시 계산한다."""
    L = load(run, main)
    cases = pick(L, ae.Backgrounds(L["inp"]).spill_busy)
    out = run / "check"
    for c in cases:  # 그림 이름은 make_images와 같은 규칙 — 다시 만들지 않고 있는 그림의 바이트로 지문을 낸다
        c["img"] = {s: f"img/{c['group']}_{c['key'].replace('/', '_')}.{s}.png" for s, v in (("L", c["left"]), ("R", c["right"])) if v}
        for p in c["img"].values():
            if not (out / p).is_file():
                raise SystemExit(f"검토 그림이 없다: {out / p} — page를 다시 만들고 다시 검토한다")
    fp = fingerprint(L, cases, out)
    return cases, read_tsv(tsv, fp, {case_id(c) for c in cases}), fp


def read_tsv(tsv: Path, fp: str, ids: set[str]) -> dict[str, dict[str, str]]:
    with tsv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    if not rows or set(TSV_COLUMNS) - set(rows[0]):
        raise SystemExit(f"TSV 열이 다르다 — 필요: {TSV_COLUMNS}")
    seen: dict[str, dict[str, str]] = {}
    for x in rows:
        cid = f"{x['group']}:{x['key']}"
        if x["review_fingerprint"] != fp:
            raise SystemExit(f"지문이 다르다({x['review_fingerprint']} ≠ {fp}) — 다른 결과로 만든 페이지의 검토다")
        if cid not in ids:
            raise SystemExit(f"검토 사례에 없다: {cid}")
        if x["verdict"] not in {c for c, _ in GENERIC} | {""}:
            raise SystemExit(f"판단 값이 다르다: {x['verdict']}")
        if cid in seen:
            raise SystemExit(f"같은 사례가 두 번 있다: {cid}")
        seen[cid] = x
    return seen


def summarize(cases: list[dict[str, Any]], seen: dict[str, dict[str, str]]) -> dict[str, Any]:
    res: dict[str, Any] = {"note": "사람 판단은 본 사례에 대한 것이다(전수 아님 · 이 표본 GS-01 · GS-02 한정). 묶음 안 순서는 무작위라 앞부분만 본 경우 무작위 표본이다",
                           "groups": {}}
    for g in ORDER:
        cs = [c for c in cases if c["group"] == g]
        labels = dict(zip((x for x, _ in GENERIC), GROUPS[g]["labels"]))
        sub: dict[str, Any] = {"title": GROUPS[g]["title"], "cases": len(cs),
                               "reviewed": sum(1 for c in cs if (seen.get(case_id(c)) or {}).get("verdict"))}
        split = {"spill": lambda c: "대조(단색 여백 어림)" if c.get("control") else "사진 · 무늬 위 어림",
                 "fit": lambda c: "기각 제안" if c.get("rejected") else "적용"}.get(g, lambda c: "전체")
        table: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for c in cs:
            v = (seen.get(case_id(c)) or {}).get("verdict") or ""
            table[split(c)][labels.get(v, "미검토")] += 1
        sub["verdicts"] = {k: dict(v) for k, v in table.items()}
        sub["memos"] = [{"key": c["key"], "verdict": labels.get(seen[case_id(c)]["verdict"], ""), "note": seen[case_id(c)]["note"]}
                        for c in cs if case_id(c) in seen and seen[case_id(c)]["note"].strip()]
        res["groups"][g] = sub
    return res


def aggregate(run: Path, tsv: Path, main: str = MAIN) -> dict[str, Any]:
    cases, seen, fp = load_review(run, tsv, main)
    res = {"fingerprint": fp, "main": main, **summarize(cases, seen)}
    (run / "check" / "review_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>자동 조정 육안 검수</title><style>
:root{--bg:#f4f5f2;--fg:#1f2326;--card:#fff;--line:#d9dcd6;--mut:#626a70;--ok:#2f8f55;--minor:#b7791f;--bad:#c8362f;--uns:#6d6a8a;--acc:#24567a}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Malgun Gothic",sans-serif}
header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:8px 16px}
.hrow{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center} header h1{font-size:17px;margin:0}
.bar{flex:1;min-width:140px;height:8px;background:#e6e8e3;border-radius:4px;overflow:hidden} .bar i{display:block;height:100%;background:var(--ok);width:0}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px} .tabs button{padding:4px 10px;font-size:13.5px} .tabs button[aria-selected=true]{background:var(--acc);color:#fff;border-color:var(--acc)}
.tabs small{opacity:.8;margin-left:4px}
button{font:inherit;cursor:pointer;border:1px solid #b9beb6;background:#fff;border-radius:6px;padding:6px 12px} button:hover{border-color:var(--acc)}
button:focus-visible,input:focus-visible{outline:3px solid #8cb7d6;outline-offset:2px}
main{max-width:1280px;margin:0 auto;padding:14px 16px 40px} .mut{color:var(--mut);font-size:13px}
details.guide{background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px 16px;margin-bottom:12px} details.guide summary{cursor:pointer;font-weight:600}
.guide li{margin:3px 0}
.case{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between} .top h2{margin:0;font-size:17px}
.why{font-size:13.5px;color:var(--mut);margin:6px 0 0} .q{font-size:18px;font-weight:600;margin:10px 0 4px}
.text{white-space:pre-wrap;background:#f1f3ef;border-radius:6px;padding:6px 10px;margin:6px 0;font-size:14px} .info{font-size:14px;margin:4px 0 10px}
.tag{display:inline-block;font-size:12px;border-radius:4px;padding:0 6px;margin-left:6px;background:#eef1f6;color:#33507a}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px} figure{margin:0;min-width:0}
figcaption{font-size:13px;color:var(--mut);margin-bottom:4px} .view{position:relative;border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:62vh;background:#888}
.view .wrap{position:relative;display:inline-block;line-height:0} .view img{display:block} .view svg{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}
.tools{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin:8px 0;font-size:13px}
.tools button{padding:2px 8px;font-size:12.5px} .tools button[aria-pressed=true]{background:var(--acc);color:#fff;border-color:var(--acc)}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin:0 4px 0 8px}
.lines{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:6px 0 10px} .lines ol{margin:2px 0;padding-left:22px;background:#f1f3ef;border-radius:6px;padding-top:4px;padding-bottom:4px}
.answers{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 8px} .answers button{flex:1;min-width:150px;padding:11px;font-size:15px;font-weight:600}
.answers button kbd{font:12px Consolas,monospace;border:1px solid currentColor;border-radius:4px;padding:0 5px;margin-left:6px;opacity:.7}
.answers .ok[aria-pressed=true]{background:var(--ok);border-color:var(--ok);color:#fff} .answers .minor[aria-pressed=true]{background:var(--minor);border-color:var(--minor);color:#fff}
.answers .bad[aria-pressed=true]{background:var(--bad);border-color:var(--bad);color:#fff} .answers .unsure[aria-pressed=true]{background:var(--uns);border-color:var(--uns);color:#fff}
.memo{display:flex;gap:8px;align-items:center} .memo input{flex:1;font:inherit;padding:6px 8px;border:1px solid #b9beb6;border-radius:6px}
details.tool{margin-top:10px;font-size:13.5px} details.tool summary{cursor:pointer;color:var(--mut)} .toolbox{background:#f3f5fa;border:1px dashed #9fb0d0;border-radius:6px;padding:8px 12px;margin-top:6px}
.saved{color:var(--ok);font-size:13px;min-width:60px} .nav{margin-top:14px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px 14px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px} .chips button{width:38px;padding:3px 0;font-size:12.5px}
.chips .ok{background:#dff1e5;border-color:var(--ok)} .chips .minor{background:#f6ead2;border-color:var(--minor)} .chips .bad{background:#f8dedc;border-color:var(--bad)} .chips .unsure{background:#e7e5f0;border-color:var(--uns)}
.chips .cur{outline:3px solid var(--acc)}
@media(max-width:760px){.lines{grid-template-columns:1fr} header h1{font-size:15px}}
</style></head><body>
<header><div class="hrow"><h1>자동 조정 육안 검수</h1><span id="cnt"></span><div class="bar"><i id="bar"></i></div>
<button id="next-todo">다음 미검토 ⏭</button><button id="exp">결과 내려받기(TSV)</button></div><div class="tabs" id="tabs" role="tablist"></div></header>
<main>
<details class="guide" id="guide" open><summary>무엇을 하나요? (처음 한 번 읽어 주세요)</summary><ol>
<li>자동 조정 실험(줄바꿈 → 폭 우선 → 영역 확장 → 글자 축소 −10%, 박스 자동 맞춤)의 결과를 묶음 7개로 나눴습니다. 위 탭으로 묶음을 고릅니다.
묶음마다 <b>질문 하나</b>에 4가지 중 하나로 답합니다.</li>
<li>그림은 실제 섹션 합성(배경 · 글자색 그대로)에서 <b>조정 전과 조정 후를 같은 자리</b>로 잘라 나란히 놓은 것입니다. 선은 그림 위에 겹쳐 그린 것이라
<b>L</b> 키 또는 "선 숨기기"로 끌 수 있습니다:
<span class="legend"><i style="border-color:#e0302a"></i>원래 영역<i style="border-color:#1f6fd1;border-top-style:dashed"></i>조정 뒤 영역<i style="border-color:#ef8a17"></i>이웃 블록</span></li>
<li>묶음 안 순서는 무작위입니다. 다 못 봐도 앞에서부터 본 만큼이 무작위 표본이 됩니다.</li>
<li>키보드 <b>1 · 2 · 3 · 4</b> 답, <b>←/→</b> 이전/다음, <b>L</b> 선 켜고 끄기. 답하면 같은 묶음의 다음 미검토 사례로 넘어갑니다.
입력은 이 브라우저에만 저장됩니다 — 끝나면(또는 중간에라도) <b>결과 내려받기(TSV)</b>로 파일을 전달해 주세요.</li>
</ol><p class="mut">"도구 값 보기"는 답한 뒤에 펼치기를 권합니다. · __META__</p></details>
<section class="case" id="case" aria-live="polite"></section>
<nav class="nav"><b>이 묶음의 사례</b> <span class="mut">회색 미검토 · 색은 답</span><div class="chips" id="chips"></div></nav>
</main>
<script>
const DATA=__DATA__, GROUPS=__GROUPS__, K=__KEY__, FP=__FP__, KEYS={ok:'1',minor:'2',bad:'3',unsure:'4'};
const G=Object.fromEntries(GROUPS.map(g=>[g.id,g]));
let st={}; try{st=JSON.parse(localStorage.getItem(K)||'{}')||{}}catch(e){st={}}
let grp=GROUPS.find(g=>g.n)?.id, cur=0, zoom='fit', lines=true;
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const items=()=>DATA.filter(d=>d.g===grp), done=id=>!!(st[id]&&st[id].v);
function save(){try{localStorage.setItem(K,JSON.stringify(st))}catch(e){}; chips(); progress()}
function progress(){const n=DATA.filter(d=>done(d.id)).length; $('#cnt').textContent=`전체 ${n} / ${DATA.length} 검토`; $('#bar').style.width=(100*n/DATA.length)+'%';
  $('#tabs').innerHTML=GROUPS.map((g,i)=>`<button role="tab" data-g="${g.id}" aria-selected="${g.id===grp}" ${g.n?'':'disabled'}>${i+1}. ${esc(g.title)}<small>${DATA.filter(d=>d.g===g.id&&done(d.id)).length}/${g.n}</small></button>`).join('');
  document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{grp=b.dataset.g; const it=items(), f=it.findIndex(x=>!done(x.id)); show(f>=0?f:0)})}
function chips(){const it=items(); $('#chips').innerHTML=it.map((d,i)=>{const v=(st[d.id]||{}).v||''; return `<button class="${v}${i===cur?' cur':''}" data-i="${i}">${i+1}</button>`}).join('');
  document.querySelectorAll('#chips button').forEach(b=>b.onclick=()=>show(+b.dataset.i))}
const COLOR={orig:['#e0302a',''],adj:['#1f6fd1','6 4'],prop:['#1f6fd1','6 4'],nb:['#ef8a17','']};
function svg(d,side,w,h,ox,oy,s){const r=d.rects.filter(r=>r.side===side||(!side)).map(r=>{const [c,da]=COLOR[r.kind];
  return `<rect x="${(r.x-ox)*s-1.5}" y="${(r.y-oy)*s-1.5}" width="${r.w*s+3}" height="${r.h*s+3}" fill="none" stroke="${c}" stroke-width="2" ${da?`stroke-dasharray="${da}"`:''} vector-effect="non-scaling-stroke"/>`}).join('');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="ov" ${lines?'':'style="display:none"'}>${r}</svg>`}
function fig(d,side){const src=d.img[side]; if(!src)return ''; const c=d.crop, w=c.w*c.dpr, h=c.h*c.dpr;
  return `<figure><figcaption>${side==='L'?'왼쪽':'오른쪽'} — ${esc(d.labels[side])}</figcaption><div class="view"><span class="wrap"><img class="zm" src="${src}" data-w="${w}" alt="${esc(d.labels[side])}">${svg(d,side,w,h,c.x,c.y,c.dpr)}</span></div></figure>`}
function thumb(d){const t=d.thumb; if(!t)return ''; const own=d.rects.filter(r=>r.side==='R'&&r.kind==='adj').concat(d.rects.filter(r=>r.kind==='orig').slice(0,1));
  const r=own.map(r=>{const [c,da]=COLOR[r.kind]; return `<rect x="${r.x-3}" y="${r.y-3}" width="${r.w+6}" height="${r.h+6}" fill="none" stroke="${c}" stroke-width="3" ${da?`stroke-dasharray="${da}"`:''} vector-effect="non-scaling-stroke"/>`}).join('');
  return `<figure><figcaption>섹션 전체 — 위계 · 주변 확인용</figcaption><div class="view"><span class="wrap"><img src="${t.src}" style="width:100%;min-width:260px" alt="섹션 전체"><svg viewBox="0 0 ${t.w} ${t.h}" preserveAspectRatio="none" class="ov" ${lines?'':'style="display:none"'}>${r}</svg></span></div></figure>`}
function setZoom(z){zoom=z; document.querySelectorAll('img.zm').forEach(im=>{im.style.width=z==='fit'?'100%':(im.dataset.w/DATA[0].crop.dpr*z)+'px'; im.parentElement.style.width=z==='fit'?'100%':'';
  im.style.imageRendering=z==='fit'||z===1?'auto':'pixelated'}); document.querySelectorAll('.zoom button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.z===String(z)))}
function toggleLines(){lines=!lines; document.querySelectorAll('svg.ov').forEach(s=>s.style.display=lines?'':'none'); const b=$('#tl'); if(b)b.textContent=lines?'선 숨기기 (L)':'선 보이기 (L)'}
function show(i){const it=items(); if(!it.length)return; cur=(i+it.length)%it.length; const d=it[cur], g=G[d.g], v=st[d.id]||{};
  const ln=d.lines?`<div class="lines"><div><b>전</b><ol>${d.lines[0].map(t=>`<li>${esc(t)}</li>`).join('')}</ol></div><div><b>후</b><ol>${d.lines[1].map(t=>`<li>${esc(t)}</li>`).join('')}</ol></div></div>`:'';
  $('#case').innerHTML=`<div class="top"><h2>${esc(g.title)} · ${cur+1} / ${it.length}${d.rejected?'<span class="tag">기각 제안</span>':''}</h2><div><button id="prev">◀ 이전</button> <button id="next">다음 ▶</button></div></div>
  <p class="why">${esc(g.why)}</p><p class="q">${esc(g.question)}</p><div class="text">${esc(d.text)}</div>
  ${d.info?`<div class="info">${esc(d.info)}</div>`:''}${ln}
  <div class="tools"><span class="zoom">배율 ${['fit',1,2,4].map(z=>`<button data-z="${z}">${z==='fit'?'맞춤':z+'배'}</button>`).join(' ')}</span><button id="tl">${lines?'선 숨기기 (L)':'선 보이기 (L)'}</button>
  <span class="legend mut"><i style="border-color:#e0302a"></i>원래 영역<i style="border-color:#1f6fd1;border-top-style:dashed"></i>조정 뒤${d.g==='overlap'?'<i style="border-color:#ef8a17"></i>이웃':''}</span></div>
  <div class="figs">${fig(d,'L')}${fig(d,'R')}${thumb(d)}</div>
  <div class="answers">${g.codes.map((k,j)=>`<button class="${k}" data-v="${k}" aria-pressed="${v.v===k}">${esc(g.labels[j])}<kbd>${KEYS[k]}</kbd></button>`).join('')}</div>
  <div class="memo"><input id="memo" placeholder="메모(선택)${g.memo?' — '+esc(g.memo):''}" value="${esc(v.n||'')}"><span class="saved" id="saved"></span></div>
  <details class="tool"><summary>도구 값 보기(답한 뒤 권장)</summary><div class="toolbox">${esc(d.key)} · ${esc(d.role)}<br>${esc(d.tool)}${d.control?'<br>대조 사례':''}</div></details>`;
  $('#prev').onclick=()=>show(cur-1); $('#next').onclick=()=>show(cur+1); $('#tl').onclick=toggleLines;
  document.querySelectorAll('.zoom button').forEach(b=>b.onclick=()=>setZoom(b.dataset.z==='fit'?'fit':+b.dataset.z)); setZoom(zoom);
  document.querySelectorAll('.answers button').forEach(b=>b.onclick=()=>answer(b.dataset.v));
  $('#memo').oninput=e=>{(st[d.id]=st[d.id]||{}).n=e.target.value; save(); $('#saved').textContent='저장됨'};
  progress(); chips()}
function answer(val){const it=items(), d=it[cur]; (st[d.id]=st[d.id]||{}).v=val; save();
  document.querySelectorAll('.answers button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===val)); $('#saved').textContent='저장됨';
  const nx=it.findIndex((x,i)=>i>cur&&!done(x.id)), first=it.findIndex(x=>!done(x.id)); const to=nx>=0?nx:first;
  if(to>=0&&to!==cur)setTimeout(()=>show(to),300); else if(to<0)$('#saved').textContent='이 묶음을 모두 검토함 — 다음 묶음 탭으로'}
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return; const m={'1':'ok','2':'minor','3':'bad','4':'unsure'}[e.key];
  if(m)answer(m); else if(e.key==='ArrowLeft')show(cur-1); else if(e.key==='ArrowRight')show(cur+1); else if(e.key==='l'||e.key==='L')toggleLines()});
$('#next-todo').onclick=()=>{let it=items(), i=it.findIndex((x,j)=>j>cur&&!done(x.id)); if(i<0)i=it.findIndex(x=>!done(x.id));
  if(i>=0)return show(i); const g=GROUPS.find(g=>DATA.some(d=>d.g===g.id&&!done(d.id))); if(g){grp=g.id; show(items().findIndex(x=>!done(x.id)))}};
$('#exp').onclick=()=>{let t='case_no\tgroup\tkey\tverdict\tnote\treview_fingerprint\n'; DATA.forEach(d=>{const v=st[d.id]; if(!v||!(v.v||v.n))return;
  t+=[d.n,d.g,d.key,v.v||'',(v.n||'').replace(/[\t\n]/g,' '),FP].join('\t')+'\n'});
  const u=URL.createObjectURL(new Blob([t],{type:'text/tab-separated-values'})); const a=document.createElement('a'); a.href=u; a.download='typeset_adjust_review_'+FP+'.tsv'; a.click()};
if(DATA.some(d=>done(d.id)))$('#guide').open=false;
{const f=items().findIndex(x=>!done(x.id)); show(f>=0?f:0)}
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("page")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--main", default=MAIN, help="기준 후보(<기준>/<후보>)")
    a = sub.add_parser("aggregate")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--tsv", type=Path, required=True)
    a.add_argument("--main", default=MAIN, help="페이지를 만든 기준 후보")
    args = ap.parse_args()
    if args.cmd == "page":
        print(f"검토 페이지: {build_page(args.run.resolve(), args.main)}")
    else:
        print(json.dumps(aggregate(args.run.resolve(), args.tsv, args.main), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
