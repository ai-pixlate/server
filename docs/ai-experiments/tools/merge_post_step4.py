"""③ OCR 후처리 4단계 실험(1 · 2차) — 점검 세트 고정 · 변형 실행 · 집계(설계: 2026-10-23_03-merge_post-step4-design-v1.md).

LLM 호출 없이 blocks-input-v1의 휴리스틱만 비교한다. 파이프라인 코드는 바꾸지 않고, 실험 도구 안에서만 함수를 바꿔 끼운다.
변형은 하나씩 별도 프로세스로 실행한다(`variant` 하위 명령을 변형마다 따로 부른다 — `all`이 그렇게 한다).

    # 1차 — B3 채택 전 코드(예: 4ec58bb)에서만
    python docs/ai-experiments/tools/merge_post_step4.py all --round round1 --input pipeline/samples/local/blocks-input-v1 \
        --label-run pipeline/out/label-run-v1_2026-09-29 --out RUN_DIR
    # 2차 · 3차 — B3 채택 뒤 코드에서, 1차 점검 세트를 다시 써서(3차 = A2 후보 v0, 실험 준비용)
    python docs/ai-experiments/tools/merge_post_step4.py all --round round2 --checkset RUN1_DIR/checkset.json \
        --input pipeline/samples/local/blocks-input-v1 --out RUN2_DIR

하위 명령:
- checkset: A0(기본 config + merge.xycut=true) 기준으로 점검 세트 S1 · S2 · S3 · S4를 뽑아 RUN_DIR/checkset.json에 고정한다.
  사례는 원시 영역 집합으로 정의한다(변형마다 블록 번호가 달라도 같은 사례). ④ 라벨 1회 실측이 true로 본 블록에 속하면
  label_hint를 붙인다(사람 판단 아님).
- variant --name N [--set 표.키=값 ...] [--patch b3|gapcond]: 한 변형을 돌려 RUN_DIR/variants/N.json에 쓴다.
  b3 = 가로줄의 Line 나누기에서 높이비 조건만 끔, gapcond = 같은 곳에 기존 가로 간격 조건(line_gap_min · line_gap)을 더함.
- report: 변형 결과를 A0와 비교해 RUN_DIR/summary.json · RUN_DIR/report.md · RUN_DIR/crops/를 만든다.
- llm-run --round round4 --name B3|B3_yonly [--dry-run | --fake identity | --approved-budget-usd N --max-calls N]: 4차 18섹션
  llm_assist 실행. 실제 호출은 사용자 사전 예산 승인 뒤 두 승인 인자와 GEMINI_API_KEY가 모두 있어야 시작한다
  (계획: 2026-10-23_03-merge_post-step4-round4-plan.md). llm-report: 그 결과를 1차 사람 판정(S1)과 대조한다.
  5차(round5)는 같은 두 변형을 --sections 목록 · 반복 실행(실행마다 다른 --out)으로 돌린다(계획: 2026-10-26_03-merge_post-step4-round5-plan.md).
- 칸 묶음 경계(y 절단 LLM 병합 허용)를 채택한 코드(2026-10-26)에서는 round4 · round5와 yonly patch가 멈춘다(동작 확인).
- llm-diff --a DIR --b DIR: llm-run 결과 폴더 두 개(변형끼리 · 반복끼리)의 입력 같음 여부 · 잎을 넘은 블록 · 구성 차이 · 역할 차이(호출 없음).
- all [--round round1|round2] [--checkset PATH]: checkset(또는 고정본 복사) → 그 차수의 변형(각각 새 프로세스) → report.
  2차(round2)는 B3 채택 뒤의 기준(B3)과 B3 + C_tol0.7을 1차에서 고정한 점검 세트로 비교한다.
구조 지표(블록 수 · 걸친 영역 수 등)는 원인 분석용이며 품질 판정이 아니다. 어떤 결과도 정답이 아니다.
S3 순서 지표는 기준과 원시 영역 목록이 똑같은 블록끼리만 비교한다. 역전 0은 칸 합침 · 읽기 순서 회귀가 없다는 뜻이 아니며,
비교에서 빠진 기준 블록 수(S3_unmatched_base_blocks)를 함께 본다.
차수(--round)는 반드시 지정한다. round1은 B3 채택 전 코드, round2는 채택 뒤 코드에서만 돈다(variant · all이 동작으로 검사).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402
from pipeline.types import OcrResult  # noqa: E402

FONT = ImageFont.load_default(size=14)

# 1차 변형(설계 3절). A0 = 기준, OFF = 참고, D = 전각 변환 목록
ROUND1 = [
    ("A0", [], None),
    ("OFF", ["merge.xycut=false"], None),
    ("A1_gap1.0", ["merge.xycut_gap_ratio=1.0"], None),
    ("A1_gap1.2", ["merge.xycut_gap_ratio=1.2"], None),
    ("A1_gap1.5", ["merge.xycut_gap_ratio=1.5"], None),
    ("B3_no_hratio", [], "b3"),
    ("C_tol0.35", ["merge.line_center_tol=0.35"], None),
    ("C_tol0.7", ["merge.line_center_tol=0.7"], None),
    ("C_min12", ["merge.line_center_tol_min_px=12"], None),
    ("C_gapcond", [], "gapcond"),
    ("D_fullwidth", ["merge.fullwidth=true"], None),
]
# 2차(2026-10-23): B3(가로줄 Line 나누기의 높이비 해제)를 파이프라인에 채택한 뒤의 기준과 C_tol0.7 조합.
# 1차 A0(높이비 있음)는 채택 전 코드(4ec58bb)에서만 재현된다. b3 patch는 채택 뒤 코드에서는 아무것도 바꾸지 않는다.
ROUND2 = [
    ("B3", [], None),
    ("B3_C_tol0.7", ["merge.line_center_tol=0.7"], None),
]
# 3차(2026-10-23, 실험 준비 · 로컬 검증): 과분할 대응 A2 후보 — y 절단을 여백 위아래 줄 높이 기준으로도 거른다(아래 a2 patch).
# 후보 정의 v0이며 채택 규칙이 아니다. x 절단(칸 사이)은 그대로다.
ROUND3 = [
    ("B3", [], None),
    ("A2_r0.6", [], "a2:0.6"),
    ("A2_r0.8", [], "a2:0.8"),
    ("A2_r1.0", [], "a2:1.0"),
]
# 4차(2026-10-23, LLM 실행 준비): y 절단으로만 나뉜 후보 영역끼리 LLM 병합 허용(yonly patch). 휴리스틱은 B3와 같고,
# LLM에 보내는 region · 영역을 넘는 묶음 분리 · 조립 정렬만 바뀐다. 실험 전용이며 승인된 규칙(영역 경계 고정)의 변경 후보다.
ROUND4 = [
    ("B3", [], None),
    ("B3_yonly", [], "yonly"),
]
# 5차(2026-10-26, 계획): 4차와 같은 두 변형을 다른 섹션(--sections) · 반복 실행으로 다시 돌린다.
ROUND5 = ROUND4
ROUNDS = {"round1": ROUND1, "round2": ROUND2, "round3": ROUND3, "round4": ROUND4, "round5": ROUND5}
# 차수마다 필요한 코드 기준: B3(가로줄 Line 나누기의 높이비 해제)가 파이프라인에 들어 있어야 하는가
ROUND_NEEDS_B3 = {"round1": False, "round2": True, "round3": True, "round4": True, "round5": True}
# 4차 LLM 실행 대상 18섹션(3차 기록 4.5): 사람 판정 S1 섹션 5 · S3 대표 6 · B3 검수 섹션 9(중복 제거)
LLM_SECTIONS = [
    "GS-01_001/sec_1_01", "GS-01_002/sec_1_04", "GS-01_002/sec_1_06", "GS-01_002/sec_1_08", "GS-01_002/sec_1_12",
    "GS-01_002/sec_1_19", "GS-02_008/sec_1_04", "GS-02_008/sec_1_06", "GS-02_009/sec_1_01", "GS-02_009/sec_1_03",
    "GS-02_013/sec_1_02", "GS-02_014/sec_1_05", "GS-02_014/sec_1_11", "GS-03_008/sec_1_02", "GS-03_012/sec_1_01",
    "GS-03_014/sec_1_01", "GS-03_018/sec_1_02", "GS-03_018/sec_1_08",
]


def b3_adopted() -> bool:
    """현재 파이프라인 코드가 B3를 채택했는지 동작으로 판별한다: 높이비 2인 이웃 영역(gutter 없음)이 한 Line이면 채택."""
    from pipeline.types import BBox, OcrRegion

    def reg(key: str, x: int, h: int) -> OcrRegion:
        return OcrRegion(region_key=key, text="가", score=0.9, poly=[(x, 0), (x + 40, 0), (x + 40, h), (x, h)],
                         bbox=BBox(x=x, y=0, w=40, h=h))

    m = cfgmod.load_config()["merge"]
    return len(merge._split_row([reg("a", 0, 40), reg("b", 45, 20)], m, [])) == 1


def yonly_adopted() -> bool:
    """현재 파이프라인 코드가 칸 묶음 경계(y 절단 LLM 병합 허용, 2026-10-26 채택)를 쓰는지 동작으로 판별한다:
    y 절단으로만 나뉜 두 후보 영역이 LLM 입력에서 같은 region 번호면 채택."""
    from pipeline.types import BBox, OcrRegion, Section

    def reg(key: str, y: int) -> OcrRegion:
        return OcrRegion(region_key=key, text="가", score=0.9, poly=[(0, y), (100, y), (100, y + 20), (0, y + 20)],
                         bbox=BBox(x=0, y=y, w=100, h=20))

    sec = Section(section_key="probe", source_image_id=1, section_order=1, top_offset=0, height=400, width=200,
                  image_path="unused.png")
    heur, layout, _, _ = merge._heuristic(sec, OcrResult(section_key="probe", regions=[reg("a", 0), reg("b", 200)]),
                                          base_cfg([]))
    payload, _ = merge.build_payload(sec, heur, layout)
    return len({b["region"] for b in payload["blocks"]}) == 1


# 4 · 5차는 칸 묶음 경계 채택 전 코드에서만 돈다 — 채택 뒤에는 B3 변형이 칸 묶음 동작으로 돌아 이름과 어긋난다
ROUND_NEEDS_PRE_YONLY = {"round4", "round5"}


def require_b3_code() -> None:
    """후보 영역(잎) 번호만 다시 계산하는 도구(llm-diff · 검수 페이지)용: B3 채택 뒤 코드면 된다(칸 묶음 채택과 무관)."""
    if not b3_adopted():
        raise SystemExit("B3 채택 뒤 코드(8353716 이후)에서만 실행한다")


def check_round_code(round_name: str) -> None:
    """차수에 맞지 않는 코드에서 실행하면 멈춘다(예: B3 채택 뒤 코드로 1차를 돌리면 A0 이름으로 B3 결과가 나온다)."""
    need, have = ROUND_NEEDS_B3[round_name], b3_adopted()
    if need != have:
        where = "B3 채택 전 코드(예: 4ec58bb)" if not need else "B3 채택 뒤 코드(8353716 이후)"
        raise SystemExit(f"{round_name}은 {where}에서만 실행한다 — 현재 코드는 B3 {'채택' if have else '미채택'} 상태다")
    if round_name in ROUND_NEEDS_PRE_YONLY and yonly_adopted():
        raise SystemExit(f"{round_name}은 칸 묶음 경계 채택 전 코드에서만 실행한다 — 현재 코드는 채택 상태다"
                         "(B3 변형이 칸 묶음 동작으로 돌아 이름과 어긋난다)")


def rkeys(b) -> list[str]:
    return [r.region_key for ln in b.source_lines for r in ln.regions]


def sections(inp: Path):
    for img in sorted(p for p in inp.iterdir() if p.is_dir() and (p / "split.json").is_file()):
        split = jsonio.load_split(img / "split.json")
        for ocr_path in sorted((img / "ocr").glob("*.json")):
            ocr_res = jsonio.load_ocr(ocr_path)
            sec = next(s for s in split.sections if s.section_key == ocr_res.section_key)
            yield img, sec, ocr_res, img / "merge" / ocr_path.name


def base_cfg(sets: list[str]) -> dict:
    cfg = cfgmod.load_config(None, ["merge.xycut=true", *sets])
    merge.validate_config(cfg)
    return cfg


def provenance(inp: Path, sets: list[str], patch: str | None) -> dict:
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    sums = inp / "SHA256SUMS"
    return {"commit": git("rev-parse", "HEAD"), "dirty_paths": git("status", "--porcelain").splitlines(),
            "input": str(inp), "input_sha256sums_sha256": hashlib.sha256(sums.read_bytes()).hexdigest() if sums.is_file() else None,
            "set": ["merge.xycut=true", *sets], "patch": patch}


# ---------------------------------------------------------------------------
# 점검 세트
# ---------------------------------------------------------------------------
def label_regions(label_run: Path | None, img: Path, sec_key: str, final) -> set[str]:
    if label_run is None:
        return set()
    p = label_run / img.name / "label" / f"{sec_key}.json"
    if not p.is_file():
        return set()
    d = json.loads(p.read_text(encoding="utf-8"))
    true_keys = {x["block_key"] for x in d.get("labels", []) if x.get("is_product_label") is True}
    return {rk for b in final.blocks if b.block_key in true_keys for rk in rkeys(b)}


def cmd_checkset(a) -> int:
    from merge_post_compare import components, inversions, kind_of

    check_round_code("round1")  # 점검 세트는 1차 A0(B3 채택 전)를 기준으로 정의했다 — 2차는 고정본을 다시 쓴다
    from merge_post_gutter_check import check_section

    inp, out = Path(a.input), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg_off = cfgmod.load_config()
    cfg_a0 = base_cfg([])
    label_run = Path(a.label_run) if a.label_run else None
    cases: list[dict] = []
    s3: list[dict] = []
    for img, sec, ocr_res, final_path in sections(inp):
        name = f"{img.name}/{sec.section_key}"
        final = jsonio.load_merge(final_path)
        labels = label_regions(label_run, img, sec.section_key, final)
        off = merge.heuristic(sec, ocr_res, cfg_off)
        a0, layout, _, _ = merge._heuristic(sec, ocr_res, cfg_a0)
        same_pairs = []

        def add(set_name: str, regs: list[str], basis: str) -> None:
            cases.append({"set": set_name, "section": name, "regions": sorted(regs), "basis": basis,
                          "label_hint": bool(labels & set(regs))})

        for offs, ons in components(off, a0):
            kind = kind_of(offs, ons, layout)
            if kind == "same":
                same_pairs.append((offs[0], ons[0]))
            elif kind == "split_cut":
                add("S1", rkeys(offs[0]), "끔 휴리스틱 블록이 A0에서 다른 후보 영역으로 나뉨")
            elif kind == "split_inner":
                add("S2", rkeys(offs[0]), "끔 휴리스틱 블록이 A0에서 같은 후보 영역 안 여러 블록으로 나뉨")
            elif kind == "merge":
                add("S4", rkeys(ons[0]), "끔 휴리스틱 블록 여러 개가 A0에서 한 블록으로 합쳐짐")
        a0_region = {rk: layout.block_region[b.block_key] for b in a0.blocks for rk in rkeys(b)}
        for fb in final.blocks:
            regs = rkeys(fb)
            if len({a0_region[rk] for rk in regs}) > 1:
                add("S1", regs, "끈 상태 LLM 최종 블록이 A0에서 여러 후보 영역에 걸침")
        _, gcases = check_section(sec, ocr_res, final, cfg_a0)
        for c in gcases:
            if c["exact_cover"]:
                add("S4", c["off_regions"], "교차 블록 병합 점검의 범위 같은 대리 사례")
        cross, inner, _ = inversions(same_pairs, layout)
        if cross:
            s3.append({"section": name, "cross_region_inversions": cross, "same_region_inversions": inner})
    # 같은 세트 안 중복 제거(같은 영역 집합), 세트 간 중복 표시
    seen: dict[tuple, dict] = {}
    for c in cases:
        k = (c["set"], c["section"], tuple(c["regions"]))
        if k in seen:
            seen[k]["basis"] = sorted(set(seen[k]["basis"].split(" + ")) | {c["basis"]})
            seen[k]["basis"] = " + ".join(seen[k]["basis"])
            continue
        seen[k] = c
    uniq = list(seen.values())
    by_sig: dict[tuple, set[str]] = {}
    for c in uniq:
        by_sig.setdefault((c["section"], tuple(c["regions"])), set()).add(c["set"])
    uniq.sort(key=lambda c: (c["set"], c["section"], c["regions"][0], len(c["regions"]), c["regions"]))
    used: Counter = Counter()
    for c in uniq:
        base_id = f"{c['set']}:{c['section']}:{c['regions'][0]}"
        used[base_id] += 1  # 첫 영역 키가 같은 다른 영역 집합은 #2, #3 … 으로 구분한다(정렬 순서로 고정)
        c["id"] = base_id if used[base_id] == 1 else f"{base_id}#{used[base_id]}"
        c["also_in"] = sorted(by_sig[(c["section"], tuple(c["regions"]))] - {c["set"]})
    s3.sort(key=lambda s: -s["cross_region_inversions"])
    reps = {s["section"] for s in s3[:5]} | {"GS-01_001/sec_1_01", "GS-03_014/sec_1_01"}
    for s in s3:
        s["representative"] = s["section"] in reps
    doc = {"provenance": provenance(inp, [], None), "label_run": str(label_run) if label_run else None,
           "cases": uniq, "S3": s3}
    (out / "checkset.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    cnt = Counter(c["set"] for c in uniq)
    print(json.dumps({"cases": dict(cnt), "label_hint": dict(Counter(c["set"] for c in uniq if c["label_hint"])),
                      "cross_set": sum(1 for c in uniq if c["also_in"]), "S3_sections": len(s3),
                      "S3_representative": sorted(reps & {s["section"] for s in s3})}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# 변형 실행(별도 프로세스)
# ---------------------------------------------------------------------------
def patch(name: str | None) -> None:
    if name is None:
        return
    if name == "b3":
        def split_row(regs, m, gutters):
            pieces = [[regs[0]]]
            for b in regs[1:]:
                a = pieces[-1][-1]
                if m["gutter"] and any(a.bbox.x2 <= g0 and g1 <= b.bbox.x for g0, g1 in gutters):
                    pieces.append([b])
                else:
                    pieces[-1].append(b)
            return pieces
    elif name == "gapcond":
        orig = merge._split_row

        def split_row(regs, m, gutters):
            out = []
            for piece in orig(regs, m, gutters):
                cur = [piece[0]]
                for b in piece[1:]:
                    a = cur[-1]
                    base = min(a.bbox.h, b.bbox.h)
                    if m["line_gap_min"] * base <= b.bbox.x - a.bbox.x2 <= m["line_gap"] * base:
                        cur.append(b)
                    else:
                        out.append(cur)
                        cur = [b]
                out.append(cur)
            return out
    elif name.startswith("a2:"):
        ratio = float(name.split(":", 1)[1])

        def y_gaps(regions):
            """y 방향 빈 구간마다 (폭, 가운데, 위쪽 경계를 만든 상자 높이, 아래쪽 첫 상자 높이)."""
            boxes = sorted(regions, key=lambda r: (r.bbox.y, r.bbox.y2))
            out, reach, top = [], boxes[0].bbox.y2, boxes[0]
            for b in boxes[1:]:
                if b.bbox.y > reach:
                    out.append((float(b.bbox.y - reach), (reach + b.bbox.y) / 2, top.bbox.h, b.bbox.h))
                if b.bbox.y2 > reach:
                    reach, top = b.bbox.y2, b
            return out

        def choose_cut(regions, min_gap):
            """A2 후보 v0: x 절단은 현행 그대로. y 절단은 최소 여백(섹션 기준) 이상이면서 여백 위아래 줄 높이 중 작은 값 ×
            ratio 이상인 빈 구간 중 가장 넓은 것. 더 넓은 쪽을 먼저 자르는 규칙(같으면 x)은 현행과 같다."""
            if len(regions) <= 1:
                return None
            gap_x, cut_x = merge.widest_gap([(r.bbox.x, r.bbox.x2) for r in regions])
            ok = [(g, c) for g, c, hu, hl in y_gaps(regions) if g >= min_gap and g >= ratio * min(hu, hl)]
            gap_y, cut_y = max(ok, key=lambda t: (t[0], -t[1])) if ok else (0.0, None)
            if gap_x >= min_gap and gap_x >= gap_y and cut_x is not None:
                return "x", cut_x
            if cut_y is not None:
                return "y", cut_y
            return None

        merge._choose_cut = choose_cut
        return
    elif name == "yonly":
        if yonly_adopted():
            raise SystemExit("yonly patch는 칸 묶음 경계 채택 전 코드에서만 쓴다 — 채택 뒤 코드에는 이미 들어 있다")
        orig = merge._heuristic
        big = 1_000_000

        def y_only(path_a: str, path_b: str, axis: dict[str, str]) -> bool:
            """두 잎 사이의 분할 트리 경로(공통 조상 → 각 잎)의 절단이 모두 y인가."""
            i = 0
            while i < min(len(path_a), len(path_b)) and path_a[i] == path_b[i]:
                i += 1
            return all(axis[p[:k]] == "y" for p in (path_a, path_b) for k in range(i, len(p)))

        def heuristic_yonly(section, ocr, cfg):
            """B3 휴리스틱 그대로 두고 배치 정보만 바꾼다: 후보 영역(잎) → y 절단으로만 이어진 칸 묶음 번호.
            칸 묶음은 순수 y 분할 부분 트리의 잎이라 깊이 우선 순서에서 연달아 나온다. 정렬 키 앞에 잎 번호를 넣어(× big)
            위아래 잎의 가로줄 번호가 섞이지 않게 하고 블록 순서는 바꾸지 않는다."""
            res, layout, post, diag = orig(section, ocr, cfg)
            if layout is None:
                return res, layout, post, diag
            axis = {c["path"]: c["axis"] for c in layout.cuts if "axis" in c}
            leaf_path = {c["region"]: c["path"] for c in layout.cuts if "region" in c}
            cluster, cid, prev = {}, 0, None
            for leaf in sorted(leaf_path):
                if prev is None or not y_only(leaf_path[prev], leaf_path[leaf], axis):
                    cid += 1
                cluster[leaf], prev = cid, leaf
            leaf_of_line = {ln.line_key: layout.block_region[b.block_key] for b in res.blocks for ln in b.source_lines}
            layout.line_rank = {k: (leaf_of_line[k] * big + r[0],) + tuple(r[1:]) for k, r in layout.line_rank.items()}
            layout.block_rank = {k: (layout.block_region[k] * big + r[0],) + tuple(r[1:]) for k, r in layout.block_rank.items()}
            layout.block_region = {k: cluster[leaf] for k, leaf in layout.block_region.items()}
            if diag is not None:
                diag["yonly_clusters"] = {str(leaf): c for leaf, c in cluster.items()}
            return res, layout, post, diag

        merge._heuristic = heuristic_yonly
        return
    else:
        raise SystemExit(f"모르는 patch {name}")
    merge._split_row = split_row


def check_invariants(sec, ocr_res, res, cfg) -> list[str]:
    problems = []
    out = [r for b in res.blocks for ln in b.source_lines for r in ln.regions]
    if Counter(r.region_key for r in out) != Counter(r.region_key for r in ocr_res.regions):
        problems.append("영역이 정확히 한 번이 아니다")
    by_key = {r.region_key: r for r in ocr_res.regions}
    if any(r.model_dump() != by_key[r.region_key].model_dump() for r in out):
        problems.append("원시 영역 값이 바뀌었다")
    if [b.block_order for b in res.blocks] != list(range(1, len(res.blocks) + 1)):
        problems.append("block_order가 연속이 아니다")
    shuffled = list(ocr_res.regions)
    random.Random(7).shuffle(shuffled)
    if merge.heuristic(sec, OcrResult(section_key=ocr_res.section_key, regions=shuffled), cfg) != res:
        problems.append("입력 순서에 따라 결과가 다르다")
    return problems


def proxy_text(sec, res, layout, regs: list[str], cfg) -> str | None:
    """대리 재조립: 사례 영역을 정확히 덮는 블록들을 한 묶음으로 assemble한 텍스트. 정확히 덮지 못하면 None.
    묶음이 후보 영역을 넘으면 assemble이 영역별로 나누므로 나뉜 블록 텍스트를 ` ‖ `로 잇는다."""
    want = set(regs)
    members = [b for b in res.blocks if set(rkeys(b)) & want]
    if {rk for b in members for rk in rkeys(b)} != want or not all(merge._sendable(b) for b in members):
        return None
    if len(members) == 1:
        return members[0].source_ko
    ids = {f"b{i}": b for i, b in enumerate(members, start=1)}
    out, _, _ = merge.assemble(sec, res, ids, [(list(ids), "body")], layout, cfg["merge"])
    return " ‖ ".join(b.source_ko for b in out.blocks if set(rkeys(b)) & want)


def cmd_variant(a) -> int:
    inp, out = Path(a.input), Path(a.out)
    if a.name not in {n for n, _, _ in ROUNDS[a.round]}:
        raise SystemExit(f"{a.name}은 {a.round}의 변형이 아니다")
    check_round_code(a.round)  # 함수를 바꿔 끼우기 전에 본다
    patch(a.patch)
    cfg = base_cfg(a.set or [])
    checkset = json.loads((out / "checkset.json").read_text(encoding="utf-8"))
    cases_by_sec: dict[str, list[dict]] = {}
    for c in checkset["cases"]:
        cases_by_sec.setdefault(c["section"], []).append(c)
    secs: dict[str, dict] = {}
    problems: dict[str, list[str]] = {}
    case_out: dict[str, dict] = {}
    fullwidth: list[dict] = []
    for img, sec, ocr_res, _ in sections(inp):
        name = f"{img.name}/{sec.section_key}"
        if cfg["merge"]["xycut"]:
            res, layout, _, _ = merge._heuristic(sec, ocr_res, cfg)
            region_of = {b.block_key: layout.block_region[b.block_key] for b in res.blocks}
        else:
            res, layout = merge.heuristic(sec, ocr_res, cfg), None
            region_of = {b.block_key: 1 for b in res.blocks}
        p = check_invariants(sec, ocr_res, res, cfg)
        if p:
            problems[name] = p
        secs[name] = {"regions": (layout.n_regions if layout else None),
                      "blocks": [{"regions": rkeys(b), "role": b.role, "text": b.source_ko, "lines": len(b.source_lines),
                                  "region": region_of[b.block_key]} for b in res.blocks]}
        owner = {rk: b for b in res.blocks for rk in rkeys(b)}
        for c in cases_by_sec.get(name, []):
            touched = list({owner[rk].block_key: owner[rk] for rk in c["regions"]}.values())
            case_out[c["id"]] = {
                "blocks": len(touched), "candidate_regions": len({region_of[b.block_key] for b in touched}),
                "exact": {rk for b in touched for rk in rkeys(b)} == set(c["regions"]),
                "lines": sum(len(b.source_lines) for b in touched),
                "texts": [b.source_ko for b in sorted(touched, key=lambda b: b.block_order)],
                "roles": [b.role for b in sorted(touched, key=lambda b: b.block_order)],
                "proxy_text": proxy_text(sec, res, layout, c["regions"], cfg) if layout else None,
            }
        if cfg["merge"]["fullwidth"]:
            for b in res.blocks:
                for ln in b.source_lines:
                    raw = " ".join(r.text.strip() for r in ln.regions)
                    if raw != ln.text:
                        fullwidth.append({"section": name, "raw": raw, "converted": ln.text})
    doc = {"name": a.name, "provenance": provenance(inp, a.set or [], a.patch), "invariant_problems": problems,
           "sections": secs, "cases": case_out, "fullwidth_changes": fullwidth}
    (out / "variants").mkdir(parents=True, exist_ok=True)
    (out / "variants" / f"{a.name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{a.name}: 섹션 {len(secs)} · 불변식 위반 {len(problems)} · 블록 {sum(len(s['blocks']) for s in secs.values())}")
    return 0


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------
def order_inversions(base: list[dict], other: list[dict]) -> tuple[int, int, int]:
    """(역전 쌍 수, 비교한 블록 수, 비교에서 빠진 기준 블록 수).

    두 결과에 원시 영역 목록(블록 안 순서 포함)이 똑같은 블록끼리만 순서를 비교한다. 블록이 합쳐지거나 나뉘었거나 블록 안
    영역 순서가 달라진 블록은 비교에서 빠진다 — 역전 0은 "비교 가능한 동일 블록 사이에 순서 역전이 없다"는 뜻이지 칸 합침이나
    읽기 순서 회귀가 없다는 뜻이 아니다. 빠진 블록 수를 함께 본다."""
    pos = {tuple(b["regions"]): i for i, b in enumerate(other)}
    seq = [pos[tuple(b["regions"])] for b in base if tuple(b["regions"]) in pos]
    inv = sum(1 for i in range(len(seq)) for j in range(i + 1, len(seq)) if seq[i] > seq[j])
    return inv, len(seq), len(base) - len(seq)


def crop_case(inp: Path, case: dict, dest: Path, margin: int = 30) -> str | None:
    img_name, sec_key = case["section"].split("/")
    split = jsonio.load_split(inp / img_name / "split.json")
    sec = next(s for s in split.sections if s.section_key == sec_key)
    ocr_res = jsonio.load_ocr(inp / img_name / "ocr" / f"{sec_key}.json")
    regs = [r for r in ocr_res.regions if r.region_key in set(case["regions"])]
    if not regs:
        return None
    x0, y0 = min(r.bbox.x for r in regs), min(r.bbox.y for r in regs)
    x1, y1 = max(r.bbox.x2 for r in regs), max(r.bbox.y2 for r in regs)
    with Image.open(inp / img_name / sec.image_path) as im:
        src = im.convert("RGB")
    box = (max(0, x0 - margin), max(0, y0 - margin), min(src.width, x1 + margin), min(src.height, y1 + margin))
    crop = src.crop(box)
    k = 2 if crop.width < 400 else 1
    if k > 1:
        crop = crop.resize((crop.width * k, crop.height * k), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    for r in regs:
        d.rectangle([(r.bbox.x - box[0]) * k, (r.bbox.y - box[1]) * k, (r.bbox.x2 - box[0]) * k, (r.bbox.y2 - box[1]) * k],
                    outline=(220, 0, 0), width=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest)
    return str(dest)


def cmd_report(a) -> int:
    inp, out = Path(a.input), Path(a.out)
    checkset = json.loads((out / "checkset.json").read_text(encoding="utf-8"))
    names = [n for n, _, _ in ROUNDS[a.round] if (out / "variants" / f"{n}.json").is_file()]
    v = {n: json.loads((out / "variants" / f"{n}.json").read_text(encoding="utf-8")) for n in names}
    base = v[names[0]]  # 그 차수의 첫 변형이 기준(1차 A0, 2차 B3)
    s3_reps = {s["section"] for s in checkset["S3"] if s["representative"]}
    s3_all = {s["section"] for s in checkset["S3"]}
    table = {}
    for n in names:
        d = v[n]
        blocks = [b for s in d["sections"].values() for b in s["blocks"]]
        changed = [k for k in d["sections"] if d["sections"][k]["blocks"] != base["sections"][k]["blocks"]]
        inv_s3 = {k: order_inversions(base["sections"][k]["blocks"], d["sections"][k]["blocks"]) for k in s3_all}
        cs = d["cases"]
        row = {
            "invariant_problems": len(d["invariant_problems"]),
            "blocks": len(blocks), "roles": dict(Counter(b["role"] for b in blocks)),
            "candidate_regions": sum(s["regions"] or 0 for s in d["sections"].values()),
            "sections_changed_vs_base": len(changed),
            "S1_single_region": sum(1 for c in checkset["cases"] if c["set"] == "S1" and cs[c["id"]]["candidate_regions"] == 1),
            "S1_single_block": sum(1 for c in checkset["cases"] if c["set"] == "S1" and cs[c["id"]]["blocks"] == 1),
            "S2_single_block": sum(1 for c in checkset["cases"] if c["set"] == "S2" and cs[c["id"]]["blocks"] == 1),
            # 비교 가능한 동일 블록(영역 목록이 같은 블록) 사이의 순서 역전만 센다 — order_inversions 참고
            "S3_sections_with_inversion_among_matched": sum(1 for k in s3_all if inv_s3[k][0]),
            "S3_reps_with_inversion_among_matched": sorted(k for k in s3_reps if inv_s3[k][0]),
            "S3_matched_blocks": sum(inv_s3[k][1] for k in s3_all),
            "S3_unmatched_base_blocks": sum(inv_s3[k][2] for k in s3_all),
            "S3_sections_with_unmatched": sum(1 for k in s3_all if inv_s3[k][2]),
            "S3_reps_with_unmatched": sorted(k for k in s3_reps if inv_s3[k][2]),
            "S4_changed_vs_base": sum(1 for c in checkset["cases"] if c["set"] == "S4"
                                    and (cs[c["id"]]["texts"] != base["cases"][c["id"]]["texts"]
                                         or cs[c["id"]]["proxy_text"] != base["cases"][c["id"]]["proxy_text"])),
            "fullwidth_changes": len(d["fullwidth_changes"]),
        }
        table[n] = row
    (out / "summary.json").write_text(json.dumps({"checkset_provenance": checkset["provenance"],
                                                  "variants": {n: v[n]["provenance"] for n in names}, "table": table},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 4단계 1차 실험 — 변형별 점검 세트 상태(LLM 없음)", "",
             "빨간 상자 = 사례의 원시 영역. 텍스트 칸: 사례 영역을 덮는 블록들의 텍스트(` ‖ `로 구분). 어느 결과도 정답이 아니다.", ""]
    for set_name in ("S1", "S2", "S4"):
        sel = [c for c in checkset["cases"] if c["set"] == set_name]
        lines += [f"## {set_name} ({len(sel)})", ""]
        for c in sel:
            diff = [n for n in names if v[n]["cases"][c["id"]]["texts"] != base["cases"][c["id"]]["texts"]
                    or v[n]["cases"][c["id"]]["proxy_text"] != base["cases"][c["id"]]["proxy_text"]]
            if set_name == "S4" and not diff:
                continue  # S4는 A0와 달라진 사례만 보여 준다(회귀 후보)
            crop = crop_case(inp, c, out / "crops" / f"{c['id'].replace(':', '_').replace('/', '_')}.png")
            tag = (" · 라벨 판정기 힌트" if c["label_hint"] else "") + (f" · 다른 세트: {','.join(c['also_in'])}" if c["also_in"] else "")
            lines += [f"### {c['id']}{tag}", "", f"근거: {c['basis']}", "", f"crop: `{crop}`", "",
                      "| 변형 | 블록 | 후보 영역 | 텍스트 |", "|---|---|---|---|"]
            for n in names:
                r = v[n]["cases"][c["id"]]
                text = " ‖ ".join(t.replace("\n", " / ") for t in r["texts"])
                if set_name == "S4" and r["proxy_text"] is not None:
                    text += f"  〔대리 재조립: {r['proxy_text'].replace(chr(10), ' / ')}〕"
                lines.append(f"| {n} | {r['blocks']} | {r['candidate_regions']} | {text.replace('|', '¦')} |")
            lines.append("")
    lines += ["## D 전각 변환 목록", ""]
    for x in v.get("D_fullwidth", {}).get("fullwidth_changes", []):
        lines.append(f"- {x['section']}: `{x['raw']}` → `{x['converted']}`")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(table, ensure_ascii=False, indent=1))
    return 0


def cmd_all(a) -> int:
    check_round_code(a.round)
    if a.round != "round1" and not a.checkset:
        raise SystemExit(f"{a.round}은 1차에서 고정한 점검 세트를 --checkset으로 줘야 한다")
    common = ["--input", a.input, "--out", a.out]
    me = [sys.executable, str(Path(__file__).resolve())]
    if a.checkset:  # 고정한 점검 세트를 다시 쓴다(새로 뽑지 않음)
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "checkset.json").write_bytes(Path(a.checkset).read_bytes())
    else:
        subprocess.run([*me, "checkset", *common, *(["--label-run", a.label_run] if a.label_run else [])], check=True)
    for name, sets, p in ROUNDS[a.round]:
        argv = [*me, "variant", *common, "--round", a.round, "--name", name, *[x for s in sets for x in ("--set", s)], *(["--patch", p] if p else [])]
        subprocess.run(argv, check=True)  # 변형마다 새 프로세스 — 바꿔 끼운 함수가 다음 변형에 남지 않는다
    subprocess.run([*me, "report", *common, "--round", a.round], check=True)
    return 0


# ---------------------------------------------------------------------------
# 4차 LLM 실행(llm-run) · 판정 대조(llm-report)
# ---------------------------------------------------------------------------
class IdentityLlm:
    """가짜 호출자(로컬 점검용): 보낸 블록을 하나씩 그대로 돌려준다. 역할은 body 고정. 실제 LLM 판단이 아니다."""

    config = {"model": "fake-identity", "temperature": 0, "timeout_s": 60}

    def __call__(self, prompt: str, payload: str):
        from pipeline.vlm import LlmReply

        ids = [b["id"] for b in json.loads(payload)["blocks"]]
        return LlmReply(json.dumps({"blocks": [{"members": [i], "role": "body"} for i in ids]}), {"total_token_count": 0})


def section_inputs(inp: Path, name: str):
    img, key = name.split("/")
    split = jsonio.load_split(inp / img / "split.json")
    sec = next(s for s in split.sections if s.section_key == key)
    return img, sec, jsonio.load_ocr(inp / img / "ocr" / f"{key}.json")


def past_usage(inp: Path, name: str) -> dict:
    """끈 상태 기존 LLM 기록(blocks-input-v1/merge_debug)의 사용량 · 보낸 입력 크기. 추정 근거로만 쓴다."""
    img, key = name.split("/")
    d = json.loads((inp / img / "merge_debug" / f"{key}.json").read_text(encoding="utf-8"))
    u = d.get("usage") or {}
    payload_chars = len(merge.payload_text(d["payload"])) if d.get("payload") else 0
    total, pin, pout = u.get("total_token_count") or 0, u.get("prompt_token_count") or 0, u.get("candidates_token_count") or 0
    return {"status": d["status"], "prompt_tokens": pin, "output_tokens": pout, "thinking_tokens": max(0, total - pin - pout),
            "payload_chars": payload_chars}


def cmd_llm_run(a) -> int:
    """18섹션 llm_assist 실행. 모드: --dry-run(호출 없음 — 입력 크기 · 토큰 추정) · --fake identity(가짜 호출자 — 배선 점검) ·
    실제 호출(--approved-budget-usd · --max-calls와 GEMINI_API_KEY가 모두 있어야 한다. 사용자 사전 예산 승인 뒤에만 쓴다)."""
    inp, out = Path(a.input), Path(a.out) / a.name
    if a.name not in {n for n, _, _ in ROUNDS[a.round]}:
        raise SystemExit(f"{a.name}은 {a.round}의 변형이 아니다")
    check_round_code(a.round)
    sets, p = next((x[1], x[2]) for x in ROUNDS[a.round] if x[0] == a.name)
    patch(p)
    cfg = base_cfg(sets)
    sections = ([x.strip() for x in Path(a.sections).read_text(encoding="utf-8").splitlines() if x.strip()]
                if a.sections else LLM_SECTIONS)
    real = not a.dry_run and a.fake is None
    if real and (a.approved_budget_usd is None or a.max_calls is None):
        raise SystemExit("실제 호출은 --approved-budget-usd와 --max-calls를 모두 줘야 한다(사용자 사전 예산 승인 뒤)")
    prompt = merge.validate_llm_config(cfg, need_api_key=real)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"출력 폴더가 비어 있지 않다(이전 결과를 덮지 않는다): {out}")
    out.mkdir(parents=True, exist_ok=True)
    plan = []
    for name in sections:
        img, sec, ocr_res = section_inputs(inp, name)
        heur, layout, _, _ = merge._heuristic(sec, ocr_res, cfg)
        payload, ids = merge.build_payload(sec, heur, layout)
        plan.append({"section": name, "sendable_blocks": len(ids), "payload_chars": len(merge.payload_text(payload)),
                     "regions_sent": sorted({b["region"] for b in payload["blocks"]}) if ids else [],
                     "past": past_usage(inp, name)})
    calls = sum(1 for x in plan if x["sendable_blocks"])
    if real and calls > a.max_calls:
        raise SystemExit(f"호출 예정 {calls}회가 승인 상한 {a.max_calls}회를 넘는다 — 실행하지 않는다")
    # 입력 토큰 추정: 기존 기록의 (프롬프트 토큰 ÷ (기본 프롬프트 + 페이로드 글자 수)) 비율을 새 입력 글자 수에 곱한다
    base_prompt = merge.resolve_prompt_path(cfg["merge"]["prompt_path"]).read_text(encoding="utf-8")
    past_in = sum(x["past"]["prompt_tokens"] for x in plan)
    past_chars = sum(len(base_prompt) + x["past"]["payload_chars"] for x in plan if x["past"]["status"] == "ok")
    ratio = past_in / past_chars if past_chars else None
    new_chars = sum(len(prompt) + x["payload_chars"] for x in plan if x["sendable_blocks"])
    summary = {"round": a.round, "name": a.name, "mode": "dry-run" if a.dry_run else ("fake:" + a.fake if a.fake else "real"),
               "provenance": provenance(inp, sets, p), "prompt_chars": len(prompt), "prompt_sha256": merge.sha256_text(prompt),
               "sections": len(plan), "calls_planned": calls,
               "estimate": {"input_tokens": round(ratio * new_chars) if ratio else None, "tokens_per_char_basis": ratio,
                            "past_input_tokens": past_in,
                            "past_output_tokens": sum(x["past"]["output_tokens"] for x in plan),
                            "past_thinking_tokens": sum(x["past"]["thinking_tokens"] for x in plan),
                            "note": "기존 끈 상태 기록 기준 추정 — 실제 사용량 · 비용 아님"},
               "approval": {"approved_budget_usd": a.approved_budget_usd, "max_calls": a.max_calls}, "plan": plan, "results": []}
    if not a.dry_run:
        from pipeline.vlm import VlmError

        llm = IdentityLlm() if a.fake == "identity" else merge.default_assistant(cfg)
        for x in plan:
            img, sec, ocr_res = section_inputs(inp, x["section"])
            d = out / img
            try:
                res = merge.run(sec, ocr_res, cfg, use_llm=True, llm=llm, recorder=merge.json_recorder(d / "merge_debug"),
                                post_recorder=merge.json_recorder(d / "merge_post_debug"))
                (d / "merge").mkdir(parents=True, exist_ok=True)
                (d / "merge" / f"{sec.section_key}.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
                rec = json.loads((d / "merge_debug" / f"{sec.section_key}.json").read_text(encoding="utf-8"))
                summary["results"].append({"section": x["section"], "status": rec["status"], "usage": rec.get("usage")})
            except VlmError as e:  # 재시도하지 않는다 — 실패로 남긴다
                summary["results"].append({"section": x["section"], "status": "failed", "error": str(e)})
    (out / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("name", "mode", "sections", "calls_planned", "prompt_chars", "estimate")},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_llm_report(a) -> int:
    """llm-run 결과를 1차 사람 판정(S1)과 대조한다: 사례 영역이 최종 블록 몇 개에 걸쳤는지, 영역을 넘은 묶음 분리 · 교차 기록."""
    import csv

    out = Path(a.out)
    ck = json.loads(Path(a.checkset).read_text(encoding="utf-8"))
    tsv = {r["case_ids"]: r for r in csv.DictReader(open(a.review_tsv, encoding="utf-8"), delimiter="\t")}
    rows = []
    for name in sorted(p.name for p in out.iterdir() if p.is_dir()):
        for c in ck["cases"]:
            if c["set"] != "S1" or c["id"] not in tsv or tsv[c["id"]]["q1"] == "excluded":
                continue
            img, key = c["section"].split("/")
            mp = out / name / img / "merge" / f"{key}.json"
            if not mp.is_file():
                continue
            res = jsonio.load_merge(mp)
            want = set(c["regions"])
            touched = [b for b in res.blocks if want & set(rkeys(b))]
            rows.append({"variant": name, "case": c["id"], "human_q1": tsv[c["id"]]["q1"], "final_blocks": len(touched),
                         "texts": [b.source_ko for b in touched]})
        notes = {"region_splits": 0, "interleaved": 0}
        for f in (out / name).glob("*/merge_debug/*.json"):
            r = json.loads(f.read_text(encoding="utf-8")).get("result") or {}
            notes["region_splits"] += len(r.get("region_splits", []))
            notes["interleaved"] += len(r.get("interleaved", []))
        rows.append({"variant": name, "notes": notes})
    (out / "llm_report.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        if "notes" in r:
            print(r)
        else:
            print(f"{r['variant']:10} {r['case'].split(':', 1)[1]:32} 사람 {r['human_q1']:13} 최종 블록 {r['final_blocks']}")
    return 0


def cmd_llm_diff(a) -> int:
    """llm-run 결과 폴더 두 개(변형끼리 · 같은 변형의 반복끼리)를 섹션마다 비교한다. 호출 없음.
    후보 영역(잎) 번호는 B3 휴리스틱(기본 config + merge.xycut=true)에서 다시 계산한다 — yonly patch를 걸지 않은 프로세스에서 돈다.
    - payload: 두 실행의 LLM 입력이 같은가(같음 · region만 다름 · 그 밖도 다름) — 같은데 결과가 다르면 실행 변동이다
    - cross_leaf: 잎을 넘어 합쳐진 최종 블록(양쪽 각각)
    - components: 블록 구성이 다른 묶음(원시 영역이 겹치는 블록끼리의 연결 성분)
    - roles: 구성이 같은 블록의 역할 · 텍스트 차이"""
    inp, da, db = Path(a.input), Path(a.a), Path(a.b)
    require_b3_code()  # 잎 번호만 쓴다 — 칸 묶음 채택 뒤 코드에서도 같다
    cfg = base_cfg([])
    names = ([x.strip() for x in Path(a.sections).read_text(encoding="utf-8").splitlines() if x.strip()]
             if a.sections else [x["section"] for x in json.loads((da / "run.json").read_text(encoding="utf-8"))["plan"]])
    out = {"a": str(da), "b": str(db), "sections": [], "missing": []}
    for name in names:
        img, sec, ocr_res = section_inputs(inp, name)
        key = sec.section_key
        pa, pb = da / img / "merge" / f"{key}.json", db / img / "merge" / f"{key}.json"
        if not (pa.is_file() and pb.is_file()):
            out["missing"].append({"section": name, "a": pa.is_file(), "b": pb.is_file()})
            continue
        heur, layout, _, _ = merge._heuristic(sec, ocr_res, cfg)
        leaf = {r: layout.block_region[b.block_key] for b in heur.blocks for r in rkeys(b)} if layout else {}
        ra, rb = jsonio.load_merge(pa), jsonio.load_merge(pb)
        dbg = [json.loads((d / img / "merge_debug" / f"{key}.json").read_text(encoding="utf-8")) for d in (da, db)]
        if dbg[0]["payload_sha256"] == dbg[1]["payload_sha256"]:
            payload = "same"
        else:
            strip = [[{k: v for k, v in x.items() if k != "region"} for x in d["payload"]["blocks"]] for d in dbg]
            payload = "region_only" if strip[0] == strip[1] else "other"
        sa = {frozenset(rkeys(b)): b for b in ra.blocks}
        sb = {frozenset(rkeys(b)): b for b in rb.blocks}

        def brief(b):
            return {"order": b.block_order, "role": b.role, "leaves": sorted({leaf.get(r, 0) for r in rkeys(b)}),
                    "regions": rkeys(b), "text": b.source_ko}

        cross = {s: [brief(b) for b in r.blocks if len({leaf.get(x, 0) for x in rkeys(b)}) > 1] for s, r in (("a", ra), ("b", rb))}
        roles = [{"a": brief(sa[k]), "b_role": sb[k].role, "text_same": sa[k].source_ko == sb[k].source_ko}
                 for k in sa.keys() & sb.keys() if sa[k].role != sb[k].role or sa[k].source_ko != sb[k].source_ko]
        rest_a = [b for k, b in sa.items() if k not in sb]
        rest_b = [b for k, b in sb.items() if k not in sa]
        comps, used_a, used_b = [], set(), set()
        for start in rest_a:
            if id(start) in used_a:
                continue
            ca, cb, regs, grow = [start], [], set(rkeys(start)), True
            used_a.add(id(start))
            while grow:
                grow = False
                for pool, acc, used in ((rest_b, cb, used_b), (rest_a, ca, used_a)):
                    for b in pool:
                        if id(b) not in used and regs & set(rkeys(b)):
                            acc.append(b); used.add(id(b)); regs |= set(rkeys(b)); grow = True
            comps.append({"a": [brief(b) for b in sorted(ca, key=lambda b: b.block_order)],
                          "b": [brief(b) for b in sorted(cb, key=lambda b: b.block_order)]})
        out["sections"].append({"section": name, "payload": payload, "blocks": [len(ra.blocks), len(rb.blocks)],
                                "same_blocks": len(sa.keys() & sb.keys()), "cross_leaf": cross,
                                "components": comps, "roles": roles})
    secs = out["sections"]
    out["summary"] = {
        "sections": len(secs), "missing": len(out["missing"]),
        "identical_sections": sum(1 for s in secs if not s["components"] and not s["roles"]),
        "payload": dict(Counter(s["payload"] for s in secs)),
        "cross_leaf_blocks": {k: sum(len(s["cross_leaf"][k]) for s in secs) for k in ("a", "b")},
        "components": sum(len(s["components"]) for s in secs),
        "role_or_text_diffs": sum(len(s["roles"]) for s in secs),
        "note": "구조 비교이며 품질 판정이 아니다"}
    Path(a.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out["summary"], ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("checkset", "variant", "report", "all"):
        sp = sub.add_parser(c)
        sp.add_argument("--input", required=True)
        sp.add_argument("--out", required=True)
        if c in ("checkset", "all"):
            sp.add_argument("--label-run")
        if c in ("variant", "report", "all"):
            sp.add_argument("--round", choices=sorted(ROUNDS), required=True,
                            help="round1 = B3 채택 전 코드 필요, round2 = 채택 뒤 코드 필요(variant · all이 검사한다)")
        if c == "all":
            sp.add_argument("--checkset", help="이미 고정한 checkset.json(주면 새로 뽑지 않는다)")
        if c == "variant":
            sp.add_argument("--name", required=True)
            sp.add_argument("--set", action="append")
            sp.add_argument("--patch", help="b3 · gapcond · a2:<비율>")
    sp = sub.add_parser("llm-run", help="4차 18섹션 llm_assist 실행(--dry-run · --fake identity · 승인 뒤 실제 호출)")
    sp.add_argument("--input", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--round", choices=sorted(ROUNDS), required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--sections", help="섹션 목록 파일(줄마다 원본/섹션). 없으면 LLM_SECTIONS 18개")
    sp.add_argument("--dry-run", action="store_true", help="호출 없이 입력 크기 · 토큰 추정만")
    sp.add_argument("--fake", choices=["identity"], help="가짜 호출자로 배선만 점검(비용 없음)")
    sp.add_argument("--approved-budget-usd", type=float, help="실제 호출 시 필수 — 사용자가 승인한 금액 상한(기록용)")
    sp.add_argument("--max-calls", type=int, help="실제 호출 시 필수 — 승인한 호출 수 상한")
    sp = sub.add_parser("llm-report", help="llm-run 결과와 1차 사람 판정(S1) 대조")
    sp.add_argument("--out", required=True, help="llm-run의 --out(변형별 하위 폴더를 모두 본다)")
    sp.add_argument("--checkset", required=True)
    sp.add_argument("--review-tsv", required=True)
    sp = sub.add_parser("llm-diff", help="llm-run 결과 폴더 두 개 비교(변형끼리 · 반복끼리, 호출 없음)")
    sp.add_argument("--input", required=True)
    sp.add_argument("--a", required=True, help="llm-run 변형 결과 폴더(예: OUT/rep1/B3)")
    sp.add_argument("--b", required=True)
    sp.add_argument("--sections", help="섹션 목록 파일. 없으면 --a의 run.json 계획 섹션")
    sp.add_argument("--out-json", required=True)
    a = ap.parse_args(argv)
    return {"checkset": cmd_checkset, "variant": cmd_variant, "report": cmd_report, "all": cmd_all,
            "llm-run": cmd_llm_run, "llm-report": cmd_llm_report, "llm-diff": cmd_llm_diff}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
