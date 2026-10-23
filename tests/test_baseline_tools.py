"""모델·DB·네트워크 없이 실행: python -m unittest discover -s tests -p test_baseline_tools.py -v"""
import json
import hashlib
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from tools.baseline import core, be, review
from tools.baseline.run import main


def evidence(sid="GS-01_001"):
    return [{"path": "fixture.json", "source_id": sid, "sha256": hashlib.sha256(b"fixture").hexdigest()}]


def evaluation(**kw):
    return {"id": "e1", "source_id": "GS-01_001", "metric": 1, "role": "primary",
            "reviewer": "test", "session": "s1", "grade": "A", "scope": "original",
            "coverage_complete": True, "evidence": evidence(kw.get("source_id", "GS-01_001")), "reason": "합성 검증", **kw}


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "fixture.json").write_bytes(b"fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_has_fixed_ids_but_no_invented_metadata(self):
        self.assertEqual(len(core.IDS), 34)
        self.assertEqual(len(core.REGIONS), 9)
        self.assertIsNone(core.template()["products"]["GS-01"]["regulatory_class"])

    def test_prepare_missing_does_not_read_past_paths_or_evaluate(self):
        missing = core.prepare(core.template(), self.root / "run")
        self.assertTrue(missing)
        self.assertEqual(core.read(self.root / "run/inputs.json"), [])
        self.assertEqual(len((self.root / "run/evaluation-sheet.csv").read_text(encoding="utf-8-sig").splitlines()), 239)
        self.assertEqual(len(core.read(self.root / "run/frozen.json")), 4)
        self.assertEqual(core.rows(self.root / "run/evaluations.jsonl"), [])
        with self.assertRaises(FileExistsError):
            core.prepare(core.template(), self.root / "run")

    def test_input_image_dimensions_hash_and_core_region_bounds(self):
        from PIL import Image
        folder = self.root / "GS-01"
        folder.mkdir()
        Image.new("RGB", (20, 30), "white").save(folder / "GS-01_002.jpg")
        data, issues = core.inspect_inputs({"input_root": str(self.root)})
        self.assertEqual(data[0]["area"], 600)
        self.assertEqual(len(data[0]["sha256"]), 64)
        self.assertTrue(any("K01" in s for s in issues))
        self.assertEqual(len(data), 1)

    def test_record_rejects_missing_bc_evidence(self):
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, evaluation(grade="C"))

    def test_record_rejects_a_without_coverage(self):
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, evaluation(coverage_complete=False))

    def test_record_preserves_history_and_requires_revision_reason(self):
        core.record_evaluation(self.root, evaluation())
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, evaluation(id="e2"))
        core.record_evaluation(self.root, evaluation(id="e2", supersedes="e1", revision_reason="기술적 오류"))
        self.assertEqual(len(core.rows(self.root / "evaluations.jsonl")), 2)

    def test_independent_same_session_rejected(self):
        core.record_evaluation(self.root, evaluation())
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, evaluation(id="i", role="independent", blind_review=True))

    def test_core_region_source_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, evaluation(scope="K01"))

    def test_conflict_is_not_automatically_selected(self):
        h = [evaluation(), evaluation(id="i", role="independent", grade="C")]
        summary = core.quality_summary(h)["all"]["1"]
        self.assertEqual(summary["graded"], 0)
        self.assertIsNone(summary["a_ratio"])
        h.append(evaluation(id="h", role="human", grade="B"))
        self.assertEqual(core.quality_summary(h)["all"]["1"]["counts"]["B"], 1)

    def test_region_human_grade_does_not_certify_original(self):
        h = [evaluation(role="human", scope="K01", source_id="GS-01_002")]
        self.assertEqual(core.quality_summary(h)["all"]["1"]["graded"], 0)

    def test_completion_excludes_partial_even_when_artifact_exists(self):
        events = [{"event": "finished", "source_id": sid, "status": status, "seconds": t}
                  for sid, status, t in (("GS-01_001", "completed", 2), ("GS-01_002", "all_excluded", 4),
                                         ("GS-02_001", "partial_failure", 8))]
        r = core.execution_summary(events)
        self.assertEqual(r["completion_rate"], 2 / 34)
        definition = r["metric_definitions"]
        self.assertEqual(definition["unit"], "원본 이미지 1장")
        self.assertIn("O1", definition["scope"])
        self.assertIn("NFR-14", definition["metric_7"])
        self.assertIn("시도 원본", definition["cost"]["per_attempted"])
        self.assertIn("기술 처리 완료 원본", definition["cost"]["per_completed"])
        completion = definition["completion_rate"]
        self.assertEqual(completion["denominator_count"], r["planned"])
        self.assertEqual(completion["included_statuses"], ["completed", "all_excluded"])
        self.assertIn("partial_failure", completion["excluded_statuses"])
        self.assertTrue(completion["unexecuted_in_denominator"])
        self.assertEqual(r["completed_seconds"]["p90"], 4)
        self.assertEqual(r["failed_seconds"]["n"], 1)
        with self.assertRaises(ValueError):
            core.execution_summary(events + events[:1])

    def test_review_includes_core_and_expands_misgrade(self):
        r = review.review_plan([evaluation(role="human", misgrade_detected=True)], "seed")
        self.assertEqual(sum(x["scope"].startswith("K") for x in r["human"]), 63)
        self.assertTrue(any(x["source_id"] == "GS-01_002" and x["scope"] == "original" for x in r["human"]))

    def test_blind_packet_contains_no_primary_grades(self):
        core.prepare(core.template(), self.root / "run")
        (self.root / "run/fixture.json").write_bytes(b"fixture")
        core.record_evaluation(self.root / "run", evaluation(reason="HIDDEN_FIRST_OPINION"))
        main(["packet", str(self.root / "run"), str(self.root / "packet.json")])
        packet = (self.root / "packet.json").read_text(encoding="utf-8")
        self.assertNotIn("HIDDEN_FIRST_OPINION", packet)
        self.assertNotIn("evaluations.jsonl", packet)

    def test_dispatch_reuses_functions_and_preserves_enqueued_retry(self):
        events, called = [], []
        def task(n):
            called.append(n)
            if n == 1:
                d("task", [2])
        d = core.SequentialDispatcher(lambda _: task, events.append)
        d("task", [1])
        d.drain()
        self.assertEqual(called, [1, 2])
        self.assertEqual(len(events), 4)

    def test_store_is_immutable_and_path_safe(self):
        s = be.FileStore(self.root)
        self.assertTrue(s.put_if_absent("../../escape", b"original"))
        self.assertFalse(s.put_if_absent("../../escape", b"changed"))
        self.assertEqual(s.get("../../escape"), b"original")
        sha = s.index["../../escape"]["sha256"]
        s.delete("../../escape")
        self.assertTrue((self.root / sha).exists())
        self.assertFalse(s.exists("../../escape"))

    def test_budget_stops_before_next_request_including_failures(self):
        events = []
        m = be.Meter({"max_calls": 2, "max_amount": 5, "reserve_per_call": 3,
                      "currency": "test", "basis": "fixture"}, events.append)
        def failed(**kw):
            raise ValueError("fixture")
        with self.assertRaises(ValueError):
            m.call(failed, model="fake")
        with self.assertRaises(be.BudgetStop):
            m.call(failed, model="fake")
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["usage"])

    def test_no_explicit_execution_option_never_imports_app(self):
        with self.assertRaises(ValueError):
            be.run_prepared(self.root)

    def test_secret_redaction_preserves_usage_token_counts(self):
        v = be.safe_record({"lease_token_hash": "secret", "nested": {"password": "secret"}, "x": "https://a?X-Amz-Signature=x", "total_token_count": 100})
        self.assertNotIn("secret", json.dumps(v))
        self.assertEqual(v["total_token_count"], 100)

    def test_policy_matrix_separates_exclusion_and_replacement(self):
        event = {"id": "p1", "source_id": "GS-01_001", "region_id": "R1", "location": [0, 0, 10, 10],
                 "text": "합성", "basis": "합성 기대값", "evidence": evidence(), "reviewer": "test",
                 "expected_exclude": True, "actual_exclude": False, "expected_replace": False, "actual_replace": False}
        core.record_detail(self.root, "policy", event)
        with self.assertRaises(ValueError):
            core.record_detail(self.root, "policy", {**event, "id": "p2"})
        r = core.policy_summary(core.rows(self.root / "policy.jsonl"))
        self.assertEqual(r["exclude"]["counts"]["FN"], 1)
        self.assertEqual(r["replace"]["counts"]["TN"], 1)

    def test_recurrence_requires_previous_resolved_run(self):
        event = {"id": "d1", "source_id": "GS-01_001", "defect_id": "D-1", "metric": 1,
                 "location": [0, 0, 10, 10], "defect_type": "합성", "observation": "합성", "expected": "합성",
                 "evidence": evidence(), "reviewer": "test", "recurrence": True}
        with self.assertRaises(ValueError):
            core.record_detail(self.root, "defects", event)

    def test_runtime_rejects_remote_database_before_import(self):
        cfg = {"product_root": str(self.root), "product_commit": "a" * 40}
        with patch("subprocess.check_output", side_effect=["a" * 40, ""]), patch.dict("os.environ", {"BASELINE_DATABASE_URL": "postgresql+psycopg://user:pw@remote/px_baseline_test"}):
            with self.assertRaises(ValueError):
                be.check_runtime(cfg)

    def test_runtime_rejects_dirty_product(self):
        cfg = {"product_root": str(self.root), "product_commit": "a" * 40}
        with patch("subprocess.check_output", side_effect=["a" * 40, " M app/typeset.py"]):
            with self.assertRaises(ValueError):
                be.check_runtime(cfg)

    def test_transitions_separate_exceptions(self):
        t = review.transitions([evaluation()], [evaluation(grade="산출물 없음")])["1"]
        self.assertEqual(sum(sum(row.values()) for row in t["matrix"].values()), 0)
        self.assertEqual(t["other"][0]["after"], "산출물 없음")

    def fake_drive(self, sections, analyze_status="review"):
        state = {"current_step": "N1", "status": "draft"}
        calls = []
        warnings = [{"blockId": 12, "code": "translation_failed"}]
        def analyze(*args):
            calls.append("analyze")
            state.update(current_step="N3", status=analyze_status)
        def proceed(*args):
            calls.append("proceed")
            state.update(current_step="N5", status="review")
        def confirm(db, job, seller, ack):
            self.assertEqual(ack, warnings)
            calls.append("confirm")
            state.update(current_step="N6", status="review")
        status = be.drive_job(1, 2, lambda: nullcontext(None), lambda *a, **k: [state],
                SimpleNamespace(drain=lambda: calls.append("drain")),
                SimpleNamespace(start_analysis=analyze), SimpleNamespace(visible_sections=lambda *a: sections),
                SimpleNamespace(proceed=proceed), SimpleNamespace(confirm=confirm,
                    unresolved_warnings=lambda *a: warnings, can_save=lambda *a: True),
                lambda w: calls.append("warnings"))
        return status, calls

    def test_driver_forwards_exact_warnings_without_editing_translation(self):
        status, calls = self.fake_drive([{"bucket": "include"}])
        self.assertEqual(status, "completed")
        self.assertEqual(calls, ["analyze", "drain", "proceed", "drain", "warnings", "confirm", "drain"])

    def test_driver_all_excluded_does_not_force_n6(self):
        status, calls = self.fake_drive([{"bucket": "exclude"}])
        self.assertEqual(status, "all_excluded")
        self.assertNotIn("confirm", calls)

    def test_driver_failure_does_not_advance(self):
        status, calls = self.fake_drive([{"bucket": "include"}], "failed")
        self.assertEqual(status, "failed")
        self.assertNotIn("proceed", calls)


if __name__ == "__main__":
    unittest.main()
