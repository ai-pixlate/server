"""계약 예제 생성 — 합성 입력 · 가짜 모델로 단계별 요청/보고 쌍을 만든다(실제 모델 · GPU · BE 연결 아님).

    python -m pipeline.handoff.examples --out pipeline/samples/handoff

모든 값은 합성이다(실제 사전 표현 · 정책 문구 아님, docs/ai/README.md 4.4). 경로는 `/srv/pixlate-work/…`로 바꿔 쓴다 — 예제를
읽는 BE가 실행 환경의 절대 경로가 들어간다는 점만 보면 된다. 테스트(test_pipeline_handoff_examples)는 임시 폴더에서 다시 만들어
`validate_report`를 통과하는지와 커밋된 예제와 같은지 확인한다.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from pipeline import config as cfgmod
from pipeline.handoff import analysis, downstream, judgment, translation
from pipeline.handoff.bundle import bundle_content_sha256, load_policy_rules
from pipeline.handoff.canonical import sha256_file
from pipeline.handoff.envelope import CONTRACT_VERSION, StageReport
from pipeline.handoff.validate import validate_report
from pipeline.stages import logo as logo_stage
from pipeline.types import BBox, Line, OcrRegion, Section, TextBlock
from pipeline.vlm import LlmReply, VlmError

PUBLIC_ROOT = "/srv/pixlate-work"
EXAMPLE_UNICODE_VERSION = "15.0.0"  # 예제 logo_record에 쓰는 고정값(Python 3.12). 운영 기록은 실제 런타임 값


# ---------------------------------------------------------------------------
# 합성 입력
# ---------------------------------------------------------------------------
def config() -> dict[str, Any]:
    cfg = cfgmod.load_config(None, [])
    cfg["section"]["long_section_px"] = 999999  # VLM 경계 선택을 부르지 않는다(대체 예제는 별도로 강제)
    return cfg


def source_image(path: Path, width: int = 600, height: int = 800) -> Path:
    """흰 배경 위·연회색 아래 두 구간 + 글자 대신 어두운 사각형. ① 색 전환 경계 하나."""
    a = np.full((height, width, 3), 255, np.uint8)
    a[height // 2:, :] = (226, 226, 226)
    for y in (60, 140, 460, 540):
        a[y:y + 24, 60:360] = (30, 30, 30)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(a).save(path)
    return path


class FakeOcr:
    """섹션마다 정해 둔 글자 영역을 순서대로 돌려준다(BGR 배열 → PaddleOCR 결과 dict 형식)."""

    def __init__(self, texts: list[list[str]]):
        self.texts, self.n = texts, 0

    def __call__(self, image_bgr: np.ndarray) -> dict[str, Any]:
        texts = self.texts[self.n] if self.n < len(self.texts) else []
        self.n += 1
        polys = [[[60, 60 + 80 * i], [360, 60 + 80 * i], [360, 84 + 80 * i], [60, 84 + 80 * i]] for i in range(len(texts))]
        return {"rec_polys": polys, "rec_texts": texts, "rec_scores": [0.97] * len(texts)}


class FailingVlm:
    def __call__(self, image, prompt):
        raise VlmError("합성: VLM 호출 실패(대체 예제)")


def block(key: str, section_key: str, order: int, text: str, y: int, role: str = "body", score: float = 0.97) -> TextBlock:
    poly = [(60, y), (360, y), (360, y + 24), (60, y + 24)]
    r = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=score, poly=poly, bbox=BBox.from_poly(poly))
    ln = Line(line_key=f"line_{order:03d}", text=text, bbox=r.bbox, regions=[r])
    return TextBlock(block_key=key, section_key=section_key, block_order=order, source_ko=text, source_lines=[ln], bbox=r.bbox,
                     role=role, ocr_confidence=score)


def section_with_image(root: Path, key: str = "sec_1_01", *, width: int = 600, height: int = 400) -> Section:
    a = np.full((height, width, 3), 250, np.uint8)
    for y in (40, 120, 200):
        a[y:y + 24, 60:360] = (20, 20, 20)
    p = root / "sections" / f"{key}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(a).save(p)
    return Section(section_key=key, source_image_id=1, section_order=1, top_offset=0, height=height, width=width, image_path=str(p))


def bundle(*, local_ok: bool = True, unsplit_alternatives: bool = False) -> dict[str, Any]:
    """합성 고정 묶음(운영 입력 형식). 표현 · 사유는 '가짜' 합성 값이다."""
    ev = [{"external_id": "EV-1", "source_type": "합성", "document": "합성 문서", "quote": None, "article": "합성 1조",
           "url": "https://example.test/1", "is_primary": True},
          {"external_id": "EV-2", "source_type": "합성", "document": None, "quote": "합성 인용", "article": None, "url": None,
           "is_primary": False}]
    reg = [
        {"external_id": "RG-901", "regulatory_class": "cosmetic", "source_expression": "가짜치료", "variant_ko": ["가짜치료"],
         "variant_en": ["fake cure"], "alternative_expression": [], "verdict_status": "regulated", "source_verdict_status": "regulated",
         "reason": "합성: 금지형", "evidence": ev, "confirmed_date": "2026-09-28"},
        # 시트의 rewritable은 서비스 판정 regulated + 대체 표현 · 원값 rewritable로 공급된다
        {"external_id": "RG-902", "regulatory_class": "cosmetic", "source_expression": "가짜완화", "variant_ko": ["가짜완화"],
         "variant_en": ["fake soothing"], "alternative_expression": ["the look of fake calm"], "verdict_status": "regulated",
         "source_verdict_status": "rewritable", "reason": "합성: 완충형", "evidence": [], "confirmed_date": None},
        {"external_id": "RG-903", "regulatory_class": "common", "source_expression": "가짜차단", "variant_ko": ["가짜차단"],
         "variant_en": ["fake block"], "alternative_expression": ["Fake Block [value]"], "verdict_status": "conditional",
         "source_verdict_status": None, "reason": "합성: 조건부(수치 템플릿)", "evidence": [], "confirmed_date": None},
        {"external_id": "RG-905", "regulatory_class": "cosmetic", "source_expression": "가짜치료 안함", "variant_ko": ["가짜치료 안함"],
         "variant_en": ["no fake cure"], "alternative_expression": [], "verdict_status": "allowed", "source_verdict_status": "allowed",
         "reason": "합성: 허용형", "evidence": [], "confirmed_date": None},
        {"external_id": "RG-906", "regulatory_class": "otc", "source_expression": "가짜약효", "variant_ko": ["가짜약효"],
         "variant_en": [], "alternative_expression": [], "verdict_status": "regulated", "source_verdict_status": "regulated",
         "reason": "합성: OTC 전용", "evidence": [], "confirmed_date": None},
    ]
    if unsplit_alternatives:  # #71 확정 전 공급 형태 — 나누지 않은 원문 문자열
        for r in reg:
            r["alternative_expression"] = "; ".join(r["alternative_expression"])
    names = ["가짜기획", "가짜문의", "가짜증정", "가짜반품", "가짜이벤트", "원", "가짜정품", "가짜몰"]
    local = [{"external_id": f"LC-0{i + 1}", "item": f"합성 항목 {i + 1}", "patterns": [n], "verdict_status": "irrelevant" if i % 2 else "needs_fix",
              "exclusion_context": f"합성 제외 맥락 {i + 1}", "keep_context": f"합성 유지 맥락 {i + 1}", "seller_message": f"합성 셀러 안내 {i + 1}",
              "confirmed_date": None} for i, n in enumerate(names)]
    rules = load_policy_rules()  # 저장소 운영 규칙 그대로
    raw: dict[str, Any] = {"bundle_id": "bundle-synthetic-1", "bundle_sha256": "0" * 64, "target_country": "US",
                           "regulation": {"version": "regulation@synthetic.1", "entries": reg},
                           "local": {"version": "local@synthetic.1", "entries": local} if local_ok else None,
                           "local_unavailable": None if local_ok else {"reason": "합성: 현지 사전 조회 실패"},
                           "rules": rules}
    raw["bundle_sha256"] = bundle_content_sha256(raw)
    return raw


def ident(stage: str, attempt: str = "a-1") -> dict[str, Any]:
    return {"contract_version": CONTRACT_VERSION, "execution_id": "exec-100", "attempt_id": attempt, "stage": stage,
            "input_snapshot_id": f"snap-{stage}-{attempt}"}


# ---------------------------------------------------------------------------
# 가짜 모델
# ---------------------------------------------------------------------------
class FakeJudge:
    """현지 8항목: 후보가 있는 항목은 present(근거 = 후보 블록), 나머지는 absent."""

    config = {"model": "fake", "temperature": 0, "timeout_s": 1}

    def __call__(self, prompt: str, payload: str, image) -> LlmReply:
        p = json.loads(payload)
        items = []
        for it in p["items"]:
            if it["candidate_blocks"]:
                items.append({"id": it["id"], "status": "present", "evidence_source": "text", "evidence": it["candidate_blocks"],
                              "reason": "합성: 후보 블록이 제외 맥락에 해당"})
            else:
                items.append({"id": it["id"], "status": "absent", "evidence_source": "image", "evidence": [], "reason": "합성: 해당 없음"})
        return LlmReply(text=json.dumps({"items": items}, ensure_ascii=False), usage=None)


class FailingLlm:
    config = {"model": "fake", "temperature": 0, "timeout_s": 1}

    def __call__(self, prompt, payload, image):
        raise VlmError("합성: 호출 시간 초과")


class FakeLabel:
    config = {"model": "fake", "temperature": 0, "timeout_s": 1}

    def __call__(self, prompt: str, payload: str, image) -> LlmReply:
        p = json.loads(payload)
        return LlmReply(text=json.dumps({"labels": [{"id": b["id"], "is_product_label": "라벨" in b["text"]} for b in p["blocks"]]}), usage=None)


class FakeTranslate:
    """대상마다 결정적 영어 문장. '판독불가'가 든 블록은 명시적 실패."""

    config = {"model": "fake", "temperature": 0, "timeout_s": 1}

    def __init__(self, fail_marker: str | None = "판독불가"):
        self.fail_marker = fail_marker

    words = {"가짜치료": "fake cure", "가짜완화": "the look of fake calm", "크림": "cream", "가짜차단": "fake block", "가격": "price",
             "3만원": "KRW 30,000", "가짜브랜드": "FakeBrand"}

    def __call__(self, prompt: str, payload: str, image) -> LlmReply:
        p = json.loads(payload)
        out = []
        for b in p["blocks"]:
            if "id" not in b:
                continue
            if self.fail_marker and self.fail_marker in b["text"]:
                out.append({"id": b["id"], "failed": True, "reason": "합성: 판독 불가"})
                continue
            t = b["text"]
            for k, v in self.words.items():
                t = t.replace(k, v)
            out.append({"id": b["id"], "translation": t})
        return LlmReply(text=json.dumps({"blocks": out}, ensure_ascii=False), usage=None)


class FakeInpaintModel:
    def describe(self) -> dict[str, Any]:
        return {"name": "fake-fill", "note": "합성: 마스크 안을 회색으로 채운다"}

    def inpaint(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        out = image.copy()
        out[mask > 0] = (200, 200, 200)
        return out


class BrokenInpaintModel(FakeInpaintModel):
    def inpaint(self, image, mask):
        raise RuntimeError("합성: CUDA 오류")


# ---------------------------------------------------------------------------
# 사례
# ---------------------------------------------------------------------------
Case = tuple[str, dict[str, Any], Callable[[dict[str, Any]], StageReport]]


def cases(root: Path) -> list[Case]:
    cfg = config()
    out: list[Case] = []
    src = source_image(root / "uploads" / "source_1.png")
    texts = [["가짜치료 크림", "가격 3만원"], ["가짜완화 SPF50", "연구원 추천"]]

    def analyze_req(name: str) -> dict[str, Any]:
        return {**ident("analyze"), "sources": [{"source_image_id": 1, "upload_order": 1, "path": str(src), "sha256": sha256_file(src)}],
                "out_dir": str(root / "analyze" / name), "use_llm": False}

    out.append(("analyze.completed", analyze_req("ok"), lambda r: analysis.run_analyze(r, cfg, ocr_engine=FakeOcr(texts))))
    cfg_vlm = copy.deepcopy(cfg)
    cfg_vlm["section"]["long_section_px"] = 300
    out.append(("analyze.completed_split_fallback", analyze_req("fallback"),
                lambda r: analysis.run_analyze(r, cfg_vlm, vlm=FailingVlm(), ocr_engine=FakeOcr([["가짜치료 크림", "가격 3만원", "가짜완화 SPF50"]]))))
    bad = analyze_req("bad")
    bad["sources"][0]["sha256"] = "f" * 64
    out.append(("analyze.failed_input_hash", bad, lambda r: analysis.run_analyze(r, cfg, ocr_engine=FakeOcr(texts))))

    # ③-1 · ③-1′ — 분석 결과를 직접 만든다
    s1 = section_with_image(root / "judge", "sec_1_01")
    s2 = section_with_image(root / "judge", "sec_1_02").model_copy(update={"section_order": 2, "top_offset": 400})
    blocks = [block("blk_001", "sec_1_01", 1, "가짜치료 크림", 40, "title"), block("blk_002", "sec_1_01", 2, "가격 3만원", 120, "price"),
              block("blk_003", "sec_1_02", 1, "가짜치료 안함 처방", 40), block("blk_004", "sec_1_02", 2, "연구원 추천", 120)]
    ar = {"schema_version": "1", "sections": [s1.model_dump(mode="json"), s2.model_dump(mode="json")],
          "blocks": [b.model_dump(mode="json") for b in blocks], "warnings": []}
    imgs = {s.section_key: {"path": s.image_path, "sha256": sha256_file(s.image_path)} for s in (s1, s2)}

    def judge_req(b: dict[str, Any]) -> dict[str, Any]:
        return {**ident("judge"), "target_country": "US", "regulatory_class": "cosmetic", "analyze_result": ar, "section_images": imgs, "bundle": b}

    out.append(("judge.completed", judge_req(bundle()), lambda r: judgment.run_judgment(r, cfg, llm=FakeJudge())))
    one = {**judge_req(bundle()), "target_section_keys": ["sec_1_02"], "section_images": {"sec_1_02": imgs["sec_1_02"]}}
    out.append(("judge.completed_single_section", one, lambda r: judgment.run_judgment(r, cfg, llm=FakeJudge())))
    out.append(("judge.completed_local_failed", judge_req(bundle()), lambda r: judgment.run_judgment(r, cfg, llm=FailingLlm())))
    out.append(("judge.completed_local_not_checked", judge_req(bundle(local_ok=False)), lambda r: judgment.run_judgment(r, cfg, llm=FakeJudge())))
    broken = bundle()
    broken["regulation"]["entries"][0]["reason"] = "변조"
    out.append(("judge.failed_bundle_hash", judge_req(broken), lambda r: judgment.run_judgment(r, cfg, llm=FakeJudge())))

    # ④⑤⑥⑦ — 섹션 하나
    ds = section_with_image(root / "downstream", "sec_1_01")
    dblocks = [block("blk_001", "sec_1_01", 1, "가짜브랜드", 40, "title"), block("blk_002", "sec_1_01", 2, "라벨 문구", 120),
               block("blk_003", "sec_1_01", 3, "가짜완화 크림", 200), block("blk_004", "sec_1_01", 4, "  ", 280, score=0.2)]
    simg = {"path": ds.image_path, "sha256": sha256_file(ds.image_path)}
    sec_json = ds.model_dump(mode="json")
    lab_req = {**ident("label"), "section": sec_json, "section_image": simg, "blocks": [b.model_dump(mode="json") for b in dblocks]}
    out.append(("label.completed", lab_req, lambda r: downstream.run_label(r, cfg, llm=FakeLabel())))
    out.append(("label.skipped_no_blocks", {**lab_req, "blocks": []}, lambda r: downstream.run_label(r, cfg, llm=FakeLabel())))
    out.append(("label.failed_model", lab_req, lambda r: downstream.run_label(r, cfg, llm=FailingLlm())))
    lab_rep = downstream.run_label(lab_req, cfg, llm=FakeLabel())
    brand = {"brand_id": "7", "name_ko": "가짜브랜드", "name_en": "FakeBrand"}
    logo_req = {**ident("logo"), "image_id": "1", "section_key": "sec_1_01", "blocks": lab_req["blocks"],
                "label_result": lab_rep.payload["label_result"], "label_result_sha256": lab_rep.payload["label_result_sha256"], "brand": brand}
    out.append(("logo.completed", logo_req, lambda r: downstream.run_logo(r, cfg)))
    one = [lab_req["blocks"][1]]
    lab_one = downstream.run_label({**lab_req, "blocks": one}, cfg, llm=FakeLabel())
    out.append(("logo.skipped_all_label", {**logo_req, "blocks": one, "label_result": lab_one.payload["label_result"],
                                           "label_result_sha256": lab_one.payload["label_result_sha256"]}, lambda r: downstream.run_logo(r, cfg)))
    out.append(("logo.failed_brand_empty", {**logo_req, "brand": {**brand, "name_en": "  "}}, lambda r: downstream.run_logo(r, cfg)))
    logo_rep = downstream.run_logo(logo_req, cfg)
    prev = {"image_id": "1", "label_result": lab_rep.payload["label_result"], "label_result_sha256": lab_rep.payload["label_result_sha256"],
            "logo_result": logo_rep.payload["logo_result"], "logo_result_sha256": logo_rep.payload["logo_result_sha256"],
            "logo_record": logo_rep.payload["logo_record"], "logo_record_sha256": logo_rep.payload["logo_record_sha256"]}
    inp = {**ident("inpaint"), "section": sec_json, "section_image": simg, "blocks": lab_req["blocks"], **prev}
    out.append(("inpaint.completed", {**inp, "out_dir": str(root / "inpaint" / "ok")},
                lambda r: downstream.run_inpaint(r, cfg, model=FakeInpaintModel())))
    # 빈 마스크: 대상 블록이 모두 보호(라벨 · 로고) 또는 공백인 섹션
    empty_blocks = [lab_req["blocks"][0], lab_req["blocks"][1]]
    lab_e = downstream.run_label({**lab_req, "blocks": empty_blocks}, cfg, llm=FakeLabel())
    logo_e = downstream.run_logo({**logo_req, "blocks": empty_blocks, "label_result": lab_e.payload["label_result"],
                                  "label_result_sha256": lab_e.payload["label_result_sha256"]}, cfg)
    prev_e = {**prev, "label_result": lab_e.payload["label_result"], "label_result_sha256": lab_e.payload["label_result_sha256"],
              "logo_result": logo_e.payload["logo_result"], "logo_result_sha256": logo_e.payload["logo_result_sha256"],
              "logo_record": logo_e.payload["logo_record"], "logo_record_sha256": logo_e.payload["logo_record_sha256"]}
    out.append(("inpaint.skipped_empty_mask", {**inp, **prev_e, "blocks": empty_blocks, "out_dir": str(root / "inpaint" / "empty")},
                lambda r: downstream.run_inpaint(r, cfg, model=FakeInpaintModel())))
    out.append(("inpaint.failed_model", {**inp, "out_dir": str(root / "inpaint" / "fail")},
                lambda r: downstream.run_inpaint(r, cfg, model=BrokenInpaintModel())))
    sty = {**ident("style"), "section": sec_json, "section_image": simg, "blocks": lab_req["blocks"], **prev}
    out.append(("style.completed", sty, lambda r: downstream.run_style(r, cfg)))
    out.append(("style.skipped_no_targets", {**sty, **prev_e, "blocks": empty_blocks}, lambda r: downstream.run_style(r, cfg)))

    # ⑧ 번역 · 검사
    tblocks = [block("blk_001", "sec_1_01", 1, "가짜치료 크림", 40, "title"), block("blk_002", "sec_1_01", 2, "가짜완화 SPF50", 120),
               block("blk_003", "sec_1_01", 3, "판독불가 ###", 200), block("blk_004", "sec_1_01", 4, "가짜차단 SPF50 PA++++", 280)]
    tb = [b.model_dump(mode="json") for b in tblocks]
    glossary = {"status": "ok", "version": "glossary@synthetic.1",
                "terms": [{"glossary_id": "G-1", "term_ko": "크림", "term_target": "cream", "enforcement": "enforced"},
                          {"glossary_id": "G-2", "term_ko": "가짜차단", "term_target": "Fake Shield", "enforcement": "enforced"}]}
    treq = {**ident("translate"), "target_country": "US", "target_lang": "en", "regulatory_class": "cosmetic", "section_key": "sec_1_01",
            "blocks": tb, "targets": [{"block_key": k, "revision": 0} for k in ("blk_001", "blk_002", "blk_004")],
            "instructions": [{"block_key": "blk_002", "external_id": "RG-902", "matched_text": "가짜완화"}], "glossary": glossary,
            "bundle": bundle()}
    out.append(("translate.completed", treq, lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    partial = {**treq, "targets": [{"block_key": k, "revision": 0} for k in ("blk_001", "blk_002", "blk_003")]}
    out.append(("translate.failed_partial", partial, lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    retry = {**ident("translate", "a-2"), **{k: v for k, v in partial.items() if k not in ident("translate")},
             "targets": [{"block_key": "blk_003", "revision": 0}],
             "preserved": [{"block_key": "blk_001", "revision": 1, "translation_sha256": "a" * 64, "source_attempt_id": "a-1"},
                           {"block_key": "blk_002", "revision": 1, "translation_sha256": "b" * 64, "source_attempt_id": "a-1"}],
             "instructions": []}
    # 재시도: 이번 실패 대상(blk_003)만 계산하고 보존 성공분은 결과로 돌려주지 않는다(5.32). 두 번째 호출은 성공하는 가짜 모델
    out.append(("translate.completed_retry_target_only", retry, lambda r: translation.run_translate(r, cfg, llm=FakeTranslate(None))))
    out.append(("translate.failed_all_call", treq, lambda r: translation.run_translate(r, cfg, llm=FailingLlm())))
    out.append(("translate.skipped_no_targets", {**treq, "targets": [], "instructions": []},
                lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    out.append(("translate.failed_template_instruction",
                {**treq, "instructions": [{"block_key": "blk_004", "external_id": "RG-903", "matched_text": "가짜차단"}]},
                lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    out.append(("translate.failed_unsplit_alternative", {**treq, "bundle": bundle(unsplit_alternatives=True)},
                lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    out.append(("translate.completed_unsplit_no_instruction", {**treq, "instructions": [], "bundle": bundle(unsplit_alternatives=True)},
                lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    out.append(("translate.failed_glossary_supply", {**treq, "glossary": {"status": "failed", "reason": "합성: 검색 실패", "terms": []}},
                lambda r: translation.run_translate(r, cfg, llm=FakeTranslate())))
    creq = {**ident("text_check"), "target_country": "US", "regulatory_class": "cosmetic", "section_key": "sec_1_01", "blocks": tb,
            "targets": [{"block_key": "blk_001", "revision": 1, "text": "Fake cure cream"},
                        {"block_key": "blk_004", "revision": 1, "text": "Fake block SPF50 PA++++"}],
            "glossary": glossary, "bundle": bundle()}
    out.append(("text_check.completed_with_flags", creq, lambda r: translation.run_text_check(r)))
    return out


def _publicize(v: Any, root: str) -> Any:
    if isinstance(v, str):
        if v.startswith(root):
            return PUBLIC_ROOT + v[len(root):].replace("\\", "/")
        return v
    if isinstance(v, list):
        return [_publicize(x, root) for x in v]
    if isinstance(v, dict):
        return {k: _publicize(x, root) for k, x in v.items()}
    return v


def generate(out_dir: Path, work_root: Path | None = None) -> dict[str, list[str]]:
    """예제를 out_dir에 쓰고 사례별 validate_report 결과를 돌려준다(빈 목록 = 통과)."""
    tmp = Path(tempfile.mkdtemp(prefix="pixlate-handoff-")) if work_root is None else work_root
    saved_unicode = logo_stage.UNICODE_VERSION
    logo_stage.UNICODE_VERSION = EXAMPLE_UNICODE_VERSION  # 실행 Python과 무관하게 예제를 재현한다(합성 값의 정규화는 버전과 무관)
    try:
        root = str(tmp.resolve())
        results: dict[str, list[str]] = {}
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, req, fn in cases(tmp.resolve()):
            rep = fn(copy.deepcopy(req))
            results[name] = validate_report(rep, req)
            doc = {"case": name, "request": _publicize(req, root), "report": _publicize(rep.model_dump(mode="json"), root),
                   "be_validation_problems": results[name]}
            (out_dir / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return results
    finally:
        logo_stage.UNICODE_VERSION = saved_unicode
        if work_root is None:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    res = generate(Path(a.out))
    bad = {k: v for k, v in res.items() if v}
    for k in res:
        print(("FAIL " if res[k] else "ok   ") + k + ("" if not res[k] else f" — {res[k]}"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
