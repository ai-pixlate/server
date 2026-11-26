"""실제 모델·DB 없이 보완 실험 경계를 검증한다."""
import ast
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from tools.baseline import gs03_supplement as s, gs03_runner as runner, be
from tools.baseline.core import write_new

class SupplementTest(unittest.TestCase):
    def test_only_merge_timeout_changes(self):
        raw='[merge]\nllm_timeout_s = 60 # same\nllm_model="gemini-3.8-flash"\n[judge]\nllm_timeout_s = 60\n'
        self.assertEqual(s.override_config(raw),raw.replace('= 60 #','= 120 #'))
        with self.assertRaises(ValueError):s.override_config(raw.replace('= 60 #','= 90 #'))
    def test_scope_and_unapproved_execution(self):
        self.assertEqual(len(s.SOURCES),18)
        self.assertTrue(all(x.startswith('GS-03') for x in s.SOURCES))
        with tempfile.TemporaryDirectory() as tmp:
            write_new(Path(tmp)/'manifest.json',{'cost_approval':{'user_execution':None}})
            with patch.object(runner,'check_runtime') as check:
                with self.assertRaisesRegex(ValueError,'승인 보류'):runner._run_prepared(tmp,True)
                check.assert_not_called()
            with self.assertRaises(ValueError):runner._run_prepared(tmp,False)
    def test_runner_selects_only_gs03(self):
        tree=ast.parse(Path(runner.__file__).read_text(encoding='utf-8'))
        cases=[ast.literal_eval(n.iter) for n in ast.walk(tree) if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='case']
        self.assertEqual(cases,[('GS-03',)])
    def test_budget_stops_before_fake_call(self):
        for changes in [{'max_calls':1},{'max_amount':1.04}]:
            b={**s.BUDGET,**changes};f=Mock(return_value=object());meter=be.Meter(b,lambda e:None)
            meter.call(f,model=b['model'])
            with self.assertRaises(be.BudgetStop):meter.call(f,model=b['model'])
            self.assertEqual(f.call_count,1)
    def test_failed_call_reservation_remains(self):
        b={**s.BUDGET,'max_amount':1.04};meter=be.Meter(b,lambda e:None);f=Mock(side_effect=RuntimeError('synthetic'))
        with self.assertRaises(RuntimeError):meter.call(f,model=b['model'])
        with self.assertRaises(be.BudgetStop):meter.call(f,model=b['model'])
        self.assertEqual(f.call_count,1)
    def test_local_storage_immutable_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=be.FileStore(Path(tmp)/'objects');store.put('x',b'original');sha=store.index['x']['sha256']
            store.put('x',b'new');self.assertEqual((store.root/sha).read_bytes(),b'original')
    def test_canonical_reference_order(self):
        self.assertEqual(s.canonical({'rows':[{'id':2},{'id':1}]}),s.canonical({'rows':[{'id':1},{'id':2}]}))
if __name__=='__main__':unittest.main()
