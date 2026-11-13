"""v2 검수 공간 합성 검증: 실제 GT 작성/검수 없음."""
import importlib.util
import json
from pathlib import Path
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('text_review', ROOT/'docs/ai-experiments/tools/ocr_truth/text_review.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

@pytest.fixture
def setup(tmp_path):
    rows=[]
    for i in range(3):
        p=tmp_path/f'{i}.png';Image.new('RGB',(20+i,30)).save(p)
        rows.append(dict(source_id=f'GS-0{i+1}_001',section_key='sec_1_01',path=str(p),sha256=t.digest(p),width=20+i,height=30,truth_v1_id=None))
    tid='GS-01_001__sec_1_01';rows[0]['truth_v1_id']=tid
    unit=dict(id=tid,lines=[dict(line_id='L001',text=''),dict(line_id='L002',text='가')],image={k:rows[0][k] for k in ['path','sha256','width','height']})
    frozen=tmp_path/'frozen';frozen.mkdir()
    (frozen/'text_units.jsonl').write_text(json.dumps(unit)+'\n',encoding='utf-8')
    (frozen/'manifest.json').write_text(json.dumps(dict(status='frozen_text_only',sections=[tid],files={'text_units.jsonl':t.digest(frozen/'text_units.jsonl')})),encoding='utf-8')
    inv=tmp_path/'inventory.json';inv.write_text(json.dumps(dict(sections=rows,summary=dict(sections=3,sources=3))))
    return inv,frozen,tmp_path/'.cache/drafts'

def body(**kw):
    return dict(revision=0,lines=[dict(line_id='L002',text='뒤'),dict(line_id='L001',text='')],status='draft',**kw)

def test_frozen_and_empty_no_writes(setup):
    w=t.Workspace(*setup);assert not setup[2].exists()
    assert w.listing()['progress']['all']==dict(total=3,frozen_v1=1,reviewed=0,draft=0)
    before=(setup[1]/'text_units.jsonl').read_bytes()
    with pytest.raises(PermissionError):w.save('GS-01_001__sec_1_01',body(),lambda:'now')
    assert before==(setup[1]/'text_units.jsonl').read_bytes()

def test_roundtrip_order_revision_and_manual_review(setup):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01'
    d=w.save(tid,body(),lambda:'now');assert [l['text'] for l in d['lines']]==['뒤','']
    with pytest.raises(ValueError,match='다른 창'):w.save(tid,body(),lambda:'now')
    b=body();b.update(revision=1,status='reviewed')
    with pytest.raises(ValueError,match='명시적'):w.save(tid,b,lambda:'now')
    b['human_confirmed']=True;w.save(tid,b,lambda:'now')
    assert t.Workspace(*setup).listing()['progress']['GS-02']['reviewed']==1
    b.update(revision=2,status='draft');b['lines'][0]['text']='수정';w.save(tid,b,lambda:'now')
    assert w.listing()['progress']['all']['reviewed']==0

@pytest.mark.parametrize('fault',['duplicate','missing','hash','dimension','truth'])
def test_invalid_links_block_start(setup,fault):
    inv=setup[0];d=json.loads(inv.read_text());rows=d['sections']
    if fault=='duplicate':rows.append(rows[0])
    if fault=='missing':rows.pop()
    if fault=='hash':rows[1]['sha256']='0'*64
    if fault=='dimension':rows[1]['width']=99
    if fault=='truth':rows[0]['truth_v1_id']=None
    inv.write_text(json.dumps(d))
    with pytest.raises(ValueError):t.Workspace(*setup)

def test_invalid_save_and_image_changed(setup):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01'
    b=body();b['lines'][1]['line_id']='L002'
    with pytest.raises(ValueError):w.save(tid,b,lambda:'now')
    b=body();b['status']='frozen'
    with pytest.raises(ValueError):w.save(tid,b,lambda:'now')
    Path(w.rows[tid]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='SHA'):w.save(tid,body(),lambda:'now')

def test_no_escape_or_unsafe_workspace(setup):
    with pytest.raises(ValueError):t.Workspace(setup[0],setup[1],setup[1])
    w=t.Workspace(*setup)
    with pytest.raises(KeyError):w.path('../escape')

def test_http_draft_roundtrip_and_read_only(setup):
    import threading
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    legacy_spec=importlib.util.spec_from_file_location('legacy_review',ROOT/'docs/ai-experiments/tools/ocr_truth/truth.py')
    legacy=importlib.util.module_from_spec(legacy_spec);legacy_spec.loader.exec_module(legacy)
    w=t.Workspace(*setup)
    server=legacy.ThreadingHTTPServer(('127.0.0.1',0),t.handler_class(legacy.Handler,w,legacy.HERE,lambda:'now'))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def post(tid,data,origin=None):
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        return urlopen(Request(base+'/api/truth/'+tid,data=json.dumps(data).encode(),headers=headers,method='POST'))
    try:
        with urlopen(base+'/') as r:assert b'lines[]' in r.read()
        with urlopen(base+'/api/list') as r:assert len(json.load(r)['items'])==3
        with urlopen(base+'/image/GS-01_001__sec_1_01') as r:assert r.read()==Path(w.rows['GS-01_001__sec_1_01']['path']).read_bytes()
        with post('GS-02_001__sec_1_01',body()) as r:assert json.load(r)['revision']==1
        with pytest.raises(HTTPError) as e:post('GS-01_001__sec_1_01',body())
        assert e.value.code==403
        with pytest.raises(HTTPError) as e:post('GS-03_001__sec_1_01',body(),'https://external.invalid')
        assert e.value.code==403
    finally:
        server.shutdown();server.server_close();thread.join()
