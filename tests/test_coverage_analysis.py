import copy
import json
import unittest
from datetime import date
from pathlib import Path

import coverage_analysis as ca

DATA = Path(__file__).resolve().parent.parent / "data"
PLANNING_DATE = date(2026, 11, 24)


def load_report():
    tables = ca.load_tables(DATA)
    return tables, ca.analyze(tables, PLANNING_DATE)


def event_area(report, event_id, area_id):
    for event in report["events"]:
        if event["event_id"] == event_id:
            for area in event["areas"]:
                if area["area_id"] == area_id:
                    return area
    raise KeyError((event_id, area_id))


def plan_area(report, area_id):
    for area in report["current_plan"]["areas"]:
        if area["area_id"] == area_id:
            return area
    raise KeyError(area_id)


class BoundaryResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.report = load_report()

    def test_event_dates_resolve_to_contemporaneous_version(self):
        version_by_event = {e["event_id"]: e["boundary_version"] for e in self.report["events"]}
        self.assertEqual(
            version_by_event,
            {"E1": "B1", "E2": "B1", "E3": "B2", "E4": "B2"},
        )

    def test_planning_date_uses_current_version(self):
        valid = ca.areas_valid_on(self.tables["areas"], PLANNING_DATE)
        self.assertEqual(valid, {"A3": "B2", "A4": "B2"})
        self.assertEqual(self.report["current_plan"]["boundary_version"], "B2")
        self.assertEqual(
            [a["area_id"] for a in self.report["current_plan"]["areas"]],
            ["A3", "A4"],
        )


class EvidenceSeparationTests(unittest.TestCase):
    def setUp(self):
        _, self.report = load_report()

    def test_missing_stage_is_none_not_zero(self):
        funnel = event_area(self.report, "E1", "A1")["funnel"]
        self.assertEqual(funnel["invited"], 12)
        self.assertIsNone(funnel["interested"])
        self.assertIsNone(funnel["registered"])
        self.assertEqual(funnel["attended"], 9)

    def test_evidence_types_never_merged(self):
        # 每条社区-活动记录都分开保存证据链与问卷，不存在合并的需求字段。
        area = event_area(self.report, "E2", "A2")
        self.assertEqual(set(area["funnel"]), set(ca.STAGES))
        self.assertIsNotNone(area["survey"])
        self.assertNotIn("demand", area)
        serialized = json.dumps(self.report, ensure_ascii=False)
        self.assertNotIn("demand", serialized)


class CoverageMetricTests(unittest.TestCase):
    def setUp(self):
        _, self.report = load_report()

    def test_attendance_rate_uses_as_of_population(self):
        self.assertAlmostEqual(event_area(self.report, "E1", "A1")["attendance_rate"], 9 / 120)
        self.assertAlmostEqual(event_area(self.report, "E3", "A3")["attendance_rate"], 10 / 150)

    def test_event_capacity_utilization(self):
        e3 = next(e for e in self.report["events"] if e["event_id"] == "E3")
        self.assertEqual(e3["capacity"], 12)
        self.assertAlmostEqual(e3["utilization"], 10 / 12)

    def test_estimated_population_flagged(self):
        flags = event_area(self.report, "E1", "A1")["flags"]
        self.assertTrue(any("估计值" in f for f in flags))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        _, self.report = load_report()

    def test_b1_comparison_flags_access_tier(self):
        comp = next(c for c in self.report["comparisons"] if c["boundary_version"] == "B1")
        self.assertEqual(comp["areas"], ["A1", "A2"])
        self.assertTrue(any("交通层级不同" in c for c in comp["confounders"]))
        self.assertFalse(comp["can_infer_interest"])

    def test_b2_comparison_flags_shuttle_difference(self):
        comp = next(c for c in self.report["comparisons"] if c["boundary_version"] == "B2")
        self.assertTrue(any("交通方式不同" in c for c in comp["confounders"]))
        self.assertFalse(comp["can_infer_interest"])

    def test_cross_boundary_note_present(self):
        self.assertIn("重叠系数", self.report["cross_boundary_note"])


class CurrentPlanTests(unittest.TestCase):
    def setUp(self):
        _, self.report = load_report()

    def actions(self, area_id):
        return [r["action"] for r in plan_area(self.report, area_id)["recommendations"]]

    def test_far_area_without_shuttle_gets_shuttle_pilot(self):
        self.assertIn("试点集体接送", self.actions("A4"))
        self.assertIn("列为优先外展对象", self.actions("A4"))

    def test_saturated_area_gets_capacity_and_shuttle_maintenance(self):
        self.assertIn("增加场次或提高容量", self.actions("A3"))
        self.assertIn("维持集体接送投入", self.actions("A3"))

    def test_translated_history_is_approximate(self):
        a3 = plan_area(self.report, "A3")
        self.assertAlmostEqual(a3["translated_attended_total"], 9 * 0.25 + 3 * 0.65)
        self.assertTrue(any("近似" in c for c in a3["cautions"]))

    def test_low_attendance_with_poor_access_not_read_as_low_interest(self):
        a4 = plan_area(self.report, "A4")
        self.assertTrue(any("不能解读为兴趣低" in c for c in a4["cautions"]))


class TemporalSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.report = load_report()

    def test_access_correction_updates_plan_but_not_b1_history(self):
        corrected = copy.deepcopy(self.tables)
        for row in corrected["access"]:
            if row["area_id"] == "A4" and row["period"] == "2026Q4":
                row["transport_mode"] = "walk"
                row["travel_min"] = "18"
        new_report = ca.analyze(corrected, PLANNING_DATE)

        new_actions = [r["action"] for a in new_report["current_plan"]["areas"]
                       if a["area_id"] == "A4" for r in a["recommendations"]]
        self.assertNotIn("试点集体接送", new_actions)

        # B1 期历史段落（E1、E2）不受当期接送资料更正影响。
        old_b1 = [e for e in self.report["events"] if e["boundary_version"] == "B1"]
        new_b1 = [e for e in new_report["events"] if e["boundary_version"] == "B1"]
        self.assertEqual(old_b1, new_b1)

    def test_new_boundary_version_replans_without_rewriting_history(self):
        shifted = copy.deepcopy(self.tables)
        for row in shifted["areas"]:
            if row["boundary_version"] == "B2":
                row["valid_to"] = "2026-12-31"
        shifted["areas"].extend([
            {"area_id": "A5", "boundary_version": "B3", "area_label": "北部片区",
             "valid_from": "2027-01-01", "valid_to": ""},
            {"area_id": "A6", "boundary_version": "B3", "area_label": "近馆片区",
             "valid_from": "2027-01-01", "valid_to": ""},
        ])
        new_report = ca.analyze(shifted, date(2027, 2, 1))

        self.assertEqual(new_report["current_plan"]["boundary_version"], "B3")
        self.assertEqual(
            [a["area_id"] for a in new_report["current_plan"]["areas"]],
            ["A5", "A6"],
        )
        # 历史活动仍按当时口径解释，段落内容不变。
        self.assertEqual(new_report["events"], self.report["events"])


class DataGapTests(unittest.TestCase):
    def setUp(self):
        _, self.report = load_report()
        self.gaps = "\n".join(self.report["data_gaps"])

    def test_estimated_population_listed(self):
        self.assertIn("A1", self.gaps)
        self.assertIn("A3", self.gaps)
        self.assertIn("估计值", self.gaps)

    def test_small_surveys_and_form_versions_listed(self):
        self.assertIn("问卷样本过少", self.gaps)
        self.assertIn("F1", self.gaps)
        self.assertIn("F2", self.gaps)

    def test_registration_source_gap_listed(self):
        self.assertIn("报名来源", self.gaps)

    def test_missing_invitation_records_listed(self):
        self.assertIn("邀请", self.gaps)
        self.assertIn("E4", self.gaps)


class ConsistencyTests(unittest.TestCase):
    def test_fixture_has_no_consistency_issues(self):
        _, report = load_report()
        for event in report["events"]:
            self.assertEqual(event["consistency_issues"], [])

    def test_participation_from_wrong_version_is_flagged(self):
        tables, _ = load_report()
        tables["participation"].append({
            "record_id": "PX", "group_token": "GX", "area_id": "A2",
            "event_id": "E4", "stage": "attended", "headcount": "2",
        })
        report = ca.analyze(tables, PLANNING_DATE)
        e4 = next(e for e in report["events"] if e["event_id"] == "E4")
        self.assertTrue(any("A2" in issue for issue in e4["consistency_issues"]))


class RenderTests(unittest.TestCase):
    def test_text_report_sections_and_json_export(self):
        _, report = load_report()
        text = ca.render_text(report)
        for marker in ["口径说明", "各活动服务覆盖", "不可推断", "下一轮外展建议",
                       "资料覆盖不足", "教研可比性", "不能合并为单一需求数"]:
            self.assertIn(marker, text)
        json.dumps(report, ensure_ascii=False)  # 必须可序列化


if __name__ == "__main__":
    unittest.main()
