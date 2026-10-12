"""Smithery MCPB 打包脚本测试：全部离线、临时目录内完成。"""
from __future__ import annotations

import json
import runpy
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import build_smithery_mcpb

ROOT = Path(__file__).resolve().parents[1]

FAKE_TOOLS = [
    {"name": "tool_a", "description": "A 工具", "inputSchema": {"type": "object"}},
    {"name": "tool_b", "description": "", "inputSchema": {"type": "string"}},
]


def _make_source_mcpb(path: Path) -> None:
    """构造一个带 manifest.json 与普通文件的标准 MCPB 样例。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"id": "demo-server", "version": "1.0.0", "tools_generated": True}, ensure_ascii=False),
        )
        archive.writestr("payload/main.py", "print('hello')\n")
        archive.writestr("README.md", "demo\n")


def test_collect_tool_cards_reads_real_mcp_server(monkeypatch):
    """@brief 服务目录不在 sys.path 时必须插入并读取真实工具注册表。"""
    server_dir = ROOT / "mcp-server"
    monkeypatch.setattr(
        sys, "path",
        [entry for entry in sys.path if Path(entry).resolve() != server_dir.resolve()],
    )

    tools = build_smithery_mcpb.collect_tool_cards(ROOT)

    assert tools, "真实 MCP Server 应注册了工具"
    assert "cadstudio_resolve_backend" in {tool["name"] for tool in tools}
    for tool in tools:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert isinstance(tool["inputSchema"], dict)
    assert str(server_dir) in sys.path


def test_collect_tool_cards_skips_insert_when_server_dir_already_on_path(monkeypatch):
    """@brief 服务目录已在 sys.path 时不得重复插入。"""
    server_dir = ROOT / "mcp-server"
    monkeypatch.syspath_prepend(str(server_dir))

    tools = build_smithery_mcpb.collect_tool_cards(ROOT)

    assert tools
    assert sys.path[0] == str(server_dir)


def test_build_smithery_bundle_replaces_manifest_tools(tmp_path, monkeypatch):
    """@brief 新包必须替换 tools 字段、删除 tools_generated 并保留其余条目。"""
    source = tmp_path / "dist" / "pack.mcpb"
    target = tmp_path / "release" / "smithery.mcpb"
    _make_source_mcpb(source)
    monkeypatch.setattr(build_smithery_mcpb, "collect_tool_cards", lambda repo_root: FAKE_TOOLS)

    count = build_smithery_mcpb.build_smithery_bundle(source, target)

    assert count == 2
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {"manifest.json", "payload/main.py", "README.md"}
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert manifest["tools"] == FAKE_TOOLS
    assert "tools_generated" not in manifest
    assert manifest["id"] == "demo-server"


def test_build_smithery_bundle_rejects_empty_tool_registry(tmp_path, monkeypatch):
    source = tmp_path / "pack.mcpb"
    target = tmp_path / "out.mcpb"
    _make_source_mcpb(source)
    monkeypatch.setattr(build_smithery_mcpb, "collect_tool_cards", lambda repo_root: [])

    with pytest.raises(RuntimeError, match="MCP Server 未注册任何工具"):
        build_smithery_mcpb.build_smithery_bundle(source, target)

    assert not target.exists()


def test_main_builds_bundle_and_prints_summary(tmp_path, monkeypatch, capsys):
    source = tmp_path / "pack.mcpb"
    target = tmp_path / "out" / "smithery.mcpb"
    _make_source_mcpb(source)
    monkeypatch.setattr(build_smithery_mcpb, "collect_tool_cards", lambda repo_root: FAKE_TOOLS)
    monkeypatch.setattr(sys, "argv", ["build_smithery_mcpb.py", str(source), str(target)])

    assert build_smithery_mcpb.main() == 0

    out = capsys.readouterr().out
    assert "Smithery MCPB 已生成" in out
    assert "2 个工具 schema" in out
    assert target.exists()


def test_main_block_argparse_error_exits_with_code_two(monkeypatch):
    """@brief 以 __main__ 方式执行且缺少参数时，argparse 必须以退出码 2 结束。"""
    monkeypatch.setattr(sys, "argv", ["build_smithery_mcpb.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "build_smithery_mcpb.py"), run_name="__main__")

    assert excinfo.value.code == 2
