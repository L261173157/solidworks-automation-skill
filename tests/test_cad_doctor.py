"""cad_doctor 环境诊断 CLI 回归测试。

所有外部探测（模块查找、PATH 探测、CAD 发现、目录写入、COM 连接）都被
monkeypatch 替换，测试完全离线确定，不触碰真实注册表与真实 COM。
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cad_doctor  # noqa: E402
import fea_analysis  # noqa: E402
import sw_connect  # noqa: E402


def _patch_doctor_inputs(
    monkeypatch,
    *,
    find_spec="missing",
    which=None,
    solver_status="pass",
    sw_registered=True,
    sw_executables=(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe",),
    autocad_installed=True,
    writable=True,
):
    """@brief 一次性打桩 run_doctor 的全部外部输入。

    find_spec: "missing"=全部未安装；callable=自定义；其他值=全部已安装。
    which: None=全部未发现；callable=自定义；其他值=全部发现。
    solver_status: "pass"/"blocked" 传给 discover_solver，"raise" 抛异常。
    """
    if callable(find_spec):
        monkeypatch.setattr(cad_doctor.importlib.util, "find_spec", find_spec)
    else:
        result = None if find_spec == "missing" else find_spec
        monkeypatch.setattr(cad_doctor.importlib.util, "find_spec", lambda _name: result)
    if callable(which):
        monkeypatch.setattr(cad_doctor.shutil, "which", which)
    else:
        result = None if which is None else which
        monkeypatch.setattr(cad_doctor.shutil, "which", lambda _name: result)

    if solver_status == "raise":
        def broken_solver(_name):
            raise RuntimeError("探测失败")

        monkeypatch.setattr(fea_analysis, "discover_solver", broken_solver)
    else:
        monkeypatch.setattr(fea_analysis, "discover_solver", lambda _name: {"status": solver_status})

    installation = {
        "registered": sw_registered,
        "executables": list(sw_executables),
        "executable": sw_executables[0] if sw_executables else None,
        "source": "test" if sw_registered else None,
        "version": "2026",
        "servicePack": "SP01",
        "displayName": "SOLIDWORKS 2026",
        "shortcut": None,
        "available": [],
    }
    monkeypatch.setattr(cad_doctor, "_solidworks_installation", lambda: installation)
    monkeypatch.setattr(
        cad_doctor,
        "discover_all",
        lambda: {
            "autocad": {
                "installed": autocad_installed,
                "executable": r"C:\Program Files\Autodesk\AutoCAD 2024\acad.exe" if autocad_installed else None,
            }
        },
    )
    monkeypatch.setattr(cad_doctor, "_is_writable", lambda _path: writable)


def test_check_builds_stable_results():
    """@brief 通过/失败、有无修复字段的全部组合都要产出稳定结构。"""
    ok = cad_doctor._check("demo", True, "一切正常")
    assert ok == {"id": "demo", "status": "passed", "code": "DEMO", "message": "一切正常"}

    named = cad_doctor._check("demo2", True, "ok", code="CUSTOM")
    assert named["code"] == "CUSTOM"

    failed = cad_doctor._check(
        "demo3",
        False,
        "缺失",
        severity="warning",
        code="MISSING",
        action="安装",
        install_command="pip install x",
        download_url="https://example.invalid",
        required=True,
    )
    assert failed == {
        "id": "demo3",
        "status": "warning",
        "code": "MISSING",
        "message": "缺失",
        "action": "安装",
        "installCommand": "pip install x",
        "downloadUrl": "https://example.invalid",
        "required": True,
    }

    bare = cad_doctor._check("demo4", False, "缺失")
    assert bare == {"id": "demo4", "status": "error", "code": "DEMO4", "message": "缺失", "required": False}


def test_collect_remediations_filters_and_deduplicates():
    """@brief 已通过/无动作/Agent 可替代/重复 ID 的检查项都不能进入修复清单。"""
    checks = [
        cad_doctor._check("a.passed", True, "ok", action="不需要"),
        cad_doctor._check("b.noaction", False, "失败"),
        cad_doctor._check("agent.one", False, "缺", severity="warning", action="装 one"),
        cad_doctor._check("agent.two", True, "ok"),
        cad_doctor._check("dup", False, "第一次", severity="warning", action="修"),
        cad_doctor._check("dup", False, "第二次", severity="warning", action="修"),
        cad_doctor._check(
            "core",
            False,
            "失败",
            severity="warning",
            action="修复",
            install_command="pip i",
            download_url="https://d",
            required=True,
        ),
    ]

    remediations = cad_doctor._collect_remediations(checks)

    assert [item["id"] for item in remediations] == ["dup", "core"]
    assert remediations[0] == {"id": "dup", "title": "修", "reason": "第一次", "required": False}
    assert remediations[1] == {
        "id": "core",
        "title": "修复",
        "reason": "失败",
        "required": True,
        "installCommand": "pip i",
        "downloadUrl": "https://d",
    }

    solo = cad_doctor._collect_remediations(
        [cad_doctor._check("agent.only", False, "缺", severity="warning", action="装")]
    )
    assert solo == [{"id": "agent.only", "title": "装", "reason": "缺", "required": False}]


def test_is_writable_true_and_oserror(tmp_path):
    """@brief 可写目录返回 True；把文件当目录探测时按不可写处理。"""
    assert cad_doctor._is_writable(tmp_path / "sub") is True
    assert (tmp_path / "sub").is_dir()

    blocker = tmp_path / "f.txt"
    blocker.write_text("占用", encoding="utf-8")
    assert cad_doctor._is_writable(blocker) is False


def test_solidworks_installation_shape(monkeypatch):
    """@brief 有可执行文件时 executables 收敛为单元素，没有时为空列表。"""
    monkeypatch.setattr(
        cad_doctor,
        "discover_all",
        lambda: {
            "solidworks": {
                "registered": True,
                "executable": r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe",
                "source": "uninstall-registry",
                "version": "2026",
                "servicePack": "SP01.1",
                "displayName": "SOLIDWORKS 2026 SP01.1",
                "shortcut": None,
            }
        },
    )
    monkeypatch.setattr(
        cad_doctor, "discover_installations", lambda _product, **_kw: [{"executable": "a"}, {"executable": "b"}]
    )

    info = cad_doctor._solidworks_installation()

    assert info["executables"] == [r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe"]
    assert info["available"] == [{"executable": "a"}, {"executable": "b"}]
    assert info["registered"] is True
    assert info["servicePack"] == "SP01.1"

    monkeypatch.setattr(
        cad_doctor,
        "discover_all",
        lambda: {
            "solidworks": {
                "registered": False,
                "executable": None,
                "source": None,
                "version": None,
                "servicePack": None,
                "displayName": None,
                "shortcut": None,
            }
        },
    )
    info = cad_doctor._solidworks_installation()
    assert info["executables"] == []
    assert info["executable"] is None


def test_run_doctor_reports_pass_when_environment_complete(monkeypatch):
    _patch_doctor_inputs(
        monkeypatch,
        find_spec=object(),
        which="C:/fake/cli.exe",
        solver_status="pass",
        sw_registered=True,
        autocad_installed=True,
        writable=True,
    )

    result = cad_doctor.run_doctor()

    assert result["schemaVersion"] == "1.0"
    assert result["summary"] == {"status": "passed", "errors": 0, "warnings": 0}
    assert result["remediations"] == []
    codes = {item["id"]: item["code"] for item in result["checks"]}
    assert codes["python"] == "PYTHON_READY"
    assert codes["python.pywin32"] == "DEPENDENCY_READY"
    assert codes["python.comtypes"] == "DEPENDENCY_READY"
    assert codes["python.ezdxf"] == "DEPENDENCY_READY"
    assert codes["python.ocp"] == "DEPENDENCY_READY"
    assert codes["python.pymupdf"] == "DEPENDENCY_READY"
    assert codes["solver.calculix"] == "CALCULIX_READY"
    assert codes["agent.codex"] == "AGENT_READY"
    assert codes["agent.opencode"] == "AGENT_READY"
    assert codes["cad.solidworks"] == "SOLIDWORKS_READY"
    assert codes["cad.autocad"] == "AUTOCAD_READY"
    assert codes["filesystem.documents"] == "DOCUMENTS_READY"
    assert codes["filesystem.temp"] == "TEMP_READY"


def test_run_doctor_reports_warnings_when_optional_stack_missing(monkeypatch):
    """@brief 可选栈全部缺失时总结为 warning，并给出全部修复动作。"""
    _patch_doctor_inputs(
        monkeypatch,
        find_spec="missing",
        which=None,
        solver_status="blocked",
        sw_registered=False,
        sw_executables=(),
        autocad_installed=False,
        writable=True,
    )

    result = cad_doctor.run_doctor(probe_cad=True)

    assert result["summary"]["status"] == "warning"
    assert result["summary"]["errors"] == 0
    checks = {item["id"]: item for item in result["checks"]}
    assert checks["python.pywin32"]["status"] == "warning"
    assert "未安装" in checks["python.pywin32"]["message"]
    assert "开放格式仍可使用" in checks["cad.solidworks"]["message"]
    assert "DXF/SVG/PDF/PNG" in checks["cad.autocad"]["message"]
    # probe_cad=True 但 COM 未注册 → 不触发真实连接
    assert all(item["id"] != "cad.solidworks.com" for item in result["checks"])
    remediation_ids = {item["id"] for item in result["remediations"]}
    assert "agent.codex" in remediation_ids
    assert "cad.solidworks" in remediation_ids
    assert "cad.autocad" in remediation_ids


def test_run_doctor_handles_probe_exceptions_and_short_circuits(monkeypatch):
    """@brief find_spec 抛错按未安装处理；which/or 短路和 find_solver 异常都要覆盖。"""
    def fake_find_spec(name):
        if name == "OCP":
            raise ImportError("OCP 元数据损坏")
        if name == "fitz":
            return object()
        return None

    def fake_which(name):
        if name == "codex":
            return "C:/fake/codex.exe"
        if name == "claude.exe":
            return "C:/fake/claude.exe"
        return None

    _patch_doctor_inputs(
        monkeypatch,
        find_spec=fake_find_spec,
        which=fake_which,
        solver_status="raise",
        sw_registered=False,
        sw_executables=(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe",),
        autocad_installed=True,
        writable=True,
    )

    result = cad_doctor.run_doctor()

    checks = {item["id"]: item for item in result["checks"]}
    assert checks["python.ocp"]["status"] == "warning"
    assert checks["python.pywin32"]["status"] == "warning"
    assert checks["python.pymupdf"]["status"] == "passed"
    assert checks["solver.calculix"]["status"] == "warning"
    assert checks["solver.calculix"]["code"] == "CALCULIX_NOT_FOUND"
    assert checks["agent.codex"]["status"] == "passed"
    assert checks["agent.claude"]["status"] == "passed"
    assert checks["agent.gemini"]["status"] == "warning"
    assert checks["agent.opencode"]["status"] == "warning"
    # 已发现 codex → 其余 Agent CLI 不再提示安装
    assert "agent.gemini" not in {item["id"] for item in result["remediations"]}
    # COM 未注册但目录存在 → 视为可用
    assert checks["cad.solidworks"]["status"] == "passed"


def test_run_doctor_flags_unsupported_python(monkeypatch):
    """@brief Python 低于 3.10 时判 error；文档/临时目录不可写也要出现在修复清单。"""
    monkeypatch.setattr(cad_doctor.sys, "version_info", (3, 9, 0))
    _patch_doctor_inputs(
        monkeypatch,
        find_spec=object(),
        which="C:/fake/cli.exe",
        solver_status="pass",
        sw_registered=True,
        autocad_installed=True,
        writable=False,
    )

    result = cad_doctor.run_doctor()

    assert result["summary"] == {"status": "error", "errors": 3, "warnings": 0}
    python_check = next(item for item in result["checks"] if item["id"] == "python")
    assert python_check["status"] == "error"
    assert python_check["code"] == "PYTHON_VERSION_UNSUPPORTED"
    assert "低于推荐的 3.10" in python_check["message"]
    remediations = {item["id"]: item for item in result["remediations"]}
    assert set(remediations) == {"python", "filesystem.documents", "filesystem.temp"}
    assert remediations["python"]["required"] is True


def test_run_doctor_probe_cad_connects_when_registered(monkeypatch):
    """@brief COM 已注册且 --probe-cad 时尝试连接，并记录连接参数。"""
    _patch_doctor_inputs(monkeypatch, find_spec=object(), which="C:/fake/cli.exe", sw_registered=True)

    calls = {}

    def fake_connect(**kwargs):
        calls.update(kwargs)
        return object(), None

    monkeypatch.setattr(sw_connect, "connect_solidworks", fake_connect)

    result = cad_doctor.run_doctor(probe_cad=True)

    assert calls == {"wait_seconds": 8, "visible": False}
    com_check = next(item for item in result["checks"] if item["id"] == "cad.solidworks.com")
    assert com_check["status"] == "passed"


def test_main_writes_json_output_and_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cad_doctor, "run_doctor", lambda **_kw: {"summary": {"status": "warning"}, "checks": []})

    out = tmp_path / "nested" / "doctor.json"
    assert cad_doctor.main(["--output", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text)["summary"]["status"] == "warning"
    assert "warning" in capsys.readouterr().out

    assert cad_doctor.main([]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["status"] == "warning"

    monkeypatch.setattr(cad_doctor, "run_doctor", lambda **_kw: {"summary": {"status": "error"}, "checks": []})
    assert cad_doctor.main([]) == 1


def test_entrypoint_help(monkeypatch):
    """@brief 以 __main__ 方式执行脚本时 --help 直接退出且无副作用。"""
    monkeypatch.setattr(sys, "argv", ["cad_doctor.py", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "cad_doctor.py"), run_name="__main__")
    assert excinfo.value.code == 0
