"""Read-only audit of original run; write new preservation sidecars and local backup only."""
import collections
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from PIL import Image
from tools.baseline import protocol, preflight

RUN_ID = 'baseline-v1-0-1-gs-v1-20261030-01'
r = ROOT / 'docs/ai-baseline/runs' / RUN_ID
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out = r / ('preservation-' + stamp)
backup = ROOT / '.cache/baseline-backups' / RUN_ID / stamp
read = lambda p: json.loads(p.read_text(encoding='utf-8'))
rows = lambda p: [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines() if s.strip()]
def sha(p):
    with p.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()
def save(name, value):
    with (out / name).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
def copy(source, relative):
    target = out / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst)
    assert sha(source) == sha(target)

assert subprocess.check_output(['git','branch','--show-current'],text=True).strip() == 'feature/baseline-v1.0.1-measurement'
before = {p.relative_to(r).as_posix(): {'sha256':sha(p),'bytes':p.stat().st_size} for p in r.rglob('*') if p.is_file()}
print('Original inventory hashed:',len(before),flush=True)
m = read(r/'manifest.json'); runtime = read(r/'runtime.json'); index = read(r/'object-index.json')
assert m['product_commit'] == 'bfa3b700144467f4adf7d6802b7b2e93791fe6de'
product = Path(m['generation_config']['product_root'])
assert subprocess.check_output(['git','-C',str(product),'rev-parse','HEAD'],text=True).strip() == m['product_commit']
assert not subprocess.check_output(['git','-C',str(product),'diff','--name-only'],text=True).strip()
issues = protocol.validate(r)
assert not issues, issues
assert sha(r/'references.json') == runtime['reference_sha256']
lock = preflight.verify_dependency_lock(r/'artifacts/requirements-windows-py312.lock.txt')
for rel,h in runtime['settings'].items(): assert sha(product/rel) == h,rel
for rel,h in runtime['tool_sha256'].items(): assert sha(ROOT/'tools/baseline'/rel) == h,rel
for asset in m['environment']['model_assets']: assert sha(Path(asset['path'])) == asset['sha256'],asset['path']
for entry in index.values():
    p=r/'objects'/entry['sha256']
    assert sha(p)==entry['sha256'] and p.stat().st_size==entry['bytes']
for a in m['artifacts']:
    assert sha(r/a['path'])==a['sha256']
    with Image.open(r/a['path']) as im:
        assert im.size==(a['width'],a['height'])
        im.verify()
    if a.get('state_ref'):
        assert sha(r/a['state_ref']['path'])==a['state_ref']['sha256']
assert m['reference_freeze'] is None and not m['evaluation_sessions']
for name in ['reference_units.jsonl','evaluations.jsonl','defects.jsonl']:
    assert not (r/name).read_text(encoding='utf-8').strip()
assert len((r/'metrics.csv').read_text(encoding='utf-8').splitlines())==1
events=rows(r/'execution.jsonl'); usage=[x for x in rows(r/'usage.jsonl') if x['status']!='reserved']
assert [x['call'] for x in usage]==list(range(1,394))
assert [x['call'] for x in rows(r/'usage.jsonl') if x['status']=='reserved']==list(range(1,394))
assert all(x['change_count']==0 for x in events if x['record_kind']=='user_edit')
assert set(x['source_keys'][0] for x in events if x.get('result_only'))==set(m['source_keys'])
snapshots={c:read(r/'evidence'/f'{c}.json') for c in ['GS-01','GS-02','GS-03']}
draftroot=ROOT/'docs/ai-baseline/runs/reference-preparation-20261027'
draft=rows(draftroot/'artifacts/reference_units.draft.jsonl'); di=read(draftroot/'artifacts/draft-index.json')
assert len(draft)==324 and len({x['reference_unit_id'] for x in draft})==324
assert all(x['verified_by'] is None and x['verified_at'] is None and x['verification_ref'] is None for x in draft)
assert sha(draftroot/'artifacts/reference_units.draft.jsonl')==di['draft_sha256']
originals={a['source_keys'][0]:a for a in m['artifacts'] if a['kind']=='original'}
assert len(originals)==34
for s in di['sources']: assert s['sha256']==originals[s['source_key']]['sha256']
basis_resolutions={}
for d in draft:
    a=originals[d['source_key']]
    assert d['location']['artifact_id']==a['artifact_id']
    x0,y0,x1,y1=d['location']['bbox']
    assert 0<=x0<x1<=a['width'] and 0<=y0<y1<=a['height']
    for b in d['basis_refs']:
        candidates=[r/b['path'],draftroot/b['path']]
        if b['path']=='artifacts/protocol-dataset.md': candidates.append(draftroot/'artifacts/protocol-dataset.txt')
        resolved=next((p for p in candidates if p.is_file() and sha(p)==b['sha256']),None)
        assert resolved,b
        basis_resolutions[b['path']+'@'+b['sha256']]={'declared_path':b['path'],'sha256':b['sha256'],'resolved_path':str(resolved)}
source_inputs=[];missing_keys=[]
for source in m['source_keys']:
    case=source[:5];s=snapshots[case];a=originals[source]
    dbsrc=next(x for x in s['source_image'] if x['upload_order']==int(source[-3:]))
    sections=sorted([x for x in s['section'] if x['source_image_id']==dbsrc['id']],key=lambda x:x['section_order'])
    previews=[x for x in m['artifacts'] if x['checkpoint']=='machine_snapshot' and source in x['source_keys']]
    finals=[x for x in m['artifacts'] if x['checkpoint']=='service_final' and source in x['source_keys']]
    coordinates=[]
    for sec in sections:
        links={}
        for field in ['image_key','inpaint_image_url','mask_image_url','render_image_key']:
            key=sec[field]
            if key:
                if key not in index: missing_keys.append([source,sec['id'],field,key])
                else: links[field]={'key':key,'path':'objects/'+index[key]['sha256'],'sha256':index[key]['sha256']}
        p=[x for x in previews if x['target_id']==sec['id']]
        stacks=[x for x in s['deliverable_section'] if x['section_id']==sec['id']]
        coordinates.append({'section_id':sec['id'],'original_source_image_id':dbsrc['id'],'section_order':sec['section_order'],
          'original_top_offset':sec['top_offset'],'height':sec['height'],'original_bbox':sec['bbox'],
          'bucket':sec['bucket'],'exclusion_reason':sec['exclusion_reason'],'original_verdict':sec['original_verdict'],
          'preview_artifacts':p,'final_placement':stacks,'intermediate_objects':links})
    assert {x['target_id'] for x in previews} <= {x['id'] for x in sections}
    status='generated' if finals else 'job_analysis_failed' if case=='GS-03' else 'ai_all_excluded_pending_pm'
    if status=='ai_all_excluded_pending_pm': assert sections and all(x['bucket']=='exclude' for x in sections),[(x['bucket'],x['exclusion_reason']) for x in sections]
    source_inputs.append({'source_key':source,'execution_status_summary':status,'original':a,'machine_snapshot':previews,'service_final':finals,
       'section_coordinate_links':coordinates,'draft_reference_unit_ids':[x['reference_unit_id'] for x in draft if x['source_key']==source],
       'database_evidence':f'evidence/{case}.json','execution_failure_group':'GS-03-job-3-N2' if case=='GS-03' else None,
       'quality_scored':False,'excluded_correctness':None})
assert not missing_keys,missing_keys
attempts=[x for x in snapshots['GS-03']['job_async_task'] if x.get('stage')=='analyze']
assert len(attempts)==3 and all(x['status']=='failed' for x in attempts)
assert len({x['input_fingerprint'] for x in attempts})==1
cost=sum((Decimal(str(x['cost'])) for x in usage if x.get('cost') is not None),Decimal(0))
held=sum(x.get('cost') is None for x in usage)*Decimal('1.04')
assert abs(Decimal(str(usage[-1]['budget_consumed']))-cost-held)<Decimal('0.00000001')
out.mkdir();backup.mkdir(parents=True)
for rel in runtime['settings']: copy(product/rel,Path('reproduction/product-settings')/rel)
for rel in runtime['tool_sha256']: copy(ROOT/'tools/baseline'/rel,Path('reproduction/baseline-tools')/rel)
for p in draftroot.rglob('*'):
    if p.is_file(): copy(p,Path('reference-draft')/p.relative_to(draftroot))
copy(Path(__file__),Path('audit-script.py'))
save('original-file-inventory.json',{'scope':'Existing files before this preservation audit; no prior baseline manifest for previously unindexed files', 'files':before})
save('evaluation-inputs.json',{'quality_scored':False,'reference_frozen':False,'path_base':'run root unless absolute','sources':source_inputs,'draft_basis_resolutions':list(basis_resolutions.values())})
audit={'checked_at':datetime.now(timezone.utc).isoformat(),'run_id':RUN_ID,'product_sha':m['product_commit'],'protocol_sha':m['protocol_commit'],
 'protocol_validation':issues,'original_files':len(before),'original_bytes':sum(x['bytes'] for x in before.values()),
 'image_artifacts_verified':len(m['artifacts']),'unique_objects_verified':len({x['sha256'] for x in index.values()}),'object_keys':len(index),
 'model_asset_hashes_verified':len(m['environment']['model_assets']),'settings_hashes_verified':len(runtime['settings']),
 'tools_hashes_verified':len(runtime['tool_sha256']),'package_lock':lock,'missing_intermediate_keys':missing_keys,
 'draft_units':len(draft),'draft_sources':len(di['sources']),'draft_source_and_bbox_verified':True,'draft_basis_refs_resolved':True,
 'reference_freeze':None,'quality_scored':False,'draft_type_counts':dict(collections.Counter(x['unit_type'] for x in draft)),
 'operational_source_status_counts':dict(collections.Counter(x['execution_status_summary'] for x in source_inputs)),
 'shared_execution_failure':{'group':'GS-03-job-3-N2','job_id':3,'attempt_ids':sorted(x['id'] for x in attempts),'automatic_retries':2,'affected_sources':18,'independent_quality_defects_assigned':False},
 'budget':{'invoice_usd':None,'price_estimate_usd':str(cost),'unknown_request_reservation_usd':str(held),'budget_consumed_usd':str(cost+held),
           'remaining_budget_usd':str(Decimal(50)-cost-held),'call_count':len(usage),'call_limit':1367,'known_total_tokens':sum((x.get('usage') or {}).get('total_token_count') or 0 for x in usage),
           'unknown_cost_requests':sum(x.get('cost') is None for x in usage),'failed_requests':sum(x['status']=='failed' for x in usage)},
 'backup_scope':'same-disk local zip; not offsite; existing run plus new audit/reproduction sidecars; model weights/venv/wheelhouse and full PostgreSQL instance not copied',
 'known_gaps':['PM reference freeze absent','Evaluation session/model/settings/transcript not registered',
 'GS-03 temporary section/debug files deleted by product TemporaryDirectory; stages and DB/usage/error evidence retained',
 'Actual API invoice unavailable','No verified independent-device/offsite backup','Source-to-result reference-unit placement requires review after freeze; section transforms are indexed']}
save('integrity-check.json',audit)
lines=['# 최초 Baseline 보존 및 품질 평가 준비 점검','',f'- 회차: `{RUN_ID}`',f'- 제품: v1.0.1 / `{m["product_commit"]}`',
 '- 원시 파일·manifest·이미지·평가 파일은 변경하지 않았다. 이 폴더는 보존/준비 점검의 추가 기록이며 공식 채점 결과가 아니다.',
 '- PM 기준 자료는 미동결. 별도 초안 사본은 검수 전 상태와 null 값을 그대로 보존했다.',
 '', '## 보존과 무결성', '',f'- 기존 {len(before):,}개 파일({sum(x["bytes"] for x in before.values()):,} bytes)의 SHA-256 목록을 새로 고정했다. 이미 등록된 manifest/object/runtime 해시와 대조했고 원시 파일의 변경 없음도 재확인한다.',
 '- 원본 34장·최초 미리보기 67개·최종 이미지 13장: 해시·이미지 구조·크기 검증. object-index의 654개 키/383개 고유 파일 검증. 기준 초안 324개 항목의 원본 ID·해시·좌표 범위 확인.',
 '- 모델 자산 31개·제품 설정 17개·실행 도구 16개 해시와 의존성 lock 확인. 설정/프롬프트/폰트와 도구 사본은 reproduction에 추가 보존. 실행 사전·용어집은 기존 references.json과 runtime 해시로 연결된다.',
 '- Windows 및 승인된 의존성 차이는 기존 manifest/runtime 기록을 유지한다. 해시 확인은 비결정적인 모델 응답의 동일 재현을 보장하지 않는다.',
 f'- 로컬 백업: `{backup / "snapshot.zip"}`. 백업 결과·해시·전수 대조는 [백업 영수증](backup-receipt.json)에 기록한다. 같은 C: 디스크이므로 장치 장애 대비 원격/외장 백업은 아니다. .cache 정리 대상에서 제외해야 한다.',
 '- 모델 가중치·venv·wheelhouse는 현재 로컬 자산 해시만 확인했으며 이 회차 백업에 복제하지 않았다. DB의 상품별 JSON 증거와 사전은 보존했지만 전체 PostgreSQL 복원용 덤프는 아니다.',
 '', '## 실패 해석과 증거 한계', '',
 '- GS-03은 job 3의 N2 분석 실패 1개 그룹이며 시도 487/488/489(최초+자동 재시도 2회)의 504 기록을 연결한다. 영향 원본은 18개지만 독립적인 quality_defect 18건이나 critical 18건을 만들지 않는다.',
 '- 원본별 산출물 부재는 그대로 남긴다. 동결 후 H01 적용 대상이면 실패 분모에 포함하고 H02/H03의 산출물 부재는 A/B/C로 강제하지 않는다. 실패 18장을 품질 결함 중복 제거를 이유로 평가 모집단에서 제거하지 않는다.',
 '- GS-03_013~018은 분석 루프 미도달이다. result_only N6 no_output은 최종 산출물 부재 요약이며 N6 실제 호출 18건이 아니다.',
 '- GS-03의 임시 섹션 이미지·merge_debug 파일은 제품의 임시 디렉터리 정리로 소실돼 복원/보존 완료로 표시하지 않는다. stages.jsonl의 부분 OCR/병합 결과, dispatch/usage/console, DB 실패 증거는 남아 있다.',
 '- 3장 AI 전체 제외는 적합성 미확정이다. PM 동결 전 not_applicable 또는 decision_wrong으로 확정하지 않는다.',
 '', '## H01~H04 준비', '',
 '| 지표 | 확보 자료 | 공식 평가 전 남은 일 |','|---|---|---|',
 '| H01 | 34개 원본·최종 실행 상태·13개 최종 산출물·전체 제외 판단 증거 | PM의 원본 전체 적용/제외 기대값 동결 후 분모/분자 확정 |',
 '| H02 | 13개 원본의 최초 미리보기 67개와 N5 수정 전 상태·N3 무수정 확인 | 섹션 순서/원본 좌표를 따라 검수; 결함 기록 후 A/B/C 계산 |',
 '| H03 | 최종 이미지 13개·deliverable/섹션 배치·무수정 확인 | 최종 결과를 별도 검수; H02 등급 자동 복사 금지 |',
 '| H04 | 핵심 사실 후보 199개·원본 좌표·초기/최종 증거 연결 | PM이 중요도/기대값/H04 두 체크포인트 적용 여부 확정; 실제 이미지 판독 확인 |',
 '', '- machine_snapshot은 원본 단위 완성 합성 이미지가 아니라 섹션 미리보기 묶음이다. [연결 색인](evaluation-inputs.json)의 original_top_offset, preview target_id, final_placement.stack_offset으로 원본/최초/최종 좌표계를 구분한다. 기준 단위와 실제 결과 위치의 정밀 대응은 평가 시 확인한다.',
 '- 검수 전 초안의 basis_refs 일부는 과거 파일명을 사용한다. protocol-dataset.md는 초안 폴더의 protocol-dataset.txt와 같은 해시로 확인했다. 현 회차의 동명 최신 protocol-dataset.md는 다른 해시이므로 바꿔치기하지 않는다. 동결본 반입 때 근거 파일을 별도 경로로 보존하고 정확한 경로/해시를 연결해야 한다.',
 '- GS-01 번역 실패 경고 2건·GS-02 15건 및 폭 초과 신호는 검수 근거다. 신호만으로 품질 결함이나 심각도를 확정하지 않는다. H05는 별도 서비스 평가이며 0건으로 집계하지 않는다.',
 '', '## 예산 기록', '',f'- 실제 청구액: 미확인. 단가 계산 가능한 372회 추정 ${cost:.8f}; 미정 21회 예약 ${held:.2f}; 예산 점유 ${cost+held:.8f}; $50 대비 잔여 ${Decimal(50)-cost-held:.8f}.',
 '- 393회 호출/1,367회 상한. 정상 응답 390회·504 3회. 응답에 제공된 총 토큰 1,243,650; 실패 3회의 토큰은 미확인. 미정 예약은 thoughts_token_count 미제공 정상 응답 18회와 실패 3회다. 미제공 값을 0으로 정산하거나 예약을 실제 비용으로 표시하지 않는다.',
 '- 예산 잔여는 추가 생성 승인으로 해석하지 않는다. 청구 명세 확보 시 요청/기간 연결 가능성을 확인한 뒤 별도 정산 기록을 추가한다.',
 '', '## PM 동결 후 순서', '',
 '1. #93의 의견을 기준 초안에 반영하고 PM 재확인, 중요도·적용 여부·전체 제외·불확실 항목을 정리한다. 별도 references/gs-v1/ 동결본과 승인 PR/커밋·검수 근거를 확보한다.',
 '2. 회차 원본 34장과 동결본의 ID/해시를 대조하고 기준 단위·근거 파일을 반입한다. 최초 manifest/빈 평가 파일은 이 백업으로 보존하고, 이후 정식 절차로 reference_units.jsonl 및 reference_freeze를 연결한다. 검증 없이 기존 초안을 단순 복사해 동결하지 않는다.',
 '3. 실제 Codex 채팅 모델·설정·지침·응답 기록을 확인해 평가 세션을 등록한다. 미확인 모델 이름을 임의 입력하지 않는다.',
 '4. H01 실행 증거와 PM 기대값 대조 → H02/H03 이미지 결함 검수 → H04 사실별 보존/판독 확인. 산출물 부재·판단 불가를 별도 처리하고 GS-03 실행 실패를 품질 결함으로 복제하지 않는다.',
 '5. PM이 K01~K09, critical/major, 불확실 판단과 기존 규칙의 A등급 표본을 검수한다. AI 최초 기록과 사람 수정 기록을 구분해 보존한다.',
 '6. 예외/분모·중복 제거·연결/해시를 검증한 뒤 공식 집계한다. 현재는 evaluations/defects/metrics를 채우지 않는다.',
 '', '## 원본 34장 연결', '', '| 원본 | 상태 | 원본 이미지 | 최초 미리보기 | 최종 이미지 | 초안 단위 |','|---|---|---|---:|---|---:|']
for x in source_inputs:
    label={'generated':'생성 완료','ai_all_excluded_pending_pm':'AI 전체 제외·적합성 미확정','job_analysis_failed':'GS-03 상품 분석 실패 영향'}[x['execution_status_summary']]
    final=' · '.join(f'[최종](../{a["path"]})' for a in x['service_final']) or '없음'
    lines.append(f'| {x["source_key"]} | {label} | [원본](../{x["original"]["path"]}) | {len(x["machine_snapshot"])} | {final} | {len(x["draft_reference_unit_ids"])} |')
with (out/'README.md').open('x',encoding='utf-8') as f: f.write('\n'.join(lines)+'\n')
for rel, info in before.items(): assert sha(r/rel)==info['sha256'],rel
print('Audit/sidecars complete, original files unchanged; creating backup',flush=True)
members={p.relative_to(r).as_posix():sha(p) for p in r.rglob('*') if p.is_file()}
archive=backup/'snapshot.zip'
with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
    for rel in members: z.write(r/rel,RUN_ID+'/'+rel)
with zipfile.ZipFile(archive) as z:
    assert len(z.infolist())==len(members)
    for rel,h in members.items():
        with z.open(RUN_ID+'/'+rel) as f: assert hashlib.file_digest(f,'sha256').hexdigest()==h,rel
receipt={'created_at':datetime.now(timezone.utc).isoformat(),'archive_path':str(archive),'archive_sha256':sha(archive),'archive_bytes':archive.stat().st_size,
 'members_verified':len(members),'members_sha256':members,'original_files_unchanged':True,'original_inventory_sha256':sha(out/'original-file-inventory.json'),
 'backup_type':'same-disk local; independent-device/offsite backup unverified','receipt_not_inside_archive':True}
save('backup-receipt.json',receipt)
shutil.copyfile(out/'backup-receipt.json',backup/'backup-receipt.json')
for rel,info in before.items(): assert sha(r/rel)==info['sha256'],rel
for url in re.findall(r'\]\(([^)]+)\)',(out/'README.md').read_text(encoding='utf-8')): assert (out/url).is_file(),url
assert not protocol.validate(r)
print(json.dumps({'report':str(out/'README.md'),'backup':str(archive),'backup_bytes':archive.stat().st_size,'original_files':len(before),'verified_backup_members':len(members),'audit':audit},ensure_ascii=False))
