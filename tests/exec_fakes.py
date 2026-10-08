"""실행·통합 테스트 대역 — AI 함수 자리에 끼우는 결정적 가짜. 실제 AI 품질·호출을 검증하지 않는다.

- FakeAnalyzer: pipeline/samples/synthetic_01 의 섹션 이미지·병합 결과로 AnalyzeResult 버전 1을 만든다.
- FakeJudge: 규제 패턴이 든 블록에 finding·매칭·판정을 만들고, 섹션별로 실패를 흉내낸다.
- QueueDispatcher: 큐 대신 메시지를 모아 두고 drain() 으로 차례로 실행한다(브로커 유실·중복 전달 재현).
"""
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from PIL import Image
from sqlalchemy import text

from app import ai_adapters
from pipeline.types import AnalyzeResult, MergeResult, Section, SplitResult

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "pipeline" / "samples" / "synthetic_01"


def sample_source_png() -> bytes:
    return (SAMPLE / "source.png").read_bytes()


def png_bytes(w: int, h: int, color=(240, 240, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeAnalyzer:
    impl_version = "fake-analyzer/1"

    def __init__(self, *, fail: Exception | None = None, fails: int = 0, mutate: Callable[[dict], None] | None = None,
                 split_fallback: bool = False):
        self.fail = fail
        self.fails = fails  # 처음 n번만 실패
        self.calls = 0
        self.mutate = mutate
        self.split_fallback = split_fallback  # 첫 원본을 ① 대체(원본 전체 섹션)로 기록

    def analyze_with_fallbacks(self, sources, out_dir: Path):
        res = self.analyze(sources, out_dir)
        first = min(sources, key=lambda s: s.upload_order)
        fb = [{"kind": "section_split_whole_image", "source_image_id": first.source_image_id, "cause": "VlmError: 합성"}]
        return res, (fb if self.split_fallback else [])

    def analyze(self, sources, out_dir: Path):
        self.calls += 1
        if self.fail is not None and (self.fails == 0 or self.calls <= self.fails):
            raise self.fail
        split = SplitResult.model_validate_json((SAMPLE / "expected" / "split.json").read_text(encoding="utf-8"))
        sections, blocks = [], []
        n = 0
        for src in sorted(sources, key=lambda s: s.upload_order):
            for sec in split.sections:
                key = f"sec_{src.source_image_id}_{sec.section_order:02d}"
                dst = out_dir / "sections" / f"{key}.png"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(SAMPLE / "expected" / "sections" / f"{sec.section_key}.png", dst)
                sections.append(Section(section_key=key, source_image_id=src.source_image_id, section_order=sec.section_order,
                                        top_offset=sec.top_offset, height=sec.height, width=sec.width, image_path=str(dst)))
                merged = MergeResult.model_validate_json(
                    (SAMPLE / "expected" / "merge" / f"{sec.section_key}.json").read_text(encoding="utf-8"))
                for b in merged.blocks:
                    n += 1
                    blocks.append(b.model_copy(update={"section_key": key, "block_key": f"blk_{n:03d}"}))
        res = AnalyzeResult(sections=sections, blocks=blocks)
        if self.mutate is not None:
            raw = res.model_dump(mode="json")
            self.mutate(raw)
            return AnalyzeResult.model_validate(raw)
        return res


POLICY = ai_adapters.PolicyInfo(rules_version="policy@test.1", rules_sha256="0" * 64, impl_version="fake-policy/1")


class FakeJudge:
    """규제: 블록 원문에 사전 패턴(variant_ko)이 있으면 present finding + 매칭 + 판정(bucket 은 regulated→exclude, 그 밖 include).
    local_fail: 현지 AI 판정 실패를 낼 section_key 집합. reg_fail: {section_key: 남은 실패 횟수} 규제 실패(retryable)."""

    impl_version = "fake-judge/1"

    def __init__(self, *, local_fail: set[str] | None = None, reg_fail: dict[str, int] | None = None,
                 mutate: Callable[[ai_adapters.JudgeOutcome], ai_adapters.JudgeOutcome] | None = None,
                 local_uncertain: set[str] | None = None):
        self.local_fail = local_fail or set()
        self.reg_fail = dict(reg_fail or {})
        self.mutate = mutate
        self.local_uncertain = local_uncertain or set()
        self.calls: list[str] = []
        self.inputs: list[Any] = []

    def judge(self, section, bundle, *, regulatory_class, target_country):
        self.calls.append(section.section_key)
        self.inputs.append(section)
        assert Path(section.image_path).is_absolute() and Path(section.image_path).exists()
        if self.reg_fail.get(section.section_key, 0) > 0:
            self.reg_fail[section.section_key] -= 1
            return ai_adapters.JudgeOutcome(
                section_key=section.section_key, regulatory=ai_adapters.ScopeStatus(status="failed", error="timeout", retryable=True),
                local=ai_adapters.ScopeStatus(status="not_inspected"), inspection={"planned": []})
        findings, matches, verdicts = [], [], []
        for e in bundle["regulatory"]["entries"]:
            for b in section.blocks:
                for pat in e["variant_ko"]:
                    i = b.source_ko.find(pat)
                    if i < 0:
                        continue
                    fk = f"f_{len(findings) + 1:02d}"
                    if any(f.content_type == "regulatory_expression" for f in findings):
                        f0 = next(f for f in findings if f.content_type == "regulatory_expression")
                        fk = f0.finding_key
                        if e["external_id"] not in f0.dictionary_refs:
                            f0.dictionary_refs.append(e["external_id"])
                        if b.key not in f0.evidence_block_keys:
                            f0.evidence_block_keys.append(b.key)
                    else:
                        findings.append(ai_adapters.FindingOut(
                            finding_key=fk, content_type="regulatory_expression", status="present", evidence_block_keys=[b.key],
                            evidence_source="text", reason="규제 표현 검출", dictionary_refs=[e["external_id"]]))
                    matches.append(ai_adapters.MatchOut(match_key=f"m_{len(matches) + 1:03d}", finding_key=fk,
                                                        dictionary_ref=e["external_id"], block_key=b.key, start=i,
                                                        end=i + len(pat), matched_text=pat))
                    if not any(v.dictionary_ref == e["external_id"] for v in verdicts) and e["verdict_status"] != "allowed":
                        verdicts.append(ai_adapters.VerdictOut(
                            verdict_key=f"v_{len(verdicts) + 1:02d}", finding_key=fk, dictionary_ref=e["external_id"],
                            verdict_status=e["verdict_status"],
                            bucket="exclude" if e["verdict_status"] == "regulated" and not e["alternative_expression"] else "include",
                            finding_status="present", problem_text=pat))
        local = ai_adapters.ScopeStatus(status="ok" if bundle["local"]["status"] == "ok" else "not_inspected")
        if section.section_key in self.local_fail:
            local = ai_adapters.ScopeStatus(status="failed", error="gemini timeout")
        elif section.section_key in self.local_uncertain and bundle["local"]["status"] == "ok":
            le = bundle["local"]["entries"][0]
            findings.append(ai_adapters.FindingOut(finding_key="f_local", content_type=f"local_{le['external_id']}", status="uncertain",
                                                   evidence_source="image", reason="맥락 확인 필요",
                                                   dictionary_refs=[le["external_id"]]))
            verdicts.append(ai_adapters.VerdictOut(verdict_key="v_local", finding_key="f_local", dictionary_ref=le["external_id"],
                                                   verdict_status=le["verdict_status"], bucket="exclude", finding_status="uncertain"))
        out = ai_adapters.JudgeOutcome(
            section_key=section.section_key, regulatory=ai_adapters.ScopeStatus(status="ok"), local=local, findings=findings,
            matches=matches, verdicts=verdicts, inspection={"planned": ["regulatory", "local"], "blocks": len(section.blocks)},
            policy=POLICY, impl={"model": "fake"})
        return self.mutate(out) if self.mutate else out


class QueueDispatcher:
    def __init__(self):
        self.messages: list[tuple[str, list[Any]]] = []
        self.drop = False  # True 면 보낸 것처럼 하고 버린다(브로커 유실)
        self.fail_send = False  # True 면 전송 실패

    def __call__(self, task_name: str, args: list[Any]) -> None:
        if self.fail_send:
            raise ConnectionError("broker down")
        if not self.drop:
            self.messages.append((task_name, list(args)))

    def drain(self, limit: int = 500, skip: set[str] | None = None) -> list[tuple[str, list[Any]]]:
        import app.tasks as tasks

        done = []
        n = 0
        while self.messages and n < limit:
            name, args = self.messages.pop(0)
            if skip and name in skip:
                done.append((name, args))
                continue
            fn = getattr(tasks, name.rsplit(".", 1)[-1])
            fn(*args)
            done.append((name, args))
            n += 1
        return done


def seed_dictionary(conn, *, with_local: bool = True, regulatory: bool = True) -> None:
    """규제 2행(cosmetic: 금지형·조건부) + 현지 8행(AI 지원 대응표 LC-01~LC-08과 같은 ID 집합) + 대표 근거."""
    rows = []
    if regulatory:
        rows += [
            ("RG-901", "regulatory", "US", "cosmetic", "치료", ["치료"], ["cure"], "regulated", "regulated", None, "질병 치료 표방 금지"),
            ("RG-902", "regulatory", "US", "cosmetic", "주름 개선", ["주름 개선", "주름개선"], ["anti-wrinkle"], "conditional",
             "conditional", None, "기능성 조건 충족 필요"),
        ]
    if with_local:
        rows.append(("LC-01", "local", "US", "common", "원료 원산지", ["국내산"], None, "needs_fix", "needs_fix", None, "현지 맥락 확인"))
        rows += [(f"LC-0{i}", "local", "US", "common", f"합성 항목 {i}", [f"합성패턴{i}"], None, "irrelevant", "irrelevant", None,
                  f"합성 안내 {i}") for i in range(2, 9)]
    for ext, dt, c, rc, src, vko, fen, vs, svs, alt, reason in rows:
        did = conn.execute(
            text(
                "INSERT INTO expression_dictionary (external_id, source_expression, variant_ko, forbidden_en, target_country, "
                "regulatory_class, dict_type, verdict_status, source_verdict_status, alternative_expression, reason, confirmed_date, "
                "exclusion_context, keep_context) VALUES (:e, :s, CAST(:v AS jsonb), CAST(:f AS jsonb), :c, :rc, :dt, :vs, :svs, "
                ":alt, :r, DATE '2026-09-30', :ec, :kc) RETURNING id"
            ),
            {"e": ext, "s": src, "v": json.dumps(vko, ensure_ascii=False), "f": json.dumps(fen) if fen is not None else None,
             "c": c, "rc": rc, "dt": dt, "vs": vs, "svs": svs, "alt": alt, "r": reason,
             "ec": "제외 맥락" if dt == "local" else None, "kc": "유지 맥락" if dt == "local" else None},
        ).scalar_one()
        if dt == "regulatory":
            conn.execute(
                text(
                    "INSERT INTO expression_dictionary_evidence (dictionary_id, external_id, evidence_source_type, evidence_article, "
                    "evidence_url, is_primary) VALUES (:d, :e, 'Warning Letter', '21 CFR 700', 'https://example.org/wl', true)"
                ),
                {"d": did, "e": f"WL-{ext[-3:]}"},
            )


# ---------------------------------------------------------------------------------------------------------
# N4 대역 — ④ 라벨·⑧ 번역은 가짜, ⑤ 로고·⑦ 스타일·⑥ 마스크 계산은 실제 파이프라인 코드(⑥ 모델만 가짜)
# ---------------------------------------------------------------------------------------------------------
class FakeLabeler:
    impl_version = "fake-label/1"

    def __init__(self, *, label_words: tuple[str, ...] = ("$",), fail_sections: dict[str, int] | None = None):
        self.label_words = label_words
        self.fail_sections = dict(fail_sections or {})
        self.calls: list[str] = []

    def label(self, section, blocks):
        from pipeline.stages.label import input_fingerprint, is_blank
        from pipeline.types import LabelChecked, LabelDecision, LabelResult

        self.calls.append(section.section_key)
        fp = input_fingerprint(blocks)
        if self.fail_sections.get(section.section_key, 0) > 0:
            self.fail_sections[section.section_key] -= 1
            return LabelResult(section_key=section.section_key, status="failed", labels=None,
                               checked=LabelChecked(input_fingerprint=fp, llm_called=True), error="vlm timeout")
        labels = [LabelDecision(block_key=b.block_key, is_product_label=any(w in b.source_ko for w in self.label_words) and not is_blank(b),
                                basis="blank_text" if is_blank(b) else "vlm")
                  for b in sorted(blocks, key=lambda b: b.block_order)]
        return LabelResult(section_key=section.section_key, status="ok", labels=labels,
                           checked=LabelChecked(input_fingerprint=fp, llm_called=True,
                                                sent_block_keys=[b.block_key for b in blocks if not is_blank(b)],
                                                blank_block_keys=[b.block_key for b in blocks if is_blank(b)]))


class FakeInpaintModel:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def describe(self):
        return {"name": "fake-lama", "weights_sha256": "0" * 64}

    def inpaint(self, image, mask):
        import numpy as np

        self.calls += 1
        if self.fail:
            raise RuntimeError("CUDA out of memory (fake)")
        out = image.copy()
        out[mask > 0] = np.array([250, 250, 250], dtype=out.dtype)
        return out


class FakeTranslator:
    """기본: 모든 대상 ok("EN:<원문 앞부분>"). fail_keys: 실패시킬 블록 키 집합, fail_all: 남은 전부 실패 횟수."""

    impl_version = "fake-translate/1"

    def __init__(self, *, fail_keys: set[str] | None = None, fail_all: int = 0, mutate=None):
        self.fail_keys = set(fail_keys or set())
        self.fail_all = fail_all
        self.mutate = mutate
        self.calls: list[tuple[str, list[str]]] = []

    def translate(self, section_key, blocks, *, target_lang, context):
        from app.ai_adapters import TranslateItem, TranslateOutcome

        targets = [b for b in blocks if b.is_target]
        self.calls.append((section_key, [b.key for b in targets]))
        all_fail = self.fail_all > 0
        if all_fail:
            self.fail_all -= 1
        items = []
        for b in targets:
            if all_fail or b.key in self.fail_keys:
                items.append(TranslateItem(key=b.key, status="failed", error="model error"))
            else:
                items.append(TranslateItem(key=b.key, status="ok", text=f"EN {b.source_ko[:24]}"))
        out = TranslateOutcome(section_key=section_key, items=items, impl={"model": "fake"})
        return self.mutate(out) if self.mutate else out


def style_defaults_file(tmp_path) -> str:
    """테스트 전용 역할 기본값(제품 기본값 아님 — 승인 값이 아니다)."""
    roles = {r: {"font_color": "#111111", "est_font_px": 20, "align": "left"} for r in ("title", "body", "caption", "price", "caution")}
    p = tmp_path / "style_defaults.test.json"
    p.write_text(json.dumps({"version": "test-only", "roles": roles}), encoding="utf-8")
    return str(p)
