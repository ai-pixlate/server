from pathlib import Path
import json,os,sys
W=Path('C:/Users/Yeram/Documents/pixlate-server/.worktrees/ai-pipeline-evaluation-metrics')
sys.path.insert(0,str(W))
from tools.baseline.ocr import verify,validate_inputs,score,BOUNDARY
from tools.baseline.core import read,digest,write_new
out=W/'docs/ai-baseline/evaluations/ocr-v4-v1-0-1-20261105'
prep=W/'docs/ai-baseline/preparations/ocr-v4-v1-0-1-20261105'
truth=W/'docs/ai-baseline/references/ocr-text/ocr-text-v1-20261104'
sup=out/'supplement'
completed=read(sup/'completed.json')
_,units=verify(truth)
records=read(prep/'inputs.json')['sections']
def rel(p):return Path(os.path.relpath(p,out)).as_posix()
for r in records:
 if r['status']=='available':
  for k in ('evidence','output'):r[k]['path']=rel((prep/r[k]['path']).resolve())
  r['origin']='initial_baseline_20261030'
 else:
  sid=r['id'];event=read(sup/(sid+'-execution.json'));u=next(u for u in units if u['id']==sid)
  assert event['status']=='success'
  assert event['input_sha256']==u['image']['sha256']
  assert event['output_sha256']==digest(sup/(sid+'.json'))
  r.update(status='available',reason=None,input_sha256=event['input_sha256'],boundary=BOUNDARY,
   output_section_key=u['section_key'],attempt_id=out.name+'-supplement:'+sid+':1',origin='supplement_20261105',
   environment_sha256=digest(sup/'environment.json'),
   evidence={'path':rel(sup/(sid+'-execution.json')),'sha256':digest(sup/(sid+'-execution.json')),'locator':'ocr.run return; supplement.py and started.json'},
   output={'path':rel(sup/(sid+'.json')),'sha256':event['output_sha256'],'pointer':''})
write_new(out/'combined-inputs.json',{'sections':records,'note':'initial nine + separately approved supplementary three; not all from initial run'})
validation=validate_inputs(truth,out/'combined-inputs.json')
assert all(r['status']=='ready' for r in validation['sections'])
write_new(out/'combined-validation.json',validation)
# 승인된 CER 측정: 동일 동결본·동일 채점기, 결과별 출처 보존
report=score(truth,out/'combined-inputs.json',out/'combined-12.json')
print(report['total'])
