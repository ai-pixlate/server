"""기존 증거만 읽는 진단. 공식 결과·정답·제품을 수정하지 않는다."""
from pathlib import Path
import json,hashlib,unicodedata,collections,sys
W=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(W))
from tools.baseline.ocr import verify,text_sequence,pointer_value
P=Path(__file__).resolve().parent
base=W/"docs/ai-baseline/evaluations/ocr-v4-v2-v1-0-1-20261109"
truth=W/"docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109"
read=lambda p:json.loads(Path(p).read_text("utf8"))
digest=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
protected={str(p):digest(p) for folder in (truth,base,W/"docs/ai-baseline/references/ocr-text/ocr-text-v1-20261104",W/"docs/ai-baseline/evaluations/ocr-v4-v1-0-1-20261105") for p in folder.rglob("*") if p.is_file()}
manifest,units=verify(truth)
report=read(base/"results-112.json"); units={u["id"]:u for u in units}
whitespace=set(range(9,14))|{32,133,160,5760,8232,8233,8239,8287,12288}|set(range(8192,8203))
def norm(parts):return "".join(c for s in parts for c in unicodedata.normalize("NFC",s) if ord(c) not in whitespace)
def distance(a,b):
 if len(a)>len(b):a,b=b,a
 previous=list(range(len(a)+1))
 for j,y in enumerate(b,1):
  current=[j]
  for i,x in enumerate(a,1):
   current.append(min(previous[i]+1,current[-1]+1,previous[i-1]+(x!=y)))
  previous=current
 return previous[-1]
rows=[];pairs={}
for r in report["sections"]:
 u=units[r["id"]];o=r["provenance"]["output"]
 file=Path(o["path"])
 assert digest(file)==o["sha256"]
 raw=file.read_text("utf8")
 data=json.loads(raw.splitlines()[o["line"]-1]) if o.get("line") else json.loads(raw)
 data=pointer_value(data,o["pointer"])
 g=[l["text"] for l in u["lines"]];q=[l["text"] for l in data["regions"]]
 R,Q=norm(g),norm(q)
 assert R==text_sequence(g) and Q==text_sequence(q)
 assert len(R)==r["N"] and len(Q)==r["output_characters"]
 err=distance(R,Q)
 if r["status"]=="measured":assert err==r["S"]+r["D"]+r["I"]
 assert len(Q)-len(R)==r["I"]-r["D"] if r["status"]=="measured" else True
 cg,cq=collections.Counter(R),collections.Counter(Q)
 missing=sum((cg-cq).values());extra=sum((cq-cg).values())
 nr,nq=[norm([x]) for x in g],[norm([x]) for x in q]
 exact=sum((collections.Counter(nr)&collections.Counter(nq)).values())
 row={k:r[k] for k in ("id","status","S","D","I","N","output_characters","cer_percent")}
 row.update(independent_distance=err,length_gap=len(Q)-len(R),character_bag_lower_bound=max(missing,extra),
  gt_lines=len(g),output_regions=len(q),exact_line_matches=exact,
  repeated_gt_nonempty={x:n for x,n in collections.Counter(nr).items() if x and n>1},
  repeated_output_nonempty={x:n for x,n in collections.Counter(nq).items() if x and n>1})
 rows.append(row);pairs[r["id"]]=(g,q)
old=read(W/"docs/ai-baseline/evaluations/ocr-v4-v1-0-1-20261105/combined-12.json")
oldchecks=[]
for r in old["sections"]:
 current=next(x for x in report["sections"] if x["id"]==r["id"])
 assert all(current[k]==r[k] for k in ("S","D","I","N","status"))
 assert current["provenance"]["output"]==r["provenance"]["output"] or current["provenance"]["output"]["sha256"]==r["provenance"]["output"]["sha256"]
 oldchecks.append(r["id"])
# 원본 표에서 확인한 열 순서로만 OCR 영역을 재배열한다.
sid="GS-02_014__sec_1_04"
perm=[0,1,2,3,7,8,4,5,6,9,10,11,16,18,19,12,14,13,15,17,20,21,22,23,27,28,29,24,26,25,30,31,32,33,34]
g,q=pairs[sid];assert sorted(perm)==list(range(len(q)))
reordered=distance(norm(g),norm([q[i] for i in perm]))
# 정확히 인식된 초급자 6줄에서 순서만 다른 사례를 확인한다.
gt_part=g[3:9];ocr_part=[q[i] for i in [3,4,5,6,7,8]]
assert collections.Counter(norm([x]) for x in gt_part)==collections.Counter(norm([x]) for x in ocr_part)
order_example={"id":sid,"ocr_region_permutation_zero_based":perm,"official_errors":103,"diagnostic_reordered_errors":reordered,"N":len(norm(g)),
 "exact_content_example":{"gt_lines":gt_part,"ocr_lines":ocr_part,"N":len(norm(gt_part)),"original_order_errors":distance(norm(gt_part),norm(ocr_part)),"reordered_errors":0}}
groups={}
for group in ("GS-01","GS-02","GS-03","all"):
 rr=[r for r in rows if r["status"]=="measured" and (group=="all" or r["id"].startswith(group))]
 groups[group]={k:sum(r[k] for r in rr) for k in ("S","D","I","N","output_characters","character_bag_lower_bound")}
 groups[group]["error"]=sum(groups[group][k] for k in ("S","D","I"))
for path,h in protected.items():assert digest(path)==h
result={"groups":groups,"sections":rows,"order_example":order_example,"old_12_consistent":oldchecks,
 "protected_files_verified":len(protected),"input_hashes":{str(truth/"manifest.json"):digest(truth/"manifest.json"),str(base/"results-112.json"):digest(base/"results-112.json")},
 "top5_text_pairs":{r["id"]:dict(zip(("gt","ocr"),pairs[r["id"]])) for r in sorted([r for r in rows if r["id"].startswith("GS-02")],key=lambda r:r["S"]+r["D"]+r["I"],reverse=True)[:5]}}
with (P/"diagnostics.json").open("x",encoding="utf8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
print(json.dumps({"groups":groups,"order_example":order_example,"old12":len(oldchecks),"protected":len(protected)},ensure_ascii=True))
