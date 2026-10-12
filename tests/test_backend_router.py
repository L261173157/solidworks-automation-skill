"""scripts/backend_router.py 的命令行解析与路由报告测试。"""
from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

from scripts import backend_router

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "capabilities.yaml"


def test_build_parser_parses_all_flags():
    args = backend_router.build_parser().parse_args([
        "--operation", "solidworks_pointer_array_api",
        "--list",
        "--available", "solidworks-com-pywin32",
        "--requirement", "sw-addon-registered",
        "--solidworks-revision", "34.1.1",
        "--exact-api",
        "--manifest", str(MANIFEST),
    ])

    assert args.operation == "solidworks_pointer_array_api"
    assert args.list is True
    assert args.available == ["solidworks-com-pywin32"]
    assert args.requirement == ["sw-addon-registered"]
    assert args.solidworks_revision == "34.1.1"
    assert args.exact_api is True
    assert args.manifest == MANIFEST


def test_build_parser_defaults_are_neutral():
    args = backend_router.build_parser().parse_args([])

    assert args.operation is None
    assert args.list is False
    assert args.available == []
    assert args.requirement == []
    assert args.solidworks_revision is None
    assert args.exact_api is False
    assert args.manifest is None


def test_route_report_lists_all_routes_from_default_manifest():
    """@brief --list 必须返回就绪摘要和逐路由的后端候选信息。"""
    args = backend_router.build_parser().parse_args(["--list"])

    report = backend_router.route_report(args)

    assert report["status"] == "ready"
    assert report["summary"]["route_count"] == len(report["routes"])
    assert report["summary"]["route_count"] > 0
    assert report["summary"]["backend_count"] > 0
    route = report["routes"][0]
    assert set(route) == {"id", "label", "candidate_backends", "blocked_reason", "requires"}


def test_route_report_requires_operation_when_not_listing():
    args = backend_router.build_parser().parse_args([])

    report = backend_router.route_report(args)

    assert report["status"] == "blocked"
    assert report["error_code"] == "OPERATION_REQUIRED"
    assert "--operation" in report["reason"]


def test_route_report_resolves_operation_with_explicit_manifest():
    """@brief 指定清单与可用后端时必须返回 ready 的后端决策。"""
    args = backend_router.build_parser().parse_args([
        "--operation", "solidworks_pointer_array_api",
        "--available", "solidworks-com-pywin32",
        "--available", "solidworks-native-cpp",
        "--exact-api",
        "--manifest", str(MANIFEST),
    ])

    report = backend_router.route_report(args)

    assert report["status"] == "ready"
    assert report["backend"] == "solidworks-native-cpp"
    assert report["semantics"] == "exact_native"
    assert report["exact_api_requested"] is True


def test_route_report_passes_none_when_no_backends_supplied():
    """@brief 未提供 --available 时必须把空列表归一为 None 交给真源处理。"""
    args = backend_router.build_parser().parse_args(["--operation", "solidworks_pointer_array_api"])

    report = backend_router.route_report(args)

    assert report["status"] == "unavailable"
    assert report["operation_id"] == "solidworks_pointer_array_api"


def test_main_prints_report_and_returns_zero_when_ready(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "backend_router.py",
        "--operation", "solidworks_pointer_array_api",
        "--available", "solidworks-native-cpp",
        "--exact-api",
    ])

    assert backend_router.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"


def test_main_returns_two_when_report_is_blocked(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backend_router.py"])

    assert backend_router.main() == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "OPERATION_REQUIRED"


def test_main_block_via_runpy(monkeypatch, capsys):
    """@brief 以 __main__ 方式执行脚本时必须正常退出并输出 JSON。"""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(sys, "argv", ["backend_router.py", "--list"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "backend_router.py"), run_name="__main__")

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"


def test_module_import_falls_back_to_flat_import_outside_package(monkeypatch):
    """@brief 脱离包上下文加载时必须走 capabilities 平铺导入回退分支。"""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "cad_backend_router_standalone", ROOT / "scripts" / "backend_router.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.build_parser().parse_args([]).operation is None

    args = module.build_parser().parse_args([
        "--operation", "solidworks_pointer_array_api",
        "--available", "solidworks-native-cpp",
        "--exact-api",
    ])
    assert module.route_report(args)["status"] == "ready"
