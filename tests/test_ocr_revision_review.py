"""합성 초안만 저장한다. 실제 사용자 검수/승인을 생성하지 않는다."""
import sys
import json
from pathlib import Path
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'docs/ai-experiments/tools/ocr_truth'))
from revision_review import RevisionWorkspace,text_hash,digest

@pytest.fixture
def workspace(tmp_path):
    p=tmp_path/'package';p.mkdir();im=p/'source.png';Image.new('RGB',(10,20)).save(im)
    lines=[{'line_id':'L001','text':'테스트'}]
    d=dict(id='GS-01_001__sec_1_01',sample='GS-01_001',section_key='sec_1_01',
           image=dict(path=str(im),sha256=digest(im),width=10,height=20),lines=lines,parent_lines=lines,
           revision=1,text_sha256=text_hash(lines),status='needs_attention',note='합성',read_only=False,
           human_reviews=[],history=[],approved=False,frozen=False)
    f=p/'draft.json';f.write_text(json.dumps(d),encoding='utf8')
    (p/'manifest.json').write_text(json.dumps(dict(status='unfrozen_draft',approved=False,frozen=False,scope=1,
        sections=[dict(id=d['id'],path=f.name,sha256=digest(f),text_sha256=d['text_sha256'])])),encoding='utf8')
    return RevisionWorkspace(p,tmp_path/'.cache/review'),d

def test_read_only_seed_and_no_auto_review(workspace):
    w,d=workspace
    assert w.listing()['progress']['all']['reviewed']==0
    assert not w.drafts.exists()
    assert not w.load(d['id'])['human_review_current']

def test_human_hash_revision_and_stale(workspace):
    w,d=workspace;before=digest(w.package/'draft.json')
    body=dict(revision=1,lines=d['lines'],status='reviewed',human_confirmed=True,note='합성 검수')
    saved=w.save(d['id'],body,lambda:'now')
    assert saved['human_reviews'][0]['text_sha256']==saved['text_sha256']
    assert saved['human_reviews'][0]['revision']==2
    assert w.load(d['id'])['human_review_current'] and not saved['approved'] and not saved['frozen']
    with pytest.raises(ValueError):w.save(d['id'],body,lambda:'now')
    body.update(revision=2,status='draft',lines=[dict(line_id='L001',text='수정')])
    saved=w.save(d['id'],body,lambda:'now')
    assert not w.load(d['id'])['human_review_current']
    assert len(saved['human_reviews'])==1
    assert digest(w.package/'draft.json')==before

def test_no_ai_or_unconfirmed_review(workspace):
    w,d=workspace;b=dict(revision=1,lines=d['lines'],status='reviewed')
    with pytest.raises(ValueError):w.save(d['id'],b,lambda:'now')
    with pytest.raises(ValueError):w.save(d['id'],b,lambda:'now',actor='AI')

def test_tampering_and_unknown_id(workspace):
    w,d=workspace
    with pytest.raises(KeyError):w.load('../outside')
    (w.package/'source.png').write_bytes(b'bad')
    with pytest.raises(ValueError):w.save(d['id'],dict(revision=1,lines=d['lines'],status='draft'),lambda:'now')
