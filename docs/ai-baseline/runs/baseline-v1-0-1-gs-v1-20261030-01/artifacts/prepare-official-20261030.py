import json, os, sys, shutil, subprocess, platform
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.baseline import protocol as p, preflight
from tools.baseline.core import digest, now, IDS
from tools.baseline.approval import require_approval, TARGET
from tools.baseline.scope import APPROVED_BUDGET
RUN='baseline-v1-0-1-gs-v1-20261030-01'
out=ROOT/'docs/ai-baseline/runs'/RUN
assert not out.exists()
old=json.loads((ROOT/'.cache/baseline-preparation-20261020/bundle-release-0716d57-preparation-final-20261021/config.json').read_text(encoding='utf-8-sig'))
assets=json.loads((ROOT/'.cache/baseline-local/environment.json').read_text(encoding='utf-8-sig'))['model_assets']
for a in assets:
 path=Path(a['path'])
 if '.paddlex' in path.parts:
  path=ROOT/'.cache/baseline-connection-check-20261028/paddlex-cache'/path.relative_to(Path.home()/'.paddlex')
 a['path']=str(path)
 a['model']='LaMa' if path.name=='big-lama.pt' else 'PaddleOCR'
 assert path.is_file() and digest(path)==a['sha256']
approval='''# 공식 이미지 생성 승인 근거

출처: 현재 Codex 대화에서 사용자 예람이 2026-10-30 직접 요청한 Pixlate v1.0.1 Baseline 공식 실측 시작 승인. 이 파일은 해당 사용자 지시의 실행용 기록이며 PM 기준 자료 승인/동결 기록이 아니다.

- 최신 origin/develop에서 feature/baseline-v1.0.1-measurement 생성 승인, 기존 작업 보존.
- 제품 v1.0.1 bfa3b700144467f4adf7d6802b7b2e93791fe6de 고정.
- GS-01→GS-02→GS-03, 미국/영어, 상품별 1 job·총 3 jobs·원본 34장, 원본별 공식 생성 1회, 제품 내부 자동 재시도 허용. 추가 재생성·best-of 금지.
- 비용 안전 중단 상한 USD 50, 최대 1367회, 호출당 예약 USD 1.04, 내부 재시도 포함. 사용자 추가 비용 승인 없이 이 범위 실행 가능. 기록·예산 통제 문제나 초과 예상 시 중단.
- Windows 격리 환경, 기존 고정 모델·사전·용어집·설정 사용. 의존성 차이 3개와 플랫폼 차이 수용 승인.
- N3/N5는 예람이 직접 변경 없이 확인. 명시적 응답까지 대기하며 자동 승인·수정·우회 금지.
- 최초/최종/중간 산출물·실패·재시도·사용량 보존. 실제 청구·토큰 단가 추정·예약 소비 구분.
- 기준 자료 동결 전 생성 허용. H01~H04 공식 채점은 PM 동결 후, H05 제외. 일반 LLM 비교 이미지 4장 평가 제외.
- 커밋/push/PR 금지. 이번 기록 작성 자체로 사람 확인 응답을 만들지 않는다.
'''
approval_bytes=approval.encode('utf-8')
import hashlib
user=dict(approved=True,approved_by='예람',approved_at=now(),
 evidence_ref={'path':'artifacts/user-generation-approval.md','sha256':hashlib.sha256(approval_bytes).hexdigest()},
 product_commit=TARGET,source_keys=[p.source_key(s) for s in IDS],job_grouping='case',
 official_generations_per_source=1,user_regeneration_allowed=False,human_intervention_scope='confirmation_only',
 human_confirmation_actor='예람',**APPROVED_BUDGET)
budget=dict(APPROVED_BUDGET,settle_usage=True,sdk_attempts=1,reservation_verified=True,
 model='gemini-3.8-flash',input_per_million=0.75,output_per_million=3.75,
 basis='2026-10-30 공식 Standard 요율·모델 한도 확인: 1048576*0.75/M+65536*3.75/M=1.032192 USD. SDK 2.29.0 retry_args(None)=1; 도구/grounding 미사용. 실제 청구액은 미확인.')
lock=ROOT/'tools/baseline/requirements-windows-py312.lock.txt'
config=p.template();config.update(evaluation_run_id=RUN,product_commit=TARGET,
 protocol_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 input_root=str(ROOT/'pipeline/samples/evaluation/gs-v1'),case_inputs=old['products'],
 generation_config={'product_root':str(ROOT/'.cache/baseline-release'),'job_grouping':'case','budget':budget},
 cost_approval={'pm_cost_agreed':True,'user_execution':user},
 environment={'hardware':{'platform':platform.platform(),'python':platform.python_version(),'cpu_count':os.cpu_count(),
 'gpu':'NVIDIA GeForce RTX 4060 Laptop GPU','driver':'555.97','gpu_total_mib':8188,'gpu_used_mib_before':841},
 'resource_limits':{'additional_limits':'none; release configuration unchanged','ocr':'cpu','lama':'cuda:0 FP32'},
 'concurrent_work':{'other_python_processes_observed':False,'gpu_background_usage_mib':841},
 'model_assets':assets,'dependency_lock':{'path':'artifacts/requirements-windows-py312.lock.txt','sha256':digest(lock)},
 'accepted_differences':['Windows vs operating Linux','Python 3.12.10 vs 3.12.15','NumPy 2.3.5 vs 2.5.4','PyYAML 6.0.2 vs 6.0.3','uvloop absent on Windows','native rendering equivalence not established']})
p.prepare(config,out)
(out/'artifacts/user-generation-approval.md').write_bytes(approval_bytes)
shutil.copyfile(lock,out/'artifacts'/lock.name)
evidence={
 'preflight.json':ROOT/'.cache/baseline-pr-check/official-preflight-20261030.json',
 'database-before.json':ROOT/'.cache/baseline-final-preflight-20261028/database-official-20261030.json',
 'connection-check.md':ROOT/'docs/ai-experiments/2026-10-28_baseline-connection-check.md'}
for name,path in evidence.items():shutil.copyfile(path,out/'artifacts'/name)
(out/'artifacts/pricing-verification.json').write_text(json.dumps({'checked_at':now(),
 'sources':['https://ai.google.dev/gemini-api/docs/pricing','https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash'],
 'model':'gemini-3.8-flash','input_per_million':0.75,'output_per_million':3.75,'max_cost_per_request':1.032192,
 'sdk_version':'2.29.0','sdk_retry_args_none_attempts':1,'invoice_amount':None},indent=2),encoding='utf-8')
m=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
for path in [out/'artifacts/user-generation-approval.md',out/'artifacts'/lock.name,*[out/'artifacts'/n for n in evidence],out/'artifacts/pricing-verification.json']:
 m['files'].append({'path':path.relative_to(out).as_posix(),'sha256':digest(path)})
(out/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
assert not p.validate(out),p.validate(out)
assert not p.readiness(m,purpose='generation'),p.readiness(m,purpose='generation')
require_approval(out)
preflight.verify_dependency_lock(out/'artifacts'/lock.name)
(out/'summary.md').write_text('# 공식 생성 회차 준비\n\n사용자 생성 승인 완료. 생성 미시작. PM 기준 자료 미동결·공식 품질 채점 미실시. H05 미측정(별도 서비스 평가 예정).\n',encoding='utf-8')
print(json.dumps({'run_id':RUN,'inputs':len(m['source_keys']),'assets':len(assets),'generation_ready':True,'quality_scoring_allowed':False},ensure_ascii=False))
