"""채점 없이 입력 변환·예외 상태·근거 보호를 검증한다."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.baseline.evaluation_prepare import exception_plan, remap, source_plan, checksum


class PreparationTests(unittest.TestCase):
    def test_remap_preserves_values_and_original(self):
        original = {'evaluation_run_id': 'old', 'evidence_refs': [{'path': 'a.json', 'sha256': 'hash'}],
                    'source_keys': ['S'], 'values': {'text': 'hello'}, 'execution_status': 'no_output', 'result_only': True}
        before = copy.deepcopy(original)
        changed = remap(original, 'new')
        self.assertEqual(original, before)
        self.assertEqual(changed['evidence_refs'][0]['path'], 'generation/a.json')
        self.assertEqual(changed['values'], original['values'])
        self.assertTrue(changed['result_only'])

    def test_exclusion_missing_output_and_pending_are_distinct(self):
        rows = []
        for s, applies in [('GS-01-IMG-001', False), ('GS-02-IMG-003', True), ('GS-03-IMG-001', True)]:
            rows += [{'source_key': s, 'unit_type': 'policy_expectation', 'applicability': {'H01': applies}},
                     {'source_key': s, 'unit_type': 'critical_fact', 'reference_unit_id': s + '-fact',
                      'applicability': {'H04': {'machine_snapshot': applies, 'service_final': applies}}}]
        m = {'source_keys': ['GS-01-IMG-001', 'GS-02-IMG-003', 'GS-03-IMG-001'],
             'artifacts': [{'source_keys': ['GS-02-IMG-003'], 'checkpoint': c, 'artifact_id': c}
                           for c in ('machine_snapshot', 'service_final')]}
        plan = exception_plan(rows, m)
        self.assertEqual([s['checkpoints']['service_final']['planned_status'] for s in plan],
                         ['not_applicable', 'pending_visual_evaluation', 'no_output'])
        self.assertEqual(sum(s['h01_expected_denominator_member'] for s in plan), 2)
        self.assertEqual(plan[2]['execution_failure_group'], 'GS-03-job-3-N2')
        self.assertEqual(plan[2]['h04'][0]['planned_status']['machine_snapshot'], 'no_output')

    def test_missing_or_conflicting_policy_stops(self):
        m = {'source_keys': ['S'], 'artifacts': []}
        with self.assertRaises(ValueError):
            exception_plan([], m)
        with self.assertRaises(ValueError):
            exception_plan([{'source_key': 'S', 'applicability': {'H01': b}} for b in (True, False)], m)

    def test_excluded_source_with_output_is_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            exception_plan([{'source_key': 'S', 'applicability': {'H01': False}}],
                           {'source_keys': ['S'], 'artifacts': [{'source_keys': ['S'],
                            'checkpoint': 'service_final', 'artifact_id': 'unexpected'}]})

    def test_hash_failure_and_unsafe_path_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'evidence').write_bytes(b'approved')
            self.assertEqual(source_plan(root, 'evidence', checksum(b'approved'), 'commit'), b'approved')
            with patch('tools.baseline.evaluation_prepare.subprocess.check_output', return_value=b'wrong'):
                with self.assertRaises(ValueError):
                    source_plan(root, 'evidence', checksum(b'other'), 'commit')
            with self.assertRaises(ValueError):
                source_plan(root, '../escape', '', 'commit')
            self.assertEqual((root/'evidence').read_bytes(), b'approved')


if __name__ == '__main__':
    unittest.main()
