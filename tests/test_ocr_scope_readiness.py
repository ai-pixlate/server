"""원본에서 분리한 설명/제외 영역 회귀. OCR 추론·CER 계산 없음."""
import json
from hashlib import sha256
from pathlib import Path
import pytest
from tools.baseline.ocr_scope import filter_section, v5_inputs

P = Path(__file__).resolve().parents[1]/'docs/ai-baseline/preparations/ocr-v6-readiness-20261109'

@pytest.mark.parametrize('sid,kind,ident,decision', [
 ('GS-02_008__sec_1_04','gt','L025','include'),
 ('GS-02_008__sec_1_04','gt','L027','include'),
 ('GS-02_008__sec_1_04','gt','L029','include'),
 ('GS-02_008__sec_1_04','gt','L021','exclude'),
 ('GS-02_008__sec_1_04','ocr','78:reg_0079','include'),
 ('GS-02_008__sec_1_04','ocr','98:reg_0099','include'),
 ('GS-02_008__sec_1_04','ocr','144:reg_0145','include'),
 ('GS-03_018__sec_1_08','ocr','43:reg_0044','exclude'),
 ('GS-03_018__sec_1_08','ocr','10:reg_0011','include'),
 ('GS-03_018__sec_1_08','gt','L079','include'),
 ('GS-02_001__sec_1_03','gt','L062','include'),
 ('GS-02_001__sec_1_03','ocr','73:reg_0074','include'),
])
def test_source_evidence_mapping(sid,kind,ident,decision):
    audits=json.loads((P/'filter-audit.json').read_text('utf8'))
    section=next(s for s in audits if s['section_id']==sid)
    assert next(r for r in section['audit'] if r['kind']==kind and r['id']==ident)['decision']==decision

@pytest.mark.parametrize('sid', ['GS-02_001__sec_1_03','GS-02_008__sec_1_04','GS-03_018__sec_1_08'])
def test_new_scope_preserves_errors_and_empty_output(sid):
    scope=next(s for s in json.loads((P/'fixed-regions.json').read_text('utf8')) if s['id']==sid)
    for region in scope['regions']:
        text='EXPECTED'
        gt=[dict(id='g',text=text)]
        links=dict(truth_sha256='test',scope_version=scope['scope_version'],lines={'g':dict(rect=region['rect'],verified=True,text_sha256=sha256(text.encode()).hexdigest())})
        kwargs=dict(image_sha256=scope['image_sha256'],gt_sha256='test',gt_links=links)
        out=[dict(id=str(i),text=t,rect=region['rect']) for i,t in enumerate(['WRONG','WRONG','EXTRA'])]
        result=filter_section(scope,gt,out,**kwargs)
        assert result['ready']
        assert all(r['decision']==region['kind'] for r in result['audit'])
        if region['kind']=='include':
            assert v5_inputs(result)==(['EXPECTED'],['WRONG','WRONG','EXTRA'])
            empty=filter_section(scope,gt,[],**kwargs)
            assert v5_inputs(empty)==(['EXPECTED'],[])

def test_previous_104_preserved_and_blockers_retained():
    rows=json.loads((P/'section-status.json').read_text('utf8'))
    old=json.loads((P.parent/'ocr-v6-connection-repair-20261109/section-status.json').read_text('utf8'))
    assert len(rows)==len({r['section_id'] for r in rows})==112
    assert {r['section_id'] for r in old if r['filter_ready']} <= {r['section_id'] for r in rows if r['filter_ready']}
    assert sum(r['filter_ready'] for r in rows)==107
    assert all(r['blockers'] for r in rows if not r['filter_ready'])
