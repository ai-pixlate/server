"""확인된 운영 증거만 집계한다. 제품 상태·품질 판정을 새로 만들지 않는다."""
import math
import statistics
import unicodedata
from .scope import automatic_only, OP06_NOTE

EVENTS = {
 'job_created': (), 'job_saved': (), 'first_download': (),
 'stage_started': (), 'stage_finished': ('seconds',), 'stage_stopped': (),
 'human_wait': ('seconds',), 'first_n5_confirmed': ('original', 'final'),
 'regulation_checked': ('target_id', 'measured', 'findings', 'auto_excluded', 'locale_unchecked'),
 'spec_checked': ('target_id', 'passed'),
 'resource': ('name', 'amount', 'unit'),
 'fallback': ('kind', 'target_id', 'eligible', 'applied'),
}


def normalized(text):
    return unicodedata.normalize('NFC', text.replace('\r\n', '\n'))


def check(row):
    event, values = row['event'], row['values']
    if event not in EVENTS or not isinstance(values, dict) or any(k not in values for k in EVENTS[event]):
        raise ValueError('운영 증거 유형/필드 오류')
    if row['job_id'] is None:
        raise ValueError('운영 증거의 작업 연결 필요')
    if event.startswith('stage_') and row['stage'] not in ('N2', 'N4', 'N6'):
        raise ValueError('시스템 처리 단계 오류')
    for k in ('seconds', 'amount'):
        if k in values and (type(values[k]) not in (int, float) or not math.isfinite(values[k]) or values[k] < 0):
            raise ValueError('유효한 비음수 측정값 필요')
    for k in ('measured', 'auto_excluded', 'locale_unchecked', 'passed', 'eligible', 'applied'):
        if k in values and type(values[k]) is not bool:
            raise ValueError('운영 판정 bool 필요')
    if event == 'fallback' and values['kind'] not in ('원본 전체 섹션 fallback','원본 배경 fallback','기본 스타일 사용','부분 번역 진행'):
        raise ValueError('fallback 종류 오류')
    if event == 'regulation_checked' and (type(values['findings']) is not int or values['findings'] < 0):
        raise ValueError('규제 판정 건수 오류')
    if event == 'first_n5_confirmed':
        a, b = values['original'], values['final']
        if not isinstance(a, dict) or not isinstance(b, dict) or a.keys() != b.keys() or any(not isinstance(x, str) for x in [*a.values(), *b.values()]):
            raise ValueError('최초 N5 전체 번역 맵 필요')


def aggregate(m, execution, metric):
    records = [r for r in execution if r['record_kind'] == 'operation']
    result = []
    def emit(mid, label, numerator, denominator, n, unit='%', unknown=False):
        row = metric(m, mid, None, label, ['measured'] * n if not unknown else ['undetermined'] * max(n, 1), numerator, denominator, unit, False)
        if unknown: row.update(value=None, display_reason='운영 증거 미확인')
        result.append(row)
    def select(event): return [r for r in records if r['event'] == event]
    jobs = {r['job_id'] for r in select('job_created')}
    completed = jobs & {r['job_id'] for r in records if r['event'] in ('job_saved', 'first_download')}
    emit('OP01', '작업 완료율', len(completed), len(jobs), len(jobs), unknown=not jobs)
    for stage in ('N2', 'N4', 'N6'):
        for event, label in (('stage_started', '도달'), ('stage_finished', '완료'), ('stage_stopped', '중단')):
            reached = {r['job_id'] for r in select(event) if r['stage'] == stage}
            emit('OP01', stage + label, len(reached & jobs), 1, len(jobs), 'count', not jobs)
            if event == 'stage_started': emit('OP01', stage + '도달률', len(reached & jobs), len(jobs), len(jobs), unknown=not jobs)
    times = {}
    for r in select('stage_finished'):
        key = (r['job_id'], r['stage'])
        if key in times: raise ValueError('중복 단계 시간 증거')
        times[key] = r['values']['seconds']
        emit('OP04', {'job_id': r['job_id'], 'stage': r['stage']}, times[key], 1, 1, 'seconds')
    totals = {j: sum(times[j, s] for s in ('N2','N4','N6')) for j in jobs if all((j,s) in times for s in ('N2','N4','N6'))}
    for stage in ('N2', 'N4', 'N6', '전체'):
        vals = list(totals.values()) if stage == '전체' else [v for (j,s),v in times.items() if s == stage]
        for label, value in (('평균', statistics.mean(vals) if vals else 0), ('중앙값', statistics.median(vals) if vals else 0)):
            emit('OP04', stage+label, value, 1, len(vals), 'seconds', not vals)
    waits=select('human_wait')
    emit('OP04', '사람 대기시간 합계', sum(r['values']['seconds'] for r in waits), 1, len(waits), 'seconds', not waits)
    approvals={}
    for r in select('first_n5_confirmed'):
        if r['job_id'] in approvals: raise ValueError('최초 N5 확정 증거 중복')
        approvals[r['job_id']] = r['values']
    target = completed & approvals.keys()
    unchanged = sum(all(normalized(v['original'][k]) == normalized(v['final'][k]) for k in v['original']) for j,v in approvals.items() if j in target)
    emit('OP06', '무수정 승인률', unchanged, len(target), len(target), unknown=bool(completed-approvals.keys()) or not jobs)
    if automatic_only(m):
        prior = result[-1]['display_reason']
        result[-1]['display_reason'] = (prior + '; ' if prior else '') + OP06_NOTE
        result[-1]['include_in_presentation'] = False
    regs = select('regulation_checked'); specs=select('spec_checked')
    for group in (regs, specs):
        if len({(r['job_id'],r['values']['target_id']) for r in group}) != len(group): raise ValueError('운영 대상 중복')
    measured=[r for r in regs if r['values']['measured']]
    emit('OP07','규제 판정 통과율',sum(r['values']['findings']==0 for r in measured),len(measured),len(regs),unknown=not regs)
    for k in ('auto_excluded','locale_unchecked'):
        emit('OP07',k,sum(r['values'][k] for r in regs),1,len(regs),'count',not regs)
    emit('OP07','규제 판정 실패 섹션',len(regs)-len(measured),1,len(regs),'count',not regs)
    emit('OP07','규격 검증 통과율',sum(r['values']['passed'] for r in specs),len(specs),len(specs),unknown=not specs)
    resources=select('resource')
    for name,unit in sorted({(r['values']['name'],r['values']['unit']) for r in resources}):
        chosen=[r for r in resources if (r['values']['name'],r['values']['unit'])==(name,unit)]
        emit('OP05',name,sum(r['values']['amount'] for r in chosen),1,len(chosen),unit)
    usage=[r for r in execution if r['record_kind']=='usage' and r.get('purpose')=='product_generation']
    costs=[r for r in usage if isinstance(r.get('cost'),dict) and type(r['cost'].get('amount')) in (int,float)]
    for currency in sorted({r['cost']['currency'] for r in costs}):
        chosen=[r for r in costs if r['cost']['currency']==currency]
        emit('OP05','토큰 단가 추정 원가',sum(r['cost']['amount'] for r in chosen),1,len(chosen),currency,len(chosen)!=len(usage))
    if not costs: emit('OP05','비용',0,0,0,'currency',True)
    for currency in sorted({r['cost']['currency'] for r in costs}):
        per_job={j:sum(r['cost']['amount'] for r in costs if r['job_id']==j and r['cost']['currency']==currency) for j in jobs}
        complete_cost_jobs={j for j in jobs if any(r['job_id']==j for r in usage) and all(isinstance(r.get('cost'),dict) and r['cost'].get('currency')==currency for r in usage if r['job_id']==j)}
        vals=[per_job[j] for j in complete_cost_jobs]
        for j in complete_cost_jobs:
            emit('OP05',{'job_id':j,'result':'추정 작업 원가'},per_job[j],1,1,currency)
        emit('OP05','추정 작업 원가 P50',statistics.median(vals) if vals else 0,1,len(vals),currency,not vals)
        emit('OP05','P90 산출 방식 미정',0,0,len(vals),currency,True)
        done=completed & complete_cost_jobs
        emit('OP05','완료 건당 추정 원가',sum(per_job[j] for j in done),len(done),len(done),currency,done!=completed)
        section_counts={r['job_id']:r['values']['amount'] for r in resources if r['values']['name']=='n2_sections'}
        for j in complete_cost_jobs & section_counts.keys():
            emit('OP05',{'job_id':j,'result':'섹션당 추정 원가'},per_job[j],section_counts[j],section_counts[j],currency)
    emit('OP05','실제 청구액',0,0,0,'currency',True)
    for label in ('원본 전체 섹션 fallback','원본 배경 fallback','기본 스타일 사용','부분 번역 진행'):
        relevant=[r for r in select('fallback') if r['values']['kind']==label]
        if len({(r['job_id'],r['values']['target_id']) for r in relevant})!=len(relevant): raise ValueError('fallback 대상 중복')
        eligible=[r for r in relevant if r['values']['eligible']]
        emit('OP03',label,sum(r['values']['applied'] for r in eligible),len(eligible),len(relevant),unknown=not relevant)
    return result
