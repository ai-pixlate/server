"""범위 회귀만 검사한다. OCR/CER 실행 없음."""
from pathlib import Path
import json
from hashlib import sha256
import pytest
from tests.test_ocr_scope import fixture
from tools.baseline.ocr_scope import filter_section, v5_inputs, classify

P = Path(__file__).resolve().parents[1]/'docs/ai-baseline/preparations/ocr-v6-connection-repair-20261109'

@pytest.mark.parametrize('approval', [False, True])
def test_empty_gt_does_not_hide_insertion(approval):
    s,g,l=fixture()
    g[0]['text']=''
    l['lines']['g1'].update(rect=None,verified=False,text_sha256=sha256(b'').hexdigest())
    if approval:l['lines']['g1'].update(required_decision='exclude',policy_evidence='user-approval')
    out=[dict(id='extra',text='EXTRA',rect=[20,20,40,30])]
    r=filter_section(s,g,out,image_sha256='img',gt_sha256='gt',gt_links=l)
    assert r['ready'] and v5_inputs(r)[1]==['EXTRA']
    assert r['audit'][0]['decision']==('exclude' if approval else 'include')

def test_nonempty_unlocated_gt_still_blocks():
    s,g,l=fixture();l['lines']['g1'].update(verified=False,rect=None)
    r=filter_section(s,g,[],image_sha256='img',gt_sha256='gt',gt_links=l)
    assert not r['ready']

@pytest.mark.parametrize('sid,oid,expected',[
 ('GS-01_002__sec_1_18','7:reg_0008','include'),
 ('GS-01_002__sec_1_21','6:reg_0007','exclude'),
 ('GS-01_002__sec_1_21','7:reg_0008','exclude'),
 ('GS-02_014__sec_1_09','8:reg_0009','exclude'),
 ('GS-02_014__sec_1_09','7:reg_0008','include'),
 ('GS-02_014__sec_1_11','11:reg_0012','include'),
 ('GS-03_002__sec_1_01','23:reg_0024','exclude'),
 ('GS-03_002__sec_1_01','31:reg_0032','include'),
 ('GS-03_002__sec_1_04','54:reg_0055','include'),
 ('GS-03_005__sec_1_01','20:reg_0021','hold'),
 ('GS-01_002__sec_1_23','0:reg_0001','hold'),
])
def test_source_checked_output_policy(sid,oid,expected):
    rows=json.loads((P/'filter-audit.json').read_text('utf8'))
    section=next(x for x in rows if x['section_id']==sid)
    item=next(x for x in section['audit'] if x['kind']=='ocr' and x['id']==oid)
    assert item['decision']==expected

def test_shared_geometry_cannot_depend_on_recognition_text():
    scopes=json.loads((P/'fixed-regions.json').read_text('utf8'))
    links={x['section_id']:x for x in json.loads((P/'gt-region-links.json').read_text('utf8'))}
    for s in scopes:
        if s.get('section_type')!='mixed':continue
        for loc in links[s['id']]['lines'].values():
            if not loc.get('verified') or not loc.get('rect'):continue
            expected=classify(s,loc['rect'])[0]
            # 동일 원본 영역의 GT와 다른 문자·중복·오검출 OCR도 동일 분류.
            gt=[dict(id='g',text='ABC')]
            link=dict(truth_sha256='test',scope_version=s['scope_version'],lines={'g':dict(rect=loc['rect'],verified=True,text_sha256=sha256(b'ABC').hexdigest())})
            out=[dict(id=str(i),text=t,rect=loc['rect']) for i,t in enumerate(['WRONG','WRONG','EXTRA'])]
            result=filter_section(s,gt,out,image_sha256=s['image_sha256'],gt_sha256='test',gt_links=link)
            assert all(a['decision']==expected for a in result['audit'])
