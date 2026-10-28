"""사람 입력·체크포인트·운영 집계 연결을 가짜 제품과 검증. 모델/DB 호출 없음."""
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_baseline_protocol as fixtures
from tools.baseline import protocol as p, operations, be
from tools.baseline.interaction import Collector, HumanSession, HumanRequired
from tools.baseline.core import read


class InteractionTest(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProtocolTest();self.fixture.setUp()
        self.c=Collector(self.fixture.root,1,self.fixture.sources)
        self.state={'values':{'1':'include','2':'exclude'},'source_by_target':dict(zip(('1','2'),self.fixture.sources)),'warnings':[]}
        self.adapter=Mock()
        self.adapter.snapshot.side_effect=lambda stage:copy.deepcopy(self.state)
        def apply(stage,answer,state):self.state['values'][answer['target_id']]=answer['value']
        self.adapter.apply.side_effect=apply
    def tearDown(self):self.fixture.tearDown()
    def response(self,path,**kw):return {'request_id':read(path)['request_id'],'actor':'human-tester',**kw}
    def test_selection_and_first_final_history(self):
        queue=[{'action':'edit','target_id':'1','value':'exclude'}, {'action':'edit','target_id':'1','value':'include'}, {'action':'continue'}]
        h=HumanSession(self.c,self.adapter,lambda path:self.response(path,**queue.pop(0)))
        h.review('N3')
        _,d=p.load(self.c.root)
        changes=[r for r in d['execution'] if r['record_kind']=='user_edit']
        self.assertEqual([r['change_count'] for r in changes],[0,0])
        self.assertEqual(self.adapter.apply.call_count,2)
        self.assertEqual(p.validate(self.c.root),[])
    def test_stale_response_never_edits(self):
        h=HumanSession(self.c,self.adapter,lambda _: {'request_id':'old','actor':'human-tester','action':'continue'})
        with self.assertRaises(HumanRequired):h.review('N3')
        self.adapter.apply.assert_not_called()
    def test_n5_does_not_auto_ack(self):
        self.state['values']={'1':'original','2':''};self.state['warnings']=[{'blockId':2,'code':'translation_failed'}]
        h=HumanSession(self.c,self.adapter,lambda path:self.response(path,action='continue'))
        with self.assertRaises(HumanRequired):h.review('N5')
    def test_claim_survives_new_collector(self):
        self.c.claim()
        with self.assertRaises(FileExistsError):Collector(self.c.root,2,self.fixture.sources).claim()
    def test_driver_needs_human_before_analysis(self):
        analysis=Mock()
        with self.assertRaises(HumanRequired):be.drive_job(1,1,None,None,None,analysis,None,None,None,None)
        analysis.start_analysis.assert_not_called()
    def test_operation_metrics_no_render_completion_shortcut(self):
        self.c.operation('job_created','N1',{})
        self.c.operation('stage_finished','N6',{'seconds':3})
        self.c.operation('first_n5_confirmed','N5',{'original':{'x':'e\u0301\r\n'},'final':{'x':'é\n'}})
        before=p.aggregate(self.c.root)['metrics']
        self.assertEqual(next(r['value'] for r in before if r['scope']=='all' and r['metric_id']=='OP01' and r['breakdown']=='작업 완료율'),0)
        self.c.operation('job_saved','N6',{})
        self.c.operation('regulation_checked','N2',dict(target_id='s1',measured=True,findings=0,auto_excluded=False,locale_unchecked=False))
        self.c.operation('spec_checked','N6',dict(target_id='file1',passed=False))
        after=p.aggregate(self.c.root)['metrics']
        def value(mid,label):return next(r for r in after if r['scope']=='all' and r['metric_id']==mid and r['breakdown']==label)
        self.assertEqual(value('OP01','작업 완료율')['value'],100)
        self.assertEqual(value('OP06','무수정 승인률')['value'],100)
        self.assertFalse(value('OP06','무수정 승인률')['include_in_presentation'])
        self.assertEqual(value('OP07','규격 검증 통과율')['value'],0)
    def test_duplicates_stop_aggregate(self):
        self.c.operation('stage_finished','N2',{'seconds':3})
        self.c.operation('stage_finished','N2',{'seconds':3})
        with self.assertRaises(ValueError):p.aggregate(self.c.root)


    def test_three_stage_driver_preserves_snapshot_before_human_change(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        state={'current_step':'N1','status':'draft'}; order=[]
        def stage(step): state.update(current_step=step,status='review')
        def human(step,jid):
            order.append('human_'+step)
            return {'original':{'1':'before'},'final':{'1':'after'},'acknowledgedWarnings':[]}
        def checkpoint(cp,jid):order.append(cp)
        def confirm(db,jid,seller,acks):
            self.assertEqual(acks,[])  # 경고 목록을 자동 대입하지 않는다
            stage('N6')
        outcome=be.drive_job(1,1,lambda:nullcontext(None),lambda *a,**k:[state],SimpleNamespace(drain=lambda:None),
            SimpleNamespace(start_analysis=lambda *a:stage('N3')),SimpleNamespace(visible_sections=lambda *a:[{'bucket':'include'}]),
            SimpleNamespace(proceed=lambda *a:stage('N5')),SimpleNamespace(confirm=confirm,can_save=lambda *a:True,
            unresolved_warnings=lambda *a:[{'blockId':1,'code':'translation_failed'}]),lambda w:None,checkpoint,human,self.c.operation)
        self.assertEqual(outcome,'completed')
        self.assertEqual(order,['human_N3','machine_snapshot','human_N5','service_final'])
        self.assertEqual(p.validate(self.c.root),[])

    def test_product_artifact_field_and_missing_preview(self):
        from tools.baseline.product_review import ProductReview
        from PIL import Image
        import io
        store=be.FileStore(self.c.root/'objects')
        buf=io.BytesIO();Image.new('RGB',(3,4)).save(buf,format='PNG')
        store.put('final',buf.getvalue(),'image/png')
        adapter=ProductReview(1,2,None,lambda *a,**k:[{'id':9,'source_image_id':7,'image_url':'final'}],None,store,{7:self.c.sources[0]})
        files=adapter.files('service_final',{})
        self.assertEqual((files[0]['width'],files[0]['height']),(3,4))
        self.c.checkpoint('service_final',{},files)
        self.assertEqual(adapter.files('machine_snapshot',{'sections':[{'id':1,'source_image_id':7,'bucket':'include','render_image_key':None}]}),[])

    def test_retry_collection_does_not_count_user_rerender_as_auto(self):
        from tools.baseline.collection import terminal_attempts
        base=dict(parent_task_id=10,target_count=1,stage='preview',unit_type='section',unit_id=1,error_code=None,started_at=None,finished_at=None)
        snapshot={'section':[{'id':1,'source_image_id':7}], 'job_async_task':[
          dict(base,status='done',attempt_no=1,is_current=False,retry_origin=None),
          dict(base,status='done',attempt_no=2,is_current=True,retry_origin='user')]}
        terminal_attempts(self.c,snapshot,{7:self.c.sources[0]},self.fixture.ev[0])
        metrics=p.aggregate(self.c.root)['metrics']
        rate=next(r for r in metrics if r['scope']=='all' and r['metric_id']=='OP03' and r['breakdown']=='자동 재시도 발생률')
        self.assertEqual(rate['value'],0)
        self.assertEqual(p.validate(self.c.root),[])


    def test_pm_agreement_does_not_authorize_execution(self):
        from tools.baseline.approval import require_approval
        with self.assertRaisesRegex(ValueError,'승인 보류'):require_approval(self.c.root)

    def test_cost_and_fallback_evidence(self):
        self.c.operation('job_created','N1',{})
        self.c.operation('job_saved','N6',{})
        self.c.operation('resource','N2',{'name':'n2_sections','amount':2,'unit':'count'})
        self.c.event('usage','translate',self.fixture.ev[0],session_id=None,purpose='product_generation',
            usage={'calls':1,'prompt_token_count':10,'candidates_token_count':5,'thoughts_token_count':2},
            cost={'amount':0.1,'currency':'USD','basis':'합성 단가'},call_id=1)
        self.c.operation('fallback','style',{'kind':'기본 스타일 사용','target_id':'sec1','eligible':True,'applied':True})
        self.c.operation('fallback','style',{'kind':'기본 스타일 사용','target_id':'sec2','eligible':True,'applied':False})
        metrics=p.aggregate(self.c.root)['metrics']
        r=next(r for r in metrics if r['scope']=='all' and r['metric_id']=='OP03' and r['breakdown']=='기본 스타일 사용')
        self.assertEqual(r['value'],50)
        r=next(r for r in metrics if r['scope']=='all' and r['metric_id']=='OP05' and r['breakdown']=='완료 건당 추정 원가')
        self.assertEqual(r['value'],0.1)
