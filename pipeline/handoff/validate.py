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

from pipeline import glossary as glossary_rules
from pipeline.handoff.bundle import BundleError, check_bundle, load_type_map
from pipeline.handoff.canonical import sha256_canonical, sha256_file, sha256_text
from pipeline.handoff.envelope import StageReport
from pipeline.matching import MatchConflictError
from pipeline.stages import text_check as tc
from pipeline.types import AnalyzeResult, ContentFinding, LabelResult, LogoResult


def _paths(rep: StageReport, local_paths: dict[tuple[str, str], Any] | None, allowed_root: Any) -> tuple[dict[tuple[str, str], Path], list[str]]:
    """측정할 로컬 경로. 대체 경로(BE가 스테이징에서 내려받은 사본 등)는 allowed_root 안이어야 한다(경로 탈출 · 링크 해석 후 확인, R17)."""
    if local_paths is None:
        return {(a.kind, a.part_key): Path(a.path) for a in rep.artifacts}, []
    if allowed_root is None:
        raise ValueError("local_paths를 주면 allowed_root도 줘야 한다")
    root = Path(allowed_root).resolve()
    out: dict[tuple[str, str], Path] = {}
    probs: list[str] = []
    for a in rep.artifacts:
        k = (a.kind, a.part_key)
        if k not in local_paths:
            probs.append(f"대체 경로가 없는 산출물 {a.kind}/{a.part_key}")
            continue
        q = Path(local_paths[k]).resolve()
        if q != root and root not in q.parents:
            probs.append(f"대체 경로가 허용 폴더 밖이다 {a.kind}/{a.part_key}")
            continue
        out[k] = q
    extra = set(local_paths) - {(a.kind, a.part_key) for a in rep.artifacts}
    if extra:
        probs.append(f"보고에 없는 산출물의 대체 경로 {sorted(extra)}")
    return out, probs


def _files(rep: StageReport, paths: dict[tuple[str, str], Path]) -> list[str]:
    out = []
    for a in rep.artifacts:
        if (a.kind, a.part_key) not in paths:
            continue
        p = paths[(a.kind, a.part_key)]
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


def _analyze(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
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
        elif (a.width, a.height) != (s.width, s.height) or (not ctx["substituted"] and Path(a.path) != Path(s.image_path).resolve()):
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


def _judge(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
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
    targets = req.get("target_section_keys")
    want = [s.section_key for s in ar.sections if targets is None or s.section_key in set(targets)]
    if [s["section_key"] for s in secs] != want:
        return ["판정 섹션이 판정 대상과 다르다(누락 · 순서 · 추가)"]
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


def _label(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
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


def _logo(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
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


def _inpaint(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
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


def _style(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    got = [b["block_key"] for b in rep.payload["blocks"]] + [b["block_key"] for b in rep.payload["excluded"]]
    p = []
    if sorted(got) != sorted(_blocks_of(req)) or len(set(got)) != len(got):
        p.append("⑦ 결과가 요청 블록과 정확히 대응하지 않는다")
    for b in rep.payload["blocks"]:
        nulls = {k for k in ("font_color", "bg_color", "est_font_px", "align") if b[k] is None}
        if nulls != set(b["null_reasons"]):
            p.append(f"{b['block_key']}: NULL 값과 사유가 맞지 않는다")
    return p


def _translate(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
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


def _text_check(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    """규칙 검사 결과. 구간 · 사전 ID · 용어 ID · flag와 매칭의 대응 · 검사 범위 완결성(complete)의 일관성을 본다."""
    want = {t["block_key"]: t for t in req.get("targets", [])}
    p = []
    got = [b["block_key"] for b in rep.payload.get("blocks", [])]
    if sorted(got) != sorted(want) or len(set(got)) != len(got):
        return ["검사 결과 키가 대상과 다르다"]
    reg = {e["external_id"]: e for e in req["bundle"]["regulation"]["entries"]}
    applied = set(req["bundle"]["rules"]["regulatory_class_map"][req["regulatory_class"]]["applied_classes"])
    expect_ids = {i for i, e in reg.items() if e["regulatory_class"] in applied}
    term_ids = {t["glossary_id"] for t in req["glossary"]["terms"]}
    src_of = {b["block_key"]: b["source_ko"] for b in req["blocks"]}
    terms_in = [tc.GlossaryTerm(x["glossary_id"], x["term_ko"], x["term_target"], x["enforcement"]) for x in req["glossary"]["terms"]]
    rows = [tc.RegRow(e["external_id"], e["verdict_status"], tuple(e.get("variant_en", []))) for e in req["bundle"]["regulation"]["entries"]
            if e["regulatory_class"] in applied]
    for b in rep.payload["blocks"]:
        k = b["block_key"]
        t = want[k]
        text = t["text"]
        # 원문에서 적용 대상인 강제 용어(공급과 같은 선별 규칙)가 빠짐없이 검사됐는지 — 결과 · flag를 함께 지운 보고를 거절한다
        try:
            applicable = {e.glossary_id for e in glossary_rules.select(
                [(k, src_of[k])], [glossary_rules.Entry(x.glossary_id, x.term_ko, x.term_target, x.enforcement) for x in terms_in])}
        except glossary_rules.GlossaryDataError as e:
            p.append(f"{k}: 강제 용어 적용 대상을 다시 계산할 수 없다(completed로 받을 수 없음): {e}")
            continue
        expect_terms = {x.glossary_id for x in terms_in if x.enforcement == "enforced" and x.glossary_id in applicable}
        if {x["glossary_id"] for x in b["terms"]} != expect_terms:
            p.append(f"{k}: 적용 대상 강제 용어가 빠짐없이 · 정확히 검사되지 않았다(기대 {sorted(expect_terms)})")
        if b["revision"] != t["revision"] or b["text_sha256"] != sha256_text(text):
            p.append(f"{k}: 검사한 revision · 문구가 요청과 다르다")
        r = b["regulatory"]
        checked, unchecked = set(r["checked_ids"]), set(r["unchecked_ids"])
        if checked & unchecked or (checked | unchecked) != expect_ids:
            p.append(f"{k}: 검사 · 미검사 항목이 적용 분류의 사전 항목과 다르다")
        mkeys = [m["match_key"] for m in r["matches"]]
        if len(set(mkeys)) != len(mkeys):
            p.append(f"{k}: match_key 중복")
        for m in r["matches"]:
            if m["external_id"] not in checked or text[m["start"]:m["end"]] != m["matched_text"]:
                p.append(f"{k}: 매칭 {m['match_key']}의 사전 ID · 구간이 맞지 않는다")
        sup = {s["match_key"] for s in r["suppressed"]}
        if sup - set(mkeys):
            p.append(f"{k}: 억제 기록이 없는 매칭을 가리킨다")
        live = {(m["external_id"], m["start"], m["end"]) for m in r["matches"]
                if m["match_key"] not in sup and reg.get(m["external_id"], {}).get("verdict_status") != "allowed"}
        reg_flags, term_flags = set(), set()
        for f in b["flags"]:
            if f.get("revision") != t["revision"] or not f.get("type") or not f.get("producer"):
                p.append(f"{k}: compliance flag 필수값(type · producer · revision) 오류")
            if f.get("type") == "regulatory":
                key = (f.get("external_id"), f.get("start"), f.get("end"))
                if (f.get("producer") != "translation_validation" or f.get("detected_by") != "rule" or key not in live
                        or f.get("problem_text") != text[f["start"]:f["end"]]):
                    p.append(f"{k}: 규제 flag가 유효 매칭과 대응하지 않는다 {f.get('external_id')}")
                reg_flags.add(key)
            elif f.get("type") == "mandatory_term_unapplied":
                if f.get("producer") != "glossary_validation" or f.get("glossary_id") not in term_ids:
                    p.append(f"{k}: 강제 용어 flag의 producer · glossary_id 오류")
                term_flags.add(f.get("glossary_id"))
            else:
                p.append(f"{k}: 모르는 flag type {f.get('type')!r}")
        if reg_flags != live:
            p.append(f"{k}: 규제 flag가 억제 이후 유효 매칭과 정확히 같지 않다")
        tids = [x["glossary_id"] for x in b["terms"]]
        if len(set(tids)) != len(tids) or set(tids) - term_ids or any(x["status"] not in ("applied", "unapplied", "unverifiable") for x in b["terms"]):
            p.append(f"{k}: 용어 검사 결과의 ID · 상태 오류")
        if {x["glossary_id"] for x in b["terms"] if x["status"] == "unapplied"} != term_flags:
            p.append(f"{k}: 미적용 용어와 강제 용어 flag가 다르다")
        if b["complete"] != (not unchecked and not any(x["status"] == "unverifiable" for x in b["terms"])):
            p.append(f"{k}: complete(검사 범위 완결성)가 미검사 항목 · unverifiable 용어와 맞지 않다")
        # 같은 규칙 함수로 다시 계산해 전체를 대조한다(결과 일부 삭제 · 상태 변조 거절)
        try:
            again = tc.check_block(k, t["revision"], text, src_of[k], rows, terms_in)
        except (MatchConflictError, glossary_rules.GlossaryDataError) as e:
            p.append(f"{k}: 검사를 다시 계산할 수 없다(completed로 받을 수 없음): {e}")
            continue
        for f in ("regulatory", "terms", "flags", "complete", "rules"):
            if b.get(f) != again[f]:
                p.append(f"{k}: {f}가 같은 입력으로 다시 계산한 결과와 다르다")
    return p


def _translation_review(rep: StageReport, req: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    """번역문 LLM 규제 판정. 실패 보고는 판정이 없어야 한다(부분 채택 없음). 성공 보고는 모든 (대상 × 검사 항목) 쌍 · 위치 · 근거 참조 ·
    판단 조합 규칙 · flag 파생을 다시 계산해 대조한다. 참조 무결성 · 보조 텍스트 검사는 판단 내용의 정확성을 보장하지 않는다."""
    from pipeline.handoff import review as rvh
    from pipeline.stages import translation_review as rv

    try:
        cb = check_bundle(req["bundle"], target_country=req["target_country"], types=load_type_map())
    except BundleError as e:
        return [f"요청 묶음 오류: {e}"]
    checks, refs, out = rvh.select_items(cb, req["regulatory_class"])
    pl = rep.payload
    tkeys = [t["block_key"] for t in req.get("targets", [])]
    rule = cb.rules.regulatory_class_map[req["regulatory_class"]]
    scope = {"check_item_ids": [i.external_id for i in checks], "reference_item_ids": [i.external_id for i in refs],
             "out_of_scope_item_ids": out, "class_basis": {"selected": req["regulatory_class"], "applied_classes": list(rule.applied_classes),
                                                          "dict_coverage": rule.dict_coverage},
             "target_keys": tkeys, "pair_count": len(tkeys) * len(checks), "context_scope": "section"}
    p = []
    if pl.get("section_key") != req["section_key"] or pl.get("scope") != scope:
        p.append("section_key · scope(검사 · 참고 · 범위 밖 항목, 분류 근거, 대상, 쌍 수)가 요청에서 다시 계산한 값과 다르다")
    if pl.get("supplied_items") != {"check": [rvh.item_snapshot(i) for i in checks], "reference": [rvh.item_snapshot(i) for i in refs]}:
        p.append("supplied_items 스냅샷이 고정 묶음과 다르다")
    ctx_sha = rvh.review_context_sha256(req["blocks"], req["targets"], req["context"])
    if pl.get("context_sha256") != ctx_sha:
        p.append("context_sha256이 요청 문맥과 다르다")
    keys = set(pl)
    if rep.outcome == "failed":
        allowed_keys = _REVIEW_COMMON | ({"size"} if rep.failure.kind == "unsupported_input" else {"non_adoptable_diagnostics"})
        if not _REVIEW_COMMON <= keys or keys - allowed_keys:
            p.append(f"실패 payload 필드가 계약과 다르다(허용 {sorted(allowed_keys)}, 받은 {sorted(keys)}) — 판정 · flag는 실패 보고에 두지 않는다")
        if sorted(rep.failure.targets) != sorted(tkeys):
            p.append("실패 보고의 failure.targets가 검사 대상 전체가 아니다")
        kind, chunks = rep.failure.kind, pl.get("chunks", [])
        if kind == "unsupported_input":
            if chunks or not isinstance(pl.get("size"), dict) or pl["size"].get("unit") != "code_points":
                p.append("입력 상한 초과 보고는 chunks가 비고 size(code_points)가 있어야 한다")
        elif kind in ("model_call_failed", "response_invalid"):
            st = [c["status"] for c in chunks]
            bad = [s for s in st if s not in ("validated", "not_called")]
            if len(bad) != 1:
                p.append("호출 · 응답 실패 보고는 실패 분할이 정확히 하나여야 한다")
            else:
                i = st.index(bad[0])
                if any(s != "validated" for s in st[:i]) or any(s != "not_called" for s in st[i + 1:]):
                    p.append("분할 상태 순서 오류(실패 앞은 validated, 뒤는 not_called)")
                want = {"model_call_failed": ("call_failed",), "response_invalid": ("response_invalid", "output_truncated")}[kind]
                if bad[0] not in want:
                    p.append(f"failure.kind {kind}와 분할 상태 {bad[0]}가 맞지 않는다")
                if (bad[0] == "output_truncated") == rep.failure.retryable:
                    p.append("출력 잘림은 retryable=false, 그 밖의 호출 · 응답 실패는 retryable=true여야 한다")
            p += _chunk_problems(chunks, tkeys, [i.external_id for i in checks])
            by_idx = {c["index"]: c for c in chunks}
            seen = set()
            for d in pl.get("non_adoptable_diagnostics", []):
                if set(d) != {"block_key", "judgments"} or d["block_key"] not in tkeys:
                    p.append("진단 항목의 형식 · 블록이 계약과 다르다")
                    continue
                for j in d["judgments"]:
                    c = by_idx.get(j.get("chunk_index"))
                    pair = (d["block_key"], j.get("external_id"))
                    if (set(j) != _JUDGMENT_KEYS or c is None or c["status"] != "validated" or d["block_key"] not in c["target_keys"]
                            or j["external_id"] not in c["item_ids"] or pair in seen):
                        p.append(f"진단 판정 {pair}이 그 판정의 검증된 분할에 속하지 않거나 중복 · 형식 오류다")
                    seen.add(pair)
        else:
            p.append(f"{kind} 실패에는 payload를 두지 않는다")
        return p
    if rep.outcome == "skipped":
        if keys != _REVIEW_COMMON | {"blocks"}:
            p.append(f"skipped payload 필드가 계약과 다르다({sorted(keys)})")
        if tkeys or pl.get("blocks") != [] or pl.get("chunks") != []:
            p.append("skipped는 대상 0개 · 빈 blocks · 빈 chunks여야 한다")
        return p
    if keys != _REVIEW_COMMON | {"blocks", "result_sha256"}:
        p.append(f"completed payload 필드가 계약과 다르다({sorted(keys)})")
    p += _chunk_problems(pl["chunks"], tkeys, [i.external_id for i in checks])
    chunk_of = {c["index"]: c for c in pl["chunks"]}
    blocks = pl["blocks"]
    if any(set(b) != _BLOCK_KEYS for b in blocks):
        return p + ["판정 블록 필드가 계약과 다르다"]
    if [b["block_key"] for b in blocks] != tkeys:
        return p + ["판정 블록이 검사 대상과 다르다(누락 · 중복 · 순서 · 대상 밖)"]
    if pl.get("result_sha256") != sha256_canonical(blocks):
        p.append("result_sha256 불일치")
    items = {i.external_id: i for i in checks}
    allowed = {i.external_id: i for i in refs}
    corpus = rv.citation_corpus(checks + refs)
    order = [i.external_id for i in checks]
    want_t = {t["block_key"]: t for t in req["targets"]}
    for b in blocks:
        k = b["block_key"]
        t = want_t[k]
        text = t["text"]
        if b["revision"] != t["revision"] or b["text_sha256"] != sha256_text(text) or b["context_sha256"] != ctx_sha:
            p.append(f"{k}: 판정한 revision · 문구 · 문맥이 요청과 다르다")
        js = b["judgments"]
        if [j["external_id"] for j in js] != order:
            p.append(f"{k}: 판정 항목이 검사 항목 전체와 다르다(누락 · 중복 · 순서 · 범위 밖)")
            continue
        n = 0
        for j in js:
            w = f"{k}/{j['external_id']}"
            if (j["judgment"] not in rv.JUDGMENTS or j["mention"] not in rv.MENTIONS
                    or (j["undetermined_reason"] is not None and j["undetermined_reason"] not in rv.UNDETERMINED_REASONS)):
                p.append(f"{w}: 허용되지 않은 판정 값")
                continue
            if set(j) != _JUDGMENT_KEYS:
                p.append(f"{w}: 판정 필드가 계약과 다르다")
                continue
            c = chunk_of.get(j["chunk_index"])
            if c is None or c["status"] != "validated" or k not in c["target_keys"] or j["external_id"] not in c["item_ids"]:
                p.append(f"{w}: chunk_index {j['chunk_index']!r}의 검증된 분할에 이 블록 · 항목이 없다")
            if not isinstance(j["reason"], str) or not j["reason"].strip():
                p.append(f"{w}: reason이 비었다")
            for loc in j["locations"]:
                n += 1
                if set(loc) != {"quote", "anchor", "start", "end", "location_key"}:
                    p.append(f"{w}: 위치 필드가 계약과 다르다")
                    continue
                s, e = loc["start"], loc["end"]
                ok = (loc["location_key"] == f"loc_{n:03d}" and text[s:e] == loc["quote"] and rv._count(text, loc["anchor"]) == 1
                      and rv._count(loc["anchor"], loc["quote"]) == 1 and text.find(loc["anchor"]) + loc["anchor"].find(loc["quote"]) == s)
                if not ok:
                    p.append(f"{w}: 위치 {loc.get('location_key')}의 quote · anchor · 구간 · 순번이 번역문과 맞지 않는다")
            kinds = []
            for bs in j["basis"]:
                kd, ext = bs.get("kind"), bs.get("external_id")
                kinds.append(kd)
                if kd in ("dictionary_entry", "evidence"):
                    if ext != j["external_id"]:
                        p.append(f"{w}: 다른 항목의 근거를 참조했다")
                        continue
                    if kd == "evidence":
                        ev = items[ext].evidence
                        i = bs.get("evidence_index")
                        if not isinstance(i, int) or not 0 <= i < len(ev) or bs.get("evidence_external_id") != ev[i].external_id:
                            p.append(f"{w}: 묶음 밖 근거 참조")
                elif kd == "allowed_reference":
                    i = bs.get("evidence_index")
                    if ext not in allowed or (i is not None and (not isinstance(i, int) or not 0 <= i < len(allowed[ext].evidence))):
                        p.append(f"{w}: 묶음 밖 참고 항목 참조")
                else:
                    p.append(f"{w}: 모르는 근거 종류 {kd!r}")
            if j["basis_summary"] != rv.basis_summary(kinds):
                p.append(f"{w}: basis_summary가 근거 참조와 맞지 않는다")
            p += [f"{w}: {x}" for x in rv.judgment_problems(j, kinds)]
            p += rv._text_problems(f"{w} reason", j["reason"], corpus)
            for c in j["unverified_conditions"]:
                p += rv._text_problems(f"{w} 미확인 조건", c, corpus)
        if b["flags"] != rvh.flags_of(t["revision"], js):
            p.append(f"{k}: flags가 판정에서 파생한 값과 다르다")
        if b["counts"] != {"pairs": len(js), **{x: sum(1 for j in js if j["judgment"] == x) for x in rv.JUDGMENTS}}:
            p.append(f"{k}: counts 불일치")
    pairs = [(t, i) for c in pl["chunks"] for t in c["target_keys"] for i in c["item_ids"]]
    if any(c["status"] != "validated" for c in pl["chunks"]):
        p.append("completed인데 검증되지 않은 분할이 있다")
    if sorted(pairs) != sorted((t, i) for t in tkeys for i in order) or len(set(pairs)) != len(pairs):
        p.append("분할의 (대상 × 항목) 합집합이 전체 검사 범위와 정확히 같지 않다")
    return p


_REVIEW_COMMON = frozenset({"section_key", "scope", "context_sha256", "supplied_items", "chunks"})
_BLOCK_KEYS = frozenset({"block_key", "revision", "text_sha256", "context_sha256", "judgments", "flags", "counts"})
_JUDGMENT_KEYS = frozenset({"external_id", "judgment", "mention", "undetermined_reason", "locations", "basis", "basis_summary",
                            "unverified_conditions", "reason", "chunk_index"})
_CHUNK_KEYS = frozenset({"index", "target_keys", "item_ids", "status", "input_code_points", "payload_sha256", "response_sha256",
                         "finish_reason", "error"})


def _chunk_problems(chunks: list[dict[str, Any]], tkeys: list[str], item_ids: list[str]) -> list[str]:
    """분할 기록 형식: 필드 · 순번 0..n-1 · 상태 값 · 요청 범위 안의 대상 · 항목."""
    from pipeline.stages import translation_review as rv

    p = []
    if any(set(c) != _CHUNK_KEYS for c in chunks):
        return ["분할 기록 필드가 계약과 다르다"]
    if [c["index"] for c in chunks] != list(range(len(chunks))):
        p.append("분할 index가 0부터 순서대로가 아니다")
    for c in chunks:
        if c["status"] not in rv.CHUNK_STATUSES or set(c["target_keys"]) - set(tkeys) or set(c["item_ids"]) - set(item_ids):
            p.append(f"분할 {c['index']}의 상태 · 대상 · 항목이 요청 범위 밖이다")
    return p


_STAGE = {"analyze": _analyze, "judge": _judge, "label": _label, "logo": _logo, "inpaint": _inpaint, "style": _style,
          "translate": _translate, "text_check": _text_check, "translation_review": _translation_review}


def validate_report(report: Any, request: dict[str, Any], *, local_paths: dict[tuple[str, str], Any] | None = None,
                    allowed_root: Any = None) -> list[str]:
    """local_paths: (kind, part_key) → 이 환경에서 측정할 파일 경로(보고의 path가 다른 서버 경로일 때). 주면 allowed_root 필수."""
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
    paths, probs = _paths(rep, local_paths, allowed_root)
    p += probs + _files(rep, paths)
    if rep.outcome == "failed" and rep.payload is None:
        return p
    if rep.outcome == "failed" and rep.stage not in ("translate", "analyze", "translation_review"):
        return p  # 실패 보고는 실패 분류 · 식별 · 입력 연결만 본다(⑧ 번역 · LLM 판정은 실패 payload의 구조도 본다)
    if rep.outcome == "failed" and rep.stage == "analyze":
        return p
    fn = _STAGE[rep.stage]
    try:
        p += fn(rep, request, {"substituted": local_paths is not None})
    except (KeyError, TypeError, ValidationError) as e:
        p.append(f"payload 필수값 누락 · 형식 오류: {e.__class__.__name__}: {e}")
    return p
