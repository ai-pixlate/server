import copy
import pytest
from hashlib import sha256
from tools.baseline.ocr_scope import filter_section, v5_inputs

def fixture():
    s=dict(image_sha256='img',scope_version='s1',policy_version='p1',width=100,height=100,default='exclude',coverage_verified=True,regions=[dict(id='text',kind='include',rect=[10,10,90,40],verified=True)])
    g=[dict(id='g1',text='ABC')];l=dict(truth_sha256='gt',scope_version='s1',lines={'g1':dict(rect=[20,20,30,30],verified=True,text_sha256=sha256(b'ABC').hexdigest())})
    return s,g,l

def run(out,change=None):
    s,g,l=fixture()
    if change:change(s,g,l)
    before=copy.deepcopy((s,g,l,out));r=filter_section(s,g,out,image_sha256='img',gt_sha256='gt',gt_links=l)
    assert (s,g,l,out)==before
    return r

@pytest.mark.parametrize('texts',[[],[''],['ABC','ABC'],['AXC'],['ABC','EXTRA']])
def test_missing_empty_duplicates_errors_preserved(texts):
    r=run([dict(id=str(i),text=t,rect=[20,20,30,30]) for i,t in enumerate(texts)])
    assert r['ready'] and v5_inputs(r)==(['ABC'],texts)

@pytest.mark.parametrize('name',['face','background','reflected_label','document'])
def test_outside_false_detection_excluded(name):
    r=run([dict(id=name,text='o',rect=[50,60,60,70])]);assert v5_inputs(r)==(['ABC'],[])

def test_crossing_and_mixed_not_split():
    for o in [dict(id='x',text='Good all goodal',rect=[0,20,30,30]),dict(id='x',text='Good all goodal',rect=[20,20,30,30],mixed=True)]:
        r=run([o]);assert not r['ready'] and r['audit'][-1]['text']==o['text']
        with pytest.raises(ValueError):v5_inputs(r)

def test_unverified_gt_blocks_even_when_output_matches():
    r=run([dict(id='x',text='ABC',rect=[20,20,30,30])],lambda s,g,l:l['lines']['g1'].update(verified=False));assert not r['ready']

def test_gt_text_cannot_be_changed_under_same_link():
    with pytest.raises(ValueError,match='gt_text_link_mismatch'):
        run([],lambda s,g,l:g[0].update(text='CHANGED'))

def test_unknown_outside_not_excluded():
    r=run([dict(id='x',text='X',rect=[50,60,60,70])],lambda s,g,l:s.update(default='unknown',coverage_verified=False));assert r['audit'][-1]['decision']=='hold'

def test_quote_override_symmetric():
    def change(s,g,l):
        s['regions'].append(dict(id='doc',kind='exclude',rect=[0,0,100,100],verified=True));s['regions'][0]['overrides']=['doc']
    r=run([dict(id='x',text='ABC',rect=[20,20,30,30])],change);assert v5_inputs(r)==(['ABC'],['ABC'])

def test_unapproved_overlap_blocks_both():
    r=run([dict(id='x',text='ABC',rect=[20,20,30,30])],lambda s,g,l:s['regions'].append(dict(id='doc',kind='exclude',rect=[0,0,100,100],verified=True)))
    assert all(a['decision']=='hold' for a in r['audit'])

@pytest.mark.parametrize('field,value',[('image_sha256','wrong'),('truth_sha256','wrong'),('scope_version','wrong')])
def test_hash_version_mismatch(field,value):
    s,g,l=fixture()
    if field=='image_sha256':s[field]=value
    else:l[field]=value
    with pytest.raises(ValueError):filter_section(s,g,[],image_sha256='img',gt_sha256='gt',gt_links=l)

def whole(mode):
    s,g,l=fixture()
    s.update(section_type=mode,default='unknown' if mode=='full_include' else 'exclude',regions=[],section_evidence=dict(verified=True,image_sha256='img',reason='source inspection',source='source.png',exclusion_elements=False))
    l['lines']['g1'].update(rect=None,verified=False)
    return s,g,l

@pytest.mark.parametrize('texts',[[],[''],['AXC'],['ABC','ABC'],['EXTRA']])
def test_full_include_no_location_preserves_all(texts):
    s,g,l=whole('full_include');o=[dict(id=str(i),text=t) for i,t in enumerate(texts)]
    before=copy.deepcopy((s,g,l,o))
    r=filter_section(s,g,o,image_sha256='img',gt_sha256='gt',gt_links=l)
    assert v5_inputs(r)==(['ABC'],texts) and (s,g,l,o)==before

def test_full_exclude_is_not_zero_cer():
    s,g,l=whole('full_exclude')
    r=filter_section(s,g,[dict(id='o',text='LOGO')],image_sha256='img',gt_sha256='gt',gt_links=l)
    assert r['ready'] and r['applicability']=='not_applicable' and all(x['decision']=='exclude' for x in r['audit'])
    with pytest.raises(ValueError,match='section_not_applicable'):v5_inputs(r)

@pytest.mark.parametrize('change',[
 lambda s:s['section_evidence'].update(verified=False),
 lambda s:s['section_evidence'].update(image_sha256='changed'),
 lambda s:s['section_evidence'].update(exclusion_elements=True),
 lambda s:s['regions'].append(dict(id='logo',kind='exclude',rect=[1,1,10,10],verified=True)),
])
def test_full_include_evidence_and_exclusion_guard(change):
    s,g,l=whole('full_include');change(s)
    with pytest.raises(ValueError):filter_section(s,g,[],image_sha256='img',gt_sha256='gt',gt_links=l)

def test_full_include_cannot_bypass_mixed_string():
    s,g,l=whole('full_include')
    r=filter_section(s,g,[dict(id='o',text='Good all goodal',mixed=True)],image_sha256='img',gt_sha256='gt',gt_links=l)
    assert not r['ready']

def test_full_exclude_cannot_remove_include_window():
    s,g,l=whole('full_exclude');s['regions']=[dict(id='quote',kind='include',rect=[1,1,10,10],verified=True)]
    with pytest.raises(ValueError):filter_section(s,g,[],image_sha256='img',gt_sha256='gt',gt_links=l)
