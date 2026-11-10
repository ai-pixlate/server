import json
from pathlib import Path
import pytest
from tests.test_ocr_v6 import case
from tools.baseline import ocr_v6 as v6


def test_inactive_gt_requires_evidence_and_preserves_ocr_insertion():
    args,kw=case(['BAD'],['EXTRA'])
    args[1][0]['active']=False
    assert v6.score_section(*args,**kw)['status']=='evaluation_failed'
    args[3]['lines']['0'].update(deactivation_evidence='user',deactivation_reason='not_present_in_source')
    r=v6.prepare_section(*args,**kw)
    assert r['filter']['audit'][0]['decision']=='exclude'
    assert r['filter']['audit'][1]['decision']=='include'
    assert r['status']=='undetermined'  # 0자 GT로 삽입 오류를 면제하지 않음


def test_fixed_partial_never_becomes_whole_and_rejects_changed_scope():
    args,kw=case(['ABC'],['AXC'],sid='included');one=v6.score_section(*args,**kw)
    args,kw=case(sid='held');args[0]['unresolved_connections']=['fixed_user_policy']
    held=v6.score_section(*args,**kw)
    plan=dict(included=['included'],not_applicable=[],undetermined=['held'])
    r=v6.aggregate_fixed_partial([one,held],plan)
    assert r['fixed_partial_complete'] and r['fixed_partial_cer_percent']==100/3
    assert r['overall']['overall_cer_percent'] is None and not r['overall']['official']
    args,kw=case(sid='included');kw['gt_complete']=False
    assert v6.aggregate_fixed_partial([v6.score_section(*args,**kw),held],plan)['fixed_partial_cer_percent'] is None
    args,kw=case(sid='held')
    with pytest.raises(ValueError,match='fixed_undetermined_changed'):
        v6.aggregate_fixed_partial([one,v6.score_section(*args,**kw)],plan)


def test_candidate_ids_text_and_counts():
    b=Path(__file__).resolve().parents[1]/'docs/ai-baseline'
    p=b/'preparations/ocr-v6-gt-candidate-20261110'
    original=[json.loads(x) for x in (b/'references/ocr-text/ocr-text-v2-20261109/text_units.jsonl').read_text('utf8').splitlines()]
    candidate=[json.loads(x) for x in (p/'text_units.jsonl').read_text('utf8').splitlines()]
    for a,c in zip(original,candidate):
        assert a['id']==c['id'] and a['image']==c['image']
        assert [x['line_id'] for x in a['lines']]==[x['line_id'] for x in c['lines']]
        for old,new in zip(a['lines'],c['lines']):
            if a['id']=='GS-02_001__sec_1_03' and old['line_id']=='L045':
                assert old['text']=='상품앞 표시품목 : 부가세 면세품목'
                assert new['text']=='상품앞 @표시품목 : 부가세 면세품목'
                assert new['change_history'][0]['approval']=='user_chat_L045_at_20261110'
            else:assert old['text']==new['text']
    assert [(u['id'],x['line_id']) for u in candidate for x in u['lines'] if x.get('active') is False]==[('GS-02_002__sec_1_01','L005'),('GS-02_002__sec_1_01','L006')]
    plan=json.loads((p/'evaluation-plan.json').read_text('utf8'))
    assert [len(plan[k]) for k in ('included','not_applicable','undetermined')]==[105,4,3]
    assert not plan['frozen']
