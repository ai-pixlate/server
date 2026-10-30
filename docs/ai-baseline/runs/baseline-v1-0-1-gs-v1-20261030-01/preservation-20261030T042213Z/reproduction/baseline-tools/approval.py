"""실행 전에 사용자 승인 기록과 실제 범위·예산을 대조한다. 승인을 생성하지 않는다."""
from .core import read
from .protocol import local_file, timestamp
from .scope import automatic_only, APPROVED_BUDGET, CONFIRMATION_ACTOR

TARGET='bfa3b700144467f4adf7d6802b7b2e93791fe6de'


def require_approval(root):
    from pathlib import Path
    root=Path(root)
    if not (root/'manifest.json').is_file(): raise ValueError('비용·실행 승인 보류: 회차/승인 기록 없음')
    m=read(root/'manifest.json')
    approval=m.get('cost_approval') or {}
    user=approval.get('user_execution') or {}
    if approval.get('pm_cost_agreed') is not True or user.get('approved') is not True:
        raise ValueError('비용·실행 승인 보류')
    if not user.get('approved_by') or not user.get('evidence_ref'): raise ValueError('사용자 승인자·근거 필요')
    timestamp(user.get('approved_at'))
    ref=user['evidence_ref'];local_file(root,ref['path'],ref['sha256'])
    g=m.get('generation_config') or {};b=g.get('budget') or {}
    if m.get('product_commit') != TARGET or m.get('product_version') != '1.0.1': raise ValueError('측정 버전 불일치')
    from .core import IDS
    from .protocol import source_key
    expected=[source_key(s) for s in IDS]
    if m['source_keys']!=expected or user.get('source_keys')!=expected or user.get('product_commit')!=TARGET:
        raise ValueError('승인 원본·제품 SHA 불일치')
    if user.get('job_grouping')!='case' or g.get('job_grouping')!='case' or user.get('official_generations_per_source')!=1 or user.get('user_regeneration_allowed') is not False:
        raise ValueError('상품별 3작업·원본별 1회·추가 재생성 제외 범위 확인 필요')
    for k in ('max_calls','max_amount','reserve_per_call','currency'):
        if k not in b or user.get(k)!=b[k]: raise ValueError('승인 예산과 실행 설정 불일치: '+k)
        if b[k] != APPROVED_BUDGET[k]: raise ValueError('최신 승인 예산 불일치: '+k)
    if user.get('human_confirmation_actor') != CONFIRMATION_ACTOR:
        raise ValueError('N3/N5 확인 담당자는 예람')
    if not automatic_only(m) or user.get('human_intervention_scope') != 'confirmation_only':
        raise ValueError('H01~H04·N3/N5 무수정 확인 범위 승인 필요')
    return m
