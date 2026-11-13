"""보존된 Baseline 형식의 OCR 출력 색인 생성. 추론·CER 계산 없음."""
import argparse
import json
import os
from pathlib import Path
import tomllib
from .core import read, digest, write_new
from .ocr import verify, BOUNDARY, validate_inputs


def candidates(stages):
    current = {}
    found = []
    for line,event in enumerate(stages,1):
        case = event["source_id"]
        if event["stage"] == "section_split":
            current[case] = (line,event.get("result"))
        if event["stage"] != "ocr" or not isinstance(event.get("result"),dict):
            continue
        split_line,split = current.get(case,(None,None))
        if not split: continue
        matches = [s for s in split["sections"] if s["section_key"]==event["result"].get("section_key")]
        if len(matches)==1:
            found.append({"line":line,"split_line":split_line,"case":case,"source_image_id":split["source_image_id"],
                          "section":matches[0],"at":event["at"],"seconds":event.get("seconds")})
    return found


def prepare(truth, run, legacy, reproduction, boundary, out):
    truth,run,legacy,reproduction,boundary,out=map(lambda p:Path(p).resolve(),(truth,run,legacy,reproduction,boundary,out))
    manifest,units=verify(truth)
    rm=read(run/"manifest.json"); runtime=read(run/"runtime.json"); objects=read(run/"object-index.json")
    inventory=read(run/"github-file-index.json")["files"]
    checked={}
    def check(path):
        key=path.relative_to(run).as_posix();actual=digest(path)
        if key not in inventory or actual!=inventory[key]["sha256"]:
            raise ValueError(f"보존 파일 해시 불일치: {key}")
        checked[key]=actual
        return actual
    for name in ("manifest.json","runtime.json","stages.jsonl","object-index.json"):
        check(run/name)
    config=reproduction/"product-settings/pipeline/config/default.toml"
    config_hash=check(config)
    wrapper=reproduction/"baseline-tools/be.py"
    if check(wrapper)!=runtime["tool_sha256"]["be.py"]:
        raise ValueError("실행 시 계측 코드와 보존 코드 불일치")
    if config_hash!=runtime["settings"]["pipeline\\config\\default.toml"]:
        raise ValueError("실행 설정 해시 불일치")
    proof=read(boundary)
    if proof["product_sha"]!=rm["product_commit"] or proof["boundary"]!=BOUNDARY or proof["wrapper_sha256"]!=digest(wrapper):
        raise ValueError("제품·반환 경계 검토 근거 불일치")
    cfg=tomllib.loads(config.read_text(encoding="utf-8"))["ocr"]
    stages=[json.loads(l) for l in (run/"stages.jsonl").read_text(encoding="utf-8").splitlines()]
    events=candidates(stages)
    db={}
    for case in {e["case"] for e in events}:
        check(run/f"evidence/{case}.json");db[case]=read(run/f"evidence/{case}.json")
    def rel(path):return Path(os.path.relpath(path,out)).as_posix()
    records=[];links=[]
    for u in units:
        split_path=legacy/u["sample"]/"split.json"
        split=read(split_path)
        original=next(s for s in split["sections"] if s["section_key"]==u["section_key"])
        shape=(original["top_offset"],original["width"],original["height"])
        local_image=legacy/u["sample"]/"sections"/Path(u["image"]["path"]).name
        if digest(local_image)!=u["image"]["sha256"]:
            raise ValueError("동결 입력과 로컬 고정 이미지 해시 불일치")
        matches=[]
        for e in events:
            sources=[x for x in db[e["case"]]["source_image"] if x["id"]==e["source_image_id"]]
            if len(sources)!=1 or Path(sources[0]["file_url"]).stem!=u["sample"]:continue
            sec=e["section"]
            if (sec["top_offset"],sec["width"],sec["height"])!=shape:continue
            saved=[s for s in db[e["case"]]["section"] if s["source_image_id"]==e["source_image_id"] and
                   (s["top_offset"],s["bbox"]["w"],s["height"])==shape]
            image_keys=[s["image_key"] for s in saved if s.get("image_key") in objects and objects[s["image_key"]]["sha256"]==u["image"]["sha256"]]
            for key in image_keys:check(run/"objects"/objects[key]["sha256"])
            source_key=sources[0]["file_url"]
            source_hash=objects.get(source_key,{}).get("sha256")
            if source_hash:check(run/"objects"/source_hash)
            matches.append({**e,"input_hash_verified":bool(image_keys),"image_keys":image_keys,
                            "original_image_sha256":source_hash,"original_image_key":source_key})
        rec={"id":u["id"],"product_version":rm["product_version"],"product_sha":rm["product_commit"],
             "model":cfg["det_model"]+" + "+cfg["rec_model"],"config_sha256":config_hash,
             "environment_sha256":digest(run/"runtime.json"),"truth_manifest_sha256":digest(truth/"manifest.json")}
        proven=[m for m in matches if m["input_hash_verified"]]
        # 재시도 선택은 임의로 하지 않는다. 동일 후보가 여러 개면 별도 결정이 필요하다.
        if len(matches)==1 and len(proven)==1:
            m=proven[0]
            rec.update(status="available",input_sha256=u["image"]["sha256"],boundary=BOUNDARY,
                output_section_key=m["section"]["section_key"],attempt_id=f"{rm['evaluation_run_id']}:stages:{m['line']}:{m['at']}",
                evidence={"path":"linkage.json","sha256":None,"locator":u["id"]},
                output={"path":rel(run/"stages.jsonl"),"sha256":digest(run/"stages.jsonl"),"line":m["line"],"pointer":"/result"})
            status="재사용 가능"
        elif matches:
            rec.update(status="undetermined",reason="후보 출력 존재; 실행 입력 바이트 동일성/시도 선택 미확인")
            status="확인 필요"
        else:
            rec.update(status="no_output",reason="원본의 해당 고정 섹션 OCR 반환 기록 없음; 상품 분석 실패 영향")
            status="추가 실행 필요"
        links.append({"id":u["id"],"state":status,"truth_image_sha256":u["image"]["sha256"],
            "truth_source_sha256":u["source"]["sha256"],"legacy_split_sha256":digest(split_path),
            "legacy_split_path":split_path.as_posix(),"local_image_sha256":digest(local_image),"candidates":matches})
        records.append(rec)
    if out.exists():raise ValueError("기존 준비 결과 덮어쓰기 금지")
    out.mkdir(parents=True)
    write_new(out/"linkage.json",{"cer_calculated":False,"run_id":rm["evaluation_run_id"],
        "product_sha":rm["product_commit"],"boundary_review":proof,"boundary_review_sha256":digest(boundary),
        "verified_preserved_files":checked,"truth_manifest_sha256":digest(truth/"manifest.json"),"sections":links})
    for r in records:
        if r["status"]=="available":r["evidence"]["sha256"]=digest(out/"linkage.json")
    write_new(out/"inputs.json",{"sections":records})
    result=validate_inputs(truth,out/"inputs.json")
    write_new(out/"validation.json",result)
    return links


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ("truth","run","legacy","reproduction","boundary","out"):p.add_argument("--"+arg,required=True)
    a=p.parse_args()
    for row in prepare(a.truth,a.run,a.legacy,a.reproduction,a.boundary,a.out):print(row["id"],row["state"])


if __name__=="__main__":main()
