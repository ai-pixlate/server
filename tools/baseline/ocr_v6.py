"""v6 후보: 범위 검증과 문자 채점을 분리한다. 파일/모델 호출 없음."""
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import unicodedata
from .ocr import text_sequence
from .ocr_scope import filter_section, v5_inputs
from .ocr_v5 import score_lines

VERSION = 'ocr-v6-candidate-1'


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def implementation():
    return dict(version=VERSION, unicode=unicodedata.unidata_version,
                files={name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                       for name in ('ocr_v6.py', 'ocr_scope.py', 'ocr_v5.py', 'ocr.py')})


def prepare_section(scope, gt, output, links, *, metadata, gt_complete=False,
                    scope_approved=False, completeness_evidence=None):
    """필터/상태만 검증한다. S/D/I·CER·문자 대응을 호출하지 않는다.

    output=None은 실행 실패/출력 없음, []는 정상 빈 반환이다.
    승인/완전성은 외부 검수 근거이며 필터 통과로 자동 승격하지 않는다.
    """
    row = dict(section_id=scope.get('id'), group=metadata.get('group'),
               status='evaluation_failed', reasons=[], filter_ready=False,
               score_ready=False, metrics=None, metadata=metadata.copy(),
               implementation=implementation(),
               hashes={k: digest(v) for k,v in [('scope',scope),('gt',gt),
                       ('output',output),('links',links),('metadata',metadata)]})
    try:
        for key in ('group','input_version','truth_version','product_version','product_sha',
                    'image_sha256','truth_sha256','output_sha256','policy_sha256','spec_sha256'):
            if not isinstance(metadata.get(key),str) or not metadata[key]:
                raise ValueError('missing_metadata:'+key)
        if not scope.get('id'): raise ValueError('missing_section_id')
        filtered=filter_section(scope,gt,output if output is not None else [],
            image_sha256=metadata['image_sha256'],gt_sha256=metadata['truth_sha256'],gt_links=links)
        row.update(filter_ready=filtered['ready'], filter=filtered)
        reasons=list(filtered['blockers'])
        if not scope_approved: reasons.append('scope_approval_pending')
        if reasons:
            row.update(status='undetermined',reasons=reasons)
        elif filtered['applicability']=='not_applicable':
            row.update(status='not_applicable',reasons=['no_target_scope'])
        elif not gt_complete or not completeness_evidence:
            row.update(status='undetermined',reasons=['gt_completeness_unverified'])
        elif output is None:
            row.update(status='no_output',reasons=['usable_output_missing'])
        else:
            g,_=v5_inputs(filtered)
            if not text_sequence(g):
                # 빈 GT만으로 무문자 이미지를 확정하거나 삽입을 면제하지 않는다.
                row.update(status='undetermined',reasons=['zero_gt_requires_scope_confirmation'])
            else:
                row.update(status='ready',score_ready=True)
        row['completeness_evidence']=completeness_evidence
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        row.update(status='evaluation_failed',score_ready=False,reasons=[str(exc)])
    return row


def score_section(scope, gt, output, links, **kwargs):
    """명시 호출 전용. 매번 검증하며 저장된 ready 플래그를 신뢰하지 않는다."""
    row=prepare_section(scope,gt,output,links,**kwargs)
    if row['score_ready']:
        row['metrics']=score_lines(*v5_inputs(row['filter']))
        row['status']='measured'
    return row


def aggregate(rows, *, expected_ids):
    """누락/중복 거부. 측정 부분 집계와 전체 확정 여부를 분리한다."""
    ids=[r['section_id'] for r in rows]
    if len(expected_ids)!=len(set(expected_ids)) or len(ids)!=len(set(ids)) or set(ids)!=set(expected_ids):
        raise ValueError('section_coverage_mismatch')
    # 다른 crop/정답/정책을 한 결과로 섞지 않는다. GS별로도 동일한 키를 사용한다.
    keys={tuple(r['metadata'][k] for k in ('input_version','truth_version','truth_sha256','policy_sha256','spec_sha256',
                                         'product_version','product_sha'))+(digest(r['implementation']),) for r in rows}
    if len(keys)>1: raise ValueError('incompatible_evaluation_versions')
    def summarize(items):
        counts=dict(Counter(r['status'] for r in items))
        measured=[r['metrics'] for r in items if r['status']=='measured']
        totals={k:sum(m[k] for m in measured) for k in ('S','D','I','N')}
        n=totals['N']; errors=sum(totals[k] for k in ('S','D','I'))
        complete=all(r['status'] in ('measured','not_applicable') for r in items)
        partial=100*errors/n if n else None
        return dict(counts=counts,measured_totals=totals,measured_subset_cer_percent=partial,
                    complete=complete,overall_cer_percent=partial if complete else None,
                    official=False,display_reason=None if n else ('해당 대상 없음' if complete else '정상 측정 0건'))
    groups=defaultdict(list)
    for r in rows: groups[r['group']].append(r)
    return dict(overall=summarize(rows),groups={g:summarize(rs) for g,rs in sorted(groups.items())},
                implementation=implementation())


def aggregate_fixed_partial(rows, plan):
    """승인된 고정 부분 범위. 추가 실패를 임의로 빼고 확정하지 않는다."""
    groups=[plan[k] for k in ('included','not_applicable','undetermined')]
    ids=[sid for group in groups for sid in group]
    result=aggregate(rows,expected_ids=ids)
    by_id={r['section_id']:r for r in rows}
    if any(by_id[s]['status']!='undetermined' for s in plan['undetermined']):
        raise ValueError('fixed_undetermined_changed')
    if any(by_id[s]['status']!='not_applicable' for s in plan['not_applicable']):
        raise ValueError('fixed_exclusion_changed')
    complete=all(by_id[s]['status']=='measured' for s in plan['included'])
    result.update(fixed_plan_sha256=digest(plan),fixed_partial_complete=complete,
        fixed_partial_cer_percent=result['overall']['measured_subset_cer_percent'] if complete else None)
    result['overall']['overall_cer_percent']=None
    result['overall']['complete']=False
    return result
