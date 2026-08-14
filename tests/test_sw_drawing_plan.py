# -*- coding: utf-8 -*-
"""制造零件图计划层无 COM 测试。

覆盖 plan_manufacturing_drawing 的图幅/比例求解、第一角布局、GB/T 1804-m
公差分配、标题栏字段合成、技术要求组装与非法输入门禁。全部纯函数,CI 不需要
SolidWorks 也能覆盖。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from sw_drawing_plan import (  # noqa: E402
    PAPER_SIZES,
    assign_overall_tolerances,
    build_technical_requirements,
    plan_manufacturing_drawing,
    scale_ratio_text,
    select_paper_and_scale,
)


def _evidence(width, height, depth, part_path=r"D:\parts\bracket.SLDPRT", **extra):
    return {
        "part_path": part_path,
        "nominal": {"width_mm": width, "height_mm": height, "depth_mm": depth},
        **extra,
    }


class TestPlanManufacturingDrawing(unittest.TestCase):
    def test_basic_plan_pass_with_first_angle_layout(self):
        plan = plan_manufacturing_drawing(_evidence(60, 10.5, 40))
        self.assertEqual(plan["status"], "pass")
        self.assertEqual(plan["frame"]["paper_size"], "A3")
        self.assertEqual(plan["frame"]["projection"], "first_angle")
        self.assertEqual(plan["scale"], 2.0)
        names = [view["name"] for view in plan["views"]]
        self.assertEqual(names, ["*Front", "*Top", "*Right"])
        # 第一角:俯视图在主视图下方,中间隔一个视图间距 gap_m。
        boxes = {view["name"]: view["box"] for view in plan["views"]}
        gap = plan["gap_m"]
        self.assertAlmostEqual(boxes["*Top"]["top"] + gap, boxes["*Front"]["bottom"], places=9)
        # 视图不侵入工作区,也不互相重叠。
        for view in plan["views"]:
            self.assertGreaterEqual(view["box"]["bottom"], 0.0)
            self.assertLessEqual(view["box"]["top"], PAPER_SIZES["A3"]["height_m"])
        self.assertEqual(plan["dimensioning"]["strategy"], "scan")

    def test_small_part_snaps_to_standard_enlarged_scale(self):
        plan = plan_manufacturing_drawing(_evidence(10, 10, 10))
        self.assertEqual(plan["scale"], 5.0)
        self.assertEqual(plan["scale_ratio_text"], "5:1")
        self.assertEqual(plan["scale_ratio"], [5, 1])

    def test_tall_part_upsizes_paper(self):
        plan = plan_manufacturing_drawing(_evidence(40, 675, 40))
        self.assertIn(plan["frame"]["paper_size"], {"A2", "A1", "A0"})
        self.assertGreaterEqual(plan["scale"], 0.5)
        tried = [item["paper_size"] for item in plan["papers_tried"]]
        self.assertEqual(tried[0], "A3")

    def test_wide_part_lands_on_a2_like_proven_experiment(self):
        plan = plan_manufacturing_drawing(_evidence(600, 25, 400))
        self.assertEqual(plan["frame"]["paper_size"], "A2")
        self.assertEqual(plan["scale"], 0.5)

    def test_huge_part_degrades_to_model_only(self):
        plan = plan_manufacturing_drawing(_evidence(2000, 2000, 2000))
        self.assertEqual(plan["status"], "review_required")
        self.assertEqual(plan["dimensioning"]["strategy"], "model_only")
        self.assertIn("fallback_reason", plan["dimensioning"])
        self.assertEqual(plan["error_code"], "DRAWING_PLAN_SCALE_BELOW_PICKABILITY")
        self.assertEqual(plan["frame"]["paper_size"], "A0")

    def test_overall_tolerances_use_gb1804m_bands(self):
        plan = plan_manufacturing_drawing(_evidence(60, 10.5, 400))
        entries = {item["id"]: item for item in plan["dimensioning"]["overall_dimensions"]}
        self.assertEqual(entries["W"]["tolerance"]["plus_mm"], 0.3)   # ≤120
        self.assertEqual(entries["H"]["tolerance"]["minus_mm"], 0.2)  # ≤30
        self.assertEqual(entries["D"]["tolerance"]["plus_mm"], 0.5)   # ≤400
        self.assertEqual(entries["W"]["tolerance"]["source"], "gb1804-m")

    def test_title_block_auto_fields_scale_and_weight(self):
        evidence = _evidence(60, 10.5, 40, mass={"mass_kg": 1.23456, "mass_meaningful": True})
        frame_spec = {
            "title_block": {"名称": "支架", "材料": "Q235-A", "比例": None},
            "technical_requirements": ["发黑处理。"],
        }
        plan = plan_manufacturing_drawing(evidence, frame_spec)
        fields = plan["title_block"]["fields"]
        self.assertEqual(fields["名称"], "支架")
        self.assertEqual(fields["材料"], "Q235-A")
        self.assertEqual(fields["比例"], "2:1")
        self.assertEqual(fields["重量"], 1.2346)
        self.assertIn("比例", plan["title_block"]["auto_fields"])
        self.assertIn("重量", plan["title_block"]["auto_fields"])
        lines = plan["technical_requirements"]["lines"]
        self.assertEqual(lines[0], "未注公差按 GB/T 1804-m。")
        self.assertIn("发黑处理。", lines)

    def test_meaningless_mass_not_written_to_title_block(self):
        evidence = _evidence(60, 10.5, 40, mass={"mass_kg": 999.0, "mass_meaningful": False})
        plan = plan_manufacturing_drawing(evidence)
        self.assertNotIn("重量", plan["title_block"]["fields"])

    def test_holes_become_review_required_mating_candidates(self):
        evidence = _evidence(60, 10.5, 40, holes=[{"diameter_mm": 8.5}, {"diameter_mm": None}])
        plan = plan_manufacturing_drawing(evidence)
        candidates = plan["mating_dimension_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["diameter_mm"], 8.5)
        self.assertIsNone(candidates[0]["suggestion"])
        self.assertTrue(candidates[0]["review_required"])

    def test_missing_part_path_blocks_plan(self):
        plan = plan_manufacturing_drawing(_evidence(60, 10.5, 40, part_path=""))
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["error_code"], "DRAWING_PLAN_INPUT_INVALID")
        failed = {check["id"] for check in plan["checks"] if check["status"] == "fail"}
        self.assertIn("plan-part-path", failed)

    def test_invalid_nominal_blocks_plan(self):
        for nominal in ({"width_mm": 0, "height_mm": 10, "depth_mm": 10}, None, {"width_mm": "x"}):
            evidence = {"part_path": "p.SLDPRT", "nominal": nominal}
            self.assertEqual(plan_manufacturing_drawing(evidence)["status"], "blocked")

    def test_unknown_projection_and_paper_block_plan(self):
        evidence = _evidence(60, 10.5, 40)
        self.assertEqual(
            plan_manufacturing_drawing(evidence, {"projection": "oblique"})["status"], "blocked"
        )
        self.assertEqual(
            plan_manufacturing_drawing(evidence, {"paper_size": "A9"})["status"], "blocked"
        )

    def test_third_angle_places_top_above_front(self):
        plan = plan_manufacturing_drawing(_evidence(60, 10.5, 40), {"projection": "third_angle"})
        boxes = {view["name"]: view["box"] for view in plan["views"]}
        self.assertAlmostEqual(
            boxes["*Front"]["top"] + plan["gap_m"], boxes["*Top"]["bottom"], places=9
        )

    def test_select_paper_and_scale_reports_tried_papers(self):
        selection = select_paper_and_scale(
            {"width_mm": 40, "height_mm": 675, "depth_mm": 40}, preferred_paper="A3"
        )
        self.assertEqual(selection["status"], "pass")
        self.assertTrue(selection["scale_ok"])
        self.assertGreaterEqual(len(selection["papers_tried"]), 2)

    def test_scale_ratio_text(self):
        self.assertEqual(scale_ratio_text(5.0), "5:1")
        self.assertEqual(scale_ratio_text(0.5), "1:2")
        self.assertEqual(scale_ratio_text(1.0), "1:1")

    def test_assign_overall_tolerances_rejects_unimplemented_grades(self):
        with self.assertRaises(ValueError):
            assign_overall_tolerances({"width_mm": 10, "height_mm": 10, "depth_mm": 10}, grade="f")

    def test_build_technical_requirements_dedupes_blank_lines(self):
        lines = build_technical_requirements(["", "  ", "调质 28~32HRC。"])
        self.assertEqual(lines[0], "未注公差按 GB/T 1804-m。")
        self.assertEqual(lines[-1], "调质 28~32HRC。")
        self.assertEqual(len(lines), 3)


class TestPlanSchemaContract(unittest.TestCase):
    def test_plan_matches_json_schema_when_available(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema 未安装,跳过契约校验")
        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "apps", "desktop", "cad_workbench",
            "schemas", "manufacturing_drawing_plan.schema.json",
        )
        import json

        with open(schema_path, encoding="utf-8") as handle:
            schema = json.load(handle)
        plan = plan_manufacturing_drawing(
            _evidence(60, 10.5, 40, mass={"mass_kg": 1.2, "mass_meaningful": True}),
            {"title_block": {"名称": "支架"}},
        )
        jsonschema.validate(plan, schema)


if __name__ == "__main__":
    unittest.main()
