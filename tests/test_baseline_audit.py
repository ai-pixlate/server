"""baseline 검토 회귀: 모델/DB 대신 값과 가짜 함수만 사용한다."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

from tools.baseline import core, be, review, audit


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "proof").write_bytes(b"fixture")
        self.ref = {"path": "proof", "sha256": hashlib.sha256(b"fixture").hexdigest(), "source_id": "GS-01_002"}

    def event(self, **kw):
        return {"id": "p", "source_id": "GS-01_002", "metric": 1, "role": "primary", "scope": "original",
                "grade": "A", "coverage_complete": True, "reviewer": "fixture", "session": "fixture",
                "evidence": [self.ref], "reason": "fixture", **kw}

    def test_region_c_blocks_original_a_and_false_transition(self):
        before = [self.event()]
        after = before + [self.event(id="h", role="human", scope="K01", grade="C")]
        self.assertEqual(core.quality_summary(after)["all"]["1"]["graded"], 0)
        self.assertEqual(review.transitions(before, after)["1"]["matrix"]["A"]["A"], 0)

    def test_region_c_requires_consistent_original_reassessment(self):
        h = self.event(id="h", role="human", scope="K01", grade="C")
        p = self.event(region_evaluation_ids=["h"])
        self.assertEqual(core.resolved_original(core.latest_evaluations([p, h]), "GS-01_002", 1), "미판정")
        p["grade"] = "C"
        self.assertEqual(core.resolved_original(core.latest_evaluations([p, h]), "GS-01_002", 1), "C")

    def test_region_a_never_certifies_whole_original(self):
        h = self.event(role="human", scope="K01")
        self.assertEqual(core.quality_summary([h])["all"]["1"]["graded"], 0)

    def test_independent_region_c_blocks_unrevised_original_a(self):
        h = [self.event(), self.event(role="independent", scope="K01", grade="C")]
        self.assertEqual(core.quality_summary(h)["all"]["1"]["graded"], 0)

    def test_calibration_does_not_replace_formal(self):
        core.record_evaluation(self.root, self.event(id="cal", phase="calibration"))
        core.record_evaluation(self.root, self.event())
        history = core.rows(self.root / "evaluations.jsonl")
        self.assertEqual(len(history), 2)
        self.assertEqual(len(core.latest_evaluations(history)), 1)

    def test_core_reviews_have_reachable_completion(self):
        h = [self.event(id=f"h{m}", role="human", scope="K01", metric=m) for m in range(1, 8)]
        r = review.review_plan(h, "fixed")
        k = [x for x in r["human"] if x["scope"] == "K01"]
        self.assertEqual(len(k), 7)
        self.assertTrue(all(x["human_record_present"] for x in k))
        self.assertFalse(any(x["independent_record_present"] for x in k))

    def test_baseline_c_required_even_if_followup_a(self):
        baseline = [self.event(grade="C", defect_ids=["D-1"])]
        current = [self.event(scope="D-1")]
        r = review.review_plan(current, "fixed", baseline=baseline)
        self.assertTrue(any(x["scope"] == "D-1" for x in r["human"]))
        self.assertTrue(any(x["scope"] == "D-1" for x in r["independent"]))

    def test_logging_failure_is_not_caught_by_product_exception_retry(self):
        def fail(e):
            raise OSError("fixture disk failure")
        m = be.Meter({"max_calls": 2, "max_amount": 2, "reserve_per_call": 1, "currency": "test", "basis": "test"}, fail)
        calls = []
        def product():
            for _ in range(2):
                try:
                    calls.append(1)
                    return m.call(lambda: SimpleNamespace(usage_metadata=None))
                except Exception:
                    continue
        with self.assertRaises(be.RecordingStop):
            product()
        self.assertEqual(len(calls), 1)

    def test_observer_preserves_identity_and_restores_patches(self):
        models = ModuleType("google.genai.models")
        class Models:
            def generate_content(self):
                return SimpleNamespace(usage_metadata=None)
        models.Models = Models
        result = {"unchanged": [1, 2]}
        def original(*a, **kw):
            return result
        class Engine:
            def __init__(self):
                self.info = {"device": "fixture"}
        modules = {}
        for name in ("section_split", "ocr", "merge", "judge", "policy", "label", "logo", "style", "translate", "text_check", "inpaint_proc"):
            modules["pipeline.stages." + name] = SimpleNamespace(run=original, translate_section=original, check_block=original,
                                                               PaddleOcrEngine=Engine, SubprocessInpaintModel=type("Other", (), {"__init__": lambda self: None}))
        budget = {"max_calls": 2, "max_amount": 2, "reserve_per_call": 1, "currency": "test", "basis": "test"}
        with patch.dict("sys.modules", {"google.genai.models": models}), patch("importlib.import_module", side_effect=lambda name: modules[name]):
            with be.observe(self.root, ["GS-01_002"], budget):
                self.assertIs(modules["pipeline.stages.ocr"].run(), result)
            self.assertIs(modules["pipeline.stages.ocr"].run, original)
            with patch.object(be, "_append", side_effect=OSError("fixture")):
                with self.assertRaises(be.RecordingStop):
                    with be.observe(self.root, ["GS-01_002"], budget):
                        modules["pipeline.stages.ocr"].run()
            self.assertIs(modules["pipeline.stages.ocr"].run, original)

    def test_blob_io_failure_stops_measurement(self):
        s = be.FileStore(self.root / "objects")
        with patch.object(Path, "open", side_effect=OSError("fixture")):
            with self.assertRaises(be.RecordingStop):
                s.put("key", b"fixture")

    def test_post_processing_collection_not_in_processing_time(self):
        evidence = {"fixture": True}
        with patch.object(be.time, "monotonic", side_effect=[10, 40]):
            result, seconds, collection_seconds = be.collect_terminal(2, lambda: evidence)
        self.assertIs(result, evidence)
        self.assertEqual(seconds, 8)
        self.assertEqual(collection_seconds, 30)

    def test_object_from_other_source_is_rejected(self):
        objects = self.root / "objects"
        objects.mkdir()
        sha = self.ref["sha256"]
        (objects / sha).write_bytes(b"fixture")
        core.append(objects / "objects.jsonl", {"key": "result/other", "sha256": sha})
        core.append(self.root / "bindings.jsonl", {"source_id": "GS-01_002", "job_id": 1})
        core.write_new(self.root / "evidence/GS-01_002.json", {"job": [{"id": 1}], "file_url": "result/own"})
        e = self.event(evidence=[{**self.ref, "path": "objects/" + sha, "object_key": "result/other"}])
        self.assertTrue(audit.evidence_issues(self.root, [e]))

    def test_reference_manifest_cannot_change_after_freeze(self):
        core.write_new(self.root / "reference.json", {"policy_regions": [], "policy_coverage": self.coverage()})
        core.write_new(self.root / "frozen.json", {"reference_file": {"file": "reference.json", "sha256": core.digest(self.root / "reference.json")}})
        self.assertEqual(audit.policy_regions(self.root), [])
        (self.root / "reference.json").write_text('{}', encoding="utf-8")
        with self.assertRaises(ValueError):
            audit.policy_regions(self.root)

    def coverage(self):
        return [{"source_id": sid, "status": "not_applicable", "reason": "fixture", "confirmed_by": "fixture"} for sid in core.IDS]

    def freeze_reference(self, data):
        core.write_new(self.root / "reference.json", data)
        core.write_new(self.root / "frozen.json", {"reference_file": {"file": "reference.json", "sha256": core.digest(self.root / "reference.json")}})

    def test_unknown_region_cannot_be_acknowledged_into_a(self):
        for grade in ("판단 불가", "산출물 없음"):
            for role in ("primary", "human"):
                with self.subTest(grade=grade, role=role):
                    h = self.event(id="h", role="human", scope="K01", grade=grade)
                    p = self.event(role=role, region_evaluation_ids=["h"])
                    self.assertEqual(core.resolved_original(core.latest_evaluations([p,h]), "GS-01_002", 1), "미판정")
                    self.assertEqual(review.transitions([self.event()], [p,h])["1"]["matrix"]["A"]["A"], 0)

    def test_known_bc_not_erased_by_unknown_other_region(self):
        h = self.event(id="h", role="human", scope="K01", grade="판단 불가")
        for grade in ("B", "C"):
            p = self.event(grade=grade, region_evaluation_ids=["h"])
            self.assertEqual(core.resolved_original(core.latest_evaluations([p,h]), "GS-01_002", 1), grade)

    def test_human_retraction_removes_obsolete_baseline_defect(self):
        baseline = [self.event(grade="C", defect_ids=["D-1"]), self.event(id="h", role="human", grade="A", defect_ids=[])]
        r = review.review_plan([], "fixed", baseline=baseline)
        self.assertFalse(any(e["scope"] == "D-1" for e in r["human"] + r["independent"]))
        result = audit.defect_summary([dict(source_id="GS-01_002", defect_id="D-1", metric=1)], [], baseline)
        self.assertEqual(result["baseline_defects"], 0)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["by_metric"]["1"]["baseline_defects"], 0)

    def test_human_downgrade_c_to_b_keeps_independent_only(self):
        b = [self.event(grade="C", defect_ids=["D-1"]), self.event(id="h", role="human", grade="B", defect_ids=["D-1"])]
        r = review.review_plan([], "fixed", baseline=b)
        self.assertFalse(any(e["scope"] == "D-1" for e in r["human"]))
        self.assertTrue(any(e["scope"] == "D-1" for e in r["independent"]))

    def test_recurrence_requires_actual_resolution_and_is_per_metric(self):
        base = [dict(source_id="GS-01_002", defect_id="D-1", metric=1), dict(source_id="GS-01_002", defect_id="D-2", metric=2)]
        follow = [dict(source_id="GS-01_002", defect_id="D-next", metric=1, baseline_defect_id="D-1", match_reason="fixture", change="유지", recurrence=True, previous_resolved_run="prior")]
        r = audit.defect_summary(base, follow)
        self.assertEqual(r["recurrence"], 0)
        self.assertTrue(any("이전 해결" in e["reason"] for e in r["issues"]))
        resolved = {"prior": [{**follow[0], "change": "해결", "recurrence": False}]}
        r = audit.defect_summary(base, follow, resolved_runs=resolved)
        self.assertEqual(r["recurrence"], 1)
        self.assertEqual(r["by_metric"]["1"]["recurrence"], 1)
        self.assertEqual(r["by_metric"]["2"]["recurrence"], 0)
        self.assertEqual(r["by_metric"]["1"]["counts"]["유지"], 1)
        self.assertEqual(r["by_metric"]["2"]["counts"]["유지"], 0)
        resolved["prior"][0]["change"] = "완화"
        self.assertEqual(audit.defect_summary(base, follow, resolved_runs=resolved)["recurrence"], 0)

    def test_empty_policy_without_source_confirmation_rejected(self):
        self.freeze_reference({"policy_regions": []})
        with self.assertRaises(ValueError):
            audit.policy_regions(self.root)

    def test_policy_missing_one_source_rejected(self):
        self.freeze_reference({"policy_regions": [], "policy_coverage": self.coverage()[:-1]})
        with self.assertRaises(ValueError):
            audit.policy_regions(self.root)

    def test_policy_complete_without_regions_rejected(self):
        coverage = self.coverage()
        coverage[0]["status"] = "complete"
        self.freeze_reference({"policy_regions": [], "policy_coverage": coverage})
        with self.assertRaises(ValueError):
            audit.policy_regions(self.root)

    def test_connections_restored_on_preflight_and_recording_failure(self):
        old_store, old_dispatch, old_adapters = object(), object(), object()
        storage = SimpleNamespace(_store=old_store)
        execution = SimpleNamespace(_dispatcher=old_dispatch)
        adapters = SimpleNamespace(_adapters=old_adapters)
        storage.set_store = lambda value: setattr(storage, "_store", value)
        adapters.reset_adapters = lambda: setattr(adapters, "_adapters", object())
        for error in (None, ValueError("empty dictionary"), be.RecordingStop("disk"), be.BudgetStop("budget")):
            with self.subTest(error=type(error).__name__):
                def run():
                    with be.isolated_connections(storage, execution, adapters, "temporary"):
                        self.assertEqual(storage._store, "temporary")
                        execution._dispatcher = "temporary"
                        if error:
                            raise error
                if error:
                    with self.assertRaises(type(error)):
                        run()
                else:
                    run()
                self.assertIs(storage._store, old_store)
                self.assertIs(execution._dispatcher, old_dispatch)
                self.assertIs(adapters._adapters, old_adapters)

    def resolution_fixture(self, root, rid, at):
        cfg = dict(run_id=rid, protocol="v1", evaluation_model="fixture", viewing_conditions="fixture", products={})
        core.write_new(root / "config.json", cfg)
        core.write_new(root / "started.json", {"at": at})
        core.write_new(root / "inputs.json", [{"source_id": sid, "sha256": "fixture"} for sid in core.IDS])
        (root / "protocol").write_bytes(b"fixture")
        core.write_new(root / "frozen.json", {"protocol": {"file": "protocol", "sha256": core.digest(root / "protocol")}})
        (root / "proof").write_bytes(b"fixture")
        core.append(root / "defects.jsonl", {"id": "resolved", "source_id": "GS-01_002", "defect_id": "D-prior", "metric": 1,
                    "baseline_defect_id": "D-1", "change": "해결", "reviewer": "fixture", "evidence": [self.ref]})

    def test_resolved_run_loader_checks_existence_order_and_input(self):
        baseline, prior, current = [self.root / name for name in ("base", "prior", "current")]
        self.resolution_fixture(baseline, "base", "2026-01-01T00:00:00+00:00")
        self.resolution_fixture(prior, "prior", "2026-01-02T00:00:00+00:00")
        self.resolution_fixture(current, "current", "2026-01-03T00:00:00+00:00")
        self.assertEqual(audit.load_resolved_runs([prior], baseline, current)["prior"][0]["change"], "해결")
        with self.assertRaises(FileNotFoundError):
            audit.load_resolved_runs([self.root / "nonexistent"], baseline, current)
        with self.assertRaises(ValueError):
            audit.load_resolved_runs([baseline], baseline, current)
        with self.assertRaises(ValueError):
            audit.load_resolved_runs([current], baseline, current)
        (prior / "inputs.json").write_text('[]', encoding="utf-8")
        with self.assertRaises(ValueError):
            audit.load_resolved_runs([prior], baseline, current)

    def test_resolved_run_loader_rejects_missing_evidence(self):
        baseline, prior, current = [self.root / name for name in ("base", "prior", "current")]
        for path, rid, day in ((baseline, "base", 1), (prior, "prior", 2), (current, "current", 3)):
            self.resolution_fixture(path, rid, f"2026-01-0{day}T00:00:00+00:00")
        (prior / "proof").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            audit.load_resolved_runs([prior], baseline, current)

    def test_policy_coverage_accepts_complete_and_not_applicable(self):
        coverage = self.coverage()
        coverage[0]["status"] = "complete"
        region = dict(source_id=core.IDS[0], region_id="R1", location=[0, 0, 10, 10], text="fixture", basis="fixture",
                      expected_exclude=False, expected_replace=False)
        self.freeze_reference({"policy_regions": [region], "policy_coverage": coverage})
        self.assertEqual(audit.policy_regions(self.root), [region])

    def test_human_confirmed_c_keeps_required_review(self):
        baseline = [self.event(grade="A"), self.event(id="h", role="human", grade="C", defect_ids=["D-1"])]
        result = review.review_plan([], "fixed", baseline=baseline)
        self.assertTrue(any(e["scope"] == "D-1" for e in result["human"]))

    def test_policy_denominator_and_exception_separation(self):
        region = dict(source_id="GS-01_002", region_id="r1", expected_exclude=True, expected_replace=False)
        domain = [region, {**region, "region_id": "r2"}, {**region, "region_id": "r3"}]
        events = [{**region, "actual_exclude": None, "actual_replace": None, "exception_state": "산출물 없음"},
                  {**region, "region_id": "r2", "actual_exclude": None, "actual_replace": None, "exception_state": "판단 불가"}]
        r = core.policy_summary(events, domain)["exclude"]
        self.assertEqual(r["total_regions"], 3)
        self.assertEqual(r["counts"]["산출물 없음"], 1)
        self.assertEqual(r["counts"]["판단 불가"], 1)
        self.assertEqual(r["counts"]["미기록"], 1)
        self.assertEqual(r["assessable_regions"], 0)

    def test_policy_cannot_override_frozen_expectation(self):
        r = dict(source_id="GS-01_002", region_id="r1", expected_exclude=True, expected_replace=False)
        e = {**r, "expected_exclude": False, "actual_exclude": False, "actual_replace": False}
        self.assertEqual(core.policy_summary([e], [r])["exclude"]["counts"]["기준 불일치"], 1)

    def test_retry_completion_is_separate(self):
        es = [dict(event="finished", source_id=s, status="completed", seconds=1, retry_count=n)
              for s, n in (("GS-01_001", 0), ("GS-01_002", 2))]
        result = core.execution_summary(es)
        self.assertEqual(result["first_attempt_completed"], 1)
        self.assertEqual(result["completed_after_retry"], 1)

    def test_cost_unknown_not_zero_and_categories_not_mixed(self):
        events = [dict(category="generation", amount=10, currency="USD"), dict(category="infrastructure", amount=2, currency="USD"),
                  dict(category="evaluation", amount=99, currency="USD")]
        execution = [dict(event="finished", source_id="GS-01_001", status="completed"),
                     dict(event="finished", source_id="GS-01_002", status="failed")]
        r = audit.cost_summary(events, execution, [])
        self.assertEqual(r["execution_total"], 12)
        self.assertEqual(r["per_attempted"], 6)
        self.assertEqual(r["per_completed"], 12)
        self.assertIsNone(audit.cost_summary(events[:1], execution, [])["execution_total"])

    def test_cost_ledger_requires_revision_and_price_basis(self):
        e = dict(id="c", category="generation", kind="estimate", amount=1, currency="USD", applied_at="fixture",
                 basis="fixture", reviewer="fixture", source_id="GS-01_002", evidence=[self.ref])
        with self.assertRaises(ValueError):
            audit.record_cost(self.root, e)
        e["rate_source"] = "fixture price"
        audit.record_cost(self.root, e)
        with self.assertRaises(ValueError):
            audit.record_cost(self.root, {**e, "id": "c2"})

    def test_defect_missing_and_split_not_double_counted(self):
        b = [dict(source_id="GS-01_002", defect_id=d, metric=1) for d in ("D-1", "D-2")]
        a = [dict(source_id="GS-01_002", defect_id=d, metric=1, baseline_defect_id="D-1", match_reason="same content", change="완화")
             for d in ("D-a", "D-b")]
        r = audit.defect_summary(b, a)
        self.assertEqual(r["counts"]["완화"], 1)
        self.assertEqual(len(r["issues"]), 1)

    def test_bad_evidence_hash_and_source_are_rejected(self):
        e = self.event(evidence=[{**self.ref, "sha256": "bad"}])
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, e)
        e["evidence"] = [{**self.ref, "source_id": "GS-01_001"}]
        with self.assertRaises(ValueError):
            core.record_evaluation(self.root, e)

    def test_evidence_cannot_escape_bundle(self):
        e = self.event(evidence=[{**self.ref, "path": "../outside"}])
        self.assertTrue(audit.evidence_issues(self.root, [e]))


if __name__ == "__main__":
    unittest.main()
