"""N6 판정(읽기 전용) — 처리 성공과 시각 품질 근거를 분리한다. 값·키·URL 은 출력하지 않는다(일치 여부·개수만).

  python n6_verify.py <최종 렌더 run id> [N5 보존 JSON 경로] [이미지 저장 폴더]

처리(processing): 하류 run 37 done · 최종 렌더 대표 실행 done · 현재 시도 done(재시도 없음) · 인계 adopted ·
  render_image(sec_10·sec_11·src_4) 채택·S3 해시·크기 · deliverable 1건(render_status done, 파일 크기·해시 = 합본) ·
  deliverable_section 2건(섹션 순서·stack_offset) · 합본 크기 = 섹션 폭 × 높이 합
  결과: 처리 성공 / 처리 실패(렌더 실패 보존) / 처리 검증 실패(산출물 무결성) / 처리 미확정
품질(quality): 블록별 최종 overflow(인계 기록·DB) 와 N5 보존값 비교, 최종 이미지 저장(육안 확인용). 품질 판정은 이 근거와 이미지로
  따로 내린다 — 처리 성공은 서비스 완료나 품질 승인이 아니다.
"""
import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.getcwd())
JOB_ID = 9
DOWNSTREAM_RUN = 37
SECTIONS = [10, 11]          # (source_image_id, section_order) 순
SOURCE_IMAGE = 4


# ---------------------------------------------------------------- 순수 판정
def evaluate(d, n5=None):
    chk, integrity, notes = {}, [], []
    runs = {r["id"]: r for r in d["runs"]}
    cur = [a for a in d["attempts"] if a["is_current"]]
    chk["작업 N6"] = d["job"]["current_step"] == "N6"
    chk["하류 run 37 done"] = (runs.get(DOWNSTREAM_RUN) or {}).get("status") == "done"
    chk["최종 렌더 대표 실행 done"] = (runs.get(d["run_id"]) or {}).get("status") == "done"
    chk["현재 시도 1건 done"] = len(cur) == 1 and cur[0]["status"] == "done"
    retries = [a["id"] for a in d["attempts"] if (a["attempt_no"] or 1) > 1 or a["retry_origin"]]
    chk["재시도 없음"] = not retries
    if any(a["retry_origin"] == "recovery" for a in d["attempts"]):
        notes.append("복구 프로세스 lease 만료 재시도 발생")
    chk["인계 adopted"] = bool(cur) and any(h["task_id"] == cur[0]["id"] and h["state"] == "adopted" for h in d["handoffs"])
    arts = {x["part_key"]: x for x in d["artifacts"] if x["kind"] == "render_image" and x["state"] == "adopted"}
    want_parts = [f"sec_{s}" for s in SECTIONS] + [f"src_{SOURCE_IMAGE}"]
    chk["render_image 3건 채택"] = all(p in arts for p in want_parts)
    for p in want_parts:
        x, m = arts.get(p), d["s3"].get(p, {})
        if not x:
            continue
        if x["declared_sha256"] and x["sha256"] != x["declared_sha256"]:
            integrity.append(f"{p} 신고 해시 불일치")
        if m.get("hash") == "mismatch":
            integrity.append(f"{p} S3 해시 불일치")
        if m.get("size") and tuple(m["size"]) != (x["width"], x["height"]):
            integrity.append(f"{p} 이미지 크기 {m['size']} ≠ 기록 {(x['width'], x['height'])}")
    secs = d["sections"]
    want_stack = (secs[SECTIONS[0]]["w"], sum(secs[s]["h"] for s in SECTIONS)) if all(s in secs for s in SECTIONS) else None
    src = arts.get(f"src_{SOURCE_IMAGE}")
    chk["합본 크기 = 섹션 폭 × 높이 합"] = bool(src) and want_stack == (src["width"], src["height"])
    for s in SECTIONS:
        x = arts.get(f"sec_{s}")
        if x and (x["width"], x["height"]) != (secs[s]["w"], secs[s]["h"]):
            integrity.append(f"sec_{s} 크기 ≠ 섹션")
    dl = d["deliverables"]
    chk["deliverable 1건 done"] = len(dl) == 1 and dl[0]["render_status"] == "done" and dl[0]["source_image_id"] == SOURCE_IMAGE
    if dl and src:
        m = d["s3"].get("deliverable", {})
        if dl[0]["image_key_matches_src"] is False:
            integrity.append("deliverable 이미지 키 ≠ 합본 산출물")
        if m.get("bytes") is not None and m["bytes"] != dl[0]["file_size"]:
            integrity.append(f"deliverable 파일 크기 {dl[0]['file_size']} ≠ 객체 {m['bytes']}")
        if m.get("hash") == "mismatch":
            integrity.append("deliverable 객체 해시 ≠ 합본 산출물")
    ds = sorted(d["deliverable_sections"], key=lambda x: x["order_no"])
    offs, acc = [], 0
    for s in SECTIONS:
        offs.append(acc)
        acc += secs.get(s, {}).get("h", 0)
    chk["deliverable_section 2건 순서·위치"] = [(x["section_id"], x["stack_offset"]) for x in ds] == list(zip(SECTIONS, offs))
    unreadable = [p for p, m in d["s3"].items() if str(m.get("hash", "")).startswith(("error", "no-key"))]
    if not chk["현재 시도 1건 done"] and cur and cur[0]["status"] == "failed":
        processing = "처리 실패(렌더 실패 보존)"
    elif integrity:
        processing = "처리 검증 실패(산출물 무결성)"
    elif all(chk.values()) and not unreadable:
        processing = "처리 성공"
    else:
        processing = "처리 미확정"
    if unreadable:
        notes.append(f"S3 읽기 실패(확인 불가): {unreadable}")
    # 품질 근거(판정은 별도)
    q = []
    n5o = {b["id"]: b["overflow"] for b in (n5 or {}).get("blocks", [])}
    for b in d["blocks"]:
        q.append({"block": b["id"], "section": b["section_id"], "order": b["block_order"], "role": b["role"],
                  "final_overflow_db": b["overflow"], "final_overflow_render": d["render_overflow"].get(str(b["id"])),
                  "n5_overflow": n5o.get(b["id"])})
    quality = {"blocks": q, "overflow_count": sum(1 for x in q if x["final_overflow_render"]),
               "same_as_n5": all(x["final_overflow_render"] == x["n5_overflow"] for x in q) if n5o else None,
               "note": "시각 품질은 최종 이미지와 이 수치로 따로 판정한다(처리 성공 ≠ 품질 승인)"}
    return {"processing": processing, "checks": chk, "integrity": integrity, "retries": retries, "notes": notes, "quality": quality}


# ---------------------------------------------------------------- 조회(운영, 읽기 전용)
def fetch(run_id, img_dir=None):
    from sqlalchemy import create_engine, text
    from PIL import Image
    from app.storage import get_store
    e = create_engine(os.environ["DATABASE_URL"])
    store = get_store()
    with e.connect() as c:
        c = c.execution_options(isolation_level="REPEATABLE READ")
        with c.begin():
            q = lambda s, **p: [dict(r) for r in c.execute(text(s), p).mappings().all()]  # noqa: E731
            d = {"run_id": run_id,
                 "job": q("SELECT status, current_step FROM job WHERE id = :j", j=JOB_ID)[0],
                 "runs": q("SELECT id, status, run_kind FROM job_async_task WHERE id IN (:a, :b)", a=DOWNSTREAM_RUN, b=run_id),
                 "attempts": q("SELECT id, stage, status, attempt_no, retry_origin, error_code, is_current FROM job_async_task "
                               "WHERE parent_task_id = :r ORDER BY id", r=run_id),
                 "handoffs": q("SELECT h.id, h.task_id, h.state, h.payload->'renders' renders FROM task_handoff h JOIN job_async_task a "
                               "ON a.id = h.task_id WHERE a.parent_task_id = :r ORDER BY h.id", r=run_id),
                 "artifacts": q("SELECT x.kind, x.part_key, x.verified_key, x.sha256, x.declared_sha256, x.width, x.height, x.state "
                                "FROM task_artifact x JOIN task_handoff h ON h.id = x.handoff_id JOIN job_async_task a ON a.id = h.task_id "
                                "WHERE a.parent_task_id = :r ORDER BY x.id", r=run_id),
                 "sections": {s["id"]: s for s in q("SELECT id, (bbox->>'w')::int w, height h FROM section WHERE id = ANY(:i)", i=SECTIONS)},
                 "deliverables": q("SELECT id, source_image_id, image_url, format, file_size, render_status FROM deliverable WHERE job_id = :j",
                                   j=JOB_ID),
                 "deliverable_sections": q("SELECT ds.section_id, ds.stack_offset, ds.order_no FROM deliverable_section ds JOIN deliverable d "
                                           "ON d.id = ds.deliverable_id WHERE d.job_id = :j", j=JOB_ID),
                 "blocks": q("SELECT id, section_id, block_order, role, overflow FROM text_block WHERE section_id = ANY(:i) "
                             "ORDER BY section_id, block_order", i=SECTIONS)}
    ro = {}
    for h in d["handoffs"]:
        for sec in (h.pop("renders") or {}).values():
            ro.update({k: v for k, v in (sec.get("overflow") or {}).items()})
    d["render_overflow"] = ro
    d["s3"] = {}
    by_part = {x["part_key"]: x for x in d["artifacts"] if x["kind"] == "render_image" and x["state"] == "adopted"}
    for p, x in by_part.items():
        try:
            b = store.get(x["verified_key"])
            im = Image.open(io.BytesIO(b))
            d["s3"][p] = {"hash": "match" if hashlib.sha256(b).hexdigest() == x["sha256"] else "mismatch", "size": list(im.size)}
            if img_dir:
                open(os.path.join(img_dir, f"n6_{p}.png"), "wb").write(b)
        except Exception as ex:  # noqa: BLE001
            d["s3"][p] = {"hash": f"error:{type(ex).__name__}"}
    src = by_part.get(f"src_{SOURCE_IMAGE}")
    for dl in d["deliverables"]:
        dl["image_key_matches_src"] = (dl.pop("image_url") == src["verified_key"]) if src else None
        try:
            b = store.get(src["verified_key"]) if src else b""
            d["s3"]["deliverable"] = {"bytes": len(b), "hash": "match" if src and hashlib.sha256(b).hexdigest() == src["sha256"] else "mismatch"}
        except Exception as ex:  # noqa: BLE001
            d["s3"]["deliverable"] = {"hash": f"error:{type(ex).__name__}"}
    for x in d["artifacts"]:
        x.pop("verified_key", None)
    return d


if __name__ == "__main__":
    run_id = int(sys.argv[1])
    n5 = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    img = sys.argv[3] if len(sys.argv) > 3 else None
    data = fetch(run_id, img)
    v = evaluate(data, n5)
    print(json.dumps({"run_id": run_id, **v}, ensure_ascii=False, default=str, indent=1))
