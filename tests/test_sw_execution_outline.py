"""@brief sw_execution_outline 执行大纲脚本的离线回归测试。"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "subskills" / "solidworks-vibecad" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import sw_execution_outline as seo  # noqa: E402


def sample_plan():
    """@brief 构造覆盖已知/未知操作和有无降级策略的最小计划。"""
    return {
        "part_family": "cnc_mount",
        "operation_sequence": ["close_same_name_documents", "new_part", "mystery_operation"],
        "features": [
            {"name": "BasePlate", "type": "base_plate", "required": True, "fallback": "降级为矩形"},
            {"name": "CenterShaftClearance", "type": "through_hole", "required": True},
        ],
        "review_plan": {
            "expected_outputs": ["SLDPRT", "STEP"],
            "views": ["isometric"],
            "checks": ["expected_outputs_exist"],
        },
    }


def test_load_plan_reads_json(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(sample_plan()), encoding="utf-8")

    assert seo.load_plan(path) == sample_plan()


def test_build_outline_maps_known_and_unknown_operations():
    outline = seo.build_outline(sample_plan())

    assert outline["source_part_family"] == "cnc_mount"
    assert outline["execution_mode"] == "solidworks_com_serial"
    assert "meters" in outline["unit_policy"]
    assert outline["steps"][0] == {
        "index": 1,
        "operation": "close_same_name_documents",
        "preferred_api": "sw_session.SolidWorksSession.close",
        "status": "ready",
    }
    assert outline["steps"][1]["status"] == "ready"
    unknown = outline["steps"][2]
    assert unknown == {
        "index": 3,
        "operation": "mystery_operation",
        "preferred_api": "api_lookup_required",
        "status": "needs_api_lookup",
    }


def test_build_outline_feature_gate_defaults_fallback():
    outline = seo.build_outline(sample_plan())

    assert outline["feature_gate"][0] == {
        "feature": "BasePlate",
        "type": "base_plate",
        "required": True,
        "fallback": "降级为矩形",
    }
    assert outline["feature_gate"][1]["fallback"] == ""
    assert outline["feature_gate"][1]["type"] == "through_hole"
    assert outline["review_gate"] == sample_plan()["review_plan"]
    assert "review report marks previews blank" in outline["stop_conditions"]
    assert "SolidWorks preflight fails" in outline["stop_conditions"]


def test_write_markdown_sections_and_fallback_lines():
    markdown = seo.write_markdown(seo.build_outline(sample_plan()))

    assert markdown.startswith("# SolidWorks 执行摘要\n")
    assert "- 零件族: `cnc_mount`" in markdown
    assert "- 执行模式: `solidworks_com_serial`" in markdown
    assert "## 步骤" in markdown
    assert "1. `close_same_name_documents` -> `sw_session.SolidWorksSession.close` [ready]" in markdown
    assert "3. `mystery_operation` -> `api_lookup_required` [needs_api_lookup]" in markdown
    assert "## 特征门禁" in markdown
    assert "- `BasePlate` (base_plate), required=True；降级：降级为矩形" in markdown
    assert "- `CenterShaftClearance` (through_hole), required=True\n" in markdown
    assert "## 停止条件" in markdown
    assert "- SolidWorks preflight fails" in markdown
    assert markdown.endswith("\n")


def test_main_writes_markdown_summary_and_creates_parents(tmp_path, monkeypatch, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(sample_plan()), encoding="utf-8")
    out = tmp_path / "deep" / "outline.md"
    monkeypatch.setattr(
        sys,
        "argv",
        ["sw_execution_outline.py", "--plan", str(plan_path), "--out", str(out)],
    )

    assert seo.main() == 0

    markdown = out.read_text(encoding="utf-8")
    assert markdown.startswith("# SolidWorks 执行摘要")
    assert "## 停止条件" in markdown
    assert capsys.readouterr().out.strip() == str(out)


def test_console_entry_runs_main(tmp_path, monkeypatch):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(sample_plan()), encoding="utf-8")
    out = tmp_path / "outline.md"
    monkeypatch.setattr(
        sys,
        "argv",
        ["sw_execution_outline.py", "--plan", str(plan_path), "--out", str(out)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPT_DIR / "sw_execution_outline.py"), run_name="__main__")

    assert excinfo.value.code == 0
    assert out.is_file()
