"""⑨ 조판 자동 조정 · 경계 이탈 판정 — 육안 검수 페이지(2026-11-24). 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/typeset_auto_adjust_review.py page --run <회차> --env local [--env server]
    python docs/ai-experiments/tools/typeset_auto_adjust_review.py aggregate --run <회차> --env local --tsv <내보낸 TSV>

입력: `typeset_auto_adjust_run.py run`의 <회차>/<env>/result.json · sections/*.png. 출력: <회차>/check/(index.html · img/ · cases.json).
페이지 틀 · 답 형식 · TSV 검증은 기존 검수 도구(`typeset_adjust_review.py`)를 그대로 쓴다.

묶음(측정 환경마다 — 로컬 측정과 서버 HTML 환경 재측정은 탭 이름에 환경을 붙여 나눈다):
1 fit          정상 — 조정 전에도 넘치지 않은 블록(조정하지 않음)
2 linebreak    줄바꿈만으로 해소
3 widen        정렬 방향 폭 확장으로 해소(축소 없음)
4 shrink       축소(최대 −10%)까지 써서 해소(폭 확장과 함께인 것 포함)
5 competition  여백 경쟁 — 먼저 조정된 이웃이 쓸 수 있던 여백을 차지한 블록(해소 · 실패 모두)
6 unresolved   끝까지 실패 — 조정 전 상태로 복원, 넘침 보고(시도한 박스는 보라 점선으로만 표시)
7 clip         섹션 밖 잘림 — 조정 전 또는 후에 글자가 섹션 이미지 밖으로 나간 블록
그림: 섹션 합성(배경 · 글자색 그대로)을 섹션 밖까지 촬영한 그림에서 조정 전 · 후를 같은 자리로 자른다. 섹션 경계는 초록 점선.
섹션 전체 그림은 실제 출력(섹션 크기로 잘린 촬영)이다.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import typeset_adjust_review as base  # noqa: E402  페이지 틀 · TSV 검증

GENERIC = base.GENERIC
SIDE_KO = {"left": "왼쪽", "right": "오른쪽", "top": "위", "bottom": "아래"}
GROUPS: dict[str, dict[str, Any]] = {
    "fit": {"title": "정상", "question": "조정하지 않은 이 블록이 영역 안에 잘 들어가 있나요?",
            "why": "조정 전에도 넘치지 않아 자동 조정이 손대지 않은 블록이다. 판정이 맞는지(실제로 넘치지 않는지) 본다.",
            "labels": ("잘 들어감", "들어가지만 빠듯함", "실제로는 넘침", "판단 불가"), "memo": ""},
    "linebreak": {"title": "줄바꿈 해소", "question": "줄을 바꾼 결과(오른쪽)가 자연스럽고 영역 안에 들어가나요?",
                  "why": "번역문의 명시적 개행을 풀어 브라우저 자연 줄바꿈에 맡겨 해소했다(글자 · 박스 · 크기는 그대로).",
                  "labels": ("괜찮음", "어색하지만 괜찮음", "고쳐야 함", "판단 불가"), "memo": "예: 해시태그가 쪼개짐"},
    "widen": {"title": "확장 성공", "question": "정렬 방향으로 폭을 넓힌 결과(오른쪽)가 괜찮나요?",
              "why": "정렬 방향(left → 오른쪽, right → 왼쪽, center → 양쪽)으로 이웃 글과 간격을 두고 폭을 넓혀 해소했다. 배율 상한은 없다.",
              "labels": ("괜찮음", "넓어진 게 보이지만 괜찮음", "너무 넓거나 구성이 깨짐", "판단 불가"), "memo": "예: 사진 위로 넓어짐"},
    "shrink": {"title": "축소 성공", "question": "글자를 줄인 결과(오른쪽)가 괜찮나요?",
               "why": "줄바꿈 · 폭 확장으로 부족해 글자를 최대 −10%까지 줄여 해소했다. 축소 뒤 폭은 들어가는 최소 폭으로 다시 줄였다.",
               "labels": ("괜찮음", "작아진 게 보이지만 괜찮음", "너무 작거나 위계가 깨짐", "판단 불가"), "memo": ""},
    "competition": {"title": "여백 경쟁", "question": "이웃(주황)과 여백을 나눈 결과가 괜찮나요?",
                    "why": "먼저 조정된 이웃이 이 블록이 넓힐 수 있던 여백을 차지했다(위 → 아래, 왼쪽 → 오른쪽 순서). 두 블록이 같은 자리를 차지하지 않는지,"
                           " 순서 때문에 한쪽만 손해를 보는지 본다.",
                    "labels": ("괜찮음", "아쉽지만 괜찮음", "순서 때문에 잘못 나뉨", "판단 불가"), "memo": "예: 이 블록이 먼저였어야 함"},
    "unresolved": {"title": "끝까지 실패", "question": "복원된 원래 상태(넘침 보고)가 맞는 처리로 보이나요?",
                   "why": "줄바꿈 · 폭 확장 · −10% 축소로도 넘침이 남아 시도한 조정을 버리고 조정 전 상태로 되돌렸다. 보라 점선은 버린 마지막 시도의 박스다.",
                   "labels": ("맞음(고칠 수 없음)", "다른 수단이면 됐을 것", "조정이 잘못됨", "판단 불가"), "memo": "어떤 처리가 맞을지"},
    "clip": {"title": "섹션 밖 잘림", "question": "섹션 경계(초록 점선) 밖으로 나간 글자가 실제로 잘리나요?",
             "why": "글자 경계가 섹션 이미지 밖으로 나가 출력에서 잘리는 블록이다(배치 영역 넘침과 별도 판정). 섹션 전체 그림이 실제 출력이다.",
             "labels": ("잘림 맞음", "닿기만 함", "잘리지 않음(판정 오류)", "판단 불가"), "memo": "예: 하강부만 잘림"},
}
ORDER = tuple(GROUPS)


def _ko_sides(sides) -> str:
    return "·".join(SIDE_KO[s] for s in sides) or "없음"


def _judge_text(j: dict[str, Any] | None) -> str:
    if not j:
        return "-"
    st = j["status"]
    if st == "measure_failed":
        return "측정 실패"
    bo, sc = j["boxOverflow"], j["sectionClip"]
    parts = []
    parts.append(f"영역 넘침 {_ko_sides([s for s in SIDE_KO if bo[s]])}" + (f" {bo['excessPx']['max']:.1f}px" if bo["any"] else ""))
    if sc["any"]:
        parts.append(f"섹션 잘림 {_ko_sides([s for s in SIDE_KO if sc[s]])} {sc['excessPx']['max']:.1f}px")
    return " · ".join(parts)


def _adjust_text(row: dict[str, Any]) -> str:
    out = []
    for s in row.get("steps", []):
        if not s.get("applied"):
            continue
        if s["op"] == "linebreak":
            out.append(f"줄바꿈({s['linesBefore']}→{s['linesAfter']}줄)")
        elif s["op"] == "widen":
            out.append(f"폭 {s['width'][0]:.0f}→{s['width'][1]:.0f}px({s['width'][1] / s['width'][0]:.2f}배 · {s['align']})")
        elif s["op"] == "shrink":
            out.append(f"축소 ×{s['scale']:.3f}")
    return " → ".join(out) or "없음"


def collect(run: Path, env: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    R = json.loads((run / env / "result.json").read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for s in R["sections"]:
        blocks = {b["blockId"]: b for b in s["blocks"]}
        rb = {b["blockId"]: b for b in s["renderBlocks"]}
        adjb = {int(k): v for k, v in s["adjust"]["blocks"].items()}
        jb = {int(k): v for k, v in s["judge"]["before"].items()}
        ja = {int(k): v for k, v in s["judge"]["after"].items()}
        for bid, row in adjb.items():
            b = blocks[bid]
            st = row["status"]
            ops = {x["op"] for x in row.get("steps", []) if x.get("applied")}
            groups = []
            if st == "not_needed":
                groups.append("fit")
            elif st == "resolved":
                groups.append("shrink" if "shrink" in ops else "widen" if "widen" in ops else "linebreak")
            elif st == "unresolved":
                groups.append("unresolved")
            if row.get("competition"):
                groups.append("competition")
            if (jb.get(bid) or {}).get("status") == "section_clip" or (ja.get(bid) or {}).get("status") == "section_clip":
                groups.append("clip")
            for g in groups:
                rects = [{"kind": "orig", "side": sd, **b["box"]} for sd in "LR"]
                rects += [{"kind": "sec", "side": sd, "x": 0, "y": 0, "w": s["width"], "h": s["height"]} for sd in "LR"]
                if rb[bid]["box"] != b["box"]:
                    rects.append({"kind": "adj", "side": "R", **rb[bid]["box"]})
                if st == "unresolved" and row.get("attempt") and row["attempt"]["box"] != b["box"]:
                    rects.append({"kind": "att", "side": "R", **row["attempt"]["box"]})
                for c in row.get("competition") or []:
                    if c in rb:
                        rects.append({"kind": "nb", "side": "R", **rb[c]["box"]})
                label = "버린 시도(복원함)" if st == "unresolved" else "적용한 조정"
                info = f"{label}: {_adjust_text(row)}  |  조정 전 판정: {_judge_text(jb.get(bid))}  →  조정 후: {_judge_text(ja.get(bid))}"
                tool = (f"상태 {st} · 원인 {', '.join(row.get('causes', [])) or '-'} · 측정 {row.get('measures', 0)}회"
                        f" · 경쟁 이웃 {row.get('competition') or '-'} · 판정 {(jb.get(bid) or {}).get('status')} → {(ja.get(bid) or {}).get('status')}")
                cases.append({"group": g, "key": f"{s['key']}/b{bid}", "section": s, "bid": bid, "text": b["text"], "role": b["role"],
                              "rects": rects, "info": info, "tool": tool,
                              "lines": [[ln["text"] for ln in (s["adjust"]["before"].get(str(bid)) or {}).get("lines", [])],
                                        [ln["text"] for ln in (s["adjust"]["after"].get(str(bid)) or {}).get("lines", [])]]
                              if g in ("linebreak", "widen", "shrink") else None,
                              "inks": [x for x in ((jb.get(bid) or {}).get("ink"), (ja.get(bid) or {}).get("ink")) if x]})
    return R, cases


def build(run: Path, envs: list[str]) -> Path:
    from PIL import Image
    out = run / "check"
    (out / "img").mkdir(parents=True, exist_ok=True)
    data, groups, metas, fp_items = [], [], [], []
    n_in: collections.Counter = collections.Counter()
    for env in envs:
        R, cases = collect(run, env)
        m = R["meta"]
        metas.append(f"{env}: {m['env']['backend']} · Chromium {m['env'].get('chromium')} · {m['env'].get('platform', {}).get('system')} · 간격 {m['params']['gapPx']}px")
        fp_items.append([env, m["inputSha256"], m["env"], m["params"], m["rules"]])
        cache: dict[Path, Any] = {}
        for c in sorted(cases, key=lambda c: (ORDER.index(c["group"]), hashlib.sha256(f"{c['group']}:{c['key']}".encode()).hexdigest())):
            gid = f"{env}:{c['group']}"
            s = c["section"]
            margin = s["margin"]
            xs = [r["x"] for r in c["rects"] if r["kind"] != "sec"] + [r["x"] + r["w"] for r in c["rects"] if r["kind"] != "sec"]
            ys = [r["y"] for r in c["rects"] if r["kind"] != "sec"] + [r["y"] + r["h"] for r in c["rects"] if r["kind"] != "sec"]
            for ink in c["inks"]:
                xs += [ink["left"], ink["right"]]
                ys += [ink["top"], ink["bottom"]]
            h = max(r["h"] for r in c["rects"] if r["kind"] == "orig")
            pad = max(28.0, h * 0.4)
            x0, y0 = max(-margin, min(xs) - pad), max(-margin, min(ys) - pad)
            x1, y1 = min(s["width"] + margin, max(xs) + pad), min(s["height"] + margin, max(ys) + pad)
            img = {}
            for side, tag in (("L", "before"), ("R", "after")):
                rel = s["images"].get(f"{tag}.full") or s["images"].get(f"{tag}.clip")
                p = run / env / rel
                if p not in cache:
                    if len(cache) > 6:
                        cache.clear()
                    cache[p] = Image.open(p).convert("RGB")
                g = margin if rel.endswith(".full.png") else 0
                im = cache[p]
                box = (max(0, round(x0 + g)), max(0, round(y0 + g)), min(im.width, round(x1 + g)), min(im.height, round(y1 + g)))
                name = f"img/{env}_{c['group']}_{c['key'].replace('/', '_')}.{side}.png"
                im.crop(box).save(out / name)
                img[side] = name
            n_in[gid] += 1
            thumb_rel = s["images"].get("after.clip")
            data.append({"id": f"{gid}:{c['key']}", "g": gid, "n": n_in[gid], "key": c["key"], "text": c["text"], "role": c["role"],
                         "img": img, "crop": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "dpr": 1}, "rects": c["rects"],
                         "thumb": {"src": f"../{env}/{thumb_rel}", "w": s["width"], "h": s["height"]} if thumb_rel else None,
                         "info": c["info"], "tool": c["tool"], "lines": c["lines"], "control": False, "rejected": False,
                         "labels": {"L": "조정 전", "R": "조정 후(해소 못 하면 복원)"}})
            fp_items.append([gid, c["key"], c["rects"], c["info"]])
        for g in ORDER:
            gid = f"{env}:{g}"
            groups.append({"id": gid, **GROUPS[g], "title": f"{GROUPS[g]['title']} [{env}]",
                           "codes": [x for x, _ in GENERIC], "n": n_in[gid]})
    fp = hashlib.sha256(json.dumps(fp_items, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]
    _finish_page(out, data, groups, metas, fp, envs,
                 "<li>자동 조정(줄바꿈 → 정렬 방향 폭 확장 → 글자 축소 최대 −10%, PM 결정 11-24)과 경계 판정 결과를 묶음으로 나눴습니다."
                 " 탭 이름의 [local]은 이 PC 측정, [server]는 서버 HTML 렌더 환경 재측정입니다.")
    return out / "index.html"


def _finish_page(out: Path, data: list, groups: list, metas: list, fp: str, envs: list, guide: str) -> None:
    """기존 검수 도구의 페이지 틀에 이 도구의 선 색 · 안내를 넣어 쓴다."""
    page = base.PAGE
    page = page.replace("const COLOR={orig:['#e0302a',''],adj:['#1f6fd1','6 4'],prop:['#1f6fd1','6 4'],nb:['#ef8a17','']};",
                        "const COLOR={orig:['#e0302a',''],adj:['#1f6fd1','6 4'],prop:['#1f6fd1','6 4'],nb:['#ef8a17',''],sec:['#16a34a','3 3'],att:['#7c3aed','4 3']};")
    page = page.replace("${d.g==='overlap'?'<i style=\"border-color:#ef8a17\"></i>이웃':''}",
                        "<i style=\"border-color:#16a34a;border-top-style:dashed\"></i>섹션 경계<i style=\"border-color:#ef8a17\"></i>경쟁 이웃"
                        "<i style=\"border-color:#7c3aed;border-top-style:dashed\"></i>버린 시도")
    page = page.replace("<li>자동 조정 실험(줄바꿈 → 폭 우선 → 영역 확장 → 글자 축소 −10%, 박스 자동 맞춤)의 결과를 묶음 7개로 나눴습니다. 위 탭으로 묶음을 고릅니다.",
                        guide)
    page = page.replace("조정 뒤 영역<i style=\"border-color:#ef8a17\"></i>이웃 블록</span></li>",
                        "조정 뒤 영역<i style=\"border-color:#16a34a;border-top-style:dashed\"></i>섹션 경계"
                        "<i style=\"border-color:#ef8a17\"></i>경쟁 이웃<i style=\"border-color:#7c3aed;border-top-style:dashed\"></i>버린 시도</span></li>")
    page = page.replace("<title>자동 조정 육안 검수</title>", "<title>자동 조정 · 경계 판정 검수</title>")
    page = page.replace("<h1>자동 조정 육안 검수</h1>", "<h1>자동 조정 · 경계 판정 검수</h1>")
    page = page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")) \
        .replace("__GROUPS__", json.dumps(groups, ensure_ascii=False).replace("</", "<\\/")) \
        .replace("__KEY__", json.dumps(f"typeset-auto-adjust-review:{fp}")).replace("__FP__", json.dumps(fp)) \
        .replace("__META__", base.esc(" / ".join(metas) + f" · 지문 {fp}"))
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "cases.json").write_text(json.dumps({"fingerprint": fp, "envs": envs, "cases": [
        {"id": d["id"], "group": d["g"], "key": d["key"], "info": d["info"], "tool": d["tool"]} for d in data]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"page": str(out / "index.html"), "fingerprint": fp, "groups": {g["id"]: g["n"] for g in groups}}, ensure_ascii=False, indent=1))


FOLLOWUP_GROUP = {"title": "높이 확장 후보 — 복잡한 바탕",
                  "question": "폭을 넓히고(축소) 아래로 늘린 결과(오른쪽)가 보기에 괜찮나요?",
                  "why": "끝까지 실패한 블록 중 높이를 더 쓰면 풀리는 것이다(PM 순서에는 높이 확장이 없다). 늘어나는 자리 바탕이 단색인 것은 에이전트가 "
                         "괜찮다고 보고 뺐고, 사진 · 그래픽 · 무늬 위로 늘어나는 것만 남겼다. 파란 점선 = 늘린 영역.",
                  "labels": ("괜찮음", "보이지만 괜찮음", "안 됨(사진 · 그래픽 · 다른 글 위)", "판단 불가"), "memo": ""}


def build_followup(run: Path, env: str) -> Path:
    """끝까지 실패 블록의 에이전트 1차 분류(followup.json) 중 사람 검수 대상만 페이지로 만든다 — <회차>/check-followup/."""
    from PIL import Image
    out = run / "check-followup"
    (out / "img").mkdir(parents=True, exist_ok=True)
    R = json.loads((run / env / "result.json").read_text(encoding="utf-8"))
    F = json.loads((run / env / "followup.json").read_text(encoding="utf-8"))
    secs = {s["key"]: s for s in R["sections"]}
    gid = f"{env}:height_busy"
    data, fp_items = [], [[env, R["meta"]["inputSha256"], F["summary"]]]
    rows = [r for r in F["rows"] if r["cls"] == "height" and r["height"]["agent"] == "human"]
    for n, r in enumerate(sorted(rows, key=lambda r: hashlib.sha256(r["key"].encode()).hexdigest()), 1):
        sk, bid = r["key"].rsplit("/b", 1)
        s, bid = secs[sk], int(bid)
        blk = next(b for b in s["blocks"] if b["blockId"] == bid)
        M, hb, ab = s["margin"], r["height"]["box"], r["attempt"]["box"]
        rects = [{"kind": "orig", "side": sd, **blk["box"]} for sd in "LR"]
        rects += [{"kind": "sec", "side": sd, "x": 0, "y": 0, "w": s["width"], "h": s["height"]} for sd in "LR"]
        rects += [{"kind": "adj", "side": "R", **hb}]
        ink = s["judge"]["before"][str(bid)].get("ink")
        xs = [blk["box"]["x"], blk["box"]["x"] + blk["box"]["w"], hb["x"], hb["x"] + hb["w"]] + ([ink["left"], ink["right"]] if ink else [])
        ys = [blk["box"]["y"], blk["box"]["y"] + blk["box"]["h"], hb["y"], hb["y"] + hb["h"]] + ([ink["top"], ink["bottom"]] if ink else [])
        pad = max(40.0, blk["box"]["h"] * 0.5)
        x0, y0 = max(-M, min(xs) - pad), max(-M, min(ys) - pad)
        x1, y1 = min(s["width"] + M, max(xs) + pad), min(s["height"] + M, max(ys) + pad)
        img = {}
        for side, rel in (("L", s["images"]["after.full"]), ("R", r["images"]["full"])):
            im = Image.open(run / env / rel).convert("RGB")
            name = f"img/{env}_{r['key'].replace('/', '_')}.{side}.png"
            im.crop((max(0, round(x0 + M)), max(0, round(y0 + M)), min(im.width, round(x1 + M)), min(im.height, round(y1 + M)))).save(out / name)
            img[side] = name
        w0 = blk["box"]["w"]
        info = (f"마지막 시도 + 높이: 폭 {w0:.0f}→{ab['w']:.0f}px({ab['w'] / w0:.2f}배) · 배율 ×{r['height']['scale']:.2f} · "
                f"아래로 +{r['height']['extraPx']:.0f}px  |  왼쪽은 지금 출력(조정 전으로 복원, 넘침)")
        tool = f"늘어나는 자리 바탕 경계 비율 {r['height']['busyRatio']:.3f}(기준 0.05) · 필요 배율(폭 확장 상태) {r['needScale']} · 원인 {r['causes']}"
        data.append({"id": f"{gid}:{r['key']}", "g": gid, "n": n, "key": r["key"], "text": blk["text"], "role": blk["role"], "img": img,
                     "crop": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "dpr": 1}, "rects": rects,
                     "thumb": {"src": f"../{env}/{r['images']['clip']}", "w": s["width"], "h": s["height"]}, "info": info, "tool": tool,
                     "lines": None, "control": False, "rejected": False, "labels": {"L": "지금(복원 · 넘침)", "R": "높이까지 쓴 경우"}})
        fp_items.append([r["key"], rects, info])
    groups = [{"id": gid, **FOLLOWUP_GROUP, "title": f"{FOLLOWUP_GROUP['title']} [{env}]", "codes": [x for x, _ in GENERIC], "n": len(data)}]
    fp = hashlib.sha256(json.dumps(fp_items, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]
    sm = F["summary"]
    total = sum(v for k, v in sm.items() if k != "height_human")
    _finish_page(out, data, groups, [f"{env} 에이전트 1차 분류 {sm}"], fp, [env],
                 f"<li>끝까지 실패한 {total}블록과 섹션 밖 잘림 14블록은 에이전트가 측정으로 먼저 가렸습니다(결과는 실험 기록). "
                 f"사람 눈이 필요한 {len(data)}건만 남겼습니다.")
    return out / "index.html"


def aggregate(run: Path, tsv: Path) -> dict[str, Any]:
    c = json.loads((run / "check" / "cases.json").read_text(encoding="utf-8"))
    seen = base.read_tsv(tsv, c["fingerprint"], {f"{x['group']}:{x['key']}" for x in c["cases"]})
    res: dict[str, Any] = {"fingerprint": c["fingerprint"], "note": "사람 판단은 본 사례에 대한 것이다(전수 아님 · GS-01 · GS-02 표본)", "groups": {}}
    for gid in sorted({x["group"] for x in c["cases"]}):
        g = gid.split(":", 1)[1]
        labels = dict(zip((x for x, _ in GENERIC), GROUPS[g]["labels"]))
        ids = [f"{gid}:{x['key']}" for x in c["cases"] if x["group"] == gid]
        res["groups"][gid] = {"cases": len(ids), "reviewed": sum(1 for i in ids if (seen.get(i) or {}).get("verdict")),
                              "verdicts": dict(collections.Counter(labels.get((seen.get(i) or {}).get("verdict"), "미검토") for i in ids)),
                              "memos": [{"key": i, "note": seen[i]["note"]} for i in ids if i in seen and seen[i]["note"].strip()]}
    (run / "check" / "review_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("page")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--env", action="append", required=True)
    a = sub.add_parser("followup")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--env", default="local")
    a = sub.add_parser("aggregate")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--tsv", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "page":
        build(args.run.resolve(), args.env)
    elif args.cmd == "followup":
        build_followup(args.run.resolve(), args.env)
    else:
        print(json.dumps(aggregate(args.run.resolve(), args.tsv), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
