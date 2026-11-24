"""제품 DB의 종료 시도 증거를 최신 회차 기록에 연결한다. 원문은 별도 보존한다."""
def terminal_attempts(collector, snapshot, source_bindings, evidence):
    sections={s['id']:source_bindings[s['source_image_id']] for s in snapshot['section']}
    for a in snapshot['job_async_task']:
        if a.get('parent_task_id') is None or a.get('target_count') == 0: continue
        if a['status'] not in ('done','failed','cancelled'): continue  # 진행 중을 실패로 확정하지 않음
        source=sections.get(a['unit_id']) if a['unit_type']=='section' else None
        sources=[source] if source else collector.sources
        code=a.get('error_code')
        status=('degraded' if code else 'recovered' if a.get('retry_origin')=='auto' else 'success') if a['status']=='done' else 'blocked'
        collector.event('attempt',a['stage'],evidence,source_keys=sources,
            logical_execution_id=f"{collector.job_id}:{a['parent_task_id']}:{a['stage']}:{a['unit_id']}",
            target_id=str(a['unit_id']),attempt_number=a['attempt_no'],execution_status=status,
            error_codes=[code] if code else [],fallback_type=None,is_final_attempt=bool(a['is_current']),
            applied_to_result=a['status']=='done' and bool(a['is_current']),artifact_ids=[],usage=None,cost=None,
            started_at=a.get('started_at'),ended_at=a.get('finished_at'),
            retry_origin=a.get('retry_origin'),unavailable_reason='fallback 종류는 원시 산출물 근거 대조 필요')
