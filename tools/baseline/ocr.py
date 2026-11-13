"""좌표·제품 엔진을 사용하지 않는 OCR v4 동결·채점. 형식: tools/baseline/ocr.md."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import re
import unicodedata

from .core import digest, now, read, write_new

BOUNDARY = "pipeline.stages.ocr.run:return"
SPACE = frozenset(chr(n) for lo, hi in ((9,13),(32,32),(133,133),(160,160),(5760,5760),
    (8192,8202),(8232,8233),(8239,8239),(8287,8287),(12288,12288)) for n in range(lo,hi+1))
LIMITATION = "L008은 사용자 결정에 따라 빈 정답으로 유지한다. 해당 출력은 제거하지 않으며 전체 CER에 반영한다."


def text_sequence(parts):
    if not isinstance(parts, list) or any(not isinstance(t, str) for t in parts):
        raise ValueError("text는 문자열 배열이어야 함")
    return "".join(c for t in parts for c in unicodedata.normalize("NFC", t) if c not in SPACE)


def edits(reference, prediction):
    """최소 거리, 역추적 동률 우선순위 일치→치환→삭제→삽입."""
    n, m = len(reference), len(prediction)
    directions = [bytearray(m+1) for _ in range(n+1)]
    previous = list(range(m+1))
    for j in range(1,m+1):
        directions[0][j] = 3
    for i, a in enumerate(reference,1):
        current = [i] + [0]*m
        directions[i][0] = 2
        for j, b in enumerate(prediction,1):
            candidates = (previous[j-1] + (a != b), previous[j]+1, current[j-1]+1)
            best = min(candidates)
            op = candidates.index(best)
            current[j] = best
            directions[i][j] = (0 if a == b else 1) if op == 0 else op+1
        previous = current
    s = d = ins = 0
    i, j = n, m
    while i or j:
        op = directions[i][j]
        if op in (0,1):
            s += op; i -= 1; j -= 1
        elif op == 2:
            d += 1; i -= 1
        else:
            ins += 1; j -= 1
    return {"S":s, "D":d, "I":ins, "N":n}


def sha(value, length=64):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{%d}" % length,value) is not None


def child(root, relative):
    p = (root / relative).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("허용된 루트 밖 경로")
    return p


def freeze(source_dir, image_root, spec, approval_path, version, out):
    approval = read(approval_path)
    for key, expected in (("transcription_review_complete",True),("errors_found",0),
                          ("order","lines_array"),("empty_truth_policy","keep"),("freeze_authorized",True)):
        if key not in approval or type(approval[key]) is not type(expected) or approval[key] != expected:
            raise ValueError(f"승인 근거 부족: {key}")
    if not approval.get("reviewer") or not approval.get("evidence") or not version:
        raise ValueError("검수자·승인 근거·버전 필요")
    source_dir, image_root, spec, out = map(Path,(source_dir,image_root,spec,out))
    if out.exists():
        raise ValueError("동결 출력 폴더가 이미 존재함")
    units, seen = [], set()
    for path in sorted(source_dir.glob("*.json")):
        data = read(path)
        sid = data["id"]
        if not isinstance(sid,str) or not sid or sid in seen:
            raise ValueError("섹션 ID 누락/중복")
        seen.add(sid)
        image = data["image"]
        if not sha(image["sha256"]) or digest(child(image_root,image["path"])) != image["sha256"]:
            raise ValueError(f"입력 이미지 해시 불일치: {sid}")
        lines, ids = [], set()
        for i, line in enumerate(data["lines"]):
            lid, text = line["line_id"], line["text"]
            if not isinstance(lid,str) or not lid or lid in ids or not isinstance(text,str):
                raise ValueError(f"줄 ID/text 오류: {sid}")
            ids.add(lid)
            lines.append({"line_id":lid,"source_index":i,"order":i,"text":text})
        units.append({"id":sid,"sample":data["sample"],"section_key":data["section_key"],
                      "source":{"path":path.as_posix(),"sha256":digest(path)},
                      "image":image,"lines":lines})
    if len(units) != approval.get("expected_sections") or sum(len(u["lines"]) for u in units) != approval.get("expected_lines"):
        raise ValueError("승인한 섹션/줄 수와 불일치")
    spec_bytes = spec.read_bytes()
    stamp = now()
    # 모든 입력 검증 후 새 폴더만 생성. 원본 JSON과 이미지에는 쓰지 않는다.
    out.mkdir(parents=True, exist_ok=False)
    with (out/"text_units.jsonl").open("x",encoding="utf-8",newline="\n") as f:
        for unit in units:
            f.write(json.dumps(unit,ensure_ascii=False)+"\n")
    write_new(out/"approval.json",approval)
    (out/"spec-v4.txt").write_bytes(spec_bytes)
    (out/"README.md").write_text(
        f"# OCR 텍스트 전용 동결본 {version}\n\n"
        f"동결 시각: {stamp}. 섹션 {len(units)}개, 줄 {sum(len(u['lines']) for u in units)}개.\n\n"
        "사용자 채팅 검수·동결 승인에 근거하며 증거 원문 발췌는 approval.json에 보존한다. "
        "전사·누락 검수 오류 0건. lines[] 순서 채택. bbox·겹침·위치 태그 검수는 생략했다. "
        "원본 v3 verified/history는 변경하지 않았다. 출력 노출 이력은 승인 자료에 기록된 범위를 따른다.\n\n"
        + LIMITATION + "\n\n"
        "spec-v4.txt는 동결 시점 명세 바이트 스냅샷이다. 미커밋 명세의 승인 커밋은 없으며 SHA-256으로 식별한다. "
        "이 동결은 실제 OCR 측정·채점 완료를 뜻하지 않는다.\n",encoding="utf-8",newline="\n")
    manifest = {"format":1,"version":version,"status":"frozen_text_only","frozen_at":stamp,
                "spec":{"path":spec.as_posix(),"sha256":hashlib.sha256(spec_bytes).hexdigest(),"commit":None},
                "sections":[u["id"] for u in units],"section_count":len(units),
                "line_count":sum(len(u["lines"]) for u in units),"held_sections":[],
                "history":[{"at":stamp,"action":"freeze_text_only","authority":"approval.json"}],
                "files":{name:digest(out/name) for name in ("text_units.jsonl","approval.json","spec-v4.txt","README.md")}}
    write_new(out/"manifest.json",manifest)
    verify(out)
    return manifest


def verify(folder):
    folder = Path(folder)
    manifest = read(folder/"manifest.json")
    if manifest.get("format") != 1 or manifest.get("status") != "frozen_text_only":
        raise ValueError("텍스트 동결본 형식/상태 오류")
    required = {"text_units.jsonl","approval.json","spec-v4.txt","README.md"}
    if set(manifest["files"]) != required:
        raise ValueError("동결 파일 목록 오류")
    for name, expected in manifest["files"].items():
        if digest(folder/name) != expected:
            raise ValueError(f"동결 해시 불일치: {name}")
    if digest(folder/"spec-v4.txt") != manifest["spec"]["sha256"]:
        raise ValueError("명세 해시 불일치")
    units = [json.loads(line) for line in (folder/"text_units.jsonl").read_text(encoding="utf-8").splitlines()]
    ids = [u["id"] for u in units]
    if len(ids) != len(set(ids)) or ids != manifest["sections"] or len(ids) != manifest["section_count"]:
        raise ValueError("동결 섹션 목록 오류")
    if sum(len(u["lines"]) for u in units) != manifest["line_count"]:
        raise ValueError("동결 줄 개수 오류")
    for u in units:
        if not sha(u["image"]["sha256"]) or not sha(u["source"]["sha256"]):
            raise ValueError("동결 출처 해시 오류")
        lines = u["lines"]
        if len({l["line_id"] for l in lines}) != len(lines):
            raise ValueError("동결 줄 ID 중복")
        for i,l in enumerate(lines):
            if l["order"] != i or l["source_index"] != i or not isinstance(l["text"],str):
                raise ValueError("동결 순서/text 오류")
    return manifest, units


def pointer_value(data, pointer):
    if not isinstance(pointer,str) or (pointer and not pointer.startswith("/")):
        raise ValueError("JSON Pointer 오류")
    for token in pointer.split("/")[1:]:
        token = token.replace("~1","/").replace("~0","~")
        data = data[int(token)] if isinstance(data,list) else data[token]
    return data


def section_score(unit, record, base, *, validate_only=False):
    ref = "" if validate_only else text_sequence([l["text"] for l in unit["lines"]])
    result = {"id":unit["id"],"status":"undetermined","reason":None,"S":None,"D":None,"I":None,
              "N":None if validate_only else len(ref),"output_characters":None,"cer":None,"cer_percent":None,
              "truth_source_sha256":unit["source"]["sha256"],"input_sha256":unit["image"]["sha256"],
              "provenance":record,"observed_hashes":{}}
    def fail(status,reason):
        result.update(status=status,reason=reason)
        return result
    if record is None:
        return fail("undetermined","입력 색인에 섹션 없음")
    if record.get("status") in ("no_output","undetermined"):
        if not record.get("reason"):
            return fail("undetermined","상태 사유 누락")
        return fail(record["status"],record["reason"])
    if record.get("status") != "available":
        return fail("evaluation_failed","지원하지 않는 출력 상태")
    required = ("product_version","attempt_id","model")
    if any(not isinstance(record.get(k),str) or not record[k].strip() for k in required):
        return fail("undetermined","제품·시도·모델 근거 누락")
    if not sha(record.get("product_sha"),40) or any(not sha(record.get(k)) for k in ("config_sha256","environment_sha256")):
        return fail("undetermined","제품·설정·환경 해시 누락/무효")
    if record.get("input_sha256") != unit["image"]["sha256"] or record.get("boundary") != BOUNDARY:
        return fail("undetermined","입력 동일성 또는 보정 후 반환 경계 불일치")
    ev = record.get("evidence")
    if not isinstance(ev,dict) or not ev.get("path") or not ev.get("locator") or not sha(ev.get("sha256")):
        return fail("undetermined","반환 경계 근거 누락")
    try:
        actual = digest(base/ev["path"])
        result["observed_hashes"]["evidence"] = actual
        if actual != ev["sha256"]:
            return fail("undetermined","경계 근거 해시 불일치")
    except OSError:
        return fail("undetermined","경계 근거 파일 접근 불가")
    output = record.get("output")
    if not isinstance(output,dict) or not output.get("path") or not sha(output.get("sha256")) or "pointer" not in output:
        return fail("undetermined","출력 경로·해시·위치 누락")
    try:
        path = base/output["path"]
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        result["observed_hashes"]["output"] = actual
        if actual != output["sha256"]:
            return fail("undetermined","출력 해시 불일치")
        content = raw.decode("utf-8-sig")
        if "line" in output:
            line = output["line"]
            if type(line) is not int or line < 1:
                raise ValueError("JSONL 행은 1 이상의 정수")
            content = content.splitlines()[line-1]
        data = pointer_value(json.loads(content),output["pointer"])
        regions = data["regions"]
        if not isinstance(regions,list):
            raise ValueError("regions 배열 필요")
        texts = [r["text"] for r in regions]
        if any(not isinstance(t,str) for t in texts):
            raise ValueError("text 문자열 필요")
        if record.get("output_section_key") and data.get("section_key") != record["output_section_key"]:
            return fail("undetermined","출력 섹션 키 불일치")
        if validate_only:
            return fail("ready","형식·해시 검증 완료; CER 미계산")
        prediction = text_sequence(texts)
        result["output_characters"] = len(prediction)
        if not ref:
            return fail("not_applicable","해당 대상 없음")
        counts = edits(ref,prediction)
        value = (counts["S"]+counts["D"]+counts["I"])/counts["N"]
        result.update(counts,status="measured",cer=value,cer_percent=value*100)
        return result
    except FileNotFoundError:
        return fail("no_output","기대한 출력 파일 없음")
    except (OSError,ValueError,KeyError,TypeError,IndexError,MemoryError) as exc:
        return fail("evaluation_failed",f"출력 파싱/채점 실패: {type(exc).__name__}")


def aggregate(results):
    valid = [r for r in results if r["status"] == "measured"]
    totals = {k:sum(r[k] for r in valid) for k in ("S","D","I","N")}
    denominator = totals["N"]
    value = (totals["S"]+totals["D"]+totals["I"])/denominator if denominator else None
    return {**totals,"cer":value,"cer_percent":value*100 if value is not None else None,
            "display_reason":None if denominator else ("해당 대상 없음" if all(r["N"]==0 for r in results) else "정상 측정 0건"),
            "planned_sections":len(results),"measured_sections":[r["id"] for r in valid],
            "status_counts":dict(Counter(r["status"] for r in results))}


def score(truth, inputs, out):
    truth, inputs, out = map(Path,(truth,inputs,out))
    if out.exists():
        raise ValueError("기존 결과 덮어쓰기 금지")
    manifest, units = verify(truth)
    raw = inputs.read_bytes()
    records = json.loads(raw)["sections"]
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)) or set(ids)-set(manifest["sections"]):
        raise ValueError("중복/알 수 없는 입력 섹션")
    lookup = {r["id"]:r for r in records}
    results = [section_score(u,lookup.get(u["id"]),inputs.parent) for u in units]
    report = {"metric":"OCR01","protocol":"ocr-v4","created_at":now(),"truth_version":manifest["version"],
              "truth_manifest_sha256":digest(truth/"manifest.json"),"spec_sha256":manifest["spec"]["sha256"],
              "inputs_sha256":hashlib.sha256(raw).hexdigest(),"scorer_sha256":digest(Path(__file__)),
              "python":platform.python_version(),"unicode_version":unicodedata.unidata_version,
              "limitation":LIMITATION,"sections":results,"total":aggregate(results)}
    write_new(out,report)
    return report


def validate_inputs(truth, inputs):
    manifest, units = verify(truth)
    inputs = Path(inputs)
    records = read(inputs)["sections"]
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)) or set(ids)-set(manifest["sections"]):
        raise ValueError("중복/알 수 없는 입력 섹션")
    lookup = {r["id"]:r for r in records}
    return {"cer_calculated":False, "sections":[section_score(u,lookup.get(u["id"]),inputs.parent,validate_only=True) for u in units]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command",required=True)
    f = sub.add_parser("freeze")
    for arg in ("source-dir","image-root","spec","approval","version","out"):
        f.add_argument("--"+arg,required=True)
    v = sub.add_parser("verify"); v.add_argument("--truth",required=True)
    s = sub.add_parser("score")
    for arg in ("truth","inputs","out"):
        s.add_argument("--"+arg,required=True)
    v = sub.add_parser("validate-inputs")
    v.add_argument("--truth",required=True); v.add_argument("--inputs",required=True); v.add_argument("--out",required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        result = freeze(args.source_dir,args.image_root,args.spec,args.approval,args.version,args.out)
        print(f"텍스트 동결: {result['section_count']}섹션 / {result['line_count']}줄")
    elif args.command == "verify":
        m,_ = verify(args.truth); print(f"무결성 확인: {m['version']}")
    elif args.command == "validate-inputs":
        result = validate_inputs(args.truth,args.inputs); write_new(args.out,result)
        print(dict(Counter(r["status"] for r in result["sections"])))
    else:
        result = score(args.truth,args.inputs,args.out); print(json.dumps(result["total"],ensure_ascii=False))


if __name__ == "__main__":
    main()
