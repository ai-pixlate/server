"""보존 결과만 읽는 통합 집계. 모델·OCR·제품 실행과 공식 채점은 수행하지 않는다."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys

REPO = Path(__file__).resolve().parents[4]
BASE = REPO / 'docs/ai-baseline'
RUN = BASE / 'runs/baseline-v1-0-1-gs-v1-20261030-01'
H = BASE / 'evaluations/h01-h04-official-20261124/pm-review-20261124'
OCR = BASE / 'evaluations/ocr-v6-v1-0-1-20261110/results'
sys.path.insert(0, str(REPO))
from tools.baseline import operations


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tq03-root', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('기존 결과를 보존하도록 새 출력 디렉터리를 지정하세요.')
    inputs = {}
    def read(path, lines=False):
        raw = path.read_bytes()
        key = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else 'TQ03_ROOT/' + path.relative_to(args.tq03_root).as_posix()
        inputs[key] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        text = raw.decode('utf-8-sig')
        return [json.loads(s) for s in text.splitlines()] if lines else json.loads(text)
    def dump(name, obj):
        (args.output/name).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')

    execution = read(RUN/'execution.jsonl', True)
    usage = read(RUN/'usage.jsonl', True)
    manifest = read(RUN/'manifest.json')
    read(RUN/'runtime.json')
    read(RUN/'artifacts/pricing-verification.json')
    completion = read(RUN/'artifacts/completion-verification.json')
    hm = read(H/'metrics.jsonl', True)
    hv = read(H/'validation.json')
    assert hv['formal_complete'] and not hv['pm_reconfirmation_pending']
    approval = read(H/'pm-approval.json')
    for mid,cp in [('H02','machine_snapshot'),('H03','service_final')]:
        for grade,n in [('A',1),('B',8),('C',4)]:
            r=next(r for r in hm if r['scope']=='all' and r['metric_id']==mid and r['breakdown']==grade)
            assert (r['numerator'],r['denominator'])==(n,13)
        r=next(r for r in hm if r['scope']=='all' and r['metric_id']=='H04' and r['checkpoint']==cp and r['breakdown']=='preserved')
        assert (r['numerator'],r['denominator'])==(105,111)
    defects = read(H/'adopted-defects.jsonl', True)
    ocr = read(OCR/'summary.json')
    sections = read(OCR/'sections.jsonl', True)
    assert len(sections)==112
    for group in ['all','GS-01','GS-02','GS-03']:
        rows = [r for r in sections if r['status']=='measured' and (group=='all' or r['group']==group)]
        totals = {k:sum(r['metrics'][k] for r in rows) for k in ('S','D','I','N')}
        expected = ocr['overall' if group=='all' else 'groups']
        if group!='all': expected=expected[group]
        assert totals==expected['measured_totals']

    # 기존 확정 audit 코드로 원시 표지 점수를 대조한다. 추론 호출 없음.
    audit_path = BASE/'analyses/tq03-registration-20261124/audit.py'
    spec = importlib.util.spec_from_file_location('existing_tq03_audit', audit_path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    tq_audit = mod.audit(args.tq03_root)
    for name in mod.EXPECTED:
        p = args.tq03_root/name
        raw=p.read_bytes();inputs['TQ03_ROOT/'+name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    tq_summary = read(args.tq03_root/'received-20261121/summary.json')
    tq_results = read(args.tq03_root/'received-20261121/results.jsonl', True)
    excluded=read(args.tq03_root/'excluded-blocks.jsonl',True)
    unavailable=read(args.tq03_root/'unavailable-images.jsonl',True)
    assert len(excluded)==333 and len(unavailable)==18
    exclusions={case:dict(Counter(r['translation_status'] for r in excluded if case=='all' or r['product_id']==case)) for case in ['all','GS-01','GS-02','GS-03']}
    tq_groups = {}
    for case in ['all','GS-01','GS-02','GS-03']:
        rows = [r for r in tq_results if case=='all' or r['product_id']==case]
        scores = [r['reported_score'] for r in rows if r['status'] in ('ok','format_error') and r.get('reported_score') is not None]
        tq_groups[case]={'targets':len(rows),'scored':len(scores),'missing':len(rows)-len(scores),'sum':sum(scores),'mean':statistics.mean(scores) if scores else None,'distribution':dict(sorted(Counter(scores).items())),'status_counts':dict(Counter(r['status'] for r in rows))}
    assert (tq_groups['all']['targets'],tq_groups['all']['scored'],tq_groups['all']['sum'])==(491,449,970)

    # 기존 operations 집계기를 그대로 호출. 지표별 증거 행 ID를 별도 연결한다.
    op_rows=[]
    def metric(m, mid, cp, label, states, num, den, unit='%', *unused):
        return dict(metric_id=mid,case=m['case_id'],label=label,numerator=num,denominator=den,value=(num/den*(100 if unit=='%' else 1)) if den else None,unit=unit,observed_records=len(states),display_reason=None)
    attempts=[r for r in execution if r['record_kind']=='attempt' and not r.get('result_only')]
    logical=defaultdict(list)
    for r in attempts:logical[r['logical_execution_id']].append(r)
    assert len(attempts)==478 and len(logical)==476
    logical_rows=[]
    for key,rows in sorted(logical.items()):
        final=[r for r in rows if r['is_final_attempt']]
        assert len(final)==1
        f=final[0]
        logical_rows.append(dict(logical_id=key,case=f['case_id'],stage=f['stage'],target_id=f['target_id'],status=f['execution_status'],attempts=len(rows),auto_retries=sum(r.get('retry_origin')=='auto' for r in rows),error_codes=sorted({c for r in rows for c in r['error_codes']}),record_ids=[r['record_id'] for r in rows]))
    details={}
    for case in ['all','GS-01','GS-02','GS-03']:
        es=[r for r in execution if case=='all' or r['case_id']==case]
        mm={**manifest,'case_id':case}
        for r in es:
            if r['record_kind']=='operation':operations.check(r)
        op_rows.extend(operations.aggregate(mm,es,metric))
        ls=[r for r in logical_rows if case=='all' or r['case']==case]
        for status in ('success','recovered','degraded','blocked','no_output'):
            op_rows.append(metric(mm,'OP02',None,status,['measured']*len(ls),sum(r['status']==status for r in ls),len(ls)))
        op_rows.append(metric(mm,'OP03',None,'자동 재시도 발생률',['measured']*len(ls),sum(r['auto_retries']>0 for r in ls),len(ls)))
        bystage={stage:dict(Counter(r['status'] for r in ls if r['stage']==stage)) for stage in sorted({r['stage'] for r in ls})}
        error_jobs={code:sorted({r['job_id'] for r in attempts if (case=='all' or r['case_id']==case) and code in r['error_codes']}) for code in sorted({c for r in attempts for c in r['error_codes']})}
        events=defaultdict(list)
        for r in es:
            if r['record_kind']=='operation':events[r['event']].append(r['record_id'])
        edits=[r for r in es if r['record_kind']=='user_edit']
        details[case]={'user_edits':{stage:{'recorded_originals':sum(r['stage']==stage for r in edits),'changes':sum(r['change_count'] for r in edits if r['stage']==stage)} for stage in ('N3','N5')},'logical_units':len(ls),'status_counts':dict(Counter(r['status'] for r in ls)),'by_stage':bystage,'auto_retry_units':sum(r['auto_retries']>0 for r in ls),'auto_retry_attempts':sum(r['auto_retries'] for r in ls),'recovered':sum(r['status']=='recovered' for r in ls),'error_jobs':error_jobs,'operation_record_ids':dict(events)}
    retry_tasks=read(RUN/'evidence/GS-03.json')['job_async_task']
    assert any(r['stage']=='analyze' and r['is_current'] and r['status']=='failed' and r['retry_count']==r['max_retry']==2 for r in retry_tasks)
    details['all']['retry_limit_exhausted_jobs']=[3]
    details['all']['excluded_result_only_records']=len([r for r in execution if r.get('result_only')])
    assert details['all']['status_counts']=={'success':470,'blocked':6}

    # 예약 행과 최종 사용량 행은 동일 호출이다. 이중 합산 금지.
    budget=manifest['generation_config']['budget']
    assert budget['input_per_million']==0.75 and budget['output_per_million']==3.75
    calls=defaultdict(list)
    for r in usage:calls[r['call']].append(r)
    final_calls=[]
    for cid,rows in sorted(calls.items()):
        reserved=[r for r in rows if r['status']=='reserved'];done=[r for r in rows if r['status']!='reserved']
        assert len(reserved)==len(done)==1
        r=done[0];u=r.get('usage') or {}
        if r['cost'] is not None:
            calc=(Decimal(u['prompt_token_count'])*Decimal(str(budget['input_per_million']))+(Decimal(u['candidates_token_count'])+Decimal(u['thoughts_token_count']))*Decimal(str(budget['output_per_million'])))/Decimal(1000000)
            assert abs(calc-Decimal(str(r['cost'])))<Decimal('0.000000000001')
        final_calls.append({'call':cid,'case':r['source_id'],'purpose':r['task_context']['task'],'status':r['status'],'model':r['model'],'usage':u,'estimated_usd':r['cost'],'held_reservation_usd':reserved[0]['reserved_amount'] if r['cost'] is None else 0})
    costs={}
    for case in ['all','GS-01','GS-02','GS-03']:
        rows=[r for r in final_calls if case=='all' or r['case']==case]
        def subtotal(rs):
            estimate=sum(Decimal(str(r['estimated_usd'])) for r in rs if r['estimated_usd'] is not None)
            reserve=sum(Decimal(str(r['held_reservation_usd'])) for r in rs)
            tokens={k:{'known_sum':sum(r['usage'][k] for r in rs if r['usage'].get(k) is not None),'known_requests':sum(r['usage'].get(k) is not None for r in rs),'missing_requests':sum(r['usage'].get(k) is None for r in rs)} for k in ['prompt_token_count','candidates_token_count','thoughts_token_count','total_token_count']}
            return dict(requests=len(rs),priced_requests=sum(r['estimated_usd'] is not None for r in rs),unpriced_requests=sum(r['estimated_usd'] is None for r in rs),estimated_known_usd=str(estimate),held_reservation_usd=str(reserve),budget_occupancy_usd=str(estimate+reserve),actual_invoice_usd=None,tokens=tokens)
        costs[case]=subtotal(rows)
        costs[case]['by_purpose']={k:subtotal([r for r in rows if r['purpose']==k]) for k in sorted({r['purpose'] for r in rows})}
    assert completion['cost_calculable_requests']==372 and completion['unpriced_requests']==21
    assert costs['all']['requests']==393 and costs['all']['priced_requests']==372
    assert Decimal(costs['all']['estimated_known_usd'])==Decimal('2.1157485')
    assert Decimal(costs['all']['held_reservation_usd'])==Decimal('21.84')
    assert costs['all']['tokens']['total_token_count']['known_sum']==1243650

    times={}
    for case in ['GS-01','GS-02','GS-03']:
        es=[r for r in execution if r['case_id']==case]
        finished={r['stage']:r['values']['seconds'] for r in es if r.get('event')=='stage_finished'}
        stopped={r['stage']:r['values']['seconds'] for r in es if r.get('event')=='stage_stopped'}
        waits={r['stage']:r['values']['seconds'] for r in es if r.get('event')=='human_wait'}
        db=read(RUN/f'evidence/{case}.json')
        total=sum(finished.values()) if len(finished)==3 else None
        times[case]={'completed_stage_seconds':finished,'stopped_stage_seconds':stopped,'human_wait_seconds':waits,'completed_system_seconds':total,'input_originals':len(db['source_image']),'seconds_per_input_original':total/len(db['source_image']) if total is not None else None}
    unique={r['root_defect_id']:r for r in defects}
    common=[]
    for stage in sorted({r['origin_stage'] for r in unique.values()}):
        rows=[r for r in unique.values() if r['origin_stage']==stage]
        common.append({'origin_stage':stage,'severity':dict(Counter(r['severity'] for r in rows)),'affected_sources':sorted({r['source_key'] for r in rows}),'root_ids':[r['root_defect_id'] for r in rows]})
    assert len(unique)==31 and len(defects)==62
    for p in [Path(__file__), audit_path, REPO/'tools/baseline/operations.py', REPO/'tools/baseline/protocol.py', BASE/'protocols/v1/aggregation.md', BASE/'protocols/v1/tq03.md']:
        raw=p.read_bytes();inputs[p.relative_to(REPO).as_posix()]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    # 입력 불변을 확인하고 실행 시각 같은 가변값은 결과에 넣지 않는다.
    for key,v in inputs.items():
        p=args.tq03_root/key.removeprefix('TQ03_ROOT/') if key.startswith('TQ03_ROOT/') else REPO/key
        assert hashlib.sha256(p.read_bytes()).hexdigest()==v['sha256']
    args.output.mkdir(parents=True)
    dump('inputs.json',inputs)
    dump('h-results.json',{'approval':approval,'metrics':hm,'note':'PM 확정값 인용. 재채점 없음.'})
    dump('tq-results.json',{'TQ01':{'status':'부분 측정(고정 105개 완료)','legacy_id':'OCR01','overall':ocr['overall'],'by_case':ocr['groups'],'provenance':'최초 67·11/05 보완 3·11/09 보완 42. GS-03 공식 N2 실패와 별도.'},'TQ02':{'status':'보류','reason':'원문 충실도 측정 보류. 방법·분모 미확정'},'TQ03':{'status':'부분 측정','limitations':tq_audit['mandatory_limitations'],'audit':tq_audit,'by_case':tq_groups,'excluded_blocks':tq_summary['excluded_translation_blocks'],'exclusion_status_counts':exclusions,'unavailable_images':18,'license_status':'미확정','interpretation':'최종판과의 변화 비교용. 절대 성능·독립 검증 주장 불가.'},'TQ04':{'status':'보류','legacy_id':'OCR02'},'TQ05':{'status':'미측정','legacy_id':'OI-54','reason':'독립 평가셋의 정답·검출 결과 없음. OP07로 대체하지 않음.'},'TQ06':{'status':'미측정','legacy_id':'NFR-15','reason':'자동 측정법·분모·기준값 미확정'}})
    dump('op-results.json',op_rows);dump('operation-evidence.json',details)
    dump('logical-executions.json',logical_rows);dump('costs.json',costs);dump('model-calls.json',final_calls)
    dump('times.json',times);dump('common-defects.json',common)
    dump('validation.json',{'inputs_unchanged':True,'h_pm_formal_complete':True,'h_metrics_unchanged':True,'tq03_scores':449,'tq03_total':491,'tq03_sum':970,'ocr_error_characters':584,'ocr_reference_characters':16742,'logical_units':476,'result_only_excluded':34,'gs03_failed_jobs':1,'gs03_affected_originals':18,'model_calls_in_this_aggregation':0,'regeneration':False})
    print('보존 결과 집계·검증 완료:', args.output)


if __name__=='__main__':main()
