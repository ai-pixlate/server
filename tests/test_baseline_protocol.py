"""최신 baseline 기록·집계·예산을 합성 자료로 검증. 제품·모델·DB 연결 없음."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.baseline import protocol as p, be
from tools.baseline.core import write_new, append, digest, now
from tools.baseline.run import main


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'artifacts').mkdir()
        (self.root / 'artifacts/evidence.txt').write_text('synthetic evidence', encoding='utf-8')
        self.ev = [{'path': 'artifacts/evidence.txt', 'sha256': digest(self.root / 'artifacts/evidence.txt')}]
        self.sources = ['GS-01-IMG-001', 'GS-01-IMG-002']
        self.m = {**p.template(), 'evaluation_run_id': 'mock', 'source_keys': self.sources, 'dataset_id': 'gs-v1',
                  'measurement_scope': None,  # 과거 H05/수정 허용 회차의 회귀 검증
                  'reference_version': 'r1', 'artifacts': [], 'files': [],
                  'dataset_manifest_path': self.ev[0]['path'], 'dataset_manifest_sha256': self.ev[0]['sha256'],
                  'evaluation_sessions': [{'session_id': 'chat1', 'channel': 'Codex 채팅', 'started_at': now(),
                    'model_name': None, 'model_settings': None, 'model_evidence_ref': None, 'conversation_ref': None,
                    'instruction_path': self.ev[0]['path'], 'instruction_sha256': self.ev[0]['sha256'],
                    'transcript_path': self.ev[0]['path'], 'transcript_sha256': self.ev[0]['sha256'],
                    'unavailable_reason': '합성 자료; 모델 미호출'}]}
        write_new(self.root / 'manifest.json', self.m)
        for kind in p.FIELDS:
            if kind == 'human_review':
                p.csv_write(self.root / 'human_review.csv', p.FIELDS[kind].split(), [])
            else:
                (self.root / (kind + '.jsonl')).touch()
        for cp in p.CHECKPOINTS:
            p.capture_artifact(self.root, dict(artifact_id=cp, case_id='GS-01', source_keys=self.sources,
                 kind='result', stage='N5' if cp == 'machine_snapshot' else 'N6', checkpoint=cp,
                 media_type='image/png', width=100, height=100, parent_artifact_id=None, crop_bbox=None,
                 created_at=now(), unavailable_reason=None), self.root / 'artifacts/evidence.txt')

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, kind, **kw):
        d = dict.fromkeys(p.FIELDS[kind].split())
        d.update(evaluation_run_id='mock', case_id='GS-01', source_key=self.sources[0], evidence_refs=self.ev)
        d.update(kw)
        return d

    def evaluation(self, eid='e1', **kw):
        return self.row('evaluations', evaluation_id=eid, session_id='chat1', checkpoint='machine_snapshot',
                        metric_id='H02', reference_version='r1', artifact_ids=['machine_snapshot'],
                        evaluation_status='measured', observed_value={'coverage_complete': True},
                        expected_value=None, reason='확인', defect_record_ids=[], evaluated_at=now(), **kw)

    def defect(self, did='d1', **kw):
        d = self.row('defects', defect_record_id=did, defect_id=did, evaluation_id='e1', recorded_by='chat1',
                     checkpoint='machine_snapshot', reference_unit_ids=[], defect_type='readability_or_layout',
                     severity='minor', location={'artifact_id': 'machine_snapshot', 'bbox': [0, 0, 10, 10], 'description': '합성'},
                     observed='test', expected='test', origin_stage='undetermined', detected_stage='렌더·배치',
                     resolution_status='unresolved', correction_evidence_refs=[])
        d.update(kw)
        return d

    def metric(self, result, mid, breakdown, cp=None):
        return next(r for r in result['metrics'] if r['scope'] == 'all' and r['metric_id'] == mid
                    and r['breakdown'] == breakdown and (cp is None or r['checkpoint'] == cp))

    def test_minor_only_A_and_missing_not_A(self):
        e = self.evaluation()
        e['defect_record_ids'] = ['d1']
        p.record(self.root, 'evaluations', e)
        p.record(self.root, 'defects', self.defect())
        r = p.aggregate(self.root)
        a = self.metric(r, 'H02', 'A')
        self.assertEqual((a['numerator'], a['denominator'], a['evaluation_failed_count']), (1, 1, 1))
        self.assertEqual(self.metric(r, 'H02', '평가 가능률')['value'], 50)
        self.assertIsNone(self.metric(r, 'H04', 'preserved')['value'])
        self.assertIsNone(self.metric(r, 'H05', 'N5')['value'])

    def test_root_defect_deduplicated_human_patch_preserves_AI(self):
        e = self.evaluation(); e['defect_record_ids'] = ['d1', 'd2']
        p.record(self.root, 'evaluations', e)
        p.record(self.root, 'defects', self.defect(severity='major'))
        p.record(self.root, 'defects', self.defect('d2', severity='major', root_defect_id='d1'))
        self.assertEqual(self.metric(p.aggregate(self.root), 'H02', 'B')['numerator'], 1)
        for i in (1, 2):
            row = self.row('human_review', review_id='r' + str(i), evaluation_id='e1', defect_record_id='d' + str(i),
                          checkpoint='machine_snapshot', review_trigger='major', review_scope='defect', reviewer_id='human',
                          reviewed_at=now(), ai_value={'severity': 'major'}, reviewed_value={'severity': 'minor'}, reason='검수')
            p.record(self.root, 'human_review', row)
        r = p.aggregate(self.root)
        self.assertEqual(self.metric(r, 'H02', 'A')['numerator'], 1)
        self.assertTrue(all(d['severity'] == 'major' for d in p.rows(self.root / 'defects.jsonl')))

    def test_h04_measured_denominator_and_readability(self):
        for i in range(10):
            ref = self.row('reference_units', reference_unit_id=f'f{i}', dataset_id='gs-v1', reference_version='r1',
                          unit_type='critical_fact', location={}, expected_value='10ml',
                          applicability={'H04': {cp: True for cp in p.CHECKPOINTS}}, importance='critical',
                          basis_refs=self.ev, verified_by='human', verified_at=now(), verification_ref=self.ev[0])
            p.record(self.root, 'reference_units', ref)
            e = self.evaluation(f'e{i}'); e.update(metric_id='H04', reference_unit_id=f'f{i}',
                      evaluation_status='measured' if i < 8 else 'undetermined', observed_value={'accurate': True, 'readable': True})
            p.record(self.root, 'evaluations', e)
        r = p.aggregate(self.root)
        self.assertEqual(self.metric(r, 'H04', 'preserved', 'machine_snapshot')['value'], 100)
        self.assertEqual(self.metric(r, 'H04', '평가 가능률', 'machine_snapshot')['value'], 80)

    def test_h01_blocked_in_denominator_and_groundtruth_exclusion(self):
        for i, s in enumerate(self.sources):
            ref = self.row('reference_units', reference_unit_id=f'p{i}', source_key=s, dataset_id='gs-v1', reference_version='r1',
                          unit_type='policy_expectation', location={}, expected_value={'all_sections_excluded': False},
                          applicability={'H01': True}, importance=None, basis_refs=self.ev, verified_by='human',
                          verified_at=now(), verification_ref=self.ev[0])
            p.record(self.root, 'reference_units', ref)
            a = self.row('execution', record_id=f'a{i}', record_kind='attempt', source_keys=[s], job_id=f'j{i}',
                         stage='N6', checkpoint='service_final', started_at=now(), ended_at=now())
            a.update(logical_execution_id=f'l{i}', target_id=s, attempt_number=1, execution_status='blocked' if i else 'success',
                     error_codes=[], fallback_type=None, is_final_attempt=True, applied_to_result=not i,
                     artifact_ids=[] if i else ['service_final'], usage=None, cost=None)
            p.record(self.root, 'execution', a)
        self.assertEqual(self.metric(p.aggregate(self.root), 'H01', 'deliverable')['value'], 50)
        bad = ref.copy(); bad.update(reference_unit_id='bad', applicability={'H01': False})
        with self.assertRaisesRegex(ValueError, '전체 섹션'):
            p.record(self.root, 'reference_units', bad)

    def test_h05_multiple_saves_count_final_blocks_only(self):
        for stage in ('N3', 'N5'):
            for revision in (1, 2):
                v0, v1 = ({'a': 'include'}, {'a': 'exclude'}) if stage == 'N3' else ({'b': ''}, {'b': 'translated'})
                e = self.row('execution', record_id=f'{stage}{revision}', record_kind='user_edit',
                             source_keys=[self.sources[0]], stage=stage, checkpoint=None,
                             started_at=now(), ended_at=now())
                e.update(target_id=self.sources[0], original_value=v0, final_value=v1, change_count=1,
                         user_retry_count=0, confirmed_at=now(), n3_intervention='selection_changed')
                p.record(self.root, 'execution', e)
        r = p.aggregate(self.root)
        self.assertEqual(self.metric(r, 'H05', '2~3')['numerator'], 1)
        self.assertEqual(self.metric(r, 'H05', 'N5')['value'], 1)

    def test_bad_links_hash_and_checkpoint_rejected(self):
        e = self.evaluation(); e['artifact_ids'] = ['service_final']
        p.record(self.root, 'evaluations', e)
        self.assertTrue(p.validate(self.root))
        with self.assertRaises(ValueError): p.aggregate(self.root)
        with self.assertRaises(ValueError): p.local_file(self.root, '../outside', 'x')

    def test_revision_chain_and_area_review_no_original_upgrade(self):
        e = self.evaluation(); p.record(self.root, 'evaluations', e)
        e2 = copy.deepcopy(e); e2['evaluation_id'] = 'e2'
        with self.assertRaises(ValueError): p.record(self.root, 'evaluations', e2)
        e2['supersedes_evaluation_id'] = 'e1'; p.record(self.root, 'evaluations', e2)
        self.assertEqual(self.metric(p.aggregate(self.root), 'H02', 'A')['numerator'], 1)

    def test_no_measurements_remain_unknown_and_export_immutable(self):
        out = self.root / 'export'
        result = p.export(self.root, out)
        self.assertFalse(result['formal_complete'])
        self.assertTrue(result['approval_pending'])
        self.assertIsNone(self.metric(result, 'H01', 'deliverable')['value'])
        self.assertTrue(all(not r['include_in_presentation'] for r in result['metrics'] if r['metric_id'] == 'OP06'))
        with self.assertRaises(FileExistsError): p.export(self.root, out)
        self.assertTrue((out / 'metrics.csv').exists())

    def test_checkpoint_artifacts_keep_different_bytes(self):
        first = self.root / 'artifacts/evidence.txt'
        meta = dict(artifact_id='before', case_id='GS-01', source_keys=self.sources, kind='evidence',
                    stage='N5', checkpoint='machine_snapshot', media_type='text/plain', width=None, height=None,
                    parent_artifact_id=None, crop_bbox=None, created_at=now(), unavailable_reason=None)
        a = p.capture_artifact(self.root, meta, first)
        first.write_text('changed', encoding='utf-8')
        b = p.capture_artifact(self.root, {**meta, 'artifact_id': 'after', 'checkpoint': 'service_final', 'stage': 'N6'}, first)
        self.assertNotEqual(a['sha256'], b['sha256'])
        self.assertEqual((self.root / a['path']).read_text(), 'synthetic evidence')
        with self.assertRaises(ValueError): p.capture_artifact(self.root, meta, first)

    def test_zero_denominators_and_critical_grade(self):
        m = {'evaluation_run_id': 'x'}
        empty = p.metric(m, 'H04', None, 'preserved', ['not_applicable'], 0)
        unknown = p.metric(m, 'H04', None, 'preserved', ['undetermined', 'no_output'], 0)
        self.assertEqual(empty['display_reason'], '해당 대상 없음')
        self.assertEqual(unknown['display_reason'], '정상 측정 0건')
        self.assertIsNone(unknown['value'])
        d = self.defect(severity='critical', resolution_status='corrected_by_user', correction_evidence_refs=self.ev)
        self.assertEqual(p.grade([d])[0], 'C')
        self.assertEqual(p.grade([{**d, 'checkpoint': 'service_final'}])[0], 'A')

    def test_api_execution_paths_block_before_import(self):
        with patch('subprocess.run', side_effect=AssertionError('must not execute')):
            for fn in (lambda: main(['execute', str(self.root), '--execute-models']),
                       lambda: be.run_prepared(self.root, True), lambda: be._run_prepared(self.root, True)):
                with self.assertRaises(ValueError): fn()


class BudgetTest(unittest.TestCase):
    def meter(self, **kwargs):
        config = dict(max_calls=100, max_amount=2.08, reserve_per_call=1.04, currency='USD', basis='synthetic',
                      settle_usage=True, sdk_attempts=1, reservation_verified=True, model='fake',
                      input_per_million=.75, output_per_million=3.75)
        config.update(kwargs)
        self.events = []
        return be.Meter(config, self.events.append)

    def response(self, **usage):
        return SimpleNamespace(usage_metadata=SimpleNamespace(model_dump=lambda **kw: usage))

    def test_settlement_releases_unused_reservation(self):
        m = self.meter()
        for i in range(10):
            m.call(lambda **k: self.response(prompt_token_count=1000, candidates_token_count=100, thoughts_token_count=100), model='fake')
        self.assertAlmostEqual(m.charged, .015)
        self.assertEqual(m.calls, 10)
        self.assertEqual(len(self.events), 20)

    def test_unknown_failed_usage_reserve_retained_and_cap_stops_before_call(self):
        m = self.meter(); fn = Mock(side_effect=ValueError('synthetic'))
        for _ in range(2):
            with self.assertRaises(ValueError): m.call(fn, model='fake')
        with self.assertRaises(be.BudgetStop): m.call(fn, model='fake')
        self.assertEqual(fn.call_count, 2)

    def test_calls_cap_and_model_and_record_failure(self):
        m = self.meter(max_calls=1)
        m.call(lambda **k: self.response(prompt_token_count=0, candidates_token_count=0, thoughts_token_count=0), model='fake')
        with self.assertRaises(be.BudgetStop): m.call(Mock(), model='fake')
        m = self.meter(); fn = Mock()
        with self.assertRaises(be.BudgetStop): m.call(fn, model='wrong')
        self.assertFalse(fn.called)
        m.record = Mock(side_effect=OSError('disk full'))
        with self.assertRaises(be.RecordingStop): m.call(fn, model='fake')
        self.assertFalse(fn.called)

    def test_missing_thinking_is_not_zero_and_overrun_latches(self):
        m = self.meter()
        m.call(lambda **k: self.response(prompt_token_count=1, candidates_token_count=1), model='fake')
        self.assertEqual(m.charged, 1.04)
        m = self.meter()
        with self.assertRaises(be.BudgetStop):
            m.call(lambda **k: self.response(prompt_token_count=0, candidates_token_count=1000000, thoughts_token_count=0), model='fake')
        with self.assertRaises(be.BudgetStop): m.call(Mock(), model='fake')


if __name__ == '__main__':
    unittest.main()
