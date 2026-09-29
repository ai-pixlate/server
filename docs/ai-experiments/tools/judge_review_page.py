"""③-1 실측 결과의 우선순위 섹션을 한눈에 보는 검수 페이지 생성(3차 · 사용자 검수용, 2026-09-29).

    python docs/ai-experiments/tools/judge_review_page.py --run pipeline/out/judge-run-v1_<date> --input pipeline/samples/local/blocks-input-v1 \
        --out pipeline/out/judge-run-v1_<date>/review_round3

출력: <out>/index.html(카드: 원본 이미지 · 오버레이 · 권고 · 항목 판정 · 규제 매칭 · verdict 사유) · <out>/img/(이미지 사본) · <out>/user_priority.json(판정표 틀).
우선순위: 제외 권고 섹션 · present 항목 · 매칭 없이 present · 규제 매칭의 검색 경계(브라이트닝 안의 라이트닝) · 문맥 사례(실측 기록 5.1 · 5.2절).
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402
from pipeline.dictionary import load_dictionaries  # noqa: E402
from pipeline.stages import judge  # noqa: E402
from pipeline.types import JudgeResult, PolicyResult  # noqa: E402

CONTEXT_CASES = {  # 실측 기록 5.1 · 5.2절의 문맥 사례(2차 재검토 결과)
    ("GS-01_002", "sec_1_20", "RG-025"): "문맥: `#기미잡티흔적커버`는 커버 표현 (2차: context_valid=아니오)",
    ("GS-03_002", "sec_1_03", "RG-024"): "문맥: `하얗게 뜨지 않는`은 백탁 부정 · 톤업 (미백 ×3은 유효)",
    ("GS-03_010", "sec_1_03", "RG-024"): "문맥: `화이트닝 톤업`은 자사 톤업크림 비교 · 즉각 보정 (2차: 아니오); `라이트닝`은 제품명 내부",
    ("GS-03_018", "sec_1_05", "RG-024"): "문맥: `하얘지는 톤업 크림`은 즉각 보정 (2차: 아니오)",
}
STATUS_KO = {"present": "해당(present)", "absent": "비해당(absent)", "uncertain": "판단불가(uncertain)"}
STATUS_COLOR = {"present": "#dc2828", "absent": "#28a03c", "uncertain": "#f09614"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    run, inp, out = Path(a.run), Path(a.input), Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    rows = json.loads((run / "sections.json").read_text(encoding="utf-8"))
    cfg_dict_dir = ROOT / "pipeline" / "samples" / "local" / "dict" / "normalized"
    dicts = load_dictionaries(cfg_dict_dir)
    pv = dicts.policy_view()
    lc_name = {e.id: e.item for e in dicts.local.entries}
    lc_ctx = {e.id: (e.exclusion_context, e.keep_context) for e in dicts.local.entries}
    rg_name = {e.id: e.source_expression for e in dicts.regulation.entries}

    # 우선순위 섹션 선정
    picked: dict[tuple[str, str], list[str]] = {}
    loaded = {}
    for r in rows:
        src, key = r["source"], r["section"]
        jr = jsonio.load_model(run / src / "judge" / f"{key}.json", JudgeResult)
        pr = jsonio.load_model(run / src / "policy" / f"{key}.json", PolicyResult)
        loaded[(src, key)] = (jr, pr, r)
        why = []
        if pr.bucket_recommendation == "exclude":
            why.append("제외 권고")
        matched = {m.dictionary_ref for m in jr.matches}
        for f in jr.content_findings.findings:
            if f.content_type.startswith("LC-") and f.status == "present":
                why.append(f"{f.content_type} present" + ("(매칭 없음)" if f.content_type not in matched else ""))
        if any(m.pattern == "라이트닝" for m in jr.matches):
            why.append("규제 검색 경계: 브라이트닝 안의 라이트닝")
        for (s, k, ref), note in CONTEXT_CASES.items():
            if s == src and k == key:
                why.append("규제 문맥 사례")
        if why:
            picked[(src, key)] = sorted(set(why))

    cards = []
    template = []
    for (src, key), why in picked.items():
        jr, pr, r = loaded[(src, key)]
        merge = jsonio.load_merge(inp / src / "merge" / f"{key}.json")
        blocks = {b.block_key: b.source_ko for b in merge.blocks}
        orig = inp / src / "sections" / f"{key}.png"
        ov = run / src / "overlay" / f"{key}.png"
        shutil.copy2(orig, out / "img" / f"{src}_{key}_orig.png")
        shutil.copy2(ov, out / "img" / f"{src}_{key}_overlay.png")
        rec = pr.bucket_recommendation or "-"
        rec_color = "#dc2828" if rec == "exclude" else "#28a03c"
        # 항목 판정(현지부적합 전부 + 규제 present)
        items_html = []
        for f in jr.content_findings.findings:
            name = lc_name.get(f.content_type) or rg_name.get(f.content_type, "")
            ev = ", ".join(f.evidence_block_ids) or "(없음)"
            ev_text = " / ".join(esc(blocks.get(b, "")[:60]) for b in f.evidence_block_ids)
            color = STATUS_COLOR[f.status]
            hl = " class='hl'" if f.status != "absent" else ""
            items_html.append(
                f"<tr{hl}><td>{esc(f.content_type)}<br><small>{esc(name)}</small></td>"
                f"<td style='color:{color};font-weight:600'>{STATUS_KO[f.status]}</td><td>{esc(f.evidence_source)}</td>"
                f"<td><code>{esc(ev)}</code><br><small>{ev_text}</small></td><td>{esc(f.reason)}</td></tr>"
            )
        # 규제 매칭
        m_html = []
        for m in jr.matches:
            if not m.dictionary_ref.startswith("RG-"):
                continue
            t = blocks[m.block_key]
            s, e = m.raw_span.start, m.raw_span.end
            before = t[s - 1] if s > 0 else "^"
            boundary = "내부" if (before.isalnum() or "가" <= before <= "힣") else "독립"
            note = CONTEXT_CASES.get((src, key, m.dictionary_ref), "")
            flag = " class='hl'" if m.pattern == "라이트닝" or note else ""
            ctx = esc(t[max(0, s - 18):s]) + "<mark>" + esc(t[s:e]) + "</mark>" + esc(t[e:e + 18])
            m_html.append(
                f"<tr{flag}><td>{esc(m.dictionary_ref)}<br><small>{esc(rg_name.get(m.dictionary_ref, ''))}</small></td><td><code>{esc(m.pattern)}</code></td>"
                f"<td>{ctx}</td><td>{esc(m.block_key)}</td><td>{boundary}</td><td>{esc(note)}</td></tr>"
            )
        # verdict
        v_html = []
        for v in pr.verdicts:
            v_html.append(
                f"<tr><td>{esc(v.dictionary_ref)}</td><td>{esc(v.verdict_status)}{' (원값 ' + esc(v.source_verdict_status) + ')' if v.source_verdict_status != v.verdict_status else ''}</td>"
                f"<td>{esc(v.finding_status)}</td><td>{esc((v.problem_text or '')[:120])}</td><td>{esc(v.reason)}</td><td>{esc(v.conflict_group or '')}</td></tr>"
            )
        lc_ctx_html = "".join(
            f"<li><b>{esc(i)} {esc(lc_name[i])}</b> — 제외: {esc(lc_ctx[i][0])}<br>유지: {esc(lc_ctx[i][1])}</li>"
            for i in sorted({f.content_type for f in jr.content_findings.findings if f.content_type.startswith('LC-') and f.status != 'absent'})
        )
        cards.append(f"""
<section class="card" id="{esc(src)}-{esc(key)}">
  <h2>{esc(src)} / {esc(key)} <span class="rec" style="background:{rec_color}">권고 {esc(rec)}</span>
      <small>가정 분류 {esc(r['regulatory_class'])} · 검사 범위 {esc(pr.applied.dict_coverage if pr.applied else '-')} · judge {esc(jr.status)}</small></h2>
  <p class="why">우선순위 이유: {esc(' · '.join(why))}</p>
  <div class="imgs">
    <figure><figcaption>원본 섹션 이미지</figcaption><img src="img/{esc(src)}_{esc(key)}_orig.png"></figure>
    <figure><figcaption>오버레이(파랑=매칭 후보 · 빨강=present · 초록=absent · 주황=uncertain)</figcaption><img src="img/{esc(src)}_{esc(key)}_overlay.png"></figure>
  </div>
  <h3>항목 판정 (LLM) — 현지부적합 8항목 + 매칭된 규제 항목</h3>
  <table><tr><th>항목</th><th>status</th><th>근거 출처</th><th>근거 블록 · 텍스트</th><th>LLM reason</th></tr>{''.join(items_html)}</table>
  {('<h3>해당 · 판단불가 항목의 사전 맥락</h3><ul>' + lc_ctx_html + '</ul>') if lc_ctx_html else ''}
  <h3>규제 매칭 (규칙, LLM 판정 없음)</h3>
  <table><tr><th>항목</th><th>패턴</th><th>원문 문맥</th><th>블록</th><th>경계</th><th>2차 검수 메모</th></tr>{''.join(m_html) or '<tr><td colspan=6>없음</td></tr>'}</table>
  <h3>정책 verdict (포함/제외 근거)</h3>
  <table><tr><th>항목</th><th>verdict_status</th><th>finding_status</th><th>problem_text</th><th>사전 사유(스냅샷)</th><th>충돌</th></tr>{''.join(v_html) or '<tr><td colspan=6>verdict 없음 → 포함</td></tr>'}</table>
</section>""")
        template.append({
            "section": f"{src}/{key}", "priority": why, "llm_bucket": rec,
            "verdict": {"human": "동의|비동의|보류", "cause": None, "memo": ""},
            "items": {f.content_type: {"llm": f.status, "human": "해당|비해당|판단불가", "agree": None, "cause": None, "memo": ""}
                      for f in jr.content_findings.findings if f.content_type.startswith("LC-") and f.status != "absent"},
            "rg": [{"ref": m.dictionary_ref, "match_key": m.match_key, "pattern": m.pattern, "matched_text": m.matched_text,
                    "string_match": "예", "boundary": "독립|내부", "context_valid": "예|아니오|불명", "valid": "", "memo": ""}
                   for m in jr.matches if m.dictionary_ref.startswith("RG-")],
            "notes": "",
        })

    nav = "".join(f"<li><a href='#{esc(s)}-{esc(k)}'>{esc(s)}/{esc(k)}</a> — {esc(' · '.join(w))}</li>" for (s, k), w in picked.items())
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>③-1 실측 3차 검수(우선순위)</title>
<style>
body{{font-family:system-ui,'Malgun Gothic',sans-serif;margin:16px;color:#222;background:#fff}} .card{{border:1px solid #ccc;border-radius:8px;padding:12px;margin:20px 0}}
h2{{margin:0 0 6px}} h2 small{{font-weight:400;color:#666;font-size:13px;margin-left:8px}} .rec{{color:#fff;padding:2px 8px;border-radius:4px;font-size:14px;margin-left:8px}}
.why{{color:#555;margin:4px 0 10px}} .imgs{{display:flex;gap:12px;align-items:flex-start}} figure{{margin:0;flex:1;min-width:0}} img{{width:100%;border:1px solid #ddd}}
figcaption{{font-size:12px;color:#666;margin-bottom:4px}} table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px}} th,td{{border:1px solid #ddd;padding:4px 6px;vertical-align:top}}
th{{background:#f4f4f4}} tr.hl{{background:#fff7e6}} mark{{background:#ffe066}} code{{font-size:12px}} h3{{font-size:14px;margin:12px 0 4px}} ul{{font-size:13px}}
.top{{background:#f4f4f4;padding:10px;border-radius:8px}} .top ul{{margin:4px 0;columns:2}}
</style></head><body>
<div class="top"><h1 style="margin:0 0 6px">③-1 · ③-1' 실측 1회 — 3차 검수(사용자, 우선순위 {len(picked)}섹션)</h1>
<p style="margin:4px 0">실행 <code>{esc(run)}</code> · 분류는 실험용 가정 · 판정표 틀 <code>user_priority.json</code>(같은 폴더). 원본 이미지를 먼저 보고 판단한 뒤 오른쪽 결과와 대조하세요. 규제 항목에는 LLM 판정이 없으며(규칙 매칭 = 포함 배지), 현지부적합만 LLM이 판정합니다.</p>
<ul>{nav}</ul></div>
{''.join(cards)}
</body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    (out / "user_priority.json").write_text(json.dumps({"reviewer": "user", "spec": "review-spec-v2", "sections": template}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"sections {len(picked)} → {out / 'index.html'}")
    for (s, k), w in picked.items():
        print(" ", s, k, "·".join(w))
    return 0


if __name__ == "__main__":
    sys.exit(main())
