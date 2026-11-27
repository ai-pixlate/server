import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import nullcontext
from tools.baseline.gs03_n4_execution import SingleRetryMeter, drive_with_retry, verify_artifact
from tools.baseline.gs03_supplement import BUDGET
from tools.baseline.be import BudgetStop


class SingleExecutionTest(unittest.TestCase):
    def test_original_background_reference_is_verified(self):
        a={'state':'adopted','verified_key':None,'source_ref':{'key':'original','sha256':'abc'},'sha256':'abc'}
        verify_artifact(a,{'original':{'sha256':'abc'}})
        with self.assertRaises(ValueError):verify_artifact(a,{})
        a['source_ref']['sha256']='changed'
        with self.assertRaises(ValueError):verify_artifact(a,{'original':{'sha256':'abc'}})
    def test_one_request_even_if_failed_and_wrong_scope_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'usage.jsonl'
            rows=[{'call':n,'status':'done','cost':0.05,'model':BUDGET['model']} for n in range(1,164)]
            # 정확한 승계 합계이며 각 요청 예약 상한 이내.
            rows[-1]['cost']=1.01462925;rows[-2]['cost']=0.15
            p.write_text('\n'.join(json.dumps(r) for r in rows),'utf-8')
            blocked=SingleRetryMeter(BUDGET,lambda e:None,history=p,allowed=lambda:False)
            fn=Mock()
            with self.assertRaises(BudgetStop):blocked.call(fn,model=BUDGET['model'])
            fn.assert_not_called()
            meter=SingleRetryMeter(BUDGET,lambda e:None,history=p,allowed=lambda:True)
            first=Mock(side_effect=RuntimeError('API failure'))
            with self.assertRaises(RuntimeError):meter.call(first,model=BUDGET['model'])
            with self.assertRaises(BudgetStop):meter.call(fn,model=BUDGET['model'])
            self.assertEqual(meter.calls,164);fn.assert_not_called()

    def test_success_and_failure_checkpoint_boundary(self):
        for success in (False,True):
            state={'current_step':'N4','status':'failed'};events=[]
            task={'retry_count':2,'max_retry':2,'retry_origin':'user','input_manifest':{'targets':[{'block_id':232,'revision':0}]}}
            def query(sql,**kw):return [task if 'job_async_task' in sql else state]
            def drain():
                if success:state.update(current_step='N5',status='review')
            def retry(j,t,**kw):self.assertEqual(t,227);return {'taskId':265}
            final=SimpleNamespace(unresolved_warnings=lambda *a:[],confirm=lambda *a:None,can_save=lambda *a:True)
            result=drive_with_retry(retry,1,1,lambda:nullcontext(None),query,SimpleNamespace(drain=drain),final,
                    lambda x:None,lambda cp,j:events.append(cp),lambda *a:{'acknowledgedWarnings':[]},lambda *a:None)
            self.assertEqual(result,'completed' if success else 'failed')
            self.assertEqual(events,['machine_snapshot','service_final'] if success else [])


if __name__=='__main__':unittest.main()
