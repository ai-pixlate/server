import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import Mock, patch
from tools.baseline import gs03_recovery as r
from tools.baseline.be import BudgetStop
from tools.baseline.core import write_new


class RecoveryTest(unittest.TestCase):
    def history(self, root, rows):
        p=Path(root)/'usage.jsonl'
        p.write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf-8');return p
    def row(self,n,status='done',cost=.01):
        return {'call':n,'status':status,'cost':cost,'model':r.BUDGET['model']}
    def test_unknown_reservation_and_call_numbers_are_inherited(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.history(tmp,[self.row(1,'reserved',None),self.row(1),self.row(2,'done',None),self.row(3,'failed',None)])
            events=[];meter=r.CumulativeMeter(r.BUDGET,events.append,history=p)
            self.assertEqual(meter.calls,3);self.assertAlmostEqual(meter.charged,2.09)
            meter.call(lambda **kw:SimpleNamespace(usage_metadata=None),model=r.BUDGET['model'])
            self.assertEqual(events[0]['call'],4);self.assertAlmostEqual(meter.charged,3.13)
    def test_inherited_budget_blocks_before_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rows in ([self.row(i,cost=0) for i in range(1,501)], [self.row(i,cost=None) for i in range(1,25)]):
                meter=r.CumulativeMeter(r.BUDGET,lambda e:None,history=self.history(tmp,rows));fn=Mock()
                with self.assertRaises(BudgetStop):meter.call(fn,model=r.BUDGET['model'])
                fn.assert_not_called()
    def test_bad_usage_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rows in ([self.row(2)], [self.row(1,cost=-1)], [self.row(1,cost=float('nan'))], [self.row(1,'reservation_exceeded',2)]):
                with self.assertRaises(ValueError):r.budget_state(self.history(tmp,rows))
    def test_missing_or_changed_object_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sha='a'*64
            (root/'objects.jsonl').write_text(json.dumps({'key':'x','sha256':sha,'bytes':1,'content_type':'image/png'}))
            with self.assertRaises((ValueError,FileNotFoundError)):r.object_index(root)
            (root/sha).write_bytes(b'x')
            with self.assertRaises(ValueError):r.object_index(root)
    def n3(self):
        return {'job':[{'current_step':'N3','status':'review'}], 'job_async_task':[{'status':'done','stage':'judge'}],
                'task_handoff':[{'state':'adopted'}], 'deliverable':[],'deliverable_section':[],
                'source_image':[{}]*18,'seller':[{}]}
    def test_n4_or_unfinished_cannot_be_recovered(self):
        r.check_n3(self.n3())
        for key,value in [('job',[{'current_step':'N4','status':'processing'}]),('job_async_task',[{'status':'running','stage':'judge'}]),
                          ('task_handoff',[{'state':'pending'}]),('job_async_task',[{'status':'done','stage':'label'}])]:
            d=self.n3();d[key]=value
            with self.assertRaises(ValueError):r.check_n3(d)
    def test_no_wait_records_automation_not_human(self):
        state={'values':{'1':'unchanged'},'warnings':[{'blockId':1,'code':'translation_failed'}]}
        adapter=SimpleNamespace(snapshot=lambda stage:copy.deepcopy(state));c=Mock()
        with patch('builtins.input',side_effect=AssertionError('입력 대기 금지')):
            result=r.NoWaitSession(c,adapter).review('N5')
        self.assertEqual(result['original'],result['final']);self.assertEqual(result['acknowledgedWarnings'],state['warnings'])
        evidence=c.evidence.call_args.args[0]
        self.assertIs(evidence['human_reviewed'],False);self.assertEqual(evidence['actor'],'experiment_automation')
        c.event.assert_not_called()
    def test_unknown_warning_or_state_change_blocks(self):
        c=Mock();adapter=Mock()
        adapter.snapshot.return_value={'values':{},'warnings':[{'code':'unknown'}]}
        with self.assertRaises(ValueError):r.NoWaitSession(c,adapter).review('N5')
        adapter.snapshot.side_effect=[{'values':{},'warnings':[]},{'values':{'x':'changed'},'warnings':[]}]
        with self.assertRaises(ValueError):r.NoWaitSession(c,adapter).review('N5')
    def test_driver_skips_n2_and_preserves_checkpoints(self):
        state={'current_step':'N3','status':'review'};order=[];analysis=Mock()
        warnings=[{'blockId':1,'code':'translation_failed'}]
        def proceed(*args):order.append('N4');state['current_step']='N5'
        def confirm(db,jid,seller,acks):self.assertEqual(acks,warnings);order.append('N6');state['current_step']='N6'
        def review(stage,jid):order.append(stage);return {'acknowledgedWarnings':warnings}
        result=r.drive_from_n3(1,1,lambda:nullcontext(None),lambda *a,**kw:[state],SimpleNamespace(drain=lambda:None),analysis,None,
            SimpleNamespace(proceed=proceed),SimpleNamespace(confirm=confirm,can_save=lambda *a:True,unresolved_warnings=lambda *a:warnings),
            lambda x:None,lambda cp,jid:order.append(cp),review,lambda *a:None)
        self.assertEqual(result,'completed');analysis.start_analysis.assert_not_called()
        self.assertEqual(order,['N3','N4','machine_snapshot','N5','N6','service_final'])
    def test_no_execution_approval_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_new(Path(tmp)/'manifest.json',{'evaluation_run_id':'clone','recovery':{'seed':{'calls':89,'occupied_usd':3.69037875},'execution_approval':None}})
            with self.assertRaisesRegex(ValueError,'유료 실행 승인'):r.require_execution(tmp)
    def test_runner_blocks_before_product_or_gpu_loading(self):
        from tools.baseline import gs03_runner
        with tempfile.TemporaryDirectory() as tmp:
            write_new(Path(tmp)/'manifest.json',{'evaluation_run_id':'clone','recovery':{'seed':{'calls':89,'occupied_usd':3.69037875},'execution_approval':None}})
            with patch.object(r,'preflight'), patch.object(gs03_runner,'initialize_native_runtime') as native:
                with self.assertRaisesRegex(ValueError,'유료 실행 승인'):
                    gs03_runner._run_prepared(tmp,True,recovery_url_file='dummy')
                native.assert_not_called()
            self.assertFalse((Path(tmp)/'started.json').exists())
    def test_official_scope_unchanged_and_recovery_explicit(self):
        from tools.baseline.scope import automatic_scope,automatic_only
        self.assertTrue(automatic_only({'measurement_scope':automatic_scope()}))
        scope=automatic_scope();scope['human_intervention']='preauthorized_no_review'
        with self.assertRaises(ValueError):automatic_only({'measurement_scope':scope})
        self.assertFalse(automatic_only({'measurement_scope':scope,'recovery':{'mode':r.MODE}}))
    @unittest.skipUnless(os.name=='nt','Windows 독립 실행 검증')
    def test_detached_child_survives_parent(self):
        from tools.baseline import gs03_launch
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);done=out/'done.txt'
            child_code=f"import time;from pathlib import Path;time.sleep(1);Path({str(done)!r}).write_text('ok')"
            code=f"import sys;sys.path.insert(0,{str(Path(gs03_launch.__file__).resolve().parents[2])!r});from pathlib import Path;from tools.baseline.gs03_launch import detached_process;detached_process([sys.executable,'-B','-c',{child_code!r}],Path({str(out)!r}))"
            subprocess.run([sys.executable,'-I','-B','-c',code],check=True,capture_output=True,timeout=20)
            end=time.monotonic()+10
            while not done.exists() and time.monotonic()<end:time.sleep(.1)
            self.assertEqual(done.read_text(),'ok')


if __name__=='__main__':unittest.main()
