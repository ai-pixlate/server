"""② OCR 품질 표본 정답 도구 — 채점 명세 v2(`docs/ai-experiments/2026-09-23_02-ocr_scoring-spec-v2.md`)용.

레포 루트에서 실행한다(pipeline 패키지를 쓴다).

    python docs/ai-experiments/tools/ocr_truth/truth.py init                 # 표본 12개 정답 파일 생성(빈 줄 목록)
    python docs/ai-experiments/tools/ocr_truth/truth.py draft [--only ID] [--model M]   # Gemini 전사 초안(GEMINI_API_KEY)
    python docs/ai-experiments/tools/ocr_truth/truth.py serve [--port 8765]  # 브라우저 작성·검수 화면
    python docs/ai-experiments/tools/ocr_truth/truth.py check [--only ID]    # 자동 검사
    python docs/ai-experiments/tools/ocr_truth/truth.py status               # 섹션별 진행 상태
    python docs/ai-experiments/tools/ocr_truth/truth.py freeze               # 12개 모두 검수 완료 시 동결 + MANIFEST

원칙
- Gemini 결과는 초안(`source: gemini_draft`, `verified: false`)이다. 사람이 원본 이미지와 줄마다 대조해 `verified`를 켠다.
- 이미 줄이 있는 파일에는 초안을 덮어쓰지 않는다. 동결된 파일은 어떤 명령으로도 고치지 않는다.
- 모든 변경은 파일 안 `history`에 남는다(줄 추가·삭제·필드 변경의 전후 값, 시각, 주체, 사유).
- 정답은 섹션 이미지 SHA-256에 묶인다. 이미지가 바뀌면 check가 실패한다.
- 이 도구는 OCR을 실행하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
from pipeline import jsonio  # noqa: E402

HERE = Path(__file__).resolve().parent
EXP = REPO / "docs" / "ai-experiments"
TRUTH_DIR = EXP / "ocr_truth" / "v1"
RUNS = EXP / "runs" / "ocr_truth_v1"
V1 = REPO / "pipeline" / "out" / "ocr_input" / "v1"
SPEC = "docs/ai-experiments/2026-09-23_02-ocr_scoring-spec-v2.md"
TAGS = ("illegible", "section_cut", "source_cut", "logo", "product_label", "ink_overlap")
MARGIN_MAX = 0.10  # 채점 명세 v2 1.2 — 사람 검수 항목(자동 검사하지 않음)

# 품질 표본 12개 (2026-09-23 사용자 확정, status.md 4절)
SAMPLES = [
    ("GS-02_014", "sec_1_09", "흰·연회색 본문(Q&A 밀집)"),
    ("GS-01_002", "sec_1_16", "흰 배경 목록 본문"),
    ("GS-01_002", "sec_1_17", "사진 배경 큰 제목+본문"),
    ("GS-03_002", "sec_1_04", "노란 배경 큰 제목+배지"),
    ("GS-02_001", "sec_1_04", "어두운 배경 유의사항"),
    ("GS-03_018", "sec_1_11", "고시 표·작은 글씨"),
    ("GS-01_002", "sec_1_15", "바다 사진 위 글자"),
    ("GS-02_007", "sec_1_02", "얼굴 사진 위 수치"),
    ("GS-02_001", "sec_1_03", "이벤트 방법·날짜 표"),
    ("GS-01_002", "sec_1_05", "영문·숫자(SPF/PA) 표"),
    ("GS-02_002", "sec_1_01", "로고·제품 라벨"),
    ("GS-03_016", "sec_1_01", "원본 경계 잘림"),
]

DRAFT_PROMPT = """This image is one section of a Korean e-commerce product detail page.
Transcribe ALL visible text as a list of LINES, for building OCR ground truth.

Rules:
- One entry per visual text line: characters on the same baseline that read continuously.
- Table cells are separate lines. Never merge text across cell borders or columns.
- Order: top to bottom, then left to right.
- Copy characters exactly as shown (Korean, English, digits, symbols such as * ※ · ® ™ %). Do not correct, translate, or normalize.
- Keep spaces as they appear.
- Include small print, footnotes, badges, logos made of letters, and text on product packaging.
- Skip anything that is not text (icons, shapes, photos).
- If a line is visibly text but unreadable, give text "" and set illegible true.
- box_2d = tight box around the ink of that line: [ymin, xmin, ymax, xmax] normalized to 0-1000.
"""
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "box_2d": {"type": "array", "items": {"type": "integer"}},
                    "illegible": {"type": "boolean"},
                },
                "required": ["text", "box_2d"],
            },
        }
    },
    "required": ["lines"],
}


# ---- 파일 ----------------------------------------------------------------------------
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truth_id(sample: str, key: str) -> str:
    return f"{sample}__{key}"


def truth_path(tid: str) -> Path:
    return TRUTH_DIR / f"{tid}.json"


def load(tid: str) -> dict[str, Any]:
    return json.loads(truth_path(tid).read_text(encoding="utf-8"))


def save(doc: dict[str, Any]) -> None:
    truth_path(doc["id"]).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def all_ids() -> list[str]:
    return [truth_id(s, k) for s, k, _ in SAMPLES]


def section_of(sample: str, key: str):
    split = jsonio.load_split(V1 / sample / "split.json")
    return next(s for s in split.sections if s.section_key == key)


def image_file(doc: dict[str, Any]) -> Path:
    return REPO / doc["image"]["path"]


def git_commit_of(path: str) -> str | None:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%H", "--", path], cwd=REPO, capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def next_line_id(doc: dict[str, Any]) -> str:
    used = [int(l["line_id"][1:]) for l in doc["lines"]] + [int(h["line_id"][1:]) for h in doc["history"] if h.get("line_id")]
    return f"L{(max(used) if used else 0) + 1:03d}"


# ---- init ----------------------------------------------------------------------------
def cmd_init(args) -> int:
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    for sample, key, kind in SAMPLES:
        tid = truth_id(sample, key)
        if truth_path(tid).exists():
            print(f"{tid}: 이미 있음 — 건너뜀")
            continue
        sec = section_of(sample, key)
        img = Path(sec.image_path)
        doc = {
            "truth_format": 1,
            "id": tid,
            "sample": sample,
            "section_key": key,
            "kind": kind,
            "spec": {"path": SPEC, "commit": None},  # freeze 때 채운다
            "image": {"path": img.relative_to(REPO).as_posix(), "sha256": jsonio.sha256_file(img),
                      "width": sec.width, "height": sec.height},
            "review": {"status": "empty", "mode": "1인 검수", "reviewer_count": 1, "reviewed_at": None, "frozen_at": None},
            "lines": [],
            "history": [{"at": now(), "by": "tool", "action": "init"}],
        }
        save(doc)
        print(f"{tid}: 생성")
    return 0


# ---- draft (Gemini) ------------------------------------------------------------------
def box_from_2d(box: list[int], w: int, h: int) -> dict[str, int] | None:
    if len(box) != 4:
        return None
    y0, x0, y1, x1 = (max(0, min(1000, int(v))) for v in box)
    if y1 <= y0 or x1 <= x0:
        return None
    X0, X1 = round(x0 * w / 1000), round(x1 * w / 1000)
    Y0, Y1 = round(y0 * h / 1000), round(y1 * h / 1000)
    return {"x": X0, "y": Y0, "w": max(1, X1 - X0), "h": max(1, Y1 - Y0)}


def gemini_transcribe(image_path: Path, model: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 없다 — 키가 있는 환경에서 실행한다(값은 출력·기록하지 않는다)")
    from google import genai
    from google.genai import types as gtypes
    from PIL import Image

    client = genai.Client(api_key=key)
    config = gtypes.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=DRAFT_SCHEMA,
        automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(disable=True),
    )
    with Image.open(image_path) as im:
        resp = client.models.generate_content(model=model, contents=[DRAFT_PROMPT, im.convert("RGB")], config=config)
    return resp.text or ""


def apply_draft(doc: dict[str, Any], response_text: str, model: str) -> int:
    data = json.loads(response_text)
    w, h = doc["image"]["width"], doc["image"]["height"]
    n = 0
    for item in data.get("lines", []):
        lid = next_line_id(doc)
        tags = ["illegible"] if item.get("illegible") else []
        line = {"line_id": lid, "text": item.get("text", ""), "bbox": box_from_2d(item.get("box_2d", []), w, h),
                "tags": tags, "source": "gemini_draft", "verified": False, "note": ""}
        doc["lines"].append(line)
        doc["history"].append({"at": now(), "by": f"gemini:{model}", "action": "add", "line_id": lid, "after": line})
        n += 1
    doc["review"]["status"] = "draft"
    return n


def cmd_draft(args) -> int:
    import tomllib

    model = args.model or tomllib.loads((REPO / "pipeline/config/default.toml").read_text(encoding="utf-8"))["section"]["vlm_model"]
    RUNS.mkdir(parents=True, exist_ok=True)
    for tid in ([args.only] if args.only else all_ids()):
        doc = load(tid)
        if doc["review"]["status"] == "frozen" or doc["lines"]:
            print(f"{tid}: 줄이 이미 있거나 동결됨 — 초안을 덮어쓰지 않는다")
            continue
        if jsonio.sha256_file(image_file(doc)) != doc["image"]["sha256"]:
            raise SystemExit(f"{tid}: 섹션 이미지 SHA-256이 정답 파일과 다르다")
        text = args.replay.read_text(encoding="utf-8") if args.replay else gemini_transcribe(image_file(doc), model)
        (RUNS / f"{tid}__draft_response.json").write_text(text, encoding="utf-8")
        n = apply_draft(doc, text, model)
        save(doc)
        print(f"{tid}: 초안 {n}줄")
    return 0


# ---- check ---------------------------------------------------------------------------
def check_doc(doc: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    img = image_file(doc)
    if not img.is_file():
        errs.append(f"이미지 없음 {doc['image']['path']}")
    elif jsonio.sha256_file(img) != doc["image"]["sha256"]:
        errs.append("이미지 SHA-256 불일치")
    W, H = doc["image"]["width"], doc["image"]["height"]
    ids = [l["line_id"] for l in doc["lines"]]
    if len(ids) != len(set(ids)):
        errs.append("line_id 중복")
    boxes = []
    for l in doc["lines"]:
        lid = l["line_id"]
        bad = [t for t in l["tags"] if t not in TAGS]
        if bad:
            errs.append(f"{lid}: 모르는 태그 {bad}")
        if not l["text"].strip() and "illegible" not in l["tags"]:
            errs.append(f"{lid}: 텍스트가 비었는데 illegible 태그 없음")
        b = l.get("bbox")
        if b is None:
            errs.append(f"{lid}: bbox 없음")
            continue
        if not all(isinstance(b.get(k), int) for k in "xywh") or b["w"] <= 0 or b["h"] <= 0:
            errs.append(f"{lid}: bbox 좌표 무효 {b}")
            continue
        if b["x"] < 0 or b["y"] < 0 or b["x"] + b["w"] > W or b["y"] + b["h"] > H:
            errs.append(f"{lid}: bbox가 섹션 밖 {b} (섹션 {W}x{H})")
        boxes.append((lid, b, "ink_overlap" in l["tags"]))
    for i, (la, a, oa) in enumerate(boxes):
        for lb, b, ob in boxes[i + 1:]:
            ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            if ix > 0 and iy > 0 and not (oa and ob):
                errs.append(f"{la}·{lb}: bbox 겹침 {ix}x{iy}px (둘 다 ink_overlap이어야 허용)")
    return errs


def cmd_check(args) -> int:
    bad = 0
    for tid in ([args.only] if args.only else all_ids()):
        errs = check_doc(load(tid))
        print(f"{tid}: {'통과' if not errs else f'{len(errs)}건'}")
        for e in errs:
            print(f"   - {e}")
        bad += bool(errs)
    return 1 if bad else 0


def cmd_status(args) -> int:
    for tid in all_ids():
        doc = load(tid)
        n = len(doc["lines"])
        v = sum(1 for l in doc["lines"] if l["verified"])
        print(f"{tid:24s} {doc['review']['status']:9s} 줄 {n:3d} · 검수 {v:3d} · 자동검사 {len(check_doc(doc))}건 · {doc['kind']}")
    return 0


# ---- 저장(화면) -----------------------------------------------------------------------
_LINE_FIELDS = ("text", "bbox", "tags", "verified", "note")
_CONTENT_FIELDS = ("text", "bbox", "tags")  # 바뀌면 다시 대조해야 하는 필드


def merge_edit(doc: dict[str, Any], new_lines: list[dict[str, Any]], reason: str) -> int:
    """화면에서 받은 줄 목록을 반영하고 차이를 history에 남긴다. 반환: 기록한 변경 수.

    - 텍스트 · bbox · 태그가 바뀐 줄은 미검수로 되돌린다. 화면이 "수정한 뒤 다시 대조했다"(`reverified: true`)고
      보낸 경우에만 검수를 유지한다 — 검수 뒤 바뀐 내용이 재확인 없이 동결되지 않게.
    - 줄 순서가 바뀌면 전후 줄 ID 목록을 `reorder`로 남긴다.
    """
    if doc["review"]["status"] == "frozen":
        raise ValueError("동결된 정답은 고칠 수 없다")
    old = {l["line_id"]: l for l in doc["lines"]}
    seen = set()
    changes = 0
    out_lines = []
    for nl in new_lines:
        lid = nl.get("line_id") or next_line_id({"lines": doc["lines"] + out_lines, "history": doc["history"]})
        line = {"line_id": lid, "text": str(nl.get("text", "")), "bbox": nl.get("bbox"),
                "tags": sorted(set(nl.get("tags", []))), "verified": bool(nl.get("verified", False)),
                "note": str(nl.get("note", "")), "source": old.get(lid, {}).get("source", "human")}
        seen.add(lid)
        if lid not in old:
            if line["verified"] and not nl.get("reverified"):
                line["verified"] = False
            doc["history"].append({"at": now(), "by": "human", "action": "add", "line_id": lid, "after": line, "reason": reason})
            changes += 1
        else:
            content_changed = any(old[lid].get(k) != line.get(k) for k in _CONTENT_FIELDS)
            reset = content_changed and line["verified"] and not nl.get("reverified")
            if reset:
                line["verified"] = False
            before = {k: old[lid].get(k) for k in _LINE_FIELDS}
            after = {k: line.get(k) for k in _LINE_FIELDS}
            diff = {k: {"before": before[k], "after": after[k]} for k in _LINE_FIELDS if before[k] != after[k]}
            if diff:
                entry = {"at": now(), "by": "human", "action": "edit", "line_id": lid, "changes": diff, "reason": reason}
                if reset:
                    entry["verified_reset"] = "내용이 바뀌어 미검수로 되돌림(수정 뒤 재대조 표시 없음)"
                doc["history"].append(entry)
                changes += 1
        out_lines.append(line)
    for lid, l in old.items():
        if lid not in seen:
            doc["history"].append({"at": now(), "by": "human", "action": "delete", "line_id": lid, "before": l, "reason": reason})
            changes += 1
    kept_before = [l["line_id"] for l in doc["lines"] if l["line_id"] in seen]
    kept_after = [l["line_id"] for l in out_lines if l["line_id"] in old]
    if kept_before != kept_after:
        doc["history"].append({"at": now(), "by": "human", "action": "reorder",
                               "before": [l["line_id"] for l in doc["lines"]], "after": [l["line_id"] for l in out_lines],
                               "reason": reason})
        changes += 1
    doc["lines"] = out_lines
    all_verified = bool(out_lines) and all(l["verified"] for l in out_lines)
    status_before = doc["review"]["status"]
    doc["review"]["status"] = "reviewed" if all_verified else ("draft" if out_lines else "empty")
    if not all_verified:
        doc["review"]["reviewed_at"] = None
    elif status_before != "reviewed" or changes:
        doc["review"]["reviewed_at"] = now()  # 검수 완료 상태에서 내용이 바뀌어 다시 확인됐으면 시각을 갱신
    if doc["review"]["status"] != status_before:
        doc["history"].append({"at": now(), "by": "tool", "action": "status", "before": status_before, "after": doc["review"]["status"]})
    return changes


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, fmt, *a):  # 조용히
        pass

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            return self._send(200, (HERE / "annotator.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/list":
            items = []
            for tid in all_ids():
                d = load(tid)
                items.append({"id": tid, "kind": d["kind"], "status": d["review"]["status"], "lines": len(d["lines"]),
                              "verified": sum(1 for l in d["lines"] if l["verified"])})
            return self._json(200, {"items": items, "tags": TAGS, "margin_max": MARGIN_MAX})
        if path.startswith("/api/truth/"):
            tid = path.removeprefix("/api/truth/")
            if tid not in all_ids():
                return self._json(404, {"error": "unknown id"})
            d = load(tid)
            return self._json(200, {**d, "check": check_doc(d)})
        if path.startswith("/image/"):
            tid = path.removeprefix("/image/")
            if tid not in all_ids():
                return self._json(404, {"error": "unknown id"})
            return self._send(200, image_file(load(tid)).read_bytes(), "image/png")
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = unquote(self.path.split("?")[0])
        if not path.startswith("/api/truth/"):
            return self._json(404, {"error": "not found"})
        tid = path.removeprefix("/api/truth/")
        if tid not in all_ids():
            return self._json(404, {"error": "unknown id"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        doc = load(tid)
        try:
            n = merge_edit(doc, body["lines"], body.get("reason", ""))
        except (ValueError, KeyError, TypeError) as e:
            return self._json(400, {"error": str(e)})
        save(doc)
        return self._json(200, {"saved": n, "status": doc["review"]["status"], "check": check_doc(doc)})


def cmd_serve(args) -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"정답 작성·검수 화면: http://127.0.0.1:{args.port}/  (Ctrl+C로 종료)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ---- freeze --------------------------------------------------------------------------
def cmd_freeze(args) -> int:
    spec_commit = git_commit_of(SPEC)
    if not spec_commit:
        raise SystemExit(f"{SPEC}의 커밋 해시를 찾지 못했다 — 명세가 커밋돼 있어야 한다")
    pending = subprocess.run(["git", "status", "--porcelain", "--", SPEC], cwd=REPO, capture_output=True, text=True).stdout.strip()
    if pending:
        raise SystemExit(f"{SPEC}에 커밋되지 않은 수정이 있다 — 명세를 먼저 커밋해야 동결 기준 커밋이 맞는다")
    problems = []
    for tid in all_ids():
        d = load(tid)
        if d["review"]["status"] == "frozen":
            problems.append(f"{tid}: 이미 동결됨")
        elif d["review"]["status"] != "reviewed":
            problems.append(f"{tid}: 검수 미완료({d['review']['status']})")
        problems += [f"{tid}: {e}" for e in check_doc(d)]
    if problems:
        print("동결하지 않음:")
        for p in problems:
            print(f"   - {p}")
        return 1
    at = now()
    manifest = {"truth_version": "v1", "frozen_at": at, "spec": {"path": SPEC, "commit": spec_commit},
                "review_mode": "1인 검수", "files": {}}
    for tid in all_ids():
        d = load(tid)
        d["spec"]["commit"] = spec_commit
        d["review"].update(status="frozen", frozen_at=at)
        d["history"].append({"at": at, "by": "tool", "action": "freeze", "spec_commit": spec_commit})
        save(d)
        manifest["files"][f"{tid}.json"] = {"sha256": jsonio.sha256_file(truth_path(tid)), "image_sha256": d["image"]["sha256"],
                                            "lines": len(d["lines"])}
    (TRUTH_DIR / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"동결 완료 — 명세 커밋 {spec_commit[:7]}, MANIFEST.json 작성. 이 상태로 커밋해야 동결이 기록된다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="truth.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sp = sub.add_parser("draft")
    sp.add_argument("--only")
    sp.add_argument("--model", help="기본: config section.vlm_model")
    sp.add_argument("--replay", type=Path, help="저장된 응답 JSON으로 초안 적용(테스트·재현용, API 호출 없음)")
    sp.set_defaults(fn=cmd_draft)
    sp = sub.add_parser("serve")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(fn=cmd_serve)
    sp = sub.add_parser("check")
    sp.add_argument("--only")
    sp.set_defaults(fn=cmd_check)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("freeze").set_defaults(fn=cmd_freeze)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
