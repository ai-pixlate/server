"""OCR v6 공통 범위 필터. 문자 비교/정규화/CER 계산을 수행하지 않는다.

혼합 섹션의 GT는 원본 위치 또는 검증된 영역 연결이 필요하다. OCR는 region_key가 아닌
원본 좌표로 분류하며, 제외/보류 기록을 삭제하지 않고 추적 자료로 돌려준다.
"""
from copy import deepcopy
from math import isfinite
from hashlib import sha256


def valid_rect(r, width, height):
    return (isinstance(r, (list, tuple)) and len(r) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) for v in r)
            and 0 <= r[0] < r[2] <= width and 0 <= r[1] < r[3] <= height)


def validate_scope(scope, image_sha256):
    if scope['image_sha256'] != image_sha256:
        raise ValueError('image_hash_mismatch')
    if not scope.get('policy_version') or not scope.get('scope_version'):
        raise ValueError('version_missing')
    mode = scope.get('section_type', 'mixed')
    if mode not in ('mixed', 'full_include', 'full_exclude'):
        raise ValueError('invalid_section_type')
    if mode != 'mixed':
        proof = scope.get('section_evidence', {})
        if (not scope.get('coverage_verified') or not proof.get('verified')
                or proof.get('image_sha256') != image_sha256
                or not proof.get('reason') or not proof.get('source')):
            raise ValueError('section_evidence_missing')
        if mode == 'full_include' and (proof.get('exclusion_elements') is not False
                or scope['default'] != 'unknown'
                or any(r['kind'] != 'include' or not r.get('verified') for r in scope['regions'])):
            raise ValueError('full_include_scope_conflict')
        if mode == 'full_exclude' and (scope['default'] != 'exclude'
                or any(r['kind'] != 'exclude' or not r.get('verified') for r in scope['regions'])):
            raise ValueError('full_exclude_scope_conflict')
    seen = set()
    for r in scope['regions']:
        if r['id'] in seen or r['kind'] not in ('include', 'exclude', 'review'):
            raise ValueError('invalid_region')
        seen.add(r['id'])
        if not valid_rect(r['rect'], scope['width'], scope['height']):
            raise ValueError('invalid_geometry')
    if scope['default'] not in ('unknown', 'exclude'):
        raise ValueError('default_must_not_include')
    if scope['default'] == 'exclude' and not scope.get('coverage_verified'):
        raise ValueError('unverified_background_exclusion')


def classify(scope, rect):
    if not valid_rect(rect, scope['width'], scope['height']):
        return 'hold', 'missing_or_invalid_location', []
    x,y,xx,yy=rect
    xs=sorted({x,xx}|{v for r in scope['regions'] for v in (r['rect'][0],r['rect'][2]) if x<v<xx})
    ys=sorted({y,yy}|{v for r in scope['regions'] for v in (r['rect'][1],r['rect'][3]) if y<v<yy})
    states=set();ids=set()
    for a,b in zip(xs,xs[1:]):
        for c,d in zip(ys,ys[1:]):
            px,py=(a+b)/2,(c+d)/2
            hits=[r for r in scope['regions'] if r['rect'][0]<px<r['rect'][2] and r['rect'][1]<py<r['rect'][3]]
            ids.update(r['id'] for r in hits)
            if any(not r.get('verified') or r['kind']=='review' for r in hits):
                states.add('unknown');continue
            # 명시한 원본 설명 영역만 배경 문서에서 차감한다.
            inc=[r for r in hits if r['kind']=='include'];exc=[r for r in hits if r['kind']=='exclude']
            if inc and exc:
                if all(set(e['id'] for e in exc)<=set(i.get('overrides',[])) for i in inc):states.add('include')
                else:states.add('unknown')
            elif inc:states.add('include')
            elif exc:states.add('exclude')
            else:states.add(scope['default'])
    if len(states)!=1 or 'unknown' in states:return 'hold','boundary_or_unverified_scope',sorted(ids)
    return next(iter(states)),'fixed_source_region',sorted(ids)


def filter_section(scope, gt, output, *, image_sha256, gt_sha256, gt_links):
    """GT [{id,text}], output [{id,text,rect}], 독립 GT 영역 sidecar를 받는다."""
    validate_scope(scope,image_sha256)
    if gt_links['truth_sha256'] != gt_sha256:
        raise ValueError('gt_hash_mismatch')
    if gt_links['scope_version'] != scope['scope_version']:
        raise ValueError('scope_version_mismatch')
    links=gt_links['lines']
    if set(links)!={x['id'] for x in gt} or len({x['id'] for x in gt})!=len(gt):
        raise ValueError('gt_link_coverage_mismatch')
    if len({x['id'] for x in output})!=len(output):raise ValueError('duplicate_output_id')
    audit=[];kept={'gt':[],'ocr':[]};blockers=[]
    for kind,records in [('gt',gt),('ocr',output)]:
        for item in records:
            if not isinstance(item['text'],str):raise ValueError('invalid_text')
            loc=links[item['id']] if kind=='gt' else item
            if kind=='gt' and loc.get('text_sha256') != sha256(item['text'].encode('utf-8')).hexdigest():
                raise ValueError('gt_text_link_mismatch')
            mode = scope.get('section_type', 'mixed')
            if kind == 'gt' and item.get('active') is False:
                if not loc.get('deactivation_evidence') or loc.get('deactivation_reason') != 'not_present_in_source':
                    raise ValueError('unapproved_gt_deactivation')
                state,reason,regions='exclude','approved_non_source_gt_inactive',[]
            elif kind == 'gt' and item['text'] == '' and mode != 'full_exclude':
                # 빈 GT에는 범위를 가를 문자가 없다. 별도 승인된 제외 이외에는
                # 원문 빈 줄을 보존한다. OCR는 원본 범위로 독립 분류한다.
                state='exclude' if loc.get('required_decision') == 'exclude' else 'include'
                reason,regions='empty_gt_no_characters',[]
            elif kind == 'ocr' and item['text'] == '' and mode != 'full_exclude':
                # 정상 빈 반환에는 범위를 가를 문자가 없다. GT는 그대로 남겨
                # 누락을 숨기지 않는다. 실행 실패/없는 출력과는 입력 단계에서 구분한다.
                state,reason,regions='include','empty_output_preserved',[]
            elif loc.get('mixed'):
                state,reason,regions='hold','mixed_string_no_split',[]
            elif mode in ('full_include', 'full_exclude'):
                state='include' if mode=='full_include' else 'exclude'
                reason,regions='verified_whole_section_policy',[]
            elif kind=='gt' and not loc.get('verified'):
                state,reason,regions='hold','gt_location_unverified',[]
            else:
                state,reason,regions=classify(scope,loc.get('rect'))
            # 사용자 정책은 기술적 위치 검증을 대체하지 않는다. 정책과 기하
            # 결과가 충돌하면 GT만 강제 제외하는 대신 섹션을 차단한다.
            required = loc.get('required_decision')
            if required is not None:
                if required not in ('include', 'exclude') or not loc.get('policy_evidence'):
                    raise ValueError('invalid_required_policy')
                if state != required:
                    state,reason='hold','policy_scope_connection_unresolved'
            audit.append({'kind':kind,'id':item['id'],'text':item['text'],'decision':state,'reason':reason,'region_ids':regions})
            if state=='include':kept[kind].append(deepcopy(item))
            if state=='hold':blockers.append(kind+':'+item['id']+':'+reason)
    if not scope.get('coverage_verified'):blockers.append('section_coverage_unverified')
    for issue in scope.get('unresolved_connections', []):
        if not isinstance(issue, str) or not issue:
            raise ValueError('invalid_unresolved_connection')
        blockers.append('scope_connection:'+issue)
    return {'scope_version':scope['scope_version'],'policy_version':scope['policy_version'],
            'image_sha256':image_sha256,'truth_sha256':gt_sha256,'audit':audit,
            'applicability':'not_applicable' if scope.get('section_type')=='full_exclude' else 'applicable',
            'included':kept,'ready':not blockers,'blockers':blockers}


def v5_inputs(result):
    """v5 score_lines에 전달할 원문 배열만 생성. 채점 함수 import/호출 없음."""
    if result.get('applicability')=='not_applicable':raise ValueError('section_not_applicable')
    if not result['ready']:raise ValueError('section_scoring_blocked')
    return [x['text'] for x in result['included']['gt']],[x['text'] for x in result['included']['ocr']]
