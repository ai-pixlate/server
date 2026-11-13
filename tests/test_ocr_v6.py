import copy
from hashlib import sha256
import pytest
from tools.baseline import ocr_v6 as v6
from tests.test_ocr_scope import fixture


def case(gt=('ABC',),out=('ABC',),sid='one',group='GS-01'):
    s,_,l=fixture();s['id']=sid
    g=[dict(id=str(i),text=t) for i,t in enumerate(gt)]
    l['lines']={x['id']:dict(rect=[20,20,30,30],verified=True,text_sha256=sha256(x['text'].encode()).hexdigest()) for x in g}
    o=[dict(id=str(i),text=t,rect=[20,20,30,30]) for i,t in enumerate(out)] if out is not None else None
    m=dict(group=group,input_version='input1',truth_version='gt1',product_version='p1',product_sha='p',image_sha256='img',truth_sha256='gt',output_sha256='out',policy_sha256='policy',spec_sha256='spec')
    return [s,g,o,l],dict(metadata=m,gt_complete=True,scope_approved=True,completeness_evidence='synthetic-reviewed-fixture')


@pytest.mark.parametrize('gt,out,s,d,i',[
 (['ABC'],['ABC'],0,0,0),(['ABC'],['AXC'],1,0,0),
 (['ABCD'],['AB','CD'],0,0,0),(['AB','CD'],['ABCD'],0,0,0),
 (['APPLE','BANANA'],['BANANA','APPLE'],0,0,0),
 (['ABC'],[],0,3,0),(['AB','CD'],['AB'],0,2,0),
 (['AB'],['AB','AB'],0,0,2),(['AB'],['AB','XYZ'],0,0,3),
 (['가 나'],['가\t나'],0,0,0),
])
def test_synthetic_characters(gt,out,s,d,i):
    args,kw=case(gt,out);before=copy.deepcopy((args,kw))
    r=v6.score_section(*args,**kw)
    assert r['status']=='measured'
    assert [r['metrics'][k] for k in ('S','D','I')]==[s,d,i]
    assert (args,kw)==before


@pytest.mark.parametrize('issue',['crossing','mixed','overlap','gt','approval','hash','empty_gt','no_output'])
def test_blockers_never_invoke_matching(monkeypatch,issue):
    args,kw=case();s,g,o,l=args
    if issue=='crossing':o[0]['rect']=[0,20,30,30]
    if issue=='mixed':o[0]['mixed']=True
    if issue=='overlap':s['unresolved_connections']=['background_foreground_overlap']
    if issue=='gt':kw['gt_complete']=False
    if issue=='approval':kw['scope_approved']=False
    if issue=='hash':kw['metadata']['image_sha256']='wrong'
    if issue=='empty_gt':g[0]['text']='';l['lines']['0']['text_sha256']=sha256(b'').hexdigest()
    if issue=='no_output':args[2]=None
    monkeypatch.setattr(v6,'score_lines',lambda *a:pytest.fail('blocked matching'))
    r=v6.score_section(*args,**kw)
    assert r['metrics'] is None and not r['score_ready']
    assert r['status']==('evaluation_failed' if issue=='hash' else 'no_output' if issue=='no_output' else 'undetermined')


@pytest.mark.parametrize('mode',['full_include','full_exclude'])
def test_whole_section(mode):
    args,kw=case();s=args[0]
    s.update(section_type=mode,default='unknown' if mode=='full_include' else 'exclude',regions=[],section_evidence=dict(verified=True,image_sha256='img',reason='synthetic policy',source='fixture',exclusion_elements=False))
    r=v6.score_section(*args,**kw)
    assert r['status']==('measured' if mode=='full_include' else 'not_applicable')
    if mode=='full_exclude':assert r['metrics'] is None


def test_symmetric_filter_and_insertion_preservation():
    args,kw=case(['ABC','LABEL'],['AXC','LABEL','EXTRA'])
    args[3]['lines']['1']['rect']=[50,60,60,70];args[2][1]['rect']=[50,60,60,70]
    r=v6.score_section(*args,**kw)
    assert r['metrics']['N']==3 and r['metrics']['S']==1 and r['metrics']['I']==5
    assert [a['decision'] for a in r['filter']['audit']]==['include','exclude','include','exclude','include']


def test_weighted_aggregation_and_no_false_complete():
    a,k=case(['A'],[],sid='a');r1=v6.score_section(*a,**k)
    a,k=case(['ABCDEFGHI'],['ABCDEFGHI'],sid='b',group='GS-02');r2=v6.score_section(*a,**k)
    result=v6.aggregate([r1,r2],expected_ids=['a','b'])
    assert result['overall']['overall_cer_percent']==10
    assert result['groups']['GS-01']['overall_cer_percent']==100
    assert result['groups']['GS-02']['overall_cer_percent']==0
    k['gt_complete']=False;r2=v6.score_section(*a,**k)
    result=v6.aggregate([r1,r2],expected_ids=['a','b'])
    assert result['overall']['overall_cer_percent'] is None
    assert result['overall']['measured_subset_cer_percent']==100
    assert not result['overall']['complete'] and not result['overall']['official']
    with pytest.raises(ValueError):v6.aggregate([r1],expected_ids=['a','b'])
    r2['metadata']['input_version']='crop-v2'
    with pytest.raises(ValueError):v6.aggregate([r1,r2],expected_ids=['a','b'])


def test_preflight_never_calls_score(monkeypatch):
    args,kw=case()
    monkeypatch.setattr(v6,'score_lines',lambda *a:pytest.fail('preflight matching'))
    r=v6.prepare_section(*args,**kw)
    assert r['score_ready'] and r['metrics'] is None


def test_zero_measurements_are_not_zero_error():
    args,kw=case();kw['gt_complete']=False
    row=v6.score_section(*args,**kw)
    summary=v6.aggregate([row],expected_ids=['one'])['overall']
    assert summary['overall_cer_percent'] is None and summary['display_reason']=='정상 측정 0건'
    args[0].update(section_type='full_exclude',regions=[],section_evidence=dict(verified=True,image_sha256='img',reason='policy',source='fixture'))
    row=v6.score_section(*args,**kw)
    summary=v6.aggregate([row],expected_ids=['one'])['overall']
    assert summary['overall_cer_percent'] is None and summary['display_reason']=='해당 대상 없음'
