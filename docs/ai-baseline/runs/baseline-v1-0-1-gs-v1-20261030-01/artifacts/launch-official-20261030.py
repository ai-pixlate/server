import os,sys,json,subprocess,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
RUN=ROOT/'docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01'
sys.path.insert(0,str(ROOT))
from tools.baseline.approval import require_approval
from tools.baseline import protocol
from tools.baseline.core import digest,now
from dotenv import dotenv_values
require_approval(RUN)
assert not protocol.validate(RUN),protocol.validate(RUN)
assert not (RUN/'started.json').exists(),'이미 시작한 회차 재실행 금지'
os.environ['BASELINE_DATABASE_URL']=(ROOT/'.cache/baseline-local/database-url.txt').read_text(encoding='utf-8-sig').strip()
key=os.environ.get('GEMINI_API_KEY') or dotenv_values(ROOT/'.env',interpolate=False).get('GEMINI_API_KEY')
assert key,'API key missing'
os.environ['GEMINI_API_KEY']=key
os.environ['PADDLE_PDX_CACHE_HOME']=str(ROOT/'.cache/baseline-connection-check-20261028/paddlex-cache')
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
os.environ['HF_HUB_OFFLINE']='1'
os.environ['MODELSCOPE_OFFLINE']='1'
os.environ['PYTHONIOENCODING']='utf-8'
os.environ['PYTHONUNBUFFERED']='1'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
os.environ['AWS_EC2_METADATA_DISABLED']='true'
os.environ['TEMP']=os.environ['TMP']=str(ROOT/'.cache/baseline-pr-check/tmp')
safe_keys=['PADDLE_PDX_CACHE_HOME','PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK','HF_HUB_OFFLINE','MODELSCOPE_OFFLINE','PYTHONIOENCODING','PYTHONUNBUFFERED','PYTHONDONTWRITEBYTECODE','AWS_EC2_METADATA_DISABLED','TEMP','TMP']
(RUN/'launch.json').write_text(json.dumps({'at':now(),'launcher_sha256':digest(__file__),'environment':{k:os.environ[k] for k in safe_keys},'secret_values_recorded':False},ensure_ascii=False,indent=2),encoding='utf-8')
child=subprocess.Popen([sys.executable,'-I','-B','-u',str(ROOT/'tools/baseline/worker.py'),str(RUN),'--execute-models'],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1)
def forward():
 for line in sys.stdin:
  if child.poll() is not None:return
  child.stdin.write(line);child.stdin.flush()
threading.Thread(target=forward,daemon=True).start()
with (RUN/'console.log').open('x',encoding='utf-8') as log:
 for line in child.stdout:
  log.write(line);log.flush();sys.stdout.write(line);sys.stdout.flush()
code=child.wait()
(RUN/'process-exit.json').write_text(json.dumps({'at':now(),'exit_code':code}),encoding='utf-8')
raise SystemExit(code)
