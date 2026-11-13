"""동결 v6 최초 측정 재현. 기존 출력만 읽으며 OCR/API 호출 없음."""
import argparse, json, hashlib, sys, time, platform, unicodedata
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
from tools.baseline import ocr_v6

def read(p):return json.loads(Path(p).read_text('utf8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def unique(rows,key):
 d={r[key]:r for r in rows};assert len(d)==len(rows),'duplicate IDs';return d

def independent_counts(a,b):
 # v4 동률 규칙의 독립 전체 행렬 구현. v5 선택 경로의 편집 수를 검산한다.
 table=[[0]*(len(b)+1) for _ in range(len(a)+1)]
 for i in range(len(a)+1):table[i][0]=i
 for j in range(len(b)+1):table[0][j]=j
 for i,x in enumerate(a,1):
  for j,y in enumerate(b,1):table[i][j]=min(table[i-1][j-1]+(x!=y),table[i-1][j]+1,table[i][j-1]+1)
 i,j=len(a),len(b);s=d=ins=0
 while i or j:
  if i and j and table[i][j]==table[i-1][j-1]+(a[i-1]!=b[j-1]):s+=a[i-1]!=b[j-1];i-=1;j-=1
  elif i and table[i][j]==table[i-1][j]+1:d+=1;i-=1
  else:ins+=1;j-=1
 return dict(S=s,D=d,I=ins,N=len(a))

def norm(s):
 spaces={*range(9,14),32,133,160,5760,*range(8192,8203),8232,8233,8239,8287,12288}
 return ''.join(c for c in unicodedata.normalize('NFC',s) if ord(c) not in spaces)

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--output',required=True,type=Path)
 args=parser.parse_args();outdir=args.output
 assert not outdir.exists(),'기존 결과 덮어쓰기 금지'
 b=ROOT/'docs/ai-baseline';frozen=b/'references/ocr-text/ocr-text-v3-v6-20261110'
 assert sha(frozen/'manifest.json')=='dd944013cb2e1d123c9d2b15b13e1086c3ef7f941c9781a9b5f5c72698feeb92'
 manifest=read(frozen/'manifest.json');plan=read(frozen/'evaluation-plan.json')
 protected=read(frozen/'source-evidence-hashes.json')
 protected.update({str(frozen/n):h for n,h in manifest['files'].items()})
 protected[str(frozen/'manifest.json')]=sha(frozen/'manifest.json')
 for p,h in protected.items():assert sha(p)==h,p
 for p,h in plan['implementation_hashes'].items():assert sha(ROOT/p)==h,p
 units=unique([json.loads(x) for x in (frozen/'text_units.jsonl').read_text('utf8').splitlines()],'id')
 scopes=unique(read(frozen/'fixed-regions.json'),'id');links=unique(read(frozen/'gt-region-links.json'),'section_id')
 index=b/'evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json';entries=unique(read(index)['sections'],'id')
 protected[str(index)]=sha(index)
 expected=plan['included']+plan['not_applicable']+plan['undetermined']
 assert len(set(expected))==112 and all(set(x)==set(expected) for x in (units,scopes,links,entries))
 prepared=[];input_rows=[]
 for sid in expected:
  s=scopes[sid];e=entries[sid];u=units[sid];ref=e['output']
  assert e['product_sha']=='bfa3b700144467f4adf7d6802b7b2e93791fe6de'
  assert e['product_version']=='1.0.1' and e['status']=='available'
  assert e['boundary']=='pipeline.stages.ocr.run:return'
  assert sha(s['image_path'])==s['image_sha256']==u['image']['sha256']==e['input_sha256']
  for evidence in (ref,e['evidence']):assert sha(evidence['path'])==evidence['sha256']
  raw=Path(ref['path']).read_text('utf8');data=json.loads(raw.splitlines()[ref['line']-1] if ref.get('line') else raw)
  for key in ref['pointer'].split('/')[1:]:data=data[int(key)] if isinstance(data,list) else data[key]
  output=[]
  for n,r in enumerate(data['regions']):
   q=r['bbox'];output.append(dict(id=f"{n}:{r['region_key']}",text=r['text'],rect=[q['x'],q['y'],q['x']+q['w'],q['y']+q['h']],mixed=sid=='GS-01_002__sec_1_23' and n==0))
  gt=[dict(id=x['line_id'],text=x['text'],active=x.get('active',True)) for x in u['lines']]
  meta=dict(group=u['sample'].split('_')[0],input_version=plan['input_version'],truth_version=plan['truth_version'],product_version=e['product_version'],product_sha=e['product_sha'],image_sha256=s['image_sha256'],truth_sha256=plan['truth_sha256'],output_sha256=ref['sha256'],policy_sha256=sha(frozen/'fixed-regions.json'),spec_sha256=sha(frozen/'snapshots/ocr-v6-scope-draft.md'),origin=e['origin'],output_locator=ref,output_boundary=e['boundary'])
  kw=dict(metadata=meta,gt_complete=True,scope_approved=True,completeness_evidence=sha(frozen/'approval.json'))
  pre=ocr_v6.prepare_section(s,gt,output,links[sid],**kw)
  wanted='ready' if sid in plan['included'] else 'not_applicable' if sid in plan['not_applicable'] else 'undetermined'
  assert pre['status']==wanted,(sid,pre['reasons'])
  prepared.append((sid,s,gt,output,links[sid],kw,pre))
  input_rows.append(dict(**e,truth_sha256=plan['truth_sha256'],scope_sha256=meta['policy_sha256'],frozen_manifest_sha256=sha(frozen/'manifest.json')))
 outdir.mkdir(parents=True)
 def write(n,v):(outdir/n).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
 write('inputs.json',dict(sections=input_rows,source_index_sha256=sha(index)))
 rows=[];start=time.perf_counter();started=datetime.now(timezone.utc).isoformat()
 with (outdir/'execution.jsonl').open('x',encoding='utf8') as log:
  for sid,s,g,o,l,kw,pre in prepared:
   t=time.perf_counter()
   try:
    r=ocr_v6.score_section(s,g,o,l,**kw) if sid in plan['included'] else pre
    if r['status']=='measured':
     gs=[norm(x['text']) for x in r['filter']['included']['gt']];ps=[norm(x['text']) for x in r['filter']['included']['ocr']]
     usedg=[];usedp=[];counts=dict(S=0,D=0,I=0,N=0)
     for match in r['metrics']['matches']:
      gi=match['gt_indices'];pi=match['output_indices'];usedg+=gi;usedp+=pi
      independent=independent_counts(''.join(gs[i] for i in gi),''.join(ps[i] for i in pi))
      assert all(independent[k]==match[k] for k in counts),(sid,'independent edits mismatch')
      for k in counts:counts[k]+=independent[k]
     assert sorted(usedg)==[i for i,t in enumerate(gs) if t]
     assert sorted(usedp)==[i for i,t in enumerate(ps) if t]
     assert all(counts[k]==r['metrics'][k] for k in counts)
     assert counts['N']==sum(map(len,gs))
     r['independent_verification']='passed'
   except Exception as exc:
    r=pre;r.update(status='evaluation_failed',metrics=None,reasons=[type(exc).__name__+':'+str(exc)])
   rows.append(r)
   with (outdir/'sections.jsonl').open('a',encoding='utf8') as fp:fp.write(json.dumps(r,ensure_ascii=False)+'\n')
   log.write(json.dumps(dict(section_id=sid,status=r['status'],elapsed_seconds=time.perf_counter()-t),ensure_ascii=False)+'\n');log.flush()
   print(sid,r['status'],flush=True)
 summary=ocr_v6.aggregate_fixed_partial(rows,plan)
 independent_groups={}
 for group in sorted({r['group'] for r in rows}):
  measured=[r['metrics'] for r in rows if r['group']==group and r['status']=='measured'];tot={k:sum(x[k] for x in measured) for k in ('S','D','I','N')}
  assert tot==summary['groups'][group]['measured_totals']
  independent_groups[group]=tot
 for p,h in protected.items():assert sha(p)==h,p
 write('summary.json',summary)
 write('validation.json',dict(independent_edit_backtraces=True,all_characters_consumed_once=True,independent_group_totals=independent_groups,protected_hashes_verified=len(protected),counts=dict(Counter(r['status'] for r in rows)),started_utc=started,elapsed_seconds=time.perf_counter()-start,python=platform.python_version(),unicode=unicodedata.unidata_version,platform=platform.platform(),additional_ocr_calls=0,external_api_calls=0,additional_paid_cost=0,measurement_approved_by='user_chat_20261110',scoring_code=ocr_v6.implementation(),adapter_sha256=sha(__file__)))
 write('source-hashes.json',protected)
 write('manifest.json',dict(files={p.name:sha(p) for p in outdir.iterdir() if p.is_file() and p.name!='manifest.json'},frozen_baseline_sha256=sha(frozen/'manifest.json')))
 print(json.dumps(summary['overall'],ensure_ascii=False),flush=True)

if __name__=='__main__':main()
