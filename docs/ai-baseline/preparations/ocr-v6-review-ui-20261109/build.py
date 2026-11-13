"""기존 AI 분석을 보존하며 정책 질문과 기술적 연결 대기열을 분리한다."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / 'ocr-v6-ai-scope-review-20261109'
raw = (SOURCE / 'review-data.json').read_bytes()
old = json.loads(raw)
old_hash = hashlib.sha256(raw).hexdigest()
specs = {
    'GS-03_005__sec_1_01:scope': dict(
        title='크림 제형으로 만든 숫자 3', line='L007', region='reg_0021',
        focus=[100, 800, 850, 1030], boxes=[[140, 850, 760, 930]],
        question='크림 모양의 숫자 3 자체를 번역 대상 문자로 포함할까요?',
        reason='위 설명의 “3중 효과”와 연결된 숫자인지, 장식 이미지인지 의미 판단이 필요합니다. 주변 설명과 하단 각주는 질문 대상이 아닙니다.',
        recommendation='제외 권고: 제형을 이용한 장식 표현으로 보입니다. 숫자의 정보 전달 기능을 인정할지는 사용자 판단이 필요합니다.',
        linkage='GT L007과 OCR reg_0021은 문자 3의 대응 후보입니다. OCR 사각형은 각주까지 걸쳐 있어 정확한 범위 연결이 확정되지 않았습니다. 선택 후에도 기술적 검증이 필요합니다.'),
    'GS-03_014__sec_1_03:scope': dict(
        title='옅은 배경 글자 b3+', line='L008', region=None,
        focus=[0, 280, 1000, 620], boxes=[[835, 340, 165, 270], [590, 610, 190, 240], [0, 350, 100, 200]],
        question='옅은 배경 b3+를 번역 대상 문자로 포함할까요?',
        reason='브랜드·장식 배경인지 독립적인 설명 문구인지 판단이 필요합니다. 강조된 옅은 글자 조각만 확인하세요. 검정 제목·영문·사진은 판단 대상이 아닙니다.',
        recommendation='제외 권고: 옅은 브랜드·장식 배경으로 보입니다. 일반 설명으로 판단한다면 포함을 선택해 주세요.',
        linkage='GT L008은 의미상 연결 후보이며 줄별 위치가 검증된 것은 아닙니다. 대응 OCR 영역은 없습니다. OCR 누락 여부는 범위 결정 이후 별도 평가합니다.'),
}
items, triage = [], []
for item in old['items']:
    ident = item['id']
    if ident in specs:
        spec = specs[ident].copy()
        line = spec.pop('line')
        gt = [g for g in item['gt'] if g['line_id'] == line]
        reg = spec.pop('region')
        ocr = [o for o in item['ocr'] if o['region_key'] == reg]
        assert len(gt) == 1 and (reg is None or len(ocr) == 1)
        items.append(dict(id=ident, section_id=item['section_id'], image=item['image'],
                          gt=gt, ocr=ocr, truth_sha256=item['truth_sha256'],
                          output_ref=item['output_ref'], decision=None, approved=False, **spec))
        category, reason = 'policy_question', spec['reason']
    elif ident == 'GS-01_002__sec_1_15:scope':
        category = 'ai_policy_resolved'
        reason = '원본의 Reef Safe는 일반 설명 문구입니다. 타원 배치만으로 인증 로고로 제외하지 않습니다. 포함 권고이며 영역·GT 연결 완료나 사용자 승인을 뜻하지 않습니다.'
    else:
        category = 'technical_pending'
        reason = ('OCR 경계 교차의 원본·영역 연결 검증 필요. 문자열 분리만으로 정책 판단을 요청하지 않음.'
                  if item.get('focus_ocr') else
                  '기존 포함·제외 정책은 유지. 섹션 전체 선택 대신 줄별 위치·연결 근거를 보완해야 함.')
    triage.append(dict(id=ident, section_id=item['section_id'], category=category, reason=reason,
                       original_reason=item['reason'], focus_ocr=item.get('focus_ocr')))
assert len(items) == 2 and len(triage) == 61
data = dict(schema='v6-focused-review-2', source_sha256=old_hash,
            reset_keys=['pixlate-v6-review:' + old_hash], items=items, triage=triage,
            approved=False, frozen=False, filter_status_changed=False)
encoded = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
(ROOT / 'review-data.json').write_bytes(encoded)
digest = hashlib.sha256(encoded).hexdigest()
(ROOT / 'data.js').write_text('window.REVIEW_DATA=' + encoded.decode().strip() + ';\nwindow.DATA_HASH="' + digest + '";\n', encoding='utf-8')
print(json.dumps(dict(policy_items=len(items), original_items=len(triage), sha256=digest)))
