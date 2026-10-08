"""BE 채택 전 보고 검증 — 필수값 · 결과 유효성 조건의 실행 가능한 명세 [통합 5.31 · 5.32 상태 전이].

`validate_report(report, request) -> list[str]` — 빈 목록이면 통과. 문제가 하나라도 있으면 인계 거절(부분 저장 없음).
- 구조 검증(StageReport 불변식)과 outcome별 필수 자료 검증을 나눈다. 실패 보고는 성공 자료가 없다는 이유로 거절하지 않는다.
- 산출물은 보고의 주장이 아니라 파일을 **다시 재서**(바이트 · SHA-256 · PNG 크기) 대조한다. 원격 업로드본 검증은 BE가 같은 기준으로 한다.
- 키는 요청의 임시 키와 정확히 대응해야 한다(모르는 키 · 누락 · 중복 거절). 문자 구간은 matched_text == 원문[start:end].
pipeline은 app/을 import하지 않는다 — BE 워커가 이 함수를 import해 쓴다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pipeline.handoff.bundle import load_type_map
from pipeline.handoff.canonical import sha256_canonical, sha256_file, sha256_text
from pipeline.handoff.envelope import StageReport
from pipeline.types import AnalyzeResult, ContentFinding, LabelResult, LogoResult


def _files(rep: StageReport) -> list[str]:
    out = []
    for a in rep.artifacts:
        p = Path(a.path)
        if not p.is_file():
            out.append(f"산출물 없음 {a.kind}/{a.part_key}: {a.path}")
            continue
        if p.stat().st_size != a.bytes or sha256_file(p) != a.sha256:
            out.append(f"산출물 바이트 · 해시 불일치 {a.kind}/{a.part_key}")
            continue
        if a.format == "png":
            from PIL import Image

            with Image.open(p) as im:
                if im.format != "PNG" or im.size != (a.width, a.height):
                    out.append(f"산출물 PNG 형식 · 크기 불일치 {a.kind}/{a.part_key}")
    return out


def _art(rep: StageReport, kind: str, part: str):
    return next((a for a in rep.artifacts if a.kind == kind and a.part_key == part), None)


def _analyze(rep: StageReport, req: dict[str, Any]) -> list[str]:
    p = []
    try:
        ar = AnalyzeResult.model_validate(rep.payload["analyze_result"])
    except (KeyError, ValidationError) as e:
        return [f"analyze_result 형식 오류: {e}"]
    if ar.schema_version != "1":
        p.append(f"AnalyzeResult 버전 {ar.schema_version} — 1만 받는다")
    want_ids = {s["source_image_id"] for s in req.get("sources", [])}
    if {s.source_image_id for s in ar.sections} != want_ids:
        p.append("섹션의 원본이 요청 원본과 다르다(원본마다 섹션 1개 이상)")
    keys = [s.section_key for s in ar.sections]
    if len(set(keys)) != len(keys):
        p.append("섹션 키 중복")
    for s in ar.sections:
        a = _art(rep, "section_image", s.section_key)
        if a is None:
            p.append(f"{s.section_key}: 섹션 이미지 산출물 없음")
        elif (a.width, a.height) != (s.width, s.height) or Path(a.path) != Path(s.image_path).resolve():
            p.append(f"{s.section_key}: 섹션 이미지 산출물 · 메타데이터 불일치")
    extra = {a.part_key for a in rep.artifacts} - set(keys)
    if extra:
        p.append(f"모르는 섹션의 산출물 {sorted(extra)}")
    bkeys = [b.block_key for b in ar.blocks]
    if len(set(bkeys)) != len(bkeys) or any(b.section_key not in set(keys) for b in ar.blocks):
        p.append("블록 키 중복 또는 모르는 섹션 키")
    for fb in rep.payload.get("split_fallbacks", []):
        if fb.get("source_image_id") not in want_ids:
            p.append(f"대체 기록의 원본이 요청에 없다 {fb}")
    return p


def _judge(rep: StageReport, req: dict[str, Any]) -> list[str]:
    p = []
    tm = load_type_map()
    local_types = [t.content_type for t in tm.local]
    allowed_types = set(local_types) | {tm.regulatory_content_type}
    ar = AnalyzeResult.model_validate(req["analyze_result"])
    blocks = {b.block_key: b for b in ar.blocks}
    bundle_ids = {e["external_id"] for e in req["bundle"]["regulation"]["entries"]}
    if req["bundle"].get("local"):
        bundle_ids |= {e["external_id"] for e in req["bundle"]["local"]["entries"]}
    secs = rep.payload.get("sections", [])
    if [s["section_key"] for s in secs] != [s.section_key for s in ar.sections]:
        return ["판정 섹션이 분석 섹션과 다르다(누락 · 순서 · 추가)"]
    for s in secs:
        k = s["section_key"]
        sec_blocks = {b for b, v in blocks.items() if v.section_key == k}
        cf = s.get("content_findings")
        if cf is None or cf.get("schema_version") != "1":
            p.append(f"{k}: content_findings 없음 · 버전 오류")
            continue
        try:
            fs = [ContentFinding.model_validate(f) for f in cf["findings"]]
        except ValidationError as e:
            p.append(f"{k}: finding 형식 오류 {e}")
            continue
        fkeys = [f.finding_key for f in fs]
        if len(set(fkeys)) != len(fkeys):
            p.append(f"{k}: finding_key 중복")
        for f in fs:
            if f.content_type not in allowed_types:
                p.append(f"{k}: 목록 밖 content_type {f.content_type}")
            if set(f.evidence_block_ids) - sec_blocks:
                p.append(f"{k}/{f.finding_key}: 섹션 밖 근거 블록")
            if f.evidence_source == "image" and f.evidence_block_ids:
                p.append(f"{k}/{f.finding_key}: 이미지 근거에 블록을 붙였다")
            if f.evidence_source != "image" and not f.evidence_block_ids:
                p.append(f"{k}/{f.finding_key}: 텍스트 근거인데 블록이 없다")
        lt = [f.content_type for f in fs if f.content_type in local_types]
        st = s.get("local_status")
        if st == "completed" and sorted(lt) != sorted(local_types):
            p.append(f"{k}: 현지 완료인데 8항목 finding이 정확히 하나씩이 아니다")
        if st in ("failed", "not_checked") and (lt or not s.get("local_failure")):
            p.append(f"{k}: 현지 {st}인데 현지 finding이 있거나 사유가 없다")
        if st not in ("completed", "failed", "not_checked"):
            p.append(f"{k}: local_status {st!r}")
        if st == "failed" and s.get("bucket_recommendation") != "exclude":
            p.append(f"{k}: 현지 판정 실패 섹션은 제외 권고여야 한다(D9-1)")
        if sum(1 for f in fs if f.content_type == tm.regulatory_content_type) > 1:
            p.append(f"{k}: 규제 검출 finding이 섹션당 1건을 넘는다")
        texts = s["audit"]["block_texts"]
        for bk, t in texts.items():
            if bk not in sec_blocks or blocks[bk].source_ko != t:
                p.append(f"{k}: audit 원문 스냅샷이 분석 원문과 다르다 {bk}")
        mkeys = set()
        for m in s["audit"]["matches"]:
            mkeys.add(m["match_key"])
            src = texts.get(m["block_key"])
            if src is None or src[m["start"]:m["end"]] != m["matched_text"] or m["external_id"] not in bundle_ids:
                p.append(f"{k}: 매칭 구간 · 사전 ID 불일치 {m['match_key']}")
        for v in s.get("verdicts", []):
            if v["finding_key"] not in set(fkeys) or v["external_id"] not in bundle_ids or set(v["match_keys"]) - mkeys:
                p.append(f"{k}: 판정 {v['verdict_key']}의 finding · 사전 · 매칭 연결 오류")
        excl = any(v["bucket"] == "exclude" for v in s.get("verdicts", [])) or st == "failed"
        if (s.get("bucket_recommendation") == "exclude") != excl:
            p.append(f"{k}: 제외 권고와 판정 집계가 맞지 않는다")
    return p


def _blocks_of(req: dict[str, Any]) -> list[str]:
    return [b["block_key"] for b in req.get("blocks", [])]


def _label(rep: StageReport, req: dict[str, Any]) -> list[str]:
    p = []
    lr = rep.payload.get("label_result")
    if lr is None or sha256_canonical(lr) != rep.payload.get("label_result_sha256"):
        return ["label_result 해시 불일치"]
    res = LabelResult.model_validate(lr)
    order = [b["block_key"] for b in sorted(req["blocks"], key=lambda b: b["block_order"])]
    if res.status != "ok" or [d.block_key for d in res.labels or []] != order:
        p.append("④ 판정이 요청 블록과 정확히 대응하지 않는다")
    if [(d["block_key"], d["is_product_label"]) for d in rep.payload["decisions"]] != [(d.block_key, d.is_product_label) for d in res.labels or []]:
        p.append("decisions가 label_result와 다르다")
    return p


def _logo(rep: StageReport, req: dict[str, Any]) -> list[str]:
    pl = rep.payload
    if sha256_canonical(pl.get("logo_result")) != pl.get("logo_result_sha256") or sha256_canonical(pl.get("logo_record")) != pl.get("logo_record_sha256"):
        return ["logo_result · logo_record 해시 불일치"]
    res = LogoResult.model_validate(pl["logo_result"])
    label = LabelResult.model_validate(req["label_result"])
    lab = {d.block_key: d.is_product_label for d in label.labels or []}
    p = []
    if res.status != "ok" or sorted(d.block_key for d in res.decisions or []) != sorted(_blocks_of(req)):
        p.append("⑤ 판정이 요청 블록과 정확히 대응하지 않는다")
    for d in res.decisions or []:
        if lab.get(d.block_key) is True and d.is_brand_logo is not None:
            p.append(f"{d.block_key}: 라벨 true인데 로고가 null(생략)이 아니다")
        if lab.get(d.block_key) is False and d.is_brand_logo is None:
            p.append(f"{d.block_key}: 라벨 false인데 로고가 미판정")
    return p


def _inpaint(rep: StageReport, req: dict[str, Any]) -> list[str]:
    key = req["section"]["section_key"]
    size = (req["section"]["width"], req["section"]["height"])
    p = []
    for kind in ("delete_mask", "protect_mask"):
        a = _art(rep, kind, key)
        if a is None or (a.width, a.height) != size:
            p.append(f"{kind} 없음 또는 크기 불일치")
    if rep.outcome == "completed":
        a = _art(rep, "background", key)
        if a is None or (a.width, a.height) != size or rep.payload.get("status") != "inpainted":
            p.append("inpainted인데 배경 산출물이 없거나 크기 불일치")
    else:
        refs = [r for r in rep.source_refs if r.kind == "background" and r.part_key == key]
        if rep.payload.get("status") != "unchanged" or len(refs) != 1 or refs[0].sha256 != req["section_image"]["sha256"]:
            p.append("빈 마스크 생략인데 원본 배경 참조가 없거나 입력 이미지와 다르다")
        if _art(rep, "background", key) is not None:
            p.append("빈 마스크 생략인데 배경 파일이 있다")
    return p


def _style(rep: StageReport, req: dict[str, Any]) -> list[str]:
    got = [b["block_key"] for b in rep.payload["blocks"]] + [b["block_key"] for b in rep.payload["excluded"]]
    p = []
    if sorted(got) != sorted(_blocks_of(req)) or len(set(got)) != len(got):
        p.append("⑦ 결과가 요청 블록과 정확히 대응하지 않는다")
    for b in rep.payload["blocks"]:
        nulls = {k for k in ("font_color", "bg_color", "est_font_px", "align") if b[k] is None}
        if nulls != set(b["null_reasons"]):
            p.append(f"{b['block_key']}: NULL 값과 사유가 맞지 않는다")
    return p


def _translate(rep: StageReport, req: dict[str, Any]) -> list[str]:
    p = []
    want = {t["block_key"]: t["revision"] for t in req.get("targets", [])}
    blocks = rep.payload.get("blocks", [])
    got = [b["block_key"] for b in blocks]
    if sorted(got) != sorted(want) or len(set(got)) != len(got):
        return ["번역 결과 키가 이번 대상과 정확히 같지 않다(누락 · 중복 · 대상 밖)"]
    src = {b["block_key"]: b["source_ko"] for b in req["blocks"]}
    failed = []
    for b in blocks:
        if b["input_revision"] != want[b["block_key"]]:
            p.append(f"{b['block_key']}: 입력 revision 불일치")
        if b["source_sha256"] != sha256_text(src[b["block_key"]]):
            p.append(f"{b['block_key']}: 원문 해시 불일치")
        if b["outcome"] == "completed":
            if not (b["trans_1"] or "").strip() or b["translation_sha256"] != sha256_text(b["trans_1"]):
                p.append(f"{b['block_key']}: 성공인데 번역문이 비었거나 해시 불일치")
        elif b["outcome"] == "failed":
            failed.append(b["block_key"])
            if b["trans_1"] is not None:
                p.append(f"{b['block_key']}: 실패 블록에 번역문이 있다")
        else:
            p.append(f"{b['block_key']}: outcome {b['outcome']!r}")
    if rep.outcome == "completed" and failed:
        p.append("completed인데 실패 블록이 있다")
    if rep.outcome == "failed" and sorted(rep.failure.targets) != sorted(failed):
        p.append("failure.targets가 실패 블록과 다르다")
    return p


def _text_check(rep: StageReport, req: dict[str, Any]) -> list[str]:
    want = {t["block_key"]: t for t in req.get("targets", [])}
    p = []
    got = [b["block_key"] for b in rep.payload.get("blocks", [])]
    if sorted(got) != sorted(want):
        return ["검사 결과 키가 대상과 다르다"]
    for b in rep.payload["blocks"]:
        t = want[b["block_key"]]
        if b["revision"] != t["revision"] or b["text_sha256"] != sha256_text(t["text"]):
            p.append(f"{b['block_key']}: 검사한 revision · 문구가 요청과 다르다")
        for f in b["flags"]:
            if f.get("revision") != t["revision"] or not f.get("type") or not f.get("producer"):
                p.append(f"{b['block_key']}: compliance flag 필수값(type · producer · revision) 오류")
    return p


_STAGE = {"analyze": _analyze, "judge": _judge, "label": _label, "logo": _logo, "inpaint": _inpaint, "style": _style,
          "translate": _translate, "text_check": _text_check}


def validate_report(report: Any, request: dict[str, Any]) -> list[str]:
    try:
        rep = report if isinstance(report, StageReport) else StageReport.model_validate(report)
    except ValidationError as e:
        return [f"보고 구조 오류: {e}"]
    p = []
    for k in ("execution_id", "attempt_id", "stage"):
        if getattr(rep, k) != request.get(k):
            p.append(f"{k} 불일치: 보고 {getattr(rep, k)!r} · 요청 {request.get(k)!r}")
    if rep.input_snapshot_id != request.get("input_snapshot_id"):
        p.append("input_snapshot_id 불일치")
    exp = request.get("expected_input_manifest_sha256")
    if exp is not None and rep.input_manifest_sha256 not in (None, exp):
        p.append("input_manifest_sha256이 BE 고정 지문과 다르다")
    if p:
        return p
    p += _files(rep)
    if rep.outcome == "failed" and rep.payload is None:
        return p
    if rep.outcome == "failed" and rep.stage not in ("translate", "analyze"):
        return p  # 실패 보고는 실패 분류 · 식별 · 입력 연결만 본다
    if rep.outcome == "failed" and rep.stage == "analyze":
        return p
    fn = _STAGE[rep.stage]
    try:
        p += fn(rep, request)
    except (KeyError, TypeError, ValidationError) as e:
        p.append(f"payload 필수값 누락 · 형식 오류: {e.__class__.__name__}: {e}")
    return p
