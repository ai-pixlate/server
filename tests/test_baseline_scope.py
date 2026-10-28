"""무수정 회차/오프라인 환경/예산 차단을 합성 입력으로 검증한다."""
import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_baseline_protocol as fixtures
from tools.baseline import protocol as p, be, preflight
from tools.baseline.scope import automatic_scope, H05_REASON, OP06_NOTE
from tools.baseline.interaction import Collector, HumanSession, HumanRequired
from tools.baseline.core import read, digest, now


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProtocolTest()
        self.fixture.setUp()
        self.root = self.fixture.root
        m = read(self.root / 'manifest.json')
        m['measurement_scope'] = automatic_scope()
        m['reference_freeze'] = {'verified_by': 'synthetic-PM', 'verified_at': now(),
            'evidence_ref': self.fixture.ev[0],
            'reference_file': {'path': 'reference_units.jsonl', 'sha256': digest(self.root / 'reference_units.jsonl')}}
        (self.root / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        self.c = Collector(self.root, 1, self.fixture.sources)
        self.state = {'values': {'1': 'include', '2': 'exclude'},
                      'source_by_target': dict(zip(('1', '2'), self.c.sources)), 'warnings': []}
        self.adapter = Mock()
        self.adapter.snapshot.side_effect = lambda _: copy.deepcopy(self.state)

    def tearDown(self):
        self.fixture.tearDown()

    def response(self, path, **kwargs):
        return {'request_id': read(path)['request_id'], 'actor': '예람', **kwargs}

    def test_no_edit_confirmation_records_evidence_but_not_h05_zero(self):
        human = HumanSession(self.c, self.adapter, lambda path: self.response(path, action='continue', acknowledgedWarnings=[]))
        self.c.operation('job_created', 'N1', {})
        human.review('N3')
        answer = human.review('N5')
        self.c.operation('first_n5_confirmed', 'N5', {k: answer[k] for k in ('original', 'final')})
        self.c.operation('job_saved', 'N6', {})
        self.adapter.apply.assert_not_called()
        data = p.load(self.root)[1]
        edits = [r for r in data['execution'] if r['record_kind'] == 'user_edit']
        self.assertEqual(len(edits), 4)
        self.assertTrue(all(r['change_count'] == 0 and r['original_value'] == r['final_value'] for r in edits))
        metrics = p.aggregate(self.root)['metrics']
        h5 = [r for r in metrics if r['metric_id'] == 'H05']
        self.assertTrue(h5)
        for r in h5:
            self.assertEqual(r['display_reason'], H05_REASON)
            self.assertTrue(all(r[k] is None for k in ('value', 'numerator', 'denominator', 'not_applicable_count', 'undetermined_count')))
            self.assertFalse(r['include_in_presentation'])
        op6 = next(r for r in metrics if r['scope'] == 'all' and r['metric_id'] == 'OP06')
        self.assertEqual(op6['value'], 100)
        self.assertIn(OP06_NOTE, op6['display_reason'])
        self.assertFalse(op6['include_in_presentation'])

    def test_edit_and_missing_human_warning_ack_stop_before_mutation(self):
        for stage, answer in [('N3', {'action': 'edit'}), ('N5', {'action': 'edit'}), ('N5', {'action': 'continue'})]:
            with self.subTest(stage=stage, answer=answer), self.assertRaises(HumanRequired):
                HumanSession(self.c, self.adapter, lambda path: self.response(path, **answer)).review(stage)
        self.adapter.apply.assert_not_called()

    def test_external_change_during_confirmation_is_rejected(self):
        def response(path):
            self.state['values']['1'] = 'exclude'
            return self.response(path, action='continue')
        with self.assertRaises(HumanRequired):
            HumanSession(self.c, self.adapter, response).review('N3')
        self.adapter.apply.assert_not_called()

    def test_changed_history_cannot_be_injected(self):
        with self.assertRaisesRegex(ValueError, '무수정'):
            self.c.event('user_edit', 'N3', self.fixture.ev[0], source_key=self.c.sources[0],
                         source_keys=[self.c.sources[0]], original_value={'x': 'include'},
                         final_value={'x': 'exclude'}, change_count=1, user_retry_count=0)
        with self.assertRaisesRegex(ValueError, '무수정'):
            self.c.operation('first_n5_confirmed', 'N5', {'original': {'x': 'a'}, 'final': {'x': 'b'}})

    def test_new_template_is_no_edit_and_unknown_scope_rejected(self):
        self.assertEqual(p.template()['measurement_scope'], automatic_scope())
        m = read(self.root / 'manifest.json')
        m['measurement_scope']['human_intervention'] = 'auto_approve'
        (self.root / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '측정 범위'):
            p.aggregate(self.root)

    def test_generation_does_not_require_freeze_but_scoring_does(self):
        m = read(self.root / 'manifest.json')
        m.update(product_commit='synthetic', protocol_commit='synthetic', environment={'mock': True},
                 generation_config={'mock': True}, case_inputs={'mock': True},
                 cost_approval={'user_execution': {'approved': True}},
                 reference_version=None, reference_freeze=None, evaluation_sessions=[])
        (self.root / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        self.assertEqual(p.readiness(m, purpose='generation'), [])
        self.assertEqual(p.validate(self.root), [])
        self.assertIn('reference_freeze: 확인 필요', p.readiness(m))
        with self.assertRaisesRegex(ValueError, '동결'):
            p.record(self.root, 'evaluations', self.fixture.evaluation())
        with self.assertRaisesRegex(ValueError, '동결'):
            p.aggregate(self.root)

    def test_frozen_reference_hash_drift_blocks_scoring(self):
        with (self.root / 'reference_units.jsonl').open('a') as f:
            f.write('\n')
        with self.assertRaises(ValueError):
            p.record(self.root, 'evaluations', self.fixture.evaluation())

    def test_wrong_confirmation_actor_cannot_continue(self):
        with self.assertRaisesRegex(HumanRequired, '예람'):
            HumanSession(self.c, self.adapter, lambda path: self.response(path, actor='other', action='continue')).review('N3')
        self.adapter.apply.assert_not_called()

    def test_latest_budget_and_actor_are_required_without_auto_approval(self):
        from tools.baseline.approval import require_approval, TARGET
        from tools.baseline.scope import APPROVED_BUDGET
        from tools.baseline.core import IDS
        m = read(self.root / 'manifest.json')
        sources = [p.source_key(s) for s in IDS]
        user = dict(approved=True, approved_by='synthetic-user', approved_at=now(),
            evidence_ref=self.fixture.ev[0], source_keys=sources, product_commit=TARGET,
            job_grouping='case', official_generations_per_source=1, user_regeneration_allowed=False,
            human_intervention_scope='confirmation_only', human_confirmation_actor='예람', **APPROVED_BUDGET)
        m.update(product_commit=TARGET, source_keys=sources, reference_freeze=None,
            generation_config={'job_grouping': 'case', 'budget': dict(APPROVED_BUDGET)},
            cost_approval={'pm_cost_agreed': True, 'user_execution': user})
        def save():
            (self.root / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        save()
        require_approval(self.root)
        for key, bad in [('max_amount',20),('max_amount',51),('max_calls',1368),('reserve_per_call',0.5)]:
            with self.subTest(key=key, value=bad):
                m['generation_config']['budget'][key] = user[key] = bad
                save()
                with self.assertRaisesRegex(ValueError, '최신 승인 예산'):
                    require_approval(self.root)
                m['generation_config']['budget'][key] = user[key] = APPROVED_BUDGET[key]
        user['human_confirmation_actor'] = 'other'
        save()
        with self.assertRaisesRegex(ValueError, '예람'):
            require_approval(self.root)
        user['approved'] = False
        save()
        with self.assertRaisesRegex(ValueError, '승인 보류'):
            require_approval(self.root)


class EnvironmentLockTest(unittest.TestCase):
    def test_paddle_first_requires_fresh_process(self):
        with patch.object(be.sys, 'platform', 'win32'), patch.dict(be.sys.modules, {'paddle': SimpleNamespace()}, clear=False):
            torch_module = be.sys.modules.pop('torch', None)
            try:
                with self.assertRaisesRegex(ValueError, '새 격리 프로세스'):
                    be.initialize_native_runtime()
            finally:
                if torch_module is not None:
                    be.sys.modules['torch'] = torch_module

    def test_missing_extra_or_version_drift_stops(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'lock.txt'
            lock.write_text('example==1 --hash=sha256:' + 'a' * 64 + '\n')
            good = SimpleNamespace(metadata={'Name': 'example'}, version='1')
            with patch.object(preflight.platform, 'system', return_value='Windows'), patch.object(preflight.platform, 'machine', return_value='AMD64'), patch.object(preflight.platform, 'python_version', return_value='3.12.10'):
                with patch.object(preflight.importlib.metadata, 'distributions', return_value=[good]):
                    self.assertTrue(preflight.verify_dependency_lock(lock)['matched'])
                for packages in ([], [SimpleNamespace(metadata={'Name': 'example'}, version='2')], [good, SimpleNamespace(metadata={'Name': 'extra'}, version='1')]):
                    with patch.object(preflight.importlib.metadata, 'distributions', return_value=packages), self.assertRaises(ValueError):
                        preflight.verify_dependency_lock(lock)


class ConcurrentBudgetTest(unittest.TestCase):
    def test_parallel_requests_cannot_pass_call_cap(self):
        events = []
        meter = be.Meter(dict(max_calls=3, max_amount=100, reserve_per_call=1, currency='synthetic', basis='mock'), events.append)
        fn = Mock(return_value=SimpleNamespace(usage_metadata=None))
        def request(_):
            try:
                meter.call(fn)
                return True
            except be.BudgetStop:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(16)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(fn.call_count, 3)
        self.assertEqual((meter.calls, meter.charged), (3, 3))
