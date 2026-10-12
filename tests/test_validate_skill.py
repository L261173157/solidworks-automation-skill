"""scripts/validate_skill.py 静态完整性检查脚本测试。"""
from __future__ import annotations

import ast
import json
import runpy
import sys
from pathlib import Path

import pytest

from scripts import validate_skill

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_RELATIVE_PATHS = [
    "apps/desktop/cad_workbench/schemas/automation_job.schema.json",
    "apps/desktop/cad_workbench/schemas/codex_final_response.schema.json",
    "apps/desktop/cad_workbench/schemas/neutral_cad_document.schema.json",
    "apps/desktop/cad_workbench/schemas/preview_manifest.schema.json",
    "apps/desktop/cad_workbench/schemas/evidence_graph.schema.json",
    "apps/desktop/cad_workbench/schemas/fea_analysis.schema.json",
    "apps/desktop/cad_workbench/schemas/advanced_geometry.schema.json",
    "apps/desktop/cad_workbench/schemas/feature_graph.schema.json",
    "subskills/solidworks-vibecad/schemas/design_plan.schema.json",
    "subskills/solidworks-engineering-drawing/schemas/drawing_spec.schema.json",
]


class RecordingStream:
    """@brief 记录 reconfigure 调用的伪输出流。"""

    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class PlainStream:
    """@brief 不支持 reconfigure 的旧式输出流。"""


def test_configure_stdio_utf8_handles_both_stream_kinds(monkeypatch):
    """@brief 支持 reconfigure 的流必须被配置，旧流必须被安全跳过。"""
    stdout, stderr = RecordingStream(), PlainStream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    validate_skill._configure_stdio_utf8()

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_check_required_files_passes_on_real_repo():
    validate_skill.check_required_files()


def test_check_required_files_lists_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_skill, "ROOT", tmp_path)

    with pytest.raises(AssertionError) as excinfo:
        validate_skill.check_required_files()

    message = str(excinfo.value)
    assert "缺少必需文件" in message
    assert "SKILL.md" in message


def test_check_skill_frontmatter_passes_on_real_repo():
    validate_skill.check_skill_frontmatter()


def test_check_skill_frontmatter_requires_leading_frontmatter(monkeypatch, tmp_path):
    (tmp_path / "SKILL.md").write_text("name: solidworks-automation\n", encoding="utf-8")
    monkeypatch.setattr(validate_skill, "ROOT", tmp_path)

    with pytest.raises(AssertionError, match="frontmatter"):
        validate_skill.check_skill_frontmatter()


def test_check_skill_frontmatter_requires_correct_name(monkeypatch, tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: other-skill\n---\ndescription: x\n", encoding="utf-8")
    monkeypatch.setattr(validate_skill, "ROOT", tmp_path)

    with pytest.raises(AssertionError, match="缺少正确的 name"):
        validate_skill.check_skill_frontmatter()


def test_check_skill_frontmatter_requires_description(monkeypatch, tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: solidworks-automation\n---\n", encoding="utf-8")
    monkeypatch.setattr(validate_skill, "ROOT", tmp_path)

    with pytest.raises(AssertionError, match="缺少 description"):
        validate_skill.check_skill_frontmatter()


def test_check_python_syntax_passes_on_real_repo():
    count = validate_skill.check_python_syntax()

    assert count > 0


def test_check_python_syntax_skips_cache_and_build_dirs(monkeypatch, tmp_path):
    """@brief 缓存目录内的语法损坏文件必须被排除，不参与 ast 检查。"""
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "__pycache__").mkdir(parents=True)
    (tmp_path / "examples").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "apps" / "desktop").mkdir(parents=True)
    (tmp_path / "mcp-server").mkdir()
    (tmp_path / "subskills").mkdir()
    (scripts_dir / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "examples" / "ok.py").write_text("VALUE = 2\n", encoding="utf-8")
    (scripts_dir / "__pycache__" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(validate_skill, "ROOT", tmp_path)

    assert validate_skill.check_python_syntax() == 2


def test_check_json_files_passes_on_real_repo():
    assert validate_skill.check_json_files() == 10


def test_check_json_files_enforces_ten_golden_workflows(monkeypatch, tmp_path):
    """@brief 黄金工作流数量偏离 10 项基线时必须失败。"""
    for relative in SCHEMA_RELATIVE_PATHS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    (tmp_path / "capabilities.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "golden-workflows.yaml").write_text(
        json.dumps({"workflows": [{"id": index} for index in range(9)]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(validate_skill, "ROOT", tmp_path)

    with pytest.raises(AssertionError, match="10 项基线"):
        validate_skill.check_json_files()


def test_main_passes_on_real_repo(capsys):
    validate_skill.main()

    out = capsys.readouterr().out
    assert "验证通过" in out
    assert "个 Python 文件" in out
    assert "个 JSON Schema 正常" in out


def test_main_block_success_via_runpy(capsys):
    runpy.run_path(str(ROOT / "scripts" / "validate_skill.py"), run_name="__main__")

    assert "验证通过" in capsys.readouterr().out


def test_main_block_failure_prints_reason_and_exits_one(monkeypatch, capsys):
    """@brief 任意检查失败时必须输出失败原因并以退出码 1 结束。"""

    def broken_parse(*_args, **_kwargs):
        raise SyntaxError("boom")

    monkeypatch.setattr(ast, "parse", broken_parse)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "validate_skill.py"), run_name="__main__")

    assert excinfo.value.code == 1
    assert "验证失败" in capsys.readouterr().err
