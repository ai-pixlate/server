"""실제 호출 없이 기록→정정→비교를 연결하고 집계 불변식을 검증한다."""
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

from tools.baseline import audit, be, core
from tools.baseline.run import main


class ConsistencyTest(unittest.TestCase):
    def test_revision_across_all_metric_pairs_never_double_counts(self):
        for old in range(1, 8):
            for new in range(1, 8):
                with self.subTest(old=old, new=new):
                    first = dict(source_id="GS-01_001", defect_id="D-1", metric=old, change="신규")
                    last = {**first, "metric": new}
                    result = audit.defect_summary([], [first, last])
                    self.assertEqual(result["new"], 1)
                    self.assertEqual(sum(r["new"] for r in result["by_metric"].values()), 1)
                    self.assertEqual(result["by_metric"][str(new)]["new"], 1)

    def test_baseline_revision_is_collapsed_before_metric_grouping(self):
        old = dict(source_id="GS-01_001", defect_id="D-1", metric=1)
        last = {**old, "metric": 2}
        follow = dict(source_id="GS-01_001", defect_id="D-next", metric=2, baseline_defect_id="D-1", match_reason="fixture", change="해결")
        result = audit.defect_summary([old, last], [follow])
        self.assertEqual(result["baseline_defects"], 1)
        self.assertEqual(sum(r["baseline_defects"] for r in result["by_metric"].values()), 1)
        self.assertEqual(result["by_metric"]["1"]["issues"], [])

    def test_final_human_grade_controls_new_defect_for_every_grade(self):
        record = dict(source_id="GS-01_001", defect_id="D-1", metric=1, change="신규")
        ai = dict(source_id="GS-01_001", metric=1, scope="original", role="primary", grade="C", defect_ids=["D-1"])
        for grade in core.GRADES:
            human = {**ai, "role": "human", "grade": grade, "defect_ids": ["D-1"] if grade in ("B", "C") else []}
            result = audit.defect_summary([], [record], after_evaluations=[ai, human])
            self.assertEqual(result["new"], int(grade in ("B", "C")))
            self.assertEqual(sum(r["new"] for r in result["by_metric"].values()), result["new"])

    def test_resolved_tracking_record_survives_final_a(self):
        base = dict(source_id="GS-01_001", defect_id="D-1", metric=1)
        follow = {**base, "defect_id": "D-after", "baseline_defect_id": "D-1", "change": "해결", "match_reason": "fixture"}
        final = dict(source_id="GS-01_001", metric=1, scope="original", role="human", grade="A", defect_ids=[])
        result = audit.defect_summary([base], [follow], after_evaluations=[final])
        self.assertEqual(result["counts"]["해결"], 1)
        self.assertEqual(result["issues"], [])
        final.update(grade="C", defect_ids=["D-after"])
        result = audit.defect_summary([base], [follow], after_evaluations=[final])
        self.assertEqual(result["counts"]["해결"], 0)
        self.assertTrue(result["issues"])

    def test_current_defect_missing_detail_is_reported(self):
        final = dict(source_id="GS-01_001", metric=1, scope="original", role="human", grade="C", defect_ids=["D-1"])
        result = audit.defect_summary([], [], after_evaluations=[final])
        self.assertTrue(any("상세 기록 없음" in e["reason"] for e in result["issues"]))

    def test_followup_can_reference_stable_baseline_defect_id(self):
        base = dict(source_id="GS-01_001", defect_id="D-1", metric=1)
        follow = {**base, "defect_id": "D-after", "baseline_defect_id": "D-1", "change": "유지", "match_reason": "fixture"}
        final = dict(source_id="GS-01_001", metric=1, scope="original", role="human", grade="C", defect_ids=["D-1"])
        result = audit.defect_summary([base], [follow], after_evaluations=[final])
        self.assertEqual(result["counts"]["유지"], 1)
        self.assertEqual(result["issues"], [])

    def test_public_executor_pending_approval_never_launches(self):
        environment, paths = dict(os.environ), list(sys.path)
        with patch.object(be.subprocess, "run") as launch:
            with self.assertRaises(ValueError):
                be.run_prepared("fixture", True)
            launch.assert_not_called()
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(sys.path, paths)

    def test_no_permission_or_cached_product_never_reaches_database(self):
        with patch.object(be.subprocess, "run") as launch:
            with self.assertRaises(ValueError):
                be.run_prepared("fixture")
            launch.assert_not_called()
        with patch.dict(sys.modules, {"app.db": ModuleType("app.db")}), patch.object(be, "read") as read:
            with self.assertRaises(ValueError):
                be._run_prepared("fixture", True)
            read.assert_not_called()

    def test_engine_and_session_must_both_point_to_isolated_database(self):
        engine = SimpleNamespace(url="local")
        db = SimpleNamespace(engine=engine, SessionLocal=SimpleNamespace(kw={"bind": engine}))
        be.verify_database_binding(db, "local", lambda value: value)
        engine.url = "other"
        with self.assertRaises(ValueError):
            be.verify_database_binding(db, "local", lambda value: value)
        engine.url = "local"
        db.SessionLocal.kw["bind"] = SimpleNamespace(url="local")
        with self.assertRaises(ValueError):
            be.verify_database_binding(db, "local", lambda value: value)

    def bundle(self, path):
        core.prepare(core.template(), path)
        # 합성 회차: 원본을 읽거나 생성하지 않는다. 비교 입력 ID·해시만 고정한다.
        (path / "inputs.json").write_text(json.dumps([{"source_id": sid, "sha256": "fixture"} for sid in core.IDS]), encoding="utf-8")
        (path / "proof").write_bytes(b"fixture")

    def event(self, path, eid, role, grade, metric=1):
        event = dict(id=eid, source_id="GS-01_001", metric=metric, role=role, reviewer="fixture", session=eid,
                     scope="original", grade=grade, evidence=[dict(path="proof", source_id="GS-01_001", sha256=core.digest(path / "proof"))],
                     reason="fixture", coverage_complete=True)
        if grade in ("B", "C"):
            event.update(defect_ids=["D-new"], locations=[[0, 0, 10, 10]], observation="fixture", expected="fixture", defect_type="fixture")
        return event

    def test_record_human_retraction_then_cli_compare_agrees_with_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            for path in (before, after):
                self.bundle(path)
            core.record_evaluation(before, self.event(before, "b", "primary", "A"))
            core.record_evaluation(after, self.event(after, "a", "primary", "C"))
            detail = dict(id="d", source_id="GS-01_001", defect_id="D-new", metric=1, location=[0,0,10,10],
                          defect_type="fixture", observation="fixture", expected="fixture", change="신규", reviewer="fixture",
                          evidence=self.event(after, "x", "primary", "A")["evidence"])
            core.record_detail(after, "defects", detail)
            core.record_evaluation(after, self.event(after, "h", "human", "A"))
            main(["compare", str(before), str(after), str(root / "comparison.json")])
            result = core.read(root / "comparison.json")
            self.assertEqual(result["transitions"]["1"]["matrix"]["A"]["A"], 1)
            self.assertEqual(result["defects"]["new"], 0)
            self.assertEqual(len(result["defects"]["excluded_current"]), 1)
            self.assertFalse(result["formal_complete"])

    def test_record_metric_revision_then_cli_compare_has_consistent_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            for path in (before, after):
                self.bundle(path)
            for metric in (1, 2):
                core.record_evaluation(before, self.event(before, f"b{metric}", "primary", "A", metric))
            core.record_evaluation(after, self.event(after, "a1", "primary", "A", 1))
            core.record_evaluation(after, self.event(after, "a2", "primary", "B", 2))
            detail = dict(id="d1", source_id="GS-01_001", defect_id="D-new", metric=1, location=[0,0,10,10],
                          defect_type="fixture", observation="fixture", expected="fixture", change="신규", reviewer="fixture",
                          evidence=self.event(after, "x", "primary", "A")["evidence"])
            core.record_detail(after, "defects", detail)
            core.record_detail(after, "defects", {**detail, "id": "d2", "metric": 2, "supersedes": "d1", "revision_reason": "지표 정정"})
            main(["compare", str(before), str(after), str(root / "comparison.json")])
            result = core.read(root / "comparison.json")["defects"]
            self.assertEqual(result["new"], 1)
            self.assertEqual(sum(v["new"] for v in result["by_metric"].values()), 1)
            self.assertEqual(result["by_metric"]["1"]["new"], 0)
            self.assertEqual(result["by_metric"]["2"]["new"], 1)


if __name__ == "__main__":
    unittest.main()
