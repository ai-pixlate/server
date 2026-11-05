"""실제 OCR·정답은 사용하지 않는 v4 합성 테스트."""
import copy
import itertools
import json
from pathlib import Path
import pytest
from tools.baseline import ocr


def write(path, data):
    path.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
    return path


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path/"source"; source.mkdir()
    (tmp_path/"image.bin").write_bytes(b"synthetic image bytes")
    ih = ocr.digest(tmp_path/"image.bin")
    for sid, texts in (("S1",["A", "B", ""]),("S2",["12345678"]),("S3",[""])):
        write(source/f"{sid}.json", {"id":sid,"sample":"synthetic","section_key":sid,
              "image":{"path":"image.bin","sha256":ih,"width":1,"height":1},
              "lines":[{"line_id":f"L{i}","text":t,"bbox":"invalid by design","verified":False} for i,t in enumerate(texts)]})
    spec = tmp_path/"v4.md"; spec.write_text("synthetic v4 spec",encoding="utf-8")
    approval = write(tmp_path/"approval.json", {"reviewer":"synthetic","evidence":["test consent"],
        "transcription_review_complete":True,"errors_found":0,"order":"lines_array","empty_truth_policy":"keep",
        "freeze_authorized":True,"expected_sections":3,"expected_lines":5})
    truth = tmp_path/"frozen"
    ocr.freeze(source,tmp_path,spec,approval,"synthetic-v1",truth)
    _,units = ocr.verify(truth)
    output = write(tmp_path/"output.json",{"regions":[{"text":"AB"}]})
    ev = tmp_path/"evidence.txt";ev.write_text("synthetic run return evidence")
    rec = {"id":"S1","status":"available","input_sha256":ih,"product_version":"synthetic",
        "product_sha":"a"*40,"attempt_id":"test-1","model":"synthetic","config_sha256":"b"*64,
        "environment_sha256":"c"*64,"boundary":ocr.BOUNDARY,
        "evidence":{"path":ev.name,"sha256":ocr.digest(ev),"locator":"synthetic return"},
        "output":{"path":output.name,"sha256":ocr.digest(output),"pointer":""}}
    return tmp_path,source,truth,units,rec


@pytest.mark.parametrize('ref,pred,expected', [
    ('','',(0,0,0)),('AB','',(0,2,0)),('','ABC',(0,0,3)),('AB','ABAB',(0,0,2)),
    ('abc','adc',(1,0,0)),('ab','ba',(2,0,0)),('가😀','가X',(1,0,0)),('A','AAAA',(0,0,3))])
def test_edits(ref,pred,expected):
    got=ocr.edits(ref,pred)
    assert tuple(got[k] for k in ('S','D','I')) == expected
    assert got['N']==len(ref)


def test_distance_exhaustive_small():
    # 독립적인 재귀 최소거리로 모든 짧은 이진 문자열 대조.
    from functools import lru_cache
    @lru_cache(None)
    def distance(a,b):
        if not a:return len(b)
        if not b:return len(a)
        return min(distance(a[:-1],b[:-1])+(a[-1]!=b[-1]),distance(a[:-1],b)+1,distance(a,b[:-1])+1)
    words=[''.join(x) for n in range(5) for x in itertools.product('ab',repeat=n)]
    for a in words:
        for b in words:
            result=ocr.edits(a,b)
            assert result['S']+result['D']+result['I']==distance(a,b)


def test_normalization():
    assert ocr.text_sequence(['가\t A','\u00a0B\u3000'])=='가AB'
    assert ocr.text_sequence(['Ａa㎖!?\u200b\ufeff'])=='Ａa㎖!?\u200b\ufeff'
    assert ocr.text_sequence(['A','B'])==ocr.text_sequence(['AB'])
    assert ocr.text_sequence(['B','A'])!='AB'
    assert ocr.text_sequence(['A'+''.join(ocr.SPACE)+'B'])=='AB'
    with pytest.raises(ValueError):ocr.text_sequence([None])


def test_freeze_preserves_inputs_and_ignores_boxes(fixture):
    root,source,truth,units,rec=fixture
    assert [l['text'] for l in units[0]['lines']]==['A','B','']
    assert [l['order'] for l in units[0]['lines']]==[0,1,2]
    for u in units:
        assert ocr.digest(Path(u['source']['path']))==u['source']['sha256']
        assert all('bbox' not in line for line in u['lines'])
    with pytest.raises(ValueError,match='이미 존재'):
        ocr.freeze(source,root,root/'v4.md',root/'approval.json','x',truth)


def test_freeze_requires_approval_and_hash(fixture):
    root,source,truth,units,rec=fixture
    approval=ocr.read(root/'approval.json');approval['freeze_authorized']=False
    write(root/'bad-approval.json',approval)
    with pytest.raises(ValueError,match='승인'):
        ocr.freeze(source,root,root/'v4.md',root/'bad-approval.json','x',root/'bad-freeze')
    (root/'image.bin').write_bytes(b'changed')
    with pytest.raises(ValueError,match='해시'):
        ocr.freeze(source,root,root/'v4.md',root/'approval.json','x',root/'bad-freeze')
    assert not (root/'bad-freeze').exists()


def test_frozen_tampering(fixture):
    root,source,truth,units,rec=fixture
    with (truth/'text_units.jsonl').open('a') as f:f.write(' ')
    with pytest.raises(ValueError,match='해시'):ocr.verify(truth)


@pytest.mark.parametrize('change,status',[
    ({'input_sha256':'d'*64},'undetermined'),({'boundary':'raw_engine'},'undetermined'),
    ({'product_sha':'short'},'undetermined'),({'evidence':None},'undetermined'),
    ({'status':'no_output','reason':'failed run'},'no_output'),({'status':'undetermined','reason':'missing provenance'},'undetermined')])
def test_provenance_failures(fixture,change,status):
    root,source,truth,units,rec=fixture
    rec.update(change)
    result=ocr.section_score(units[0],rec,root)
    assert result['status']==status and result['cer'] is None


@pytest.mark.parametrize('regions,status,deletions', [([], 'measured',2),([{'text':''}],'measured',2),
    ([{'text':None}],'evaluation_failed',None),([{}],'evaluation_failed',None),({},'evaluation_failed',None)])
def test_empty_and_malformed(fixture,regions,status,deletions):
    root,source,truth,units,rec=fixture
    write(root/'output.json',{'regions':regions});rec['output']['sha256']=ocr.digest(root/'output.json')
    r=ocr.section_score(units[0],rec,root)
    assert r['status']==status and r['D']==deletions


def test_missing_bad_hash_and_json(fixture):
    root,source,truth,units,rec=fixture
    rec['output']['path']='missing.json'
    assert ocr.section_score(units[0],rec,root)['status']=='no_output'
    rec['output']['path']='output.json';rec['output']['sha256']='0'*64
    assert ocr.section_score(units[0],rec,root)['status']=='undetermined'
    (root/'output.json').write_text('not json');rec['output']['sha256']=ocr.digest(root/'output.json')
    assert ocr.section_score(units[0],rec,root)['status']=='evaluation_failed'


def test_jsonl_pointer_and_no_coordinate_access(fixture):
    root,source,truth,units,rec=fixture
    out=root/'stages.jsonl';out.write_text('{}\n'+json.dumps({'result':{'regions':[{'text':'A','bbox':None,'score':0},{'text':'B','bbox':'wrong'}]}}))
    rec['output']={'path':out.name,'sha256':ocr.digest(out),'line':2,'pointer':'/result'}
    r=ocr.section_score(units[0],rec,root)
    assert r['cer']==0


def test_weighted_total_zero_denominator_and_immutability(fixture):
    root,source,truth,units,rec=fixture
    # S1: one error / 2; S2: zero / 8 → overall 1/10 (not 25%).
    write(root/'output.json',{'regions':[{'text':'AX'}]});rec['output']['sha256']=ocr.digest(root/'output.json')
    second=copy.deepcopy(rec);second['id']='S2'
    out=write(root/'second.json',{'regions':[{'text':'12345678'}]})
    second['output']['path']=out.name;second['output']['sha256']=ocr.digest(out)
    third=copy.deepcopy(rec);third['id']='S3'
    inputs=write(root/'inputs.json',{'sections':[rec,second,third]})
    before={p:ocr.digest(p) for p in root.rglob('*') if p.is_file()}
    report=ocr.score(truth,inputs,root/'report.json')
    assert report['total']['cer']==.1
    assert report['sections'][2]['status']=='not_applicable'
    assert report['sections'][2]['output_characters']==2
    assert all(ocr.digest(p)==h for p,h in before.items())
    with pytest.raises(ValueError,match='덮어쓰기'):ocr.score(truth,inputs,root/'report.json')
    assert ocr.aggregate([report['sections'][2]])['display_reason']=='해당 대상 없음'
    missing=ocr.section_score(units[0],None,root)
    assert ocr.aggregate([missing])['display_reason']=='정상 측정 0건'


def test_missing_and_duplicate_sections(fixture):
    root,source,truth,units,rec=fixture
    inp=write(root/'inputs.json',{'sections':[rec]})
    result=ocr.score(truth,inp,root/'report.json')
    assert result['total']['planned_sections']==3
    assert result['total']['status_counts']['undetermined']==2
    write(inp,{'sections':[rec,rec]})
    with pytest.raises(ValueError,match='중복'):ocr.score(truth,inp,root/'new.json')


def test_validate_without_scoring(fixture, monkeypatch):
    root,source,truth,units,rec=fixture
    def forbidden(*a,**kw): raise AssertionError("CER/정규화 호출 금지")
    monkeypatch.setattr(ocr,'edits',forbidden)
    monkeypatch.setattr(ocr,'text_sequence',forbidden)
    inputs=write(root/'validate-inputs.json',{'sections':[rec]})
    result=ocr.validate_inputs(truth,inputs)
    assert result['cer_calculated'] is False
    assert result['sections'][0]['status']=='ready'
    assert all(r['cer'] is None and r['N'] is None for r in result['sections'])
    rec['output_section_key']='wrong-section'
    write(inputs,{'sections':[rec]})
    assert ocr.validate_inputs(truth,inputs)['sections'][0]['status']=='undetermined'


def test_preparation_attempt_context():
    from tools.baseline.ocr_prepare import candidates
    events=[{'source_id':'CASE','stage':'section_split','result':{'source_image_id':7,'sections':[{'section_key':'a'}]}},
            {'source_id':'CASE','stage':'ocr','at':'first','result':{'section_key':'a','regions':[]}},
            {'source_id':'CASE','stage':'section_split','result':{'source_image_id':8,'sections':[{'section_key':'a'}]}},
            {'source_id':'CASE','stage':'ocr','at':'second','result':{'section_key':'a','regions':[]}},
            {'source_id':'CASE','stage':'section_split','error':'failed'},
            {'source_id':'CASE','stage':'ocr','at':'orphan','result':{'section_key':'a','regions':[]}}]
    found=candidates(events)
    assert [(x['source_image_id'],x['line']) for x in found]==[(7,2),(8,4)]
