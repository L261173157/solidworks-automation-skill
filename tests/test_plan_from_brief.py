"""@brief plan_from_brief 需求收敛脚本的离线回归测试。"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "subskills" / "solidworks-vibecad" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import plan_from_brief as pfb  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90x60x8mm", (90.0, 60.0, 8.0)),
        ("120.5X80×10 mm", (120.5, 80.0, 10.0)),
        ("板件 90 x 60 x 8 mm 规格", (90.0, 60.0, 8.0)),
        ("没有给出任何尺寸", None),
    ],
)
def test_parse_triplet_mm(text, expected):
    result = pfb.parse_triplet_mm(text)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_parse_first_number_before_keywords():
    assert pfb.parse_first_number_before("用 22mm 通孔固定", ("通孔", "中心孔")) == 22.0
    assert pfb.parse_first_number_before("25mm 中心孔需要加工", ("通孔", "中心孔")) == 25.0
    assert pfb.parse_first_number_before("没有关键词的文本", ("通孔",)) is None


def test_parse_radius_or_chamfer():
    assert pfb.parse_radius_or_chamfer("外角 R8 处理", "R", 6.0) == 8.0
    assert pfb.parse_radius_or_chamfer("倒角 c1.5", "C", 1.0) == 1.5
    assert pfb.parse_radius_or_chamfer("没有标记的简写", "R", 6.0) == 6.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("四角 M8 螺栓固定", "M8"),
        ("使用 m10 规格", "M10"),
        ("没有螺纹规格", None),
    ],
)
def test_detect_thread(text, expected):
    assert pfb.detect_thread(text) == expected


@pytest.mark.parametrize(
    ("thread", "expected"),
    [
        ("M3", 3.4),
        ("M4", 4.5),
        ("M5", 5.5),
        ("M6", 6.6),
        ("M8", 9.0),
        ("M10", 9.0),
        ("M12", 10.7),
    ],
)
def test_clearance_diameter_table_and_fallback(thread, expected):
    assert pfb.clearance_diameter(thread) == pytest.approx(expected)


FULL_BRIEF = "  150x100x12mm 安装板，四角 M8 固定，中心 30mm 通孔，外角 R8，顶边倒角 C1.5  "


def test_build_plan_extracts_all_explicit_parameters():
    plan = pfb.build_plan(FULL_BRIEF)

    assert plan["source_brief"] == FULL_BRIEF.strip()
    params = plan["parameters"]
    assert params["base_plate"] == {"length": 150.0, "width": 100.0, "thickness": 12.0}
    assert params["fixed_hole_fastener"] == "M8"
    assert params["center_hole_diameter"] == 30.0
    assert params["corner_fillet"] == 8.0
    assert params["edge_chamfer"] == 1.5
    assert params["tap_drill_reference"]["tap_drill"] == pytest.approx(6.8)
    assert params["tap_drill_reference"]["pitch"] == pytest.approx(1.25)

    holes = next(item for item in plan["features"] if item["name"] == "CornerMountingHoles")
    assert holes["params"]["fastener"] == "M8"
    assert holes["params"]["clearance_diameter"] == pytest.approx(9.0)
    assert holes["params"]["positions_xy"] == [
        [-65.0, -40.0],
        [65.0, -40.0],
        [-65.0, 40.0],
        [65.0, 40.0],
    ]
    assert len(plan["assumptions"]) == 3
    assert not any("未识别" in item for item in plan["assumptions"])


def test_build_plan_applies_defaults_and_records_assumptions():
    plan = pfb.build_plan("做一个安装座")

    params = plan["parameters"]
    assert params["base_plate"] == {"length": 90.0, "width": 60.0, "thickness": 8.0}
    assert params["fixed_hole_fastener"] == "M5"
    assert params["center_hole_diameter"] == 22.0
    assert params["corner_fillet"] == 6.0
    assert params["edge_chamfer"] == 1.0
    assert "未识别到底板尺寸" in plan["assumptions"][0]
    assert "未识别固定孔规格" in plan["assumptions"][1]
    assert len(plan["assumptions"]) == 5
    assert "M5" in plan["assumptions"][1]


def test_build_plan_structure_contract():
    plan = pfb.build_plan("90x60x8mm 底板")

    assert plan["plan_version"] == 1
    assert plan["part_family"] == "cnc_mount"
    assert [item["name"] for item in plan["features"]] == [
        "BasePlate",
        "OuterCornerFillet",
        "TopEdgeChamfer",
        "CornerMountingHoles",
        "CenterShaftClearance",
        "SideLighteningPockets",
        "HoleMouthChamfers",
    ]
    assert plan["features"][0]["required"] is True
    assert plan["features"][5]["required"] is False
    assert plan["features"][6]["params"]["distance"] == pytest.approx(0.5)
    assert plan["operation_sequence"][0] == "close_same_name_documents"
    assert plan["operation_sequence"][-1] == "save_export_review"
    assert len(plan["risk_register"]) == 3
    assert plan["risk_register"][0]["mitigation"]
    assert plan["review_plan"]["expected_outputs"][0] == "SLDPRT"
    assert plan["review_plan"]["views"] == ["isometric", "front", "top", "right"]
    assert plan["solidworks_mapping"][0]["operation"] == "new_part"


def test_main_writes_plan_json_and_creates_parent_directories(tmp_path, monkeypatch, capsys):
    out = tmp_path / "deep" / "nested" / "plan.json"
    monkeypatch.setattr(
        sys, "argv", ["plan_from_brief.py", "--brief", "100x50x6mm", "--out", str(out)]
    )

    assert pfb.main() == 0

    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["source_brief"] == "100x50x6mm"
    assert plan["parameters"]["base_plate"]["length"] == 100.0
    assert capsys.readouterr().out.strip() == str(out)


def test_main_reads_brief_from_file(tmp_path, monkeypatch):
    brief_file = tmp_path / "brief.txt"
    brief_file.write_text("120x80x10mm 底板\n", encoding="utf-8")
    out = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys, "argv", ["plan_from_brief.py", "--brief-file", str(brief_file), "--out", str(out)]
    )

    assert pfb.main() == 0

    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["source_brief"] == "120x80x10mm 底板"


def test_main_prefers_inline_brief_over_brief_file(tmp_path, monkeypatch):
    brief_file = tmp_path / "brief.txt"
    brief_file.write_text("120x80x10mm 底板", encoding="utf-8")
    out = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_from_brief.py",
            "--brief",
            "90x60x8mm",
            "--brief-file",
            str(brief_file),
            "--out",
            str(out),
        ],
    )

    assert pfb.main() == 0

    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["source_brief"] == "90x60x8mm"


def test_main_requires_brief_source(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["plan_from_brief.py", "--out", "plan.json"])

    with pytest.raises(SystemExit) as excinfo:
        pfb.main()

    assert excinfo.value.code == 2
    assert "必须提供 --brief 或 --brief-file" in capsys.readouterr().err


def test_console_entry_runs_main(tmp_path, monkeypatch):
    out = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys, "argv", ["plan_from_brief.py", "--brief", "100x50x6mm", "--out", str(out)]
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPT_DIR / "plan_from_brief.py"), run_name="__main__")

    assert excinfo.value.code == 0
    assert out.is_file()
