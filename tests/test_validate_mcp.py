"""validate_mcp MCP 协议层验证回归测试。

stdio 传输与 ClientSession 全部替换为进程内伪实现，绝不启动真实 MCP server 子进程；
__main__ 入口通过 runpy 在进程内执行。
"""
from __future__ import annotations

import asyncio
import runpy
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import mcp  # noqa: E402
import mcp.client.stdio  # noqa: E402
import validate_mcp  # noqa: E402


class FakeToolResult:
    def __init__(self, names):
        self.tools = [types.SimpleNamespace(name=name) for name in names]


class FakeSession:
    def __init__(self, read_stream, write_stream, names):
        self.read_stream = read_stream
        self.write_stream = write_stream
        self.names = names
        self.initialized = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return FakeToolResult(self.names)


class FakeStdioClient:
    def __init__(self, params):
        self.params = params

    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, *_exc):
        return False


def _patch_session(monkeypatch, names, *, on_validate_module):
    """@brief 打桩 stdio_client 与 ClientSession。

    on_validate_module=True 时替换 validate_mcp 模块内绑定（直接调用函数用）；
    否则替换 mcp 源模块属性（runpy 重新执行脚本时会重新 import）。
    """
    names = list(names)

    def session_factory(read_stream, write_stream):
        return FakeSession(read_stream, write_stream, names)

    def stdio_factory(params):
        return FakeStdioClient(params)

    if on_validate_module:
        monkeypatch.setattr(validate_mcp, "ClientSession", session_factory)
        monkeypatch.setattr(validate_mcp, "stdio_client", stdio_factory)
    else:
        monkeypatch.setattr(mcp, "ClientSession", session_factory)
        monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio_factory)


def test_list_tools_returns_server_tool_names(monkeypatch):
    """@brief 会话初始化后能拿到 tools/list 的工具名集合。"""
    _patch_session(monkeypatch, ["solidworks_connect", "cadstudio_check_dfm"], on_validate_module=True)

    names = asyncio.run(validate_mcp._list_tools())

    assert names == {"solidworks_connect", "cadstudio_check_dfm"}


def test_main_passes_when_required_tools_visible(monkeypatch, capsys):
    """@brief 关键工具全部可见（允许多出工具）时返回 0。"""
    _patch_session(
        monkeypatch, sorted(validate_mcp.REQUIRED_TOOLS) + ["extra_tool"], on_validate_module=True
    )

    assert asyncio.run(validate_mcp._main()) == 0
    assert "MCP 验证通过" in capsys.readouterr().out


def test_main_fails_when_tools_missing(monkeypatch):
    """@brief 缺少关键工具时以 AssertionError 列出缺失项。"""
    _patch_session(monkeypatch, {"cadstudio_check_dfm"}, on_validate_module=True)

    with pytest.raises(AssertionError, match="solidworks_connect"):
        asyncio.run(validate_mcp._main())


def test_main_raises_when_server_missing(monkeypatch, tmp_path):
    """@brief 找不到 MCP Server 脚本时抛出带路径的 FileNotFoundError。"""
    monkeypatch.setattr(validate_mcp, "SERVER_PATH", tmp_path / "mcp-server" / "server.py")

    with pytest.raises(FileNotFoundError, match="找不到 MCP Server"):
        asyncio.run(validate_mcp._main())


def test_entrypoint_success(monkeypatch, capsys):
    """@brief __main__ 方式执行：工具齐全时打印通过并退出 0。"""
    _patch_session(monkeypatch, sorted(validate_mcp.REQUIRED_TOOLS), on_validate_module=False)
    monkeypatch.setattr(sys, "argv", ["validate_mcp.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "validate_mcp.py"), run_name="__main__")

    assert excinfo.value.code == 0
    assert "MCP 验证通过" in capsys.readouterr().out


def test_entrypoint_failure_reported(monkeypatch, capsys):
    """@brief __main__ 方式执行：任何异常都被捕获、打印并以退出码 1 结束。"""
    _patch_session(monkeypatch, ["cadstudio_check_dfm"], on_validate_module=False)
    monkeypatch.setattr(sys, "argv", ["validate_mcp.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "validate_mcp.py"), run_name="__main__")

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "MCP 验证失败" in captured.err
    assert "solidworks_connect" in captured.err


def test_configure_stdio_utf8_tolerates_plain_streams(monkeypatch):
    """@brief 有 reconfigure 能力的流被重配置为 UTF-8，没有的跳过。"""

    class RecordingStream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    class PlainStream:
        pass

    recording = RecordingStream()
    monkeypatch.setattr(validate_mcp.sys, "stdout", recording)
    monkeypatch.setattr(validate_mcp.sys, "stderr", PlainStream())

    validate_mcp._configure_stdio_utf8()

    assert recording.calls == [{"encoding": "utf-8", "errors": "replace"}]
