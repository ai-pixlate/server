"""보존된 Codex 최초 채점 복원본에서 PM 검수 반영본을 별도 생성한다. API 호출 없음."""
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys
from collections import Counter

HERE = Path(__file__).resolve().parent
INITIAL = HERE.parent
REPO = INITIAL.parents[3]
sys.path.insert(0, str(REPO))
from tools.baseline import protocol


def read(p):
    return json.loads(p.read_text(encoding='utf-8'))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def lines(p, rows):
    p.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8', newline='\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, required=True, help='최초 reproduce.py로 검증한 로컬 복원본')
    ap.add_argument('--output', type=Path, required=True, help='존재하지 않는 새 경로')
    args = ap.parse_args()
    src, out = args.input.resolve(), args.output.resolve()
    if out.exists() or src == out or src in out.parents:
        raise SystemExit('입력 밖의 새 output 경로를 사용하세요.')
    initial_hashes = {p.name: sha(p) for p in INITIAL.iterdir() if p.is_file()}
    for name in ('evaluations.jsonl', 'defects.jsonl', 'human_review.csv'):
        if (src/name).read_bytes() != (INITIAL/name).read_bytes():
            raise SystemExit('최초 기록과 다른 입력: ' + name)
    m, data = protocol.load(src)
    assert not data['human_review'] and len(data['evaluations']) == 466
    fixed = {f['path']: sha(src/f['path']) for f in [*m['files'], *m['artifacts']]}
    for name in ('reference_units.jsonl', 'execution.jsonl', 'reference-approval.json', 'reference-origins.json'):
        fixed[name] = sha(src/name)
    out.mkdir()
    root = out/'materialized'
    shutil.copytree(src, root)
    result_dir = out/'results'
    result_dir.mkdir()
    shutil.copyfile(HERE/'pm-comment.json', root/'scoring/pm-comment.json')
    comment = read(HERE/'pm-comment.json')
    ev = {'path': 'scoring/pm-comment.json', 'sha256': sha(root/'scoring/pm-comment.json'), 'url': comment['url']}
    shutil.copyfile(HERE/'pm-reconfirmation-comment.json', root/'scoring/pm-reconfirmation-comment.json')
    reconfirmation = read(HERE/'pm-reconfirmation-comment.json')
    reconfirmation_ev = {'path': 'scoring/pm-reconfirmation-comment.json', 'sha256': sha(root/'scoring/pm-reconfirmation-comment.json'), 'url': reconfirmation['url']}
    m['files'].append({'path': ev['path'], 'sha256': ev['sha256']})
    # 기존 세션의 unknown을 덮어쓰지 않고 사용자 확인을 추가 정보로 연결한다.
    model_note = {'model_name': 'GPT-6 Astra', 'reasoning_effort': 'Medium', 'display_name': 'GPT-6 Astra(Medium)',
                  'source': '2026-11-24 사용자 지시: Codex 모델 명은 GPT-6 Astra(Medium)',
                  'screen_verified_by_codex': False, 'applies_to_session_id': m['evaluation_sessions'][0]['session_id']}
    m['evaluation_sessions'][0]['user_reported_model_addendum'] = model_note
    write(result_dir/'model-confirmation.json', model_note)
    old_e = data['evaluations']; old_d = data['defects']
    arts = {a['artifact_id']: a for a in m['artifacts']}
    coverage = {c['source_key']: c for c in read(INITIAL/'visual-coverage.json')}
    revisions, new_defects, reviews = [], [], []
    reviewer = 'i-dAb-i (PM; 사용자 지정 검수 코멘트)'
    at = comment['updated_at']
    def review(e, patch, reason, defect=None, scope=None, trigger='PM 코멘트 검수'):
        if defect:
            previous_id=defect.get('supersedes_defect_record_id',defect['defect_record_id'])
            ai_value=next((d for d in old_d if d['defect_record_id']==previous_id),None)
        else:
            previous_id=e['supersedes_evaluation_id'] or e['evaluation_id']
            ai_value=next((v for v in old_e if v['evaluation_id']==previous_id),None)
        row = dict(evaluation_run_id=m['evaluation_run_id'], review_id=f'PM155-R{len(reviews)+1:03}',
                   evaluation_id=e['evaluation_id'], defect_record_id=defect['defect_record_id'] if defect else None,
                   source_key=e['source_key'], checkpoint=e['checkpoint'], reference_unit_id=e['reference_unit_id'],
                   review_trigger=trigger, review_scope=scope or e['reference_unit_id'] or 'original', reviewer_id=reviewer,
                   reviewed_at=at, ai_value=copy.deepcopy(ai_value), reviewed_value=patch,
                   reason=reason, evidence_refs=[ev], supersedes_review_id=None)
        reviews.append(row)
    def location(source, cp, ys):
        c = coverage[source]
        ids = c['preview_artifact_ids'] if cp == 'machine_snapshot' else [c['final_artifact_id']]
        manifestations=[]; top=0
        for aid in ids:
            a=arts[aid]
            for lo, hi in ys:
                y0=max(top,lo);y1=min(top+a['height'],hi)
                if y0<y1: manifestations.append({'artifact_id':aid,'bbox':[0,y0-top,a['width'],y1-top],'original_y_offset':top})
            top+=a['height']
        assert manifestations
        return {'artifact_id':manifestations[0]['artifact_id'],'bbox':manifestations[0]['bbox'],
                'y_ranges':ys,'coordinate_system':'bbox=artifact local; y_ranges=원본 픽셀 문맥 영역', 'manifestations':manifestations}
    # PM이 최초 26개 결함의 심각도에 동의한 기록. 원인 단계 승인은 추론하지 않는다.
    for d in old_d:
        e=next(e for e in old_e if e['evaluation_id']==d['evaluation_id'])
        review(e, {'severity': d['severity'], 'valid': True}, 'PM 기존 결함·심각도 동의. 최초 원인 단계 확정으로 확대하지 않음.',d,scope='defect')
    affected={'GS-01-IMG-002','GS-02-IMG-003','GS-02-IMG-014'}
    latest_map={e['evaluation_id']:e for e in old_e}
    for old in old_e:
        if old['metric_id'] not in ('H02','H03') or old['source_key'] not in affected:
            continue
        e=copy.deepcopy(old); e['evaluation_id'] += '-PM1'; e['supersedes_evaluation_id']=old['evaluation_id']
        e['evaluated_at']=at; e['reason']='PM 검수 근거로 결함 연결을 확장한 기록 개정. 새로운 Codex 이미지 재평가 아님. 상태·판정 변경은 human_review에 연결하며 최초 판단은 supersedes_evaluation_id 참조.'
        e['recorded_by']='Codex 기록 변환 / PM 판정'; e['evidence_refs'].append(ev);e['defect_record_ids']=[]
        revisions.append(e);latest_map[old['evaluation_id']]=e
        source=e['source_key'];cp=e['checkpoint']
        for od in [d for d in old_d if d['evaluation_id']==old['evaluation_id']]:
            d=copy.deepcopy(od);d['defect_record_id'] += '-PM1';d['evaluation_id']=e['evaluation_id'];d['evidence_refs'].append(ev)
            d['supersedes_defect_record_id']=od['defect_record_id'];d['recorded_by']=reviewer+' / Codex 기록 변환'
            if source=='GS-01-IMG-002' and d['root_defect_id'].endswith('-layout'):
                d['location']=location(source,cp,[[0,3200],[5808,6774],[8000,10671],[18400,22000],[22303,24460],[27000,31000],[33684,36365]])
                d['observed']='기존 제목·본문·배지·라인업 겹침과 비건 설명 잘림 유지. 수치 배지 15.92%/6.78% 판독 실패는 별도 critical로 분리해 중복 가산하지 않음. PM 추가: K01 Protection Factor가 SPF/PA 값 칸에 겹침(값은 읽힘); K02 Sunscreen과 To support healthy marine ecosystems 겹침; K03 goodal/Good for All of Us, Continuous Commitment/설명, Free of 18 Suspicious Ingredients/설명·각주 겹침; 목록 8) o-Cymen-5-ol·9) Sorbic Acid가 02. Plasticizers에 가려 판독 불가; sulfate surfactants 각주와 04. Sunscreen Agents 겹침.'
                d['reference_unit_ids']=sorted(set(d['reference_unit_ids']+['GS-01_002-REF-'+n for n in ('037','040','041','042')]))
                d['h04_failure_separated_roots']=['GS-01-IMG-002-badge-020','GS-01-IMG-002-badge-025']
            if source=='GS-02-IMG-003':
                d['observed']='큰 제목 #1 Amazon Best의 Seller가 이미지 밖으로 잘림. Performance/제품명 겹침의 판독 실패는 별도 name-readability critical로 분리. 기존 major 심각도 유지.'
                d['h04_failure_separated_roots']=['GS-02-IMG-003-name-readability']
            if d['root_defect_id'] in ('GS-01-IMG-002-layout', 'GS-02-IMG-003-layout'):
                removed = {'GS-01_002-REF-020', 'GS-01_002-REF-025'} if source == 'GS-01-IMG-002' else {'GS-02_003-REF-001'}
                d['reference_unit_ids'] = [rid for rid in d['reference_unit_ids'] if rid not in removed]
                d['evidence_refs'].append(reconfirmation_ev)
            new_defects.append(d);e['defect_record_ids'].append(d['defect_record_id'])
            review(e,{'severity':d['severity'],'valid':True},'PM 동의 판정을 결함 연결 개정에 유지. 위치 보완은 동일 root로 연결.',d,scope='defect')
        specs=[]
        if source=='GS-01-IMG-002':
            specs=[('badge-020','readability_or_layout','critical',[[12800,13800]],['GS-01_002-REF-020'],'15.92%가 두 원으로 갈라진 배지에 가려 1 · 92만 남아 판독 불가.','15.92%를 실제 이미지에서 판독할 수 있어야 함.','undetermined'),
                   ('badge-025','readability_or_layout','critical',[[19100,20700]],['GS-01_002-REF-025'],'6.78%가 같은 모양의 배지에 가려 6. · %만 남아 판독 불가.','6.78%를 실제 이미지에서 판독할 수 있어야 함.','undetermined'),
                   ('irritation','translation_meaning_error','major',[[24460,25308]],['GS-01_002-REF-039'],'저자극 순한 선크림이 gentle, hypoallergenic sunscreen으로 번역됨(block 188).','자극과 알레르기성의 의미를 바꾸지 않음.','번역·현지화')]
        elif source=='GS-02-IMG-003':
            specs=[('name-readability','readability_or_layout','critical',[[200,400]],['GS-02_003-REF-001'],'Performance가 Retinal 위에 겹쳐 원본 없이 제품명을 확정할 수 없음.','제품명을 실제 이미지에서 판독할 수 있어야 함.','undetermined')]
        else:
            specs=[('rice-name','translation_naturalness','minor',[[3150,3400]],['GS-02_014-REF-006'],'쌀알 크기를 size of an Oryza Sativa (Rice) Kernel로 표현. 의미는 맞으나 사용법에 불필요한 학명.','쌀알 크기라는 이해하기 쉬운 사용법 표현. 기존 H04 의미 정확성 판정은 유지.','번역·현지화')]
        for key,typ,severity,ys,rids,observed,expected,origin in specs:
            rootid=source+'-'+key;did=rootid+'-'+cp+'-PM1'
            d=dict(evaluation_run_id=m['evaluation_run_id'],defect_record_id=did,defect_id=rootid,evaluation_id=e['evaluation_id'],recorded_by=reviewer+' / Codex 기록 변환',case_id=source[:5],source_key=source,checkpoint=cp,reference_unit_ids=rids,defect_type=typ,severity=severity,location=location(source,cp,ys),observed=observed,expected=expected,evidence_refs=e['evidence_refs'],origin_stage=origin,detected_stage='N5' if cp=='machine_snapshot' else 'N6',root_defect_id=rootid,resolution_status='unresolved',correction_evidence_refs=[])
            new_defects.append(d);e['defect_record_ids'].append(did)
            review(e,{'severity':severity,'valid':True},'PM 코멘트의 신규 판독 실패/신규 결함 판정.',d,scope='defect')
        if source=='GS-02-IMG-003':
            review(e,{'evaluation_status':'measured','observed_value':{'coverage_complete':True}},'PM이 제품명 판독 불가 critical로 확정. 원본 상태를 measured로 전환하며 등급은 기존 집계기가 계산.')
    # H04 개별 단위에만 적용. 다른 H04 항목을 PM 검수 완료로 일괄 표시하지 않는다.
    changed={'GS-02_003-REF-001','GS-01_002-REF-020','GS-01_002-REF-025'}
    kept={'GS-02_009-REF-015','GS-02_009-REF-018'}
    db=read(root/'generation/evidence/GS-02.json')
    name_block=next(b for b in db['text_block'] if b['id']==421)
    assert name_block['trans_1']=='celimax Retinal Shot Booster'
    for e in old_e:
        rid=e['reference_unit_id']
        if e['metric_id']=='H04' and rid in changed:
            obs={'readable':False,'pm_reason':'PM: 원본 도움 없이 해당 정보를 판독할 수 없음.'}
            if rid=='GS-01_002-REF-020':obs['text']='PM: 배지가 두 원으로 갈라져 1 · 92만 남아 15.92% 판독 불가.'
            if rid=='GS-01_002-REF-025':obs['text']='PM: 같은 방식으로 6. · %만 남아 6.78% 판독 불가.'
            reason='PM 판독 불가 → H04 실패. 정확성은 기존 값 유지.'
            if rid=='GS-02_003-REF-001':
                # PM 판정은 판독 실패. 기존 null을 bool로 채우는 근거/작성자를 명시한다.
                obs.update(accurate=True,accuracy_basis='Codex 기록 변환: 보존 block 421 trans_1=celimax Retinal Shot Booster. 텍스트 값 일치이며 이미지에서 읽힌다는 뜻 아님.',text='PM: Performance가 Retinal에 겹쳐 제품명을 판독할 수 없음.')
                reason='PM은 판독 불가를 확정. 정확성 bool은 보존 번역값 일치로 기록 변환자가 보완; readable=False이므로 보존 실패.'
            review(e,{'evaluation_status':'measured','observed_value':obs},reason)
        elif e['metric_id']=='H04' and rid in kept:
            review(e,{'observed_value':{'readable':True}},'PM 저대비 수치 판독 가능, 보존 유지.')
        elif e['source_key']=='GS-02-IMG-012' and e['metric_id'] in ('H02','H03'):
            review(e,{'evaluation_status':'measured','observed_value':{'coverage_complete':True}},'PM 추천 표본 방식 채택, GS-02_012 전체 원본 A 유지.',trigger='A등급 표본 검수')
    for region, sid, box in protocol.REGIONS:
        source=protocol.source_key(sid)
        for cp in protocol.CHECKPOINTS:
            original=next(e for e in old_e if e['source_key']==source and e['checkpoint']==cp and e['metric_id'] in ('H02','H03'))
            e=latest_map[original['evaluation_id']]
            status='K01~K03 추가 위치는 기존 layout 개정에 반영' if region in ('K01','K02','K03') else '정상 전체 제외 동의' if region=='K04' else 'N2 실패·미생성 예외 동의' if region in ('K07','K08','K09') else '해당 K영역 판정 동의'
            review(e,{'evaluation_status':e['evaluation_status']},status,scope=region,trigger=region+' 영역 검수; 전체 원본 검수로 확대하지 않음')
    all_e=old_e+revisions;all_d=old_d+new_defects
    lines(root/'evaluations.jsonl',all_e);lines(root/'defects.jsonl',all_d)
    with (root/'human_review.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=protocol.FIELDS['human_review'].split(),lineterminator='\n');w.writeheader()
        for r in reviews:w.writerow({k:protocol.cell(r[k]) for k in w.fieldnames})
    write(root/'manifest.json',m)
    assert all(sha(src/p)==h and sha(root/p)==h for p,h in fixed.items())
    # aggregate가 원본·개정·검수 참조와 해시를 검증한다. 공식 계산기는 수정하지 않는다.
    result=protocol.aggregate(root)
    _, combined=protocol.load(root)
    latest, dm=protocol.adopted(combined)
    metrics=[r for r in result['metrics'] if r['metric_id'] in ('H01','H02','H03','H04') or (r['metric_id'] is None and r['scope'] in ('all','case'))]
    basis={}
    for r in metrics:
        b=r.pop('aggregation_basis');key=hashlib.sha256(json.dumps(b,sort_keys=True).encode()).hexdigest();basis[key]=b;r['aggregation_basis_ref']='aggregation-basis.json#'+key
    def metric(mid,cp,label):return next(r for r in metrics if r['scope']=='all' and r['metric_id']==mid and r['checkpoint']==cp and r['breakdown']==label)
    for mid,cp in [('H02','machine_snapshot'),('H03','service_final')]:
        for label,n in [('A',1),('B',8),('C',4)]:
            r=metric(mid,cp,label);assert (r['numerator'],r['denominator'])==(n,13)
        r=metric('H04',cp,'preserved');assert (r['numerator'],r['denominator'])==(105,111)
        assert r['no_output_count']==67 and r['not_applicable_count']==21 and r['undetermined_count']==0
    assert metric('H01','service_final','deliverable')['numerator']==13
    active=[]
    for e in latest.values():
        if e['metric_id'] in ('H02','H03'):
            _, defs=protocol.grade([d for d in dm.values() if d['evaluation_id']==e['evaluation_id']]);active.extend(defs)
    unique={d['root_defect_id']:d for d in active}
    assert len(active)==62 and len(unique)==31
    assert Counter(d['severity'] for d in unique.values())=={'critical':8,'major':20,'minor':3}
    lines(result_dir/'evaluation-additions.jsonl',revisions);lines(result_dir/'defect-additions.jsonl',new_defects)
    shutil.copyfile(root/'human_review.csv',result_dir/'human_review.csv')
    adopted=[]
    for e in latest.values():
        entry=copy.deepcopy(e)
        entry['pm_review_refs']=[{'review_id':r['review_id'],'scope':r['review_scope'],'reason':r['reason']} for r in reviews if r['evaluation_id']==e['evaluation_id'] and not r['defect_record_id']]
        entry['reason_provenance']='reason은 해당 평가 기록 작성 당시 근거. PM 변경은 observed_value 및 pm_review_refs/human_review에서 구분.'
        adopted.append(entry)
    lines(result_dir/'adopted-evaluations.jsonl',adopted);lines(result_dir/'adopted-defects.jsonl',active)
    lines(result_dir/'metrics.jsonl',metrics);write(result_dir/'aggregation-basis.json',basis)
    fields=['metric_id','scope','case_id','source_key','checkpoint','breakdown','numerator','denominator','value','unit','applicable_count','measured_count','undetermined_count','no_output_count','not_applicable_count','evaluation_failed_count','human_checked_count','ai_only_count']
    with (result_dir/'metrics.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore',lineterminator='\n');w.writeheader()
        for r in metrics:w.writerow({**r,'breakdown':json.dumps(r['breakdown'],ensure_ascii=False) if isinstance(r['breakdown'],dict) else r['breakdown']})
    candidates=read(INITIAL/'a-candidates.json')
    for c in candidates:
        c['selection_status']='PM 추천안 채택·검수 완료';c['pm_evidence_url']=comment['url']
        c['sort_keys']={s:hashlib.sha256(('gs-v1|baseline-a-sample-v1|'+s).encode()).hexdigest() for s in c['candidates']}
        c['selected']=sorted(c['candidates'],key=lambda s:(c['sort_keys'][s],s))[:1]
        c['review_result']='A 유지' if c['selected'] else 'A 대상 없음; GS-03은 결과 없음'
    write(result_dir/'a-sample-review.json',candidates)
    write(result_dir/'issues.json',{'EI-01':{'status':'평가 이슈 유지','quality_defect_added':False},'EI-02':{'status':'PM 문제없음','quality_defect_added':False},'glossary_followup':{'in_scope':False,'request':'최신 버전의 콩·쌀 강제 용어가 콩알·쌀알 등 단어 내부에 매칭되는지, 보존 glossary_ids 빈 값으로 확인하지 못한 최초 원인을 별도 조사','investigation_performed':False},'pm_evidence_url':comment['url']})
    write(result_dir/'validation.json',{'issues':[],'expected_counts_match':True,'initial_file_hashes':initial_hashes,'initial_files_unchanged':all(sha(INITIAL/p)==h for p,h in initial_hashes.items()),'protected_input_paths_unchanged':len(fixed),'evaluation_additions':len(revisions),'defect_additions':len(new_defects),'human_review_records':len(reviews),'adopted_evaluations':len(latest),'active_defect_records':len(active),'unique_defect_roots':len(unique),'by_severity':dict(Counter(d['severity'] for d in unique.values())),'formal_complete':False,'pm_reconfirmation_pending':True,'model_api_calls':0})
    # 명시적 판정 범위만 human_checked로 표시한 파생 표.
    def esc(x):return str(x).replace('|','\\|').replace('\n','<br>')
    table=['# PM 반영 H04 비교표','','두 체크포인트의 동일 결과는 한 행으로 표시. PM이 명시적으로 판정한 항목만 PM 표시하며 나머지는 최초 판단 유지.','','| 기준 ID | 기대값 | 정확성 | 판독성 | 상태 | PM 단위 검수 |','|---|---|---|---|---|---|']
    for e in latest.values():
        if e['metric_id']=='H04' and e['checkpoint']=='service_final':
            o=e['observed_value'] or {};table.append(f"| {e['reference_unit_id']} | {esc(e['expected_value']['text'])} | {o.get('accurate','—')} | {o.get('readable','—')} | {e['evaluation_status']} | {bool(e.get('human_checked'))} |")
    (result_dir/'h04-review-table.md').write_text('\n'.join(table)+'\n',encoding='utf-8',newline='\n')
    table=['# PM 반영 결함 목록','','동일 root의 양 체크포인트 관찰은 한 행으로 표시. 최초 이력·개정 이력은 별도 보존.','','| root ID | 심각도 | 원인 단계 | 관찰 |','|---|---|---|---|']
    for d in unique.values():table.append(f"| {d['root_defect_id']} | {d['severity']} | {d['origin_stage']} | {esc(d['observed'])} |")
    (result_dir/'defect-inventory.md').write_text('\n'.join(table)+'\n',encoding='utf-8',newline='\n')
    table=['# PM 반영 원본별 결과','','| 원본 | H02 | H03 | H04 최초/최종 보존 |','|---|---|---|---|']
    for s in m['source_keys']:
        grades=[]
        for mid,cp in [('H02','machine_snapshot'),('H03','service_final')]:
            e=latest[(s,cp,mid,None)];grades.append(protocol.grade([d for d in active if d['evaluation_id']==e['evaluation_id']])[0] if e['evaluation_status']=='measured' else e['evaluation_status'])
        all_facts=[e for e in latest.values() if e['source_key']==s and e['checkpoint']=='service_final' and e['metric_id']=='H04']
        facts=[e for e in all_facts if e['evaluation_status']=='measured']
        good=sum(e['observed_value']['accurate'] and e['observed_value']['readable'] for e in facts)
        zero_reason='해당 대상 없음' if all(e['evaluation_status']=='not_applicable' for e in all_facts) else '정상 측정 0건'
        table.append(f'| {s} | {grades[0]} | {grades[1]} | {good}/{len(facts)}'+(' ('+zero_reason+')' if not facts else '')+' |')
    (result_dir/'source-results.md').write_text('\n'.join(table)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'H02_H03':'A1 B8 C4 /13','H04':'105/111','reviews':len(reviews),'defect_roots':len(unique),'issues':[]},ensure_ascii=False))


if __name__=='__main__':main()
