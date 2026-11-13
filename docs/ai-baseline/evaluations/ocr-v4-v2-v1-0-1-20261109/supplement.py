"""사용자 승인: v1.0.1 고정 섹션 부족분 로컬 OCR 보완. 네트워크·재실행 금지."""
import os,sys,json,time,hashlib,subprocess,platform,importlib.metadata as md
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path('C:/Users/Yeram/Documents/pixlate-server')
WORK=ROOT/'.worktrees/ai-pipeline-evaluation-metrics'
PRODUCT=ROOT/'.cache/baseline-release'
OUT=Path(__file__).resolve().parent/'supplement'
RUN=WORK/'docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01'
TRUTH=WORK/'docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109'
SHA='bfa3b700144467f4adf7d6802b7b2e93791fe6de'
IDS=tuple(json.loads((OUT.parent/'execution-plan.json').read_text(encoding='utf-8'))['execute_ids'])
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def now():return datetime.now(timezone.utc).isoformat()
def save(p,data):
 with Path(p).open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
def main():
 if OUT.exists():raise RuntimeError('기존 보완 회차 재실행 금지')
 assert subprocess.check_output(['git','-C',str(PRODUCT),'rev-parse','HEAD'],text=True).strip()==SHA
 assert not subprocess.check_output(['git','-C',str(PRODUCT),'status','--porcelain']).strip()
 runtime=read(RUN/'runtime.json')
 packages={d.metadata['Name'].lower().replace('_','-'):d.version for d in md.distributions()}
 assert all(packages.get(k.lower().replace('_','-'))==v for k,v in runtime['packages'].items())
 assets=[a for a in runtime['model_assets'] if a.get('model')=='PaddleOCR']
 for a in assets:assert digest(a['path'])==a['sha256']
 config=PRODUCT/'pipeline/config/default.toml'
 assert digest(config)==runtime['settings']['pipeline\\config\\default.toml']
 units={u['id']:u for u in map(json.loads,(TRUTH/'text_units.jsonl').read_text(encoding='utf-8').splitlines())}
 for sid in IDS:assert digest(ROOT/units[sid]['image']['path'])==units[sid]['image']['sha256']
 OUT.mkdir()
 save(OUT/'started.json',{'at':now(),'run_id':OUT.parent.name+'-supplement','ids':IDS,'product_sha':SHA,
  'approval':'사용자 2026-11-05 승인할게: 9개 부분 채점 및 GS-03 고정 섹션 부족분 로컬 OCR 보완',
  'attempts_per_section':1,'network':'blocked','external_api_cost_usd':0,'truth_manifest_sha256':digest(TRUTH/'manifest.json'),
  'runner_sha256':digest(__file__)})
 save(OUT/'environment.json',{'python':sys.version,'platform':platform.platform(),'packages':packages,'model_assets':assets,
   'config_sha256':digest(config),'product_sha':SHA,'package_differences_from_initial':{},'runtime_reference_sha256':digest(RUN/'runtime.json')})
 os.environ.update(PADDLE_PDX_CACHE_HOME=str(ROOT/'.cache/baseline-connection-check-20261028/paddlex-cache'),
  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True',HF_HUB_OFFLINE='1',MODELSCOPE_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1')
 import socket
 def deny(*a,**kw):raise RuntimeError('local OCR only: network blocked')
 socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny
 sys.path.insert(0,str(PRODUCT))
 import torch  # 최초 회차와 동일한 native import 순서
 from pipeline.config import load_config
 from pipeline.stages import ocr
 from pipeline.types import Section
 assert Path(ocr.__file__).resolve().is_relative_to(PRODUCT)
 cfg=load_config();started=time.monotonic()
 try:
  engine=ocr.build_engine(cfg)
 except BaseException as exc:
  save(OUT/'initialization-error.json',{'at':now(),'type':type(exc).__name__,'message':str(exc)});raise
 save(OUT/'model.json',{'info':engine.info,'initialization_seconds':time.monotonic()-started})
 total_calls=0
 for sid in IDS:
  u=units[sid];legacy=read(ROOT/'pipeline/out/ocr_input/v1'/u['sample']/'split.json')
  data=next(s for s in legacy['sections'] if s['section_key']==u['section_key'])
  data=dict(data);data['image_path']=str(ROOT/u['image']['path'])
  sec=Section.model_validate(data)
  assert sec.height <= ocr.validate_config(cfg).threshold_px
  save(OUT/(sid+'-started.json'),{'at':now(),'id':sid,'input_sha256':digest(sec.image_path),'attempt':1})
  count=0
  def counted(image):
   nonlocal count,total_calls
   if count: raise RuntimeError('second engine call blocked')
   count+=1;total_calls+=1
   return engine(image)
  t=time.monotonic();diag={}
  try:
   result=ocr.run(sec,cfg,engine=counted,diag=diag)
   save(OUT/(sid+'.json'),result.model_dump(mode='json'))
   event={'status':'success','output_sha256':digest(OUT/(sid+'.json')),'region_count':len(result.regions)}
  except Exception as exc:
   event={'status':'failed','error_type':type(exc).__name__,'message':str(exc)}
  event.update(id=sid,at=now(),seconds=time.monotonic()-t,engine_calls=count,input_sha256=digest(sec.image_path),
    boundary='pipeline.stages.ocr.run:return',product_sha=SHA,diag=diag)
  save(OUT/(sid+'-execution.json'),event)
  print(sid,event['status'],event['seconds'],flush=True)
 save(OUT/'completed.json',{'at':now(),'engine_calls':total_calls,'external_api_calls':0,'external_api_cost_usd':0,
    'note':'로컬 실행 시간/전력 비용 별도; 토큰 과금 없음'})
if __name__=='__main__':main()
