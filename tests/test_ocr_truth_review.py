"""검수 도구의 합성 테스트. 실제 정답/추론/채점을 실행하지 않는다."""
import copy
import importlib.util
import json
from pathlib import Path
import threading
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("truth_review", ROOT / "docs/ai-experiments/tools/ocr_truth/truth.py")
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

@pytest.fixture
def draft(tmp_path, monkeypatch):
    Image.new("RGB", (40,40)).save(tmp_path / "image.png")
    monkeypatch.setattr(t, "IMAGE_ROOT", tmp_path)
    monkeypatch.setattr(t, "TRUTH_DIR", tmp_path)
    monkeypatch.setattr(t, "all_ids", lambda: ["synthetic"])
    d = {"id":"synthetic", "kind":"test", "image":{"path":"image.png", "sha256":t.jsonio.sha256_file(tmp_path / "image.png"), "width":40,"height":40},
         "review":{"status":"draft"}, "history":[], "lines":[
        {"line_id":"L001","text":"A","bbox":{"x":0,"y":0,"w":20,"h":10},"tags":[],"verified":False,"note":""},
        {"line_id":"L002","text":"B","bbox":{"x":5,"y":0,"w":20,"h":10},"tags":[],"verified":False,"note":""}]}
    t.save(d)
    return d

def test_overlap_tag_exceptions(draft):
    assert len(t.check_doc(draft)) == 1
    draft['lines'][0]['tags'] = ['ink_overlap']
    assert len(t.check_doc(draft)) == 1
    draft['lines'][1]['tags'] = ['ink_overlap']
    assert t.check_doc(draft) == []
    draft['lines'][0]['tags'] = ['rotated']
    draft['lines'][1]['tags'] = []
    assert t.check_doc(draft) == []

def test_edit_invalidates_review_and_records_history(draft):
    draft['lines'][0]['verified'] = True
    lines = copy.deepcopy(draft['lines'])
    lines[0]['text'] = 'changed'
    t.merge_edit(draft, lines, 'synthetic test')
    assert not draft['lines'][0]['verified']
    assert draft['history']

def test_read_only_http_and_image(draft, monkeypatch):
    monkeypatch.setattr(t, 'READ_ONLY', True)
    before = t.truth_path('synthetic').read_bytes()
    server = t.ThreadingHTTPServer(('127.0.0.1',0), t.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base+'/api/list') as response:
            data=json.load(response)
        assert data['read_only'] and data['items'][0]['verified']==0
        with urlopen(base+'/image/synthetic') as response:
            assert response.read() == t.image_file(draft).read_bytes()
        with pytest.raises(HTTPError) as e:
            urlopen(Request(base+'/api/truth/synthetic', data=b'{}',method='POST'))
        assert e.value.code == 403
        assert before == t.truth_path('synthetic').read_bytes()
    finally:
        server.shutdown();server.server_close();thread.join()

def test_image_mismatch_blocks_start(draft):
    from argparse import Namespace
    t.image_file(draft).write_bytes(b'wrong image')
    with pytest.raises(SystemExit, match='SHA-256'):
        t.cmd_serve(Namespace(image_root=t.IMAGE_ROOT, read_only=True, port=0))

def test_proposed_normalization_and_order():
    # 제안의 합성 확인만 수행하며 채점기를 구현하지 않는다.
    import re, unicodedata
    ws = '[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]'
    def aux(s): return re.sub(ws+'+', ' ', unicodedata.normalize('NFC',s)).strip(' ')
    assert aux('  A\t B  ') == 'A B'
    assert aux('Ａ') != aux('A')
    assert aux('가') == '가'
    assert aux('A\u200bB') == 'A\u200bB'
    assert ' '.join(filter(None,map(aux,[' A ', 'B']))) == 'A B'
    assert re.sub(ws, '', unicodedata.normalize('NFC',' A B ')) == 'AB'
    assert sorted([(0,0,10,20,'L002'),(0,0,10,20,'L001')])[0][-1] == 'L001'
    assert sorted([(0,0,10,20,10),(0,0,10,20,2)])[0][-1] == 2
