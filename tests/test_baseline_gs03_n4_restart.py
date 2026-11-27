import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from contextlib import nullcontext
from tools.baseline import gs03_n4_restart as r
from tools.baseline.gs03_recovery import CumulativeMeter
from tools.baseline.gs03_supplement import BUDGET
from tools.baseline.be import BudgetStop


class RestartTest(unittest.TestCase):
    def test_no_paid_execution_without_explicit_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'manifest.json').write_text(json.dumps({'evaluation_run_id':'new','recovery':{'execution_approval':None}}))
            with self.assertRaisesRegex(ValueError,'별도 비용 승인'):r.require_execution(p)

    def test_driver_only_official_abort_and_n3_driver(self):
        abort=Mock(return_value={'returnTo':'N3'});downstream=Mock();analysis=Mock();record=Mock()
        with patch.object(r,'official_abort',abort),patch.object(r,'drive_from_n3',return_value='completed') as driver:
            result=r.drive(1,2,lambda:nullcontext('db'),Mock(),Mock(),analysis,Mock(),downstream,Mock(),record,Mock(),Mock(),Mock())
            self.assertEqual(result,'completed');abort.assert_called_once_with('db',1,2)
            self.assertIs(driver.call_args.args[7].proceed,r.official_proceed)
            analysis.assert_not_called();downstream.assert_not_called()
            self.assertFalse(record.call_args.args[0]['human_reviewed'])
        with patch.object(r,'official_abort',return_value={'returnTo':'N1'}),patch.object(r,'drive_from_n3') as driver:
            with self.assertRaises(ValueError):r.drive(1,2,lambda:nullcontext('db'),*[Mock() for _ in range(10)])
            driver.assert_not_called()

    def test_uncertain_reservation_preserved_and_budget_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'usage.jsonl'
            rows=[{'call':n,'model':BUDGET['model'],'status':'done','cost':0.0} for n in range(1,165)]
            for row in rows[-9:]:row.update(status='reserved',cost=None)
            rows[0]['cost']=.89462925
            p.write_text('\n'.join(json.dumps(x) for x in rows),'utf-8')
            meter=CumulativeMeter(BUDGET,lambda e:None,history=p)
            self.assertEqual(meter.calls,164);self.assertAlmostEqual(meter.charged,10.25462925)
            call=Mock(side_effect=RuntimeError('synthetic unknown result'))
            for _ in range(14):
                with self.assertRaises(RuntimeError):meter.call(call,model=BUDGET['model'])
            with self.assertRaises(BudgetStop):meter.call(call,model=BUDGET['model'])
            self.assertEqual(call.call_count,14)
            self.assertLessEqual(meter.charged,25)

if __name__=='__main__':unittest.main()
