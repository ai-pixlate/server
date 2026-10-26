"""③ OCR 후처리 5단계 — 보류 기능 3종(숫자 속 o/O 교정 · 겹친 앞 글자 제거 · 맨 앞 따옴표 제거)의 후보 조사 · 개별 비교(2026-10-26).

    python docs/ai-experiments/tools/merge_post_step5.py scan --input pipeline/samples/local/blocks-input-v1 --out RUN_DIR
    python docs/ai-experiments/tools/merge_post_step5.py compare --input pipeline/samples/local/blocks-input-v1 --out RUN_DIR \
        [--llm-run pipeline/out/merge-post-step4-round5_2026-10-26/new/B3_yonly ...]

파이프라인 코드 · config는 바꾸지 않는다. OCR · LLM을 호출하지 않는다. 원시 OCR 영역(`regions[].text`)은 어느 경우에도 바꾸지 않고,
각 기능은 영역마다 "줄 조립에 쓸 글자"만 바꾼다(`_Line.text`를 실험 프로세스 안에서만 바꿔 끼움). 기능 규칙은 참고 구현
seed-builder PR #1(`fc72ecd`, 미병합) `amazon/reorder_ocr_v2.py`의 `fix_chars` · `strip_stray`를 그대로 옮겼다(아래 상수 주석).

- scan: 세 기능의 규칙 적중 후보와, 규칙이 잘못 고칠 위험을 보려고 넓게 잡은 주변 후보(규칙 미적중)를 모은다. 후보마다 원본
  섹션 이미지에서 영역 주변을 잘라 `crops/`에 두고 `scan.json`에 원시 텍스트 · 교정 텍스트 · 적용 조건을 적는다.
- selfcheck: 합성 사례로 규칙 동작(교정 · 누락 · 훼손)을 확인한다 — 실제 품질 근거가 아니다.
- page: 사람 검수 페이지(`review/`)를 만든다. 카드 묶음과 에이전트 관찰은 아래 OBS 표(원본 대조 결과)다.
- compare: 기준(기본 config + merge.xycut=true, 채택된 B3 · 칸 묶음 규칙)에 기능을 하나씩 켜고 LLM 없는 휴리스틱 결과를 비교한다
  (블록 구성 · 역할 · source_ko · 줄 텍스트 · 길이 변화). --llm-run을 주면 기존 LLM 결과(B3_yonly)의 최종 블록에 같은 영역 교정을
  사후 적용한 source_ko 차이도 낸다 — LLM이 교정된 입력을 받았을 때의 응답은 아니다(가정).
결과는 정답이 아니며, 실제 오류 여부는 원본 이미지 대조(에이전트 관찰)와 사람 검수로 정한다.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from PIL import Image, ImageDraw  # noqa: E402

import merge_post_step4 as step4  # noqa: E402
from merge_post_step4_review_page import Section  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402

# ---------------------------------------------------------------------------
# 기능 규칙 — seed-builder fc72ecd reorder_ocr_v2.py 그대로
# ---------------------------------------------------------------------------
# 숫자 사이에 낀 o/O (1o,ooo / 1oo% / SPF 5O / 13.O). 숫자로 시작하고 숫자 · o · 쉼표 · 점만 있는 덩어리
NUM_WITH_O = re.compile(r"(?<![A-Za-z])(\d[\doO]*(?:[.,][\doO]+)*)(?=%|\b)")
# 맨 앞의 작은따옴표 · 백틱 + 띄어쓰기(체크 표시 · 글머리 아이콘을 따옴표로 읽은 것). 큰따옴표는 진짜 인용일 수 있어 둔다
LEADING_QUOTE = re.compile(r"^['`‘’]\s+(?=\S)")
# 겹친 상자가 앞 단어 끝 글자를 다시 읽은 것: 상자 글자가 '한 글자 + 띄어쓰기'(기호 , . ; :는 붙어 있어도)로 시작
LEADING_STRAY = re.compile(r"^([A-Za-z0-9,.;:])\s*(?=\S)")
SIMILAR = [set("1Iil|dt"), set("oO0pdbqaecg")]

# 위험 · 재현율을 보려는 넓은 탐색(규칙 아님)
WIDE_NUM_O = re.compile(r"\d[oO]|[oO]\d")
WIDE_QUOTE = re.compile(r"['`‘’\"“”]")
WIDE_STRAY = re.compile(r"^\S\s")


def fix_num_o(text: str) -> str:
    return NUM_WITH_O.sub(lambda m: m.group(1).replace("o", "0").replace("O", "0") if re.search(r"[oO]", m.group(1))
                          else m.group(1), text)


def fix_quote(text: str) -> str:
    return LEADING_QUOTE.sub("", text)


def looks_like_tail(ch: str, prev_last: str) -> bool:
    if ch.lower() == prev_last.lower():
        return True
    return any(ch in s and prev_last in s for s in SIMILAR)


def stray_fixes(regions, leaves) -> dict[str, dict]:
    """strip_stray와 같은 조건. 원 구현의 block(XY-cut 잎)은 우리 후보 영역(잎)으로 본다. 반환: region_key → 적용 기록."""
    leaf_of = {r.region_key: i for i, leaf in enumerate(leaves) for r in leaf}
    out = {}
    for b in regions:
        t = b.text.strip()
        m = LEADING_STRAY.match(t)
        if not m or b.region_key not in leaf_of:
            continue
        if m.group(1).isalnum() and not re.match(r"^\S\s", t):
            continue
        h = max(1.0, b.bbox.h)
        cy = b.bbox.y + b.bbox.h / 2
        left = [o for o in regions if o is not b and leaf_of.get(o.region_key) == leaf_of[b.region_key]
                and abs(o.bbox.y + o.bbox.h / 2 - cy) <= h / 2
                and o.bbox.x < b.bbox.x and b.bbox.x - o.bbox.x2 <= h * 0.3]
        if not left:
            continue
        prev = max(left, key=lambda o: o.bbox.x2)
        pt = prev.text.strip()
        if pt and looks_like_tail(m.group(1), pt[-1]):
            out[b.region_key] = {"clean": t[m.end():], "removed": t[:m.end()], "prev_key": prev.region_key, "prev_text": pt,
                                 "gap_px": b.bbox.x - prev.bbox.x2, "h": b.bbox.h}
    return out


def text_regions(ocr):
    return [r for r in ocr.regions if merge.region_kind(r) == "text"]


def leaves_of(ocr):
    regs = text_regions(ocr)
    if not regs:
        return []
    min_gap = step4.base_cfg([])["merge"]["xycut_gap_ratio"] * statistics.median(r.bbox.h for r in regs)
    return merge.build_cut_tree(regs, min_gap)[1]


def clean_map(feature: str, ocr) -> dict[str, dict]:
    """region_key → {clean, ...적용 조건}. 바뀌는 영역만."""
    out = {}
    if feature == "stray":
        return stray_fixes(text_regions(ocr), leaves_of(ocr))
    for r in ocr.regions:
        t = r.text.strip()
        c = fix_num_o(t) if feature == "num_o" else fix_quote(t)
        if c != t:
            out[r.region_key] = {"clean": c}
    return out


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def crop(sec: Section, keys: list[str], dest: Path, mark: str | None, margin: int = 40, whole: bool = False) -> str:
    """영역 주변(왼쪽 이웃이 보이게 왼쪽을 넓게) 또는 섹션 전체(whole)를 잘라 저장한다. mark가 있으면 빨간 틀."""
    with Image.open(sec.path) as im:
        src = im.convert("RGB")
    x0, y0, x1, y1 = sec.union(keys)
    w = x1 - x0
    box = ((0, 0, src.width, src.height) if whole else
           (max(0, x0 - margin - w), max(0, y0 - margin), min(src.width, x1 + margin + w // 2), min(src.height, y1 + margin)))
    c = src.crop(box)
    k = 520 / c.width if whole else max(1, min(4, round(500 / max(1, c.width))))
    c = c.resize((max(1, round(c.width * k)), max(1, round(c.height * k))), Image.LANCZOS)
    d = ImageDraw.Draw(c)
    if mark:
        bx0, by0, bx1, by1 = sec.union([mark])
        d.rectangle([(bx0 - box[0]) * k, (by0 - box[1]) * k, (bx1 - box[0]) * k, (by1 - box[1]) * k], outline=(220, 0, 0),
                    width=3 if whole else 2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    c.save(dest)
    return dest.name


def cmd_scan(a) -> int:
    inp, out = Path(a.input), Path(a.out)
    rows = []
    for img, sec_meta, ocr, _ in step4.sections(inp):
        name = f"{img.name}/{sec_meta.section_key}"
        sec = Section(inp, name)
        regs = {r.region_key: r for r in ocr.regions}
        hits = {f: clean_map(f, ocr) for f in ("num_o", "stray", "quote")}
        for r in ocr.regions:
            t = r.text.strip()
            found = []
            for f, wide in (("num_o", WIDE_NUM_O), ("quote", WIDE_QUOTE), ("stray", WIDE_STRAY)):
                if r.region_key in hits[f]:
                    found.append((f, "rule"))
                elif wide.search(t) if f != "stray" else wide.match(t):
                    found.append((f, "wide"))
            for f, kind in found:
                info = hits[f].get(r.region_key, {})
                keys = [r.region_key] + ([info["prev_key"]] if info.get("prev_key") else [])
                cid = f"{f}:{name}:{r.region_key}"
                rows.append({"id": cid, "feature": f, "kind": kind, "section": name, "region": r.region_key, "raw": t,
                             "clean": info.get("clean", t), "score": r.score, "bbox": [r.bbox.x, r.bbox.y, r.bbox.w, r.bbox.h],
                             **{k: v for k, v in info.items() if k != "clean"},
                             "crop": crop(sec, keys, out / "crops" / f"{f}_{img.name}_{sec_meta.section_key}_{r.region_key}.png",
                                          r.region_key)})
        del regs
    (out / "scan.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in ("num_o", "stray", "quote"):
        print(f, "rule", sum(1 for x in rows if x["feature"] == f and x["kind"] == "rule"),
              "wide", sum(1 for x in rows if x["feature"] == f and x["kind"] == "wide"))
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def patched_heuristic(sec_meta, ocr, cfg, cmap: dict[str, dict]):
    """_Line.text만 바꿔 끼워 휴리스틱을 돌린다. 원시 영역은 그대로다."""
    orig = merge._Line.text
    merge._Line.text = property(lambda self: " ".join(cmap[r.region_key]["clean"] if r.region_key in cmap else r.text.strip()
                                                      for r in self.regions))
    try:
        return merge._heuristic(sec_meta, ocr, cfg)[0]
    finally:
        merge._Line.text = orig


def shape(res):
    return [([r.region_key for ln in b.source_lines for r in ln.regions], b.role, b.source_ko,
             [ln.text for ln in b.source_lines]) for b in res.blocks]


def cmd_compare(a) -> int:
    inp, out = Path(a.input), Path(a.out)
    step4.require_b3_code()
    if not step4.yonly_adopted():
        raise SystemExit("칸 묶음 경계 채택 뒤 코드에서 실행한다(기준 = 채택 규칙)")
    cfg = step4.base_cfg([])
    report = {f: {"sections_changed": 0, "blocks_changed": 0, "structure_changed": 0, "role_changed": [], "text_changes": [],
                  "raw_region_unchanged": True, "llm_posthoc": []} for f in ("num_o", "stray", "quote")}
    ocr_by = {}
    for img, sec_meta, ocr, _ in step4.sections(inp):
        name = f"{img.name}/{sec_meta.section_key}"
        ocr_by[name] = ocr
        raw_before = [(r.region_key, r.text) for r in ocr.regions]
        base = merge._heuristic(sec_meta, ocr, cfg)[0]
        for f in report:
            cmap = clean_map(f, ocr)
            if not cmap:
                continue
            res = patched_heuristic(sec_meta, ocr, cfg, cmap)
            rep = report[f]
            rep["raw_region_unchanged"] &= [(r.region_key, r.text) for r in ocr.regions] == raw_before
            sb, sv = shape(base), shape(res)
            if sb != sv:
                rep["sections_changed"] += 1
            if [x[0] for x in sb] != [x[0] for x in sv]:
                rep["structure_changed"] += 1
                continue
            for b0, b1, x0, x1 in zip(base.blocks, res.blocks, sb, sv):
                if x0 == x1:
                    continue
                rep["blocks_changed"] += 1
                if b0.role != b1.role:
                    rep["role_changed"].append({"section": name, "block": b0.block_key, "before": b0.role, "after": b1.role,
                                                "text": b1.source_ko})
                rep["text_changes"].append({"section": name, "block": b0.block_key, "before": b0.source_ko, "after": b1.source_ko,
                                            "len_before": len(b0.source_ko), "len_after": len(b1.source_ko),
                                            "regions": sorted(k for k in cmap if k in x0[0]),
                                            "conditions": {k: {kk: vv for kk, vv in cmap[k].items() if kk != "clean"}
                                                           for k in cmap if k in x0[0]}})
    for run in a.llm_run or []:
        for mp in sorted(Path(run).glob("*/merge/*.json")):
            name = f"{mp.parent.parent.name}/{mp.stem}"
            if name not in ocr_by:
                continue
            res = jsonio.load_merge(mp)
            for f in report:
                cmap = clean_map(f, ocr_by[name])
                for b in res.blocks:
                    hit = [r.region_key for ln in b.source_lines for r in ln.regions if r.region_key in cmap]
                    if not hit:
                        continue
                    after = "\n".join(" ".join(cmap[r.region_key]["clean"] if r.region_key in cmap else r.text.strip()
                                               for r in ln.regions) for ln in b.source_lines)
                    report[f]["llm_posthoc"].append({"run": run, "section": name, "block": b.block_key, "role": b.role,
                                                     "before": b.source_ko, "after": after, "regions": hit})
    (out / "compare.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for f, r in report.items():
        print(f, {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
    return 0


# ---------------------------------------------------------------------------
# selfcheck — 합성 사례(규칙 동작 확인용, 실제 품질 근거 아님)
# ---------------------------------------------------------------------------
# 기대 = 사람이 원하는 결과(교정 대상이면 교정, 정상 글자면 그대로). 기대와 다르면 규칙의 누락 또는 훼손이다
SYNTH_NUM_O = [  # (입력, 기대, 메모)
    ("SPF 5O+", "SPF 50+", "원 제안 예 — 숫자 사이 O"), ("1oo%", "100%", "원 제안 예"), ("13.O", "13.0", "원 제안 예"),
    ("1o,ooo", "10,000", "원 제안 예"), ("(2o17)", "(2017)", "연도"), ("SPF5O", "SPF50", "글자 바로 뒤 숫자 — 규칙이 놓침"),
    ("3o개", "30개", "한글이 붙음 — 규칙이 놓침"), ("2oz", "2oz", "정상 단위 — 그대로여야 함"), ("10 Oz", "10 Oz", "정상 — 띄어 쓴 단위"),
    ("No.1", "No.1", "정상"), ("B6O1", "B6O1", "제품 코드(글자 뒤) — 그대로"), ("Model 4O", "Model 4O", "숫자+O 코드가 실제면 훼손 위험"),
]
SYNTH_STRAY = [  # (왼쪽 영역 글자, 대상 영역 글자, 기대 대상 글자, 메모) — 같은 줄 · 바로 붙음(간격 0)
    ("Deep", "o Hydration", "Hydration", "원 제안 예"), ("choose", "e your", "your", "원 제안 예"), ("and", "d radiant.", "radiant.", "원 제안 예"),
    ("Extra", "a gentle formula", "a gentle formula", "정상 관사 a — 지우면 훼손"), ("Vitamin", "A long-term", "A long-term", "정상"),
    ("Spa", "a cosmetic", "a cosmetic", "정상 관사 a — 지우면 훼손"), ("분들에게", "I 추천해요", "I 추천해요", "#40 사례 모양 — 대상 아님"),
]
SYNTH_QUOTE = [
    ("' 체크 아이콘 문구", "체크 아이콘 문구", "원 제안 대상(아이콘을 따옴표로 읽음)"), ("‘ Best seller", "Best seller", "원 제안 대상"),
    ("'레티날 샷'을", "'레티날 샷'을", "실제 인용 — 띄어쓰기 없음, 그대로"), ("' 레티날 샷'을", "' 레티날 샷'을", "실제 인용인데 OCR이 띄어 읽으면 훼손"),
    ("\"인용\"", "\"인용\"", "큰따옴표 — 대상 아님"),
]


def cmd_selfcheck(a) -> int:
    from pipeline.types import BBox, OcrRegion

    def reg(key, x, w, text):
        return OcrRegion(region_key=key, text=text, score=0.9, poly=[(x, 0), (x + w, 0), (x + w, 20), (x, 20)],
                         bbox=BBox(x=x, y=0, w=w, h=20))

    rows = [{"feature": "num_o", "input": s, "expected": e, "got": fix_num_o(s), "note": n} for s, e, n in SYNTH_NUM_O]
    for left, tgt, e, n in SYNTH_STRAY:
        regs = [reg("l", 0, 100, left), reg("t", 100, 200, tgt)]
        got = stray_fixes(regs, [regs]).get("t", {}).get("clean", tgt)
        rows.append({"feature": "stray", "input": f"{left} | {tgt}", "expected": e, "got": got, "note": n})
    rows += [{"feature": "quote", "input": s, "expected": e, "got": fix_quote(s), "note": n} for s, e, n in SYNTH_QUOTE]
    for r in rows:
        r["match"] = r["got"] == r["expected"]
    (Path(a.out) / "selfcheck.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        print(r["feature"], "OK " if r["match"] else "XX ", repr(r["input"]), "->", repr(r["got"]), "|", r["note"])
    return 0


# ---------------------------------------------------------------------------
# page — 사람 검수 페이지(채팅 의견 전제, 선택은 브라우저에만 저장)
# ---------------------------------------------------------------------------
REF = "영문 참고문헌 목록(페이지 글자, 번역 대상 여부 미정)"
# 에이전트 관찰(원본 이미지 대조, 2026-10-26) — 사람 판단이 아니다. (기능, 섹션, 영역) → (묶음, 범위, 관찰)
OBS = {
    ("num_o", "GS-02_009/sec_1_01", "reg_0002"): ("A", "번역 대상 — 제목", "이미지 `MATRIXYL® 3000*3%` — 300O → 3000 맞음"),
    ("num_o", "GS-02_009/sec_1_01", "reg_0014"): ("A", "번역 대상 — 각주", "이미지 `*MATRIXYL®3000 :` — 300O → 3000 맞음"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0062"): ("A", REF, "이미지 `(2017)` — 2o17 → 2017 맞음"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0064"): ("A", REF, "이미지 `(2023)` — 2o23 → 2023 맞음"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0066"): ("A", REF, "이미지 `(2015)` — 2o15 → 2015 맞음"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0072"): ("A", REF, "이미지 `(2020)` — 2o2o → 2020 맞음"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0146"): ("A", "논문 이미지 글자(#39, 범위 밖)", "이미지 `100%` — 10o% → 100% 맞음"),
    ("num_o", "GS-01_002/sec_1_18", "reg_0029"): ("A", "제품 라벨(범위 밖)", "이미지 `SPF 50+` — 5O → 50 맞음"),
    ("num_o", "GS-01_002/sec_1_18", "reg_0032"): ("B", "제품 라벨(범위 밖)", "이미지 `SPF 50+` — 실제 오류 `SPF5O-`, 글자(F) 바로 뒤 숫자라 규칙이 놓침"),
    ("num_o", "GS-01_002/sec_1_22", "reg_0040"): ("B", "제품 라벨(범위 밖)", "이미지 `SPF 50+` — 실제 오류 `SPF5O`, 규칙이 놓침"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0070"): ("B", REF, "이미지 `(2019)` — 실제 오류 `(2o1e)`, 9 → e라서 규칙이 놓침"),
    ("num_o", "GS-01_002/sec_1_04", "reg_0044"): ("C", "제품 라벨 — 로고", "로고 `goodal` 뒤집힌 글자를 `bepoo8`로 읽음 — 숫자 아님, 규칙도 건드리지 않음(정상 처리)"),
    ("num_o", "GS-01_002/sec_1_23", "reg_0016"): ("C", "제품 라벨 — 로고", "세로 로고 `goodal`을 `Jepoo9`로 읽음 — 규칙이 건드리지 않음(정상 처리)"),
    ("num_o", "GS-02_008/sec_1_04", "reg_0044"): ("C", "논문 이미지 글자(#39, 범위 밖)", "`DISCUSSION`을 `o50U5S0N`으로 읽음 — 규칙이 건드리지 않음"),
    ("stray", "GS-02_008/sec_1_04", "reg_0154"): ("C", "논문 이미지 글자(#39, 범위 밖)", "정상 관사 `a cosmetic formulation` — 왼쪽에 붙은 이웃이 없어 규칙 미적용. 이웃 조건이 맞으면 지워질 모양(합성 확인)"),
    ("stray", "GS-03_014/sec_1_03", "reg_0002"): ("C", "제품 라벨(범위 밖)", "정상 `A long-term` — 규칙 미적용"),
    ("stray", "GS-02_008/sec_1_04", "reg_0082"): ("C", "논문 이미지 글자(#39, 범위 밖)", "`I Studies` — 앞 단어(Clinical)가 잘려 꼬리 l만 남음. 겹친 상자 중복이 아님, 규칙 미적용"),
    ("stray", "GS-02_008/sec_1_04", "reg_0037"): ("C", "논문 이미지 글자(#39, 범위 밖)", "`o Mattn` — 흐린 논문 글자 OCR 잡음, 겹친 상자 중복 아님"),
    ("quote", "GS-02_014/sec_1_11", "reg_0024"): ("C", "번역 대상 — 본문", "실제 여는 따옴표 `‘ 레티날 샷 타이트닝 부스터’를` — OCR이 띄어 읽지 않아 규칙 미적용. 띄어 읽었다면 실제 인용부호가 지워짐(합성 확인)"),
    ("quote", "GS-02_009/sec_1_02", "reg_0009"): ("C", "번역 대상 — 후기 인용", "실제 큰따옴표 인용 — 규칙 대상 아님"),
    ("quote", "GS-02_008/sec_1_05", "reg_0006"): ("D", "번역 대상 — 본문", "이미지 `에이샷®으로` — 위첨자 ®를 `'`로 읽음(다른 오류 유형, 맨 앞 아님)"),
    ("quote", "GS-02_009/sec_1_01", "reg_0003"): ("D", "번역 대상 — 그래프 제목", "이미지 `Matrixyl® 3000` — ®를 `'`로 읽음"),
    ("quote", "GS-03_002/sec_1_01", "reg_0027"): ("D", "번역 대상 — 제목", "이미지 `단 2주* 만에` — 위첨자 *를 `'`로 읽음"),
    ("quote", "GS-03_002/sec_1_01", "reg_0029"): ("D", "번역 대상 — 제목", "이미지 `내추럴 톤업**` — 위첨자 **를 `\"`로 읽음"),
    ("quote", "GS-03_005/sec_1_01", "reg_0012"): ("D", "번역 대상 — 본문", "이미지 `즉각 톤업*까지` — 위첨자 *를 `'`로 읽음"),
}
GROUPS = {
    "A": ("A. 숫자 속 o/O — 규칙이 고친 것", "이 교정이 맞는가?", [("ok", "교정 맞음"), ("bad", "교정 틀림"), ("unsure", "판단 어려움")]),
    "B": ("B. 숫자 속 o/O — 규칙이 놓친 실제 오류 후보", "이 영역에 숫자 속 o/O 오류가 있는가?", [("yes", "오류 있음"), ("no", "오류 없음"), ("unsure", "판단 어려움")]),
    "C": ("C. 잘못 고칠 위험 · 정상 사례(규칙 미적용)", "이 영역에 해당 기능이 고쳐야 할 오류가 있는가?", [("yes", "오류 있음"), ("no", "오류 없음 — 그대로가 맞음"), ("unsure", "판단 어려움")]),
    "D": ("D. 참고 — 맨 앞 따옴표가 아닌 다른 오류(위첨자 ® · *를 따옴표로 읽음)", "이 관찰이 맞는가?(이번 기능 대상 아님)", [("yes", "맞음"), ("no", "아님"), ("unsure", "판단 어려움")]),
}
FEATURE_KO = {"num_o": "숫자 속 o/O 교정", "stray": "겹친 앞 글자 제거", "quote": "맨 앞 따옴표 제거"}


def char_name(ch: str) -> str:
    import unicodedata

    ko = {"O": "영문 대문자 O", "o": "영문 소문자 o", "0": "숫자 0"}.get(ch)
    return f"{ko or unicodedata.name(ch, '?')}(U+{ord(ch):04X})"


def diff_marks(raw: str, clean: str, esc) -> tuple[str, str, str]:
    """교정 전후에서 바뀐 글자를 <mark>로 강조하고, 바뀐 글자의 이름 · 코드 포인트를 적는다(O와 0은 글꼴에서 거의 같다)."""
    import difflib

    sm = difflib.SequenceMatcher(a=raw, b=clean, autojunk=False)
    before, after, notes = "", "", []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            before += esc(raw[i1:i2])
            after += esc(clean[j1:j2])
            continue
        before += f"<mark>{esc(raw[i1:i2])}</mark>"
        after += f"<mark>{esc(clean[j1:j2])}</mark>"
        notes.append(f"{i1 + 1}번째 글자: " + " · ".join(char_name(ch) for ch in raw[i1:i2]) + " → "
                     + (" · ".join(char_name(ch) for ch in clean[j1:j2]) or "삭제"))
    return before, after, "<br>".join(esc(n) for n in notes)


def cmd_page(a) -> int:
    import html

    inp, out = Path(a.input), Path(a.out)
    rows = {(r["feature"], r["section"], r["region"]): r for r in json.loads((out / "scan.json").read_text(encoding="utf-8"))}
    page = out / "review"
    cards = []
    for key, (g, scope, obs) in OBS.items():
        r = rows[key]
        f, name, region = key
        sec = Section(inp, name)
        keys = [region] + ([r["prev_key"]] if r.get("prev_key") else [])
        stem = f"{f}_{name.replace('/', '_')}_{region}"
        imgs = {"plain": crop(sec, keys, page / "img" / f"{stem}_plain.png", None),
                "mark": crop(sec, keys, page / "img" / f"{stem}_mark.png", region),
                "whole": crop(sec, keys, page / "img" / f"{stem}_whole.png", region, whole=True)}
        cards.append({"id": f"{f}:{name}:{region}", "group": g, "feature": f, "scope": scope, "obs": obs, "raw": r["raw"],
                      "clean": r["clean"], "kind": r["kind"], "imgs": imgs})
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731
    body, last = "", None
    for c in sorted(cards, key=lambda c: c["group"]):
        title, q, opts = GROUPS[c["group"]]
        if c["group"] != last:
            body += f"<h2>{esc(title)}</h2>"
            last = c["group"]
        if c["kind"] == "rule":
            before, after, notes = diff_marks(c["raw"], c["clean"], esc)
            change = (f"<tr><th>교정 전(원시 OCR)</th><td><code>{before}</code></td></tr>"
                      f"<tr><th>교정 후</th><td><code>{after}</code></td></tr>"
                      f"<tr><th>바뀐 글자</th><td>{notes}</td></tr>")
        else:
            change = f"<tr><th>원시 OCR(규칙 미적용)</th><td><code>{esc(c['raw'])}</code></td></tr>"
        radios = "".join(f'<label><input type="radio" name="j:{esc(c["id"])}" value="{v}"> {esc(t)}</label>' for v, t in opts)
        body += f"""
<section class="card" data-id="{esc(c['id'])}">
  <h3>{esc(FEATURE_KO[c['feature']])} · {esc(c['id'].split(':', 1)[1])}</h3>
  <div class="meta">범위: <b>{esc(c['scope'])}</b> · 에이전트 관찰(사람 판단 아님): {esc(c['obs'])}</div>
  <div class="imgs"><figure><figcaption>표시 없음</figcaption><img src="img/{c['imgs']['plain']}"></figure>
  <figure><figcaption>대상 영역(빨간 틀)</figcaption><img src="img/{c['imgs']['mark']}"></figure></div>
  <details><summary>섹션 전체 보기</summary><img class="whole" src="img/{c['imgs']['whole']}"></details>
  <table>{change}</table>
  <div class="form"><b>{esc(q)}</b> (선택 · 채팅으로 줘도 됨)<div class="q">{radios} <button type="button" class="clear">지우기</button></div>
  <input type="text" class="note" placeholder="메모(선택)"></div>
</section>"""
    intro = ("교정 전후에서 바뀐 글자는 노란색으로 표시하고 문자 이름을 적었다 — 영문 O와 숫자 0은 글꼴에서 거의 같아 보인다(2026-10-26 보완). "
             "보류 기능 3종의 후보를 원본 이미지와 비교한다. 표본은 고정 입력본 100섹션(영역 1,815)뿐이며 겹친 앞 글자 · 맨 앞 따옴표는 "
             "규칙에 맞는 실제 사례가 없었다(C · D는 위험 · 참고 사례). 판정 선택은 필수가 아니다(채팅 의견으로 충분). OCR · LLM 추가 호출 없음. "
             "이 페이지는 정답이 아니며 기능 채택 승인이 아니다.")
    data = json.dumps([{"id": c["id"]} for c in cards], ensure_ascii=False)
    doc = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>보류 기능 검수</title>
<style>
body {{ margin:0; background:#fff; color:#111; font:14px/1.55 system-ui, sans-serif; }}
header {{ position:sticky; top:0; background:#fff; border-bottom:1px solid #ddd; padding:8px 16px; z-index:1; }}
main {{ padding:8px 16px 40px; max-width:1400px; }} h2 {{ margin:22px 0 4px; font-size:17px; }} h3 {{ margin:0 0 4px; font-size:15px; }}
.card {{ background:#fafafa; border:1px solid #ddd; border-radius:6px; padding:10px 12px; margin:12px 0; }}
.meta {{ color:#555; font-size:12.5px; }} .imgs {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:6px; }}
figure {{ margin:0; max-width:100%; }} figure img, img.whole {{ max-width:100%; border:1px solid #ddd; background:#fff; }}
figcaption {{ font-weight:600; font-size:12px; }} details {{ margin:6px 0; }} summary {{ cursor:pointer; color:#555; font-size:12.5px; }}
table {{ border-collapse:collapse; margin-top:6px; background:#fff; }} th, td {{ border:1px solid #ddd; padding:3px 6px; text-align:left; }}
code {{ white-space:pre-wrap; font-size:15px; }} mark {{ background:#ffd54f; color:#000; font-weight:700; padding:0 1px; }} .form {{ margin-top:8px; }} .q label {{ margin-right:12px; white-space:nowrap; }} .note {{ width:100%; max-width:800px; }}
</style></head><body>
<header><strong>OCR 후처리 5단계 — 보류 기능 후보 검수</strong> <button type="button" id="export">TSV 내보내기(선택)</button></header>
<main><p class="meta">{esc(intro)}</p>{body}</main>
<script id="cards" type="application/json">{data.replace('</', '<' + chr(92) + '/')}</script>
<script>
const CARDS = JSON.parse(document.getElementById('cards').textContent);
const KEY = 'merge-post-step5-review-v1' + (new URLSearchParams(location.search).get('sandbox') ? ':sandbox' : '');
let store = {{}}; try {{ store = JSON.parse(localStorage.getItem(KEY) || '{{}}') || {{}}; }} catch (e) {{}}
function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(store)); }} catch (e) {{}} }}
CARDS.forEach(c => {{
  const el = document.querySelector('.card[data-id="' + CSS.escape(c.id) + '"]'), s = store[c.id] || {{}};
  el.querySelectorAll('input[type=radio]').forEach(r => {{ r.checked = s.judgment === r.value;
    r.addEventListener('change', () => {{ store[c.id] = Object.assign({{}}, store[c.id], {{judgment: r.value}}); save(); }}); }});
  const n = el.querySelector('.note'); n.value = s.note || '';
  n.addEventListener('input', e => {{ store[c.id] = Object.assign({{}}, store[c.id], {{note: e.target.value}}); save(); }});
  el.querySelector('.clear').addEventListener('click', () => {{ store[c.id] = Object.assign({{}}, store[c.id], {{judgment: ''}}); save();
    el.querySelectorAll('input[type=radio]').forEach(r => r.checked = false); }});
}});
document.getElementById('export').addEventListener('click', () => {{
  const clean = v => String(v == null ? '' : v).replace(/[\\t\\r\\n]+/g, ' ');
  const rows = ['card_id\\tjudgment\\tnote'].concat(CARDS.map(c => {{ const s = store[c.id] || {{}};
    return [c.id, s.judgment, s.note].map(clean).join('\\t'); }}));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([rows.join('\\n') + '\\n'], {{type: 'text/tab-separated-values'}}));
  a.download = 'merge_post_step5_review_' + new Date().toISOString().slice(0, 10) + '.tsv'; a.click();
}});
</script></body></html>
"""
    page.mkdir(parents=True, exist_ok=True)
    (page / "index.html").write_text(doc, encoding="utf-8")
    print(f"{page / 'index.html'}: 카드 {len(cards)}개", {g: sum(1 for c in cards if c['group'] == g) for g in GROUPS})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("scan", "compare", "selfcheck", "page"):
        sp = sub.add_parser(c)
        sp.add_argument("--out", required=True)
        if c != "selfcheck":
            sp.add_argument("--input", required=True)
        if c == "compare":
            sp.add_argument("--llm-run", action="append", help="기존 llm-run 변형 폴더(B3_yonly) — 사후 적용 비교")
    a = ap.parse_args(argv)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    return {"scan": cmd_scan, "compare": cmd_compare, "selfcheck": cmd_selfcheck, "page": cmd_page}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
