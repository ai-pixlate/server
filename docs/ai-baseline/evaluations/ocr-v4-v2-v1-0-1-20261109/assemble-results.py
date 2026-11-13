"""보존 출력 검증 및 승인된 OCR Text v2 CER 집계. OCR 실행 없음."""
from pathlib import Path
import json,sys,collections,subprocess
W=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(W))
from tools.baseline.core import read,digest,write_new
from tools.baseline.ocr import verify,validate_inputs,score,aggregate,BOUNDARY
out=Path(__file__).resolve().parent
truth=W/'docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109'
sup=out/'supplement'
manifest,units=verify(truth)
lookup={u['id']:u for u in units}
completed=read(sup/'completed.json')
plan=read(out/'execution-plan.json')
records=read(out/'reused-inputs.json')['sections']
for r in records:
 if r['id'] not in plan['execute_ids']:continue
 sid=r['id'];event=read(sup/(sid+'-execution.json'));u=lookup[sid]
 assert event['input_sha256']==u['image']['sha256']
 assert event['engine_calls']<=1
 r['origin']='supplement_20261109'
 if event['status']!='success':
  r.update(status='no_output',reason=event.get('message',event['status']))
  continue
 assert event['output_sha256']==digest(sup/(sid+'.json'))
 r.update(status='available',reason=None,input_sha256=event['input_sha256'],boundary=BOUNDARY,
  output_section_key=u['section_key'],attempt_id=out.name+':'+sid+':1',
  environment_sha256=digest(sup/'environment.json'),
  evidence={'path':str(sup/(sid+'-execution.json')),'sha256':digest(sup/(sid+'-execution.json')),'locator':'ocr.run return; supplement.py and started.json'},
  output={'path':str(sup/(sid+'.json')),'sha256':event['output_sha256'],'pointer':''})
write_new(out/'inputs.json',{'sections':records})
validation=validate_inputs(truth,out/'inputs.json')
write_new(out/'validation.json',validation)
invalid=[r for r in validation['sections'] if r['status']!='ready']
if invalid:
 write_new(out/'blocked.json',{'reason':'input validation failed; no scoring','sections':invalid})
 raise RuntimeError('입력 검증 실패. 채점 중단')
report=score(truth,out/'inputs.json',out/'results-112.json')
groups={g:aggregate([r for r in report['sections'] if r['id'].startswith(g)]) for g in ['GS-01','GS-02','GS-03']}
events=[read(sup/(sid+'-execution.json')) for sid in plan['execute_ids']]
summary={'total':report['total'],'groups':groups,'origin_counts':dict(collections.Counter(r['origin'] for r in records)),
 'supplement_success':sum(e['status']=='success' for e in events),'supplement_failure':sum(e['status']!='success' for e in events),
 'supplement_seconds':sum(e['seconds'] for e in events),'initialization_seconds':read(sup/'model.json')['initialization_seconds'],
 'engine_calls':completed['engine_calls'],'external_api_calls':0,'external_api_cost_usd':0,
 'scope':'34 original images, 112 fixed sections, standalone OCR evaluation',
 'limitations':['16 sections accept unreadable/untranscribed parts as empty truth; recognized output remains in CER',
                '53 AI-only reviewed sections; human zero-errors finding limited to 47',
                'N=0 sections reported separately','input images remain in local fixed input tree']}
write_new(out/'summary.json',summary)
for path,sha in read(out/'protected-files.json')['files'].items():assert digest(W/path)==sha,path
run=W/'docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01'
preserved=read(run/'github-file-index.json')['files']
for path,r in preserved.items():assert digest(run/path)==r['sha256'],path
write_new(out/'integrity.json',{'protected_files':len(read(out/'protected-files.json')['files']),
 'baseline_preserved_files':len(preserved),'truth_verified':True,'input_validation_ready':len(validation['sections']),
 'product_sha':subprocess.check_output(['git','-C','C:/Users/Yeram/Documents/pixlate-server/.cache/baseline-release','rev-parse','HEAD'],text=True).strip()})
print(json.dumps(summary))
