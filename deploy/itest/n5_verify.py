"""N4→N5 검증 판정(읽기 전용) — DB 행과 S3 객체(EC2 역할, GET 만)로 단계별 판정을 낸다. 감시 종료와는 별개.

  python n5_verify.py <하류 run id>     판정 출력 + /ev/n5_verdict.json
  python n5_verify.py selftest          분석 run 22 의 채택된 section_image 로 S3 읽기·해시 대조 경로만 확인(하류 무관)

판정: 성공(모든 단계 done·채택 + LaMa 실제 추론 1섹션 이상 검증) / 부분 검증(N5 도달, 일부 단계 실패·생략) / 실패(N5 미도달)
생략된 경로는 '미검증'으로 따로 적는다. 값·키·URL 은 출력하지 않는다(일치 여부·개수만).
스타일: 계약 검증(4키 존재)과 미리보기 렌더 가능(그릴 블록의 font_color·est_font_px·align 이 모두 있음)을 따로 판정한다.
"""
import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.getcwd())
EV = os.environ.get("ITEST_EV", "/ev")
JOB_ID = 9
SECTIONS = {10, 11}
STYLE_KEYS = ("font_color", "bg_color", "est_font_px", "align")
RENDER_KEYS = ("font_color", "est_font_px", "align")     # 현행 렌더러가 쓰는 항목(bg_color 는 렌더 기본값 조건 아님)
LOGO_OK = {(True, None), (False, True), (False, False)}
STAGES = ("label", "logo", "style", "translate", "inpaint", "preview")


# ---------------------------------------------------------------- 순수 판정
def evaluate(d):
    """d: fetch() 결과(+ S3 측정). 반환: {'overall', 'stages': {stage: {...}}, 'unverified': [...], 'notes': [...]}"""
    st, unverified, notes = {}, [], []
    atts = [a for a in d["attempts"] if a["is_current"]]
    by = {s: [a for a in atts if a["stage"] == s] for s in STAGES}
    ho = {h["task_id"]: h for h in d["handoffs"]}
    blocks = d["blocks"]
    retries = [a["id"] for a in d["attempts"] if (a["attempt_no"] or 1) > 1 or a["retry_origin"]]

    def base(stage):
        xs = by[stage]
        r = {"attempts": len(xs), "done": sum(a["status"] == "done" for a in xs),
             "failed": [(a["unit_id"], a["error_code"]) for a in xs if a["status"] == "failed"],
             "adopted": sum(1 for a in xs if (ho.get(a["id"]) or {}).get("state") == "adopted"),
             "units": sorted({a["unit_id"] for a in xs})}
        r["ok"] = (r["attempts"] == len(SECTIONS) and r["done"] == r["attempts"] and r["adopted"] == r["attempts"]
                   and set(r["units"]) == SECTIONS)
        return r

    r = base("label")
    r["flags_set"] = sum(b["is_product_label"] is not None for b in blocks)
    r["ok"] = r["ok"] and r["flags_set"] == len(blocks)
    st["label"] = r

    r = base("logo")
    combos = [(b["is_product_label"], b["is_brand_logo"]) for b in blocks]
    r["bad_combos"] = sum(c not in LOGO_OK for c in combos)
    r["logo_true"] = sum(c == (False, True) for c in combos)
    r["ok"] = r["ok"] and r["bad_combos"] == 0
    if r["logo_true"] == 0:
        unverified.append("logo: 브랜드명 일치(로고 판정 True) 경로")
    st["logo"] = r

    drawn = [b for b in blocks if not b["is_excluded"] and (b["trans_1"] or "").strip()]
    nonex = [b for b in blocks if not b["is_excluded"]]
    r = base("style")
    r["contract_ok_blocks"] = sum(isinstance(b["style"], dict) and all(k in b["style"] for k in STYLE_KEYS) for b in nonex)
    r["render_missing"] = [b["id"] for b in drawn if not isinstance(b["style"], dict)
                           or any(b["style"].get(k) is None for k in RENDER_KEYS)]
    r["bg_color_null"] = sum(isinstance(b["style"], dict) and b["style"].get("bg_color") is None for b in nonex)
    r["contract_ok"] = r["ok"] and r["contract_ok_blocks"] == len(nonex)
    r["renderable_without_defaults"] = not r["render_missing"]
    r["ok"] = r["contract_ok"]
    st["style"] = r

    r = base("translate")
    targets = [b for b in blocks if not b["is_excluded"] and (b["source_ko"] or "").strip()]
    r["targets"] = len(targets)
    r["translated"] = sum(bool((b["trans_1"] or "").strip()) for b in targets)
    r["verify_failed"] = sum(len((ho.get(a["id"]) or {}).get("verify_failed") or []) for a in by["translate"])
    r["ok"] = r["ok"] and r["translated"] == r["targets"] and r["verify_failed"] == 0
    st["translate"] = r

    r = base("inpaint")
    secs = []
    for a in by["inpaint"]:
        h = ho.get(a["id"]) or {}
        s = d["sections"].get(a["unit_id"], {})
        arts = {x["kind"]: x for x in d["artifacts"] if x["handoff_id"] == h.get("id")}
        m = d["s3"].get(a["unit_id"], {})
        one = {"section": a["unit_id"], "ai_outcome": h.get("ai_outcome"), "status": h.get("status"),
               "final_px": h.get("final_px"), "model": bool(h.get("model")), "handoff": h.get("state"), "attempt": a["status"],
               "artifacts_adopted": sorted(k for k, x in arts.items() if x["state"] == "adopted"),
               "size_ok": all((x["width"], x["height"]) == (s.get("w"), s.get("h")) for x in arts.values()) if arts else False,
               "hash_ok": all(x["sha256"] == x["declared_sha256"] for x in arts.values() if x["declared_sha256"]) if arts else False,
               "s3_bg": m.get("background"), "s3_mask": m.get("delete_mask"), "s3_protect": m.get("protect_mask"),
               "mask_nonzero": m.get("mask_nonzero"),
               "section_done": s.get("inpaint_status") == "done", "no_badge": s.get("warning_badge") is None,
               "mask_key_match": s.get("mask_matches_delete_mask")}
        # 산출물 무결성: 실제로 읽은 산출물이 보고와 다르면 '검증 실패'(미검증 아님). 읽지 못했으면 '확인 불가'.
        reads = [one["s3_bg"], one["s3_mask"], one["s3_protect"]]
        bad = []
        if arts:
            if not one["size_ok"]:
                bad.append("크기 불일치")
            if not one["hash_ok"]:
                bad.append("신고 해시 불일치")
        bad += [f"{k} 해시 불일치" for k, val in zip(("background", "delete_mask", "protect_mask"), reads) if val == "mismatch"]
        if one["s3_mask"] == "match" and one["final_px"] is not None and one["mask_nonzero"] != one["final_px"]:
            bad.append(f"마스크 픽셀 {one['mask_nonzero']} ≠ final_px {one['final_px']}")
        if one["s3_mask"] == "match" and one["mask_key_match"] is False:
            bad.append("섹션 mask 키 ≠ delete_mask")
        unreadable = [k for k, val in zip(("background", "delete_mask", "protect_mask"), reads)
                      if val is None or str(val).startswith(("error", "no-key"))]
        one["integrity"] = ("failed: " + ", ".join(bad)) if bad else ("unreadable: " + ",".join(unreadable) if unreadable else "ok")
        if one["status"] == "inpainted":
            one["lama_verified"] = (one["integrity"] == "ok" and one["ai_outcome"] == "completed" and (one["final_px"] or 0) > 0
                                    and one["model"] and one["handoff"] == "adopted" and one["attempt"] == "done"
                                    and one["artifacts_adopted"] == ["background", "delete_mask", "protect_mask"]
                                    and one["section_done"] and one["no_badge"] and one["mask_key_match"] is True)
        else:
            one["lama_verified"] = False
        secs.append(one)
    r["sections"] = secs
    r["lama_verified_sections"] = [x["section"] for x in secs if x["lama_verified"]]
    r["skipped_empty_mask"] = [x["section"] for x in secs if x["status"] == "unchanged"]
    r["integrity_failed"] = [(x["section"], x["integrity"]) for x in secs if x["integrity"].startswith("failed")]
    r["unreadable"] = [(x["section"], x["integrity"]) for x in secs if x["integrity"].startswith("unreadable")]
    r["ok"] = r["ok"] and bool(r["lama_verified_sections"]) and not r["integrity_failed"] and not r["unreadable"] and all(
        x["lama_verified"] or (x["status"] == "unchanged" and x["handoff"] == "adopted") for x in secs)
    if r["integrity_failed"]:
        notes.append(f"inpaint 산출물 검증 실패: {r['integrity_failed']}")
    if r["unreadable"]:
        notes.append(f"inpaint 산출물 확인 불가(읽기 실패): {r['unreadable']}")
    if not r["lama_verified_sections"]:
        unverified.append("inpaint: LaMa 실제 추론(모든 섹션 생략 또는 실패)")
    st["inpaint"] = r

    r = base("preview")
    r["render_keys"] = sum(1 for sid in SECTIONS if d["sections"].get(sid, {}).get("render_image_key_set"))
    r["render_artifacts_adopted"] = sum(1 for x in d["artifacts"] if x["kind"] == "render_image" and x["state"] == "adopted")
    r["style_defaults_failure"] = [u for u, c in r["failed"] if c == "STYLE_DEFAULTS_UNAPPROVED"]
    r["ok"] = r["ok"] and r["render_keys"] == len(SECTIONS) and r["render_artifacts_adopted"] == len(SECTIONS)
    if r["style_defaults_failure"]:
        notes.append("preview: 승인된 스타일 기본값 미설정으로 실패(STYLE_DEFAULTS_UNAPPROVED) — 설정 미비, 앱·설정 변경 안 함")
        unverified.append("preview: 렌더 결과(스타일 기본값 필요 블록 있음)")
    st["preview"] = r

    if retries:
        notes.append(f"재시도 발생: {retries}")
    reached = d["job"]["current_step"] == "N5"
    all_ok = all(v["ok"] for v in st.values()) and not retries
    integrity_failed = st["inpaint"]["integrity_failed"]
    if not reached:
        overall = "실패(N5 미도달)"
    elif integrity_failed:
        # 다른 섹션이 성공해도 전체 성공이 아니다
        ok_stages = [k for k, val in st.items() if val["ok"]]
        overall = "실패(산출물 검증 실패)"
        notes.append(f"일부 단계 성공({ok_stages}), 섹션 {[s for s, _ in integrity_failed]} 산출물 검증 실패")
    else:
        overall = "성공" if all_ok else "부분 검증"
    return {"overall": overall, "job": d["job"], "stages": st, "unverified": unverified, "notes": notes, "retries": retries}


# ---------------------------------------------------------------- 조회(운영)
def fetch(run_id):
    from sqlalchemy import create_engine, text
    from app.storage import get_store
    e = create_engine(os.environ["DATABASE_URL"])
    store = get_store()
    with e.connect() as c:
        q = lambda s, **p: [dict(r) for r in c.execute(text(s), p).mappings().all()]  # noqa: E731
        job = q("SELECT status, current_step, user_facing_status FROM job WHERE id = :j", j=JOB_ID)[0]
        atts = q("SELECT id, stage, unit_id, status, attempt_no, retry_origin, error_code, is_current FROM job_async_task "
                 "WHERE parent_task_id = :r ORDER BY id", r=run_id)
        hs = q("SELECT h.id, h.task_id, h.state, h.outcome, h.payload->'ai_report'->>'outcome' ai_outcome, h.payload->>'status' status, "
               "(h.payload->'counts'->>'final_px')::bigint final_px, h.payload->>'model' model, "
               "h.verify_result->'failed' verify_failed FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
               "WHERE a.parent_task_id = :r ORDER BY h.id", r=run_id)
        for h in hs:
            if isinstance(h["verify_failed"], str):
                h["verify_failed"] = json.loads(h["verify_failed"])
        arts = q("SELECT x.id, x.handoff_id, x.kind, x.part_key, x.verified_key, x.source_ref, x.sha256, x.declared_sha256, "
                 "x.width, x.height, x.state FROM task_artifact x JOIN task_handoff h ON h.id = x.handoff_id "
                 "JOIN job_async_task a ON a.id = h.task_id WHERE a.parent_task_id = :r ORDER BY x.id", r=run_id)
        secs = {s["id"]: s for s in q("SELECT id, (bbox->>'w')::int w, height h, inpaint_status, warning_badge, mask_image_url, "
                                       "render_image_key IS NOT NULL render_image_key_set FROM section WHERE job_id = :j "
                                       "AND id = ANY(:ids)", j=JOB_ID, ids=list(SECTIONS))}
        blocks = q("SELECT b.id, b.section_id, b.is_product_label, b.is_brand_logo, b.is_excluded, b.style, b.trans_1, b.source_ko "
                   "FROM text_block b WHERE b.section_id = ANY(:ids) ORDER BY b.section_id, b.block_order", ids=list(SECTIONS))
    att_by_id = {a["id"]: a for a in atts}
    s3 = {}
    for h in hs:
        a = att_by_id.get(h["task_id"])
        if not a or a["stage"] != "inpaint":
            continue
        m = {}
        for x in [x for x in arts if x["handoff_id"] == h["id"]]:
            key = x["verified_key"] or ((x["source_ref"] or {}).get("key") if isinstance(x["source_ref"], dict) else None)
            if not key:
                m[x["kind"]] = "no-key"
                continue
            try:
                body = store.get(key)
                m[x["kind"]] = "match" if hashlib.sha256(body).hexdigest() == x["sha256"] else "mismatch"
                if x["kind"] == "delete_mask":
                    from PIL import Image
                    im = Image.open(io.BytesIO(body)).convert("L")
                    hist = im.histogram()
                    m["mask_nonzero"] = im.width * im.height - hist[0]
                    secs.get(a["unit_id"], {})["mask_matches_delete_mask"] = (secs.get(a["unit_id"], {}).get("mask_image_url") == x["verified_key"])
            except Exception as e:  # noqa: BLE001
                m[x["kind"]] = f"error:{type(e).__name__}"
        s3[a["unit_id"]] = m
    for s in secs.values():
        s.pop("mask_image_url", None)
    return {"job": job, "attempts": atts, "handoffs": hs, "artifacts": arts, "sections": secs, "blocks": blocks, "s3": s3}


def selftest():
    """하류와 무관: 분석 run 22 의 채택된 section_image 를 S3 에서 읽어 해시 대조(읽기 전용 경로 확인)."""
    from sqlalchemy import create_engine, text
    from app.storage import get_store
    e = create_engine(os.environ["DATABASE_URL"])
    store = get_store()
    with e.connect() as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT x.kind, x.verified_key, x.sha256, x.width, x.height FROM task_artifact x JOIN task_handoff h ON h.id = x.handoff_id "
            "JOIN job_async_task a ON a.id = h.task_id WHERE a.parent_task_id = 22 AND x.kind = 'section_image' AND x.state = 'adopted'"
        )).mappings().all()]
    from PIL import Image
    for r in rows:
        body = store.get(r["verified_key"])
        im = Image.open(io.BytesIO(body))
        print(f"selftest section_image: 해시 {'일치' if hashlib.sha256(body).hexdigest() == r['sha256'] else '불일치'}, "
              f"크기 {im.size == (r['width'], r['height'])}, 0 아닌 픽셀 계산 {im.width * im.height - im.convert('L').histogram()[0] >= 0}")
    print(f"selftest 대상 {len(rows)}건")
    return 0 if rows else 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "selftest":
        sys.exit(selftest())
    run_id = int(arg)
    data = fetch(run_id)
    if not data["attempts"]:
        print(json.dumps({"overall": "하류 실행 없음", "run_id": run_id, "job": data["job"]}, ensure_ascii=False, default=str))
        sys.exit(0)
    v = evaluate(data)
    with open(f"{EV}/n5_verdict.json", "w", encoding="utf-8") as f:
        json.dump({"run_id": run_id, **v}, f, ensure_ascii=False, default=str, indent=1)
    print(json.dumps({"run_id": run_id, **v}, ensure_ascii=False, default=str, indent=1))
