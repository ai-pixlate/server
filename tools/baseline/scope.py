"""명세의 회차 범위. 제품 상태·지표 정의는 변경하지 않는다."""
QUALITY_METRICS = ['H01', 'H02', 'H03', 'H04']
H05_REASON = '미측정(별도 서비스 평가 예정)'
OP06_NOTE = '무수정 실험 조건의 내부 관찰값; 실제 사용자 승인 성과로 해석·발표하지 않음'


def automatic_scope():
    return {'measured_quality_metrics': list(QUALITY_METRICS),
            'human_intervention': 'confirmation_only'}


def automatic_only(manifest):
    scope = manifest.get('measurement_scope')
    if scope is None:
        return False  # 과거 기록 읽기 호환. 새 실행 승인은 별도 차단.
    if scope != automatic_scope():
        raise ValueError('측정 범위 불일치: H01~H04·무수정 확인 필요')
    return True
