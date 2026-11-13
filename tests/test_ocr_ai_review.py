"""실제 GT/모델을 사용하지 않는 AI 검수·표본·예산 테스트."""
import copy
from datetime import datetime, timedelta, timezone
import json
import pytest
from test_ocr_text_review import setup, t, body
import ai_review as a


def response(**changes):
    return dict(lines=[],checks=a.KINDS,issues=[],reason='합성 이미지 전체와 대조: 문제 없음',complete=True,**changes)


def draft(w,tid):
    return w.save(tid,body(),lambda:'test')


def test_request_no_baseline_or_truth_leak(setup):
    w=t.Workspace(*setup);d=draft(w,'GS-02_001__sec_1_01')
    w.rows[d['id']]['old_ocr_path']='SECRET_OCR_OUTPUT'
    p=a.request(d,'transcribe')
    assert set(p)=={'prompt','schema'} and '뒤' not in json.dumps(p,ensure_ascii=False)
    r=a.request(d,'review')
    assert set(r)=={'prompt','schema','candidate_lines'} and r['candidate_lines']==d['lines']
    assert 'SECRET_OCR_OUTPUT' not in json.dumps(r)
    with pytest.raises(PermissionError):a.request(w.load('GS-01_001__sec_1_01'),'transcribe')


def test_proposal_apply_and_stale_result(setup):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01'
    data=response();data['lines']=['가','', '나']
    p=a.run_one(w,tid,'transcribe',a.MockProvider(data))
    assert not w.path(tid).exists()
    d=a.adopt(w,tid,p['run_id'],0)
    assert [l['text'] for l in d['lines']]==['가','','나'] and d['status']=='draft'
    r=a.run_one(w,tid,'review',a.MockProvider(response()))
    assert a.ai_view(w,tid)['status']=='pass' and r['mode']=='mock'
    assert w.load(tid)['status']=='draft'
    with pytest.raises(ValueError,match='이미'):a.run_one(w,tid,'review',a.MockProvider(response()))
    w.save(tid,{'revision':1,'lines':d['lines'][::-1],'status':'draft'},lambda:'test')
    assert a.ai_view(w,tid)['status']=='stale'
    assert len(a.records_for(w,tid))==2


@pytest.mark.parametrize('kind',a.KINDS)
def test_five_issue_kinds_require_review(setup,kind):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01';draft(w,tid)
    data=response();data['issues']=[dict(type=kind,line_index=-1,reason='이미지 상단 합성 근거')]
    assert a.run_one(w,tid,'review',a.MockProvider(data))['status']=='needs_review'


@pytest.mark.parametrize('fault',['missing','unknown','index','reason','incomplete','truncated'])
def test_bad_response_never_passes(setup,fault):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01';draft(w,tid);d=response()
    if fault=='missing':d['checks']=[]
    if fault=='unknown':d['issues']=[dict(type='other',line_index=-1,reason='x')]
    if fault=='index':d['issues']=[dict(type='omission',line_index=999,reason='x')]
    if fault=='reason':d['reason']=''
    if fault=='incomplete':d['complete']=False
    provider=a.MockProvider(d)
    if fault=='truncated':provider.generate=lambda *_:dict(text='{}',usage=None,model_version='mock',finish_reason='MAX_TOKENS')
    r=a.run_one(w,tid,'review',provider)
    assert r['status']==('needs_review' if fault=='incomplete' else 'failed')


def test_stratified_sample_reproducible():
    items={f'GS-{g:02}_{i:03}':dict(status='pass') for g,n in [(1,21),(2,43),(3,36)] for i in range(n)}
    chosen=a.sample_ids(items)
    assert len(chosen)==22 and chosen==a.sample_ids(dict(reversed(list(items.items()))))
    assert [sum(t.startswith(f'GS-{g:02}') for t in chosen) for g in [1,2,3]]==[5,9,8]
    assert a.sample_ids({'GS-01_0':dict(status='needs_review')})==[]


def test_plan_errors_expand_and_draft_change_invalidates(setup):
    w=t.Workspace(*setup)
    for tid in ['GS-02_001__sec_1_01','GS-03_001__sec_1_01']:
        draft(w,tid);a.run_one(w,tid,'review',a.MockProvider(response()))
    plan=a.make_plan(w);assert len(plan['sample'])==2
    assert a.make_plan(w)==plan
    event=dict(id=plan['sample'][0],plan_id=plan['id'],outcome='error_found',reason='합성 오류 발견',human_confirmed=True)
    a.save_human(w,event)
    assert a.plan_view(w)['expansion_required']
    assert w.load(event['id'])['status']=='draft'
    event['outcome']='no_error';a.save_human(w,event)
    assert a.plan_view(w)['expansion_required']
    d=w.load(event['id']);w.save(event['id'],dict(revision=1,lines=d['lines'],status='draft',note='edit'),lambda:'test')
    assert a.plan_view(w)['stale']
    with pytest.raises(ValueError):a.save_human(w,event)


def approval(w):
    return dict(approved=True,authority='synthetic test only',model=a.MODEL,inventory_sha256=w.inventory_hash,
                sections=list(w.rows),actions=['review'],max_calls=2,max_amount=1,
                expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),prompt_bundle_sha256=a.bundle_hash())


def test_live_gate_no_call_and_budget_unknown_failure(setup):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01';draft(w,tid)
    class FakeLive(a.MockProvider):
        mode='live';model=a.MODEL
        def generate(self,*args):
            self.called+=1
            raise TimeoutError('secret must not be logged')
    provider=FakeLive(response());provider.called=0
    with pytest.raises(PermissionError):a.run_one(w,tid,'review',provider)
    assert provider.called==0
    ap=approval(w);ap['max_amount']=0.34
    r=a.run_one(w,tid,'review',provider,ap,True)
    assert provider.called==1 and r['status']=='failed' and 'secret' not in json.dumps(r)
    other='GS-03_001__sec_1_01';draft(w,other)
    with pytest.raises(PermissionError):a.run_one(w,other,'review',provider,ap,True)
    assert provider.called==1
    ledger=json.loads((w.drafts/'_ai/budget.json').read_text())
    assert ledger['spent_or_reserved']=='0.34' and ledger['calls']==1


def test_usage_settlement_and_call_cap(setup):
    w=t.Workspace(*setup)
    class FakeLive(a.MockProvider):
        mode='live';model=a.MODEL
        def generate(self,*args):
            return dict(text=json.dumps(response()),usage=dict(prompt_token_count=1000,candidates_token_count=100,thoughts_token_count=0),model_version=a.MODEL,finish_reason='STOP')
    ap=approval(w);ap['max_calls']=1
    tid='GS-02_001__sec_1_01';draft(w,tid)
    r=a.run_one(w,tid,'review',FakeLive(response()),ap,True)
    assert r['estimated_usd']=='0.00055'
    other='GS-03_001__sec_1_01';draft(w,other)
    with pytest.raises(PermissionError):a.run_one(w,other,'review',FakeLive(response()),ap,True)

def test_model_change_halts_and_no_secret_record(setup):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01';draft(w,tid)
    class Wrong(a.MockProvider):
        mode='live';model=a.MODEL
        def generate(self,*args):
            return dict(text=json.dumps(response()),usage=None,model_version='another-model',finish_reason='STOP')
    r=a.run_one(w,tid,'review',Wrong(response()),approval(w),True)
    assert r['status']=='failed'
    assert json.loads((w.drafts/'_ai/budget.json').read_text())['halted']


def test_plan_requires_all_and_mandatory_not_sampled(setup):
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01';draft(w,tid)
    d=response();d['issues']=[dict(type='omission',line_index=-1,reason='test')]
    a.run_one(w,tid,'review',a.MockProvider(d))
    with pytest.raises(ValueError):a.make_plan(w)
    other='GS-03_001__sec_1_01';draft(w,other);a.run_one(w,other,'review',a.MockProvider(response()))
    plan=a.make_plan(w)
    assert plan['mandatory']==[tid] and plan['sample']==[other]


def test_http_ai_apply_plan_and_human(setup):
    import threading
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    import truth as legacy
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01'
    d=response();d['lines']=['합성 전사']
    rec=a.run_one(w,tid,'transcribe',a.MockProvider(d))
    server=legacy.ThreadingHTTPServer(('127.0.0.1',0),t.handler_class(legacy.Handler,w,legacy.HERE,legacy.now))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def post(path,data):
        with urlopen(Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'},method='POST')) as r:return json.load(r)
    try:
        with urlopen(base+'/api/ai/'+tid) as r:assert json.load(r)['proposal']['mode']=='mock'
        saved=post('/api/adopt/'+tid,dict(run_id=rec['run_id'],revision=0))
        assert saved['lines'][0]['text']=='합성 전사' and saved['status']=='draft'
        a.run_one(w,tid,'review',a.MockProvider(response()))
        other='GS-03_001__sec_1_01';draft(w,other);a.run_one(w,other,'review',a.MockProvider(response()))
        plan=post('/api/plan',{})
        event=dict(id=tid,plan_id=plan['id'],outcome='error_found',reason='synthetic only',human_confirmed=True)
        post('/api/human',event)
        with urlopen(base+'/api/plan') as r:assert json.load(r)['expansion_required']
        with pytest.raises(HTTPError):post('/api/adopt/GS-01_001__sec_1_01',dict(run_id=rec['run_id'],revision=0))
    finally:
        server.shutdown();server.server_close();thread.join()

def test_safe_api_error_allowlist_redacts(monkeypatch):
    from google.genai.errors import ClientError
    monkeypatch.setenv('GEMINI_API_KEY','test-secret-key-value')
    e=ClientError(403,{'error':{'code':403,'status':'PERMISSION_DENIED',
        'message':'API key test-secret-key-value blocked at https://example.invalid/?key=abc Bearer abc123',
        'details':[{'reason':'API_KEY_INVALID','metadata':{'credential':'do-not-save'}}]}})
    d=a.safe_error(e);s=json.dumps(d)
    assert d['http_status']==403 and d['reasons']==['API_KEY_INVALID']
    assert 'test-secret-key-value' not in s and 'abc123' not in s and 'do-not-save' not in s and 'https://' not in s
    assert a.safe_error(RuntimeError('private request body'))=={'class':'RuntimeError'}


def test_authorized_one_retry_preserves_prior_budget(setup):
    from google.genai.errors import ClientError
    w=t.Workspace(*setup);tid='GS-02_001__sec_1_01';draft(w,tid)
    class Reject(a.MockProvider):
        mode='live';model=a.MODEL
        def generate(self,*args):raise ClientError(400,{'error':{'status':'INVALID_ARGUMENT','message':'Synthetic rejection'}})
    ap=approval(w);p=Reject(response())
    first=a.run_one(w,tid,'review',p,ap,True)
    original=[x for x in (w.drafts/'_ai'/tid).glob('*/result.json')][0]
    before=original.read_bytes()
    retry=dict(approved=True,authority='synthetic one retry',run_id=first['run_id'])
    second=a.run_one(w,tid,'review',p,ap,True,retry=retry)
    assert second['retry_of']==first['run_id'] and second['error']['http_status']==400
    assert original.read_bytes()==before
    with pytest.raises(ValueError):a.run_one(w,tid,'review',p,ap,True,retry=retry)
    ledger=json.loads((w.drafts/'_ai/budget.json').read_text())
    assert ledger['calls']==2 and ledger['spent_or_reserved']=='0.68'
