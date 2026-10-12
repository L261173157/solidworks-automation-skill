"""sw_preflight 运行前自检回归测试。

交互式 input() 一律通过参数注入或 monkeypatch 替换，测试永不等待用户输入；
__main__ 入口通过 runpy 在进程内执行，副作用全部 mock。
"""
from __future__ import annotations

import importlib
import runpy
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cad_installation  # noqa: E402
import sw_preflight  # noqa: E402


def _load_packaged_sw_preflight():
    """@brief 以包内相对导入方式再加载一次，覆盖模块顶部相对导入成功的分支。"""
    package_name = "sw_preflight_fakepkg"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(SCRIPTS)]
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.sw_preflight")


PACKAGED_SW_PREFLIGHT = _load_packaged_sw_preflight()


def test_relative_import_branch_is_executed():
    """@brief 顶层导入（相对导入失败转绝对导入）与包内导入都能拿到同一份实现。"""
    assert sw_preflight.run_preflight is not None
    assert PACKAGED_SW_PREFLIGHT.run_preflight is not None
    assert PACKAGED_SW_PREFLIGHT.__package__ == "sw_preflight_fakepkg"


def test_module_available_paths(monkeypatch):
    """@brief 可导入返回 True；自身缺失返回 False；无关子模块缺失继续抛出。"""
    assert sw_preflight._module_available("json") is True
    assert sw_preflight._module_available("no_such_module_xyz_123") is False

    real_import_module = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "pkg.sub":
            raise ModuleNotFoundError("No module named 'unrelated'", name="unrelated")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(sw_preflight.importlib, "import_module", fake_import)
    with pytest.raises(ModuleNotFoundError):
        sw_preflight._module_available("pkg.sub")

    def fake_import_root(name, *args, **kwargs):
        if name == "pkg.sub":
            raise ModuleNotFoundError("No module named 'pkg'", name="pkg")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(sw_preflight.importlib, "import_module", fake_import_root)
    assert sw_preflight._module_available("pkg.sub") is False


def test_missing_com_dependencies_deduplicates_requirement(monkeypatch):
    """@brief 当前环境依赖齐全；缺失时同一 requirement 只提示一次。"""
    assert sw_preflight.missing_com_dependencies() == []

    availability = {"comtypes": True, "pythoncom": False, "win32com.client": False}
    monkeypatch.setattr(sw_preflight, "_module_available", lambda name: availability[name])
    assert sw_preflight.missing_com_dependencies() == ["pywin32>=305"]

    monkeypatch.setattr(sw_preflight, "_module_available", lambda _name: False)
    assert sw_preflight.missing_com_dependencies() == ["comtypes>=1.2.0", "pywin32>=305"]


def test_pip_install_command_and_shell_text():
    """@brief pip 命令可注入解释器；含特殊字符的参数自动加引号。"""
    assert sw_preflight.pip_install_command() == [
        sys.executable,
        "-m",
        "pip",
        "install",
        *sw_preflight.PIP_REQUIREMENTS,
    ]
    assert sw_preflight.pip_install_command("py.exe", ["pkg>=1"]) == ["py.exe", "-m", "pip", "install", "pkg>=1"]

    assert sw_preflight.shell_command_text(["python.exe", "-m", "pip", "install", "pywin32>=305"]) == (
        "python.exe -m pip install \"pywin32>=305\""
    )
    assert sw_preflight.shell_command_text(['say "hi"', "plain"]) == ('"say \\"hi\\"" plain')


def test_install_com_dependencies_runs_pip(monkeypatch):
    recorded = []
    monkeypatch.setattr(sw_preflight.subprocess, "check_call", lambda command: recorded.append(command))

    sw_preflight.install_com_dependencies("py.exe", ["pkg>=1"])
    assert recorded == [["py.exe", "-m", "pip", "install", "pkg>=1"]]

    sw_preflight.install_com_dependencies()
    assert recorded[-1] == [sys.executable, "-m", "pip", "install", *sw_preflight.PIP_REQUIREMENTS]


def test_ask_install_permission_answers():
    """@brief Y/yes 授权、其他拒绝、EOF 视为拒绝，且提示语来自常量。"""
    prompts = []

    assert sw_preflight.ask_install_permission(input_func=lambda _p: " Y ", output_func=prompts.append) is True
    assert sw_preflight.ask_install_permission(input_func=lambda _p: "no", output_func=prompts.append) is False

    def refusing(_prompt):
        raise EOFError

    assert sw_preflight.ask_install_permission(input_func=refusing, output_func=prompts.append) is False
    assert prompts and prompts[0] == sw_preflight.DEPENDENCY_PROMPT


def test_ensure_com_dependencies_matrix(monkeypatch):
    """@brief 无缺失直通；授权安装后清零；仍缺失抛错；拒绝安装给出可复制命令。"""
    # 1) 无缺失 → 直接返回 []
    monkeypatch.setattr(sw_preflight, "missing_com_dependencies", lambda: [])
    assert sw_preflight.ensure_com_dependencies(allow_install=True) == []

    # 2) 允许安装 → 按缺失列表安装并返回初始缺失
    states = [["pywin32>=305"], []]
    installed = []
    monkeypatch.setattr(sw_preflight, "missing_com_dependencies", lambda: states.pop(0))
    monkeypatch.setattr(
        sw_preflight, "install_com_dependencies", lambda *, requirements: installed.append(requirements)
    )
    assert sw_preflight.ensure_com_dependencies(allow_install=True) == ["pywin32>=305"]
    assert installed == [("pywin32>=305",)]

    # 3) 安装后仍缺失 → ModuleNotFoundError
    monkeypatch.setattr(sw_preflight, "missing_com_dependencies", lambda: ["pywin32>=305"])
    monkeypatch.setattr(sw_preflight, "install_com_dependencies", lambda *, requirements: None)
    with pytest.raises(ModuleNotFoundError, match="pywin32"):
        sw_preflight.ensure_com_dependencies(allow_install=True)

    # 4) 拒绝安装 → DependencyInstallDeclined，消息包含可执行的命令文本
    monkeypatch.setattr(sw_preflight, "missing_com_dependencies", lambda: ["comtypes>=1.2.0"])
    with pytest.raises(sw_preflight.DependencyInstallDeclined, match="pip install"):
        sw_preflight.ensure_com_dependencies(allow_install=False)

    # 5) 交互模式：询问函数同意 → 安装并清零；拒绝 → 抛 declined
    states2 = [["comtypes>=1.2.0"], []]
    monkeypatch.setattr(sw_preflight, "missing_com_dependencies", lambda: states2.pop(0))
    monkeypatch.setattr(sw_preflight, "ask_install_permission", lambda *, input_func, output_func: True)
    monkeypatch.setattr(sw_preflight, "install_com_dependencies", lambda *, requirements: None)
    assert sw_preflight.ensure_com_dependencies() == ["comtypes>=1.2.0"]

    monkeypatch.setattr(sw_preflight, "missing_com_dependencies", lambda: ["comtypes>=1.2.0"])
    monkeypatch.setattr(sw_preflight, "ask_install_permission", lambda *, input_func, output_func: False)
    with pytest.raises(sw_preflight.DependencyInstallDeclined):
        sw_preflight.ensure_com_dependencies()


def test_import_com_dependencies_uses_lazy_import(monkeypatch):
    """@brief 依赖就绪后按序导入 pythoncom 与 win32com.client 并返回 VARIANT。"""
    monkeypatch.setattr(sw_preflight, "ensure_com_dependencies", lambda *, allow_install: None)

    fake_variant = object()
    fake_pythoncom = types.SimpleNamespace(CoInitialize=lambda: None)
    fake_client = types.SimpleNamespace(VARIANT=fake_variant)

    def fake_import(name):
        return {"pythoncom": fake_pythoncom, "win32com.client": fake_client}[name]

    monkeypatch.setattr(sw_preflight.importlib, "import_module", fake_import)

    pythoncom, client, variant = sw_preflight.import_com_dependencies(allow_install=True)

    assert pythoncom is fake_pythoncom
    assert client is fake_client
    assert variant is fake_variant


def test_is_windows_branches(monkeypatch):
    """@brief os.name 为 nt 直接判定；否则回退到 platform.system 前缀判断。"""
    monkeypatch.setattr(sw_preflight.os, "name", "nt")
    assert sw_preflight._is_windows() is True

    monkeypatch.setattr(sw_preflight.os, "name", "posix")
    monkeypatch.setattr(sw_preflight.platform, "system", lambda: "Linux")
    assert sw_preflight._is_windows() is False

    monkeypatch.setattr(sw_preflight.platform, "system", lambda: "Wine")
    assert sw_preflight._is_windows() is True


def test_solidworks_detection_helpers(monkeypatch):
    """@brief COM 注册与可执行文件候选都来自 discover_installation 结果。"""
    monkeypatch.setattr(sw_preflight, "discover_installation", lambda _p, **_k: {"registered": 1})
    assert sw_preflight._solidworks_com_registered() is True
    monkeypatch.setattr(sw_preflight, "discover_installation", lambda _p, **_k: {"registered": 0})
    assert sw_preflight._solidworks_com_registered() is False

    monkeypatch.setattr(
        sw_preflight, "discover_installation", lambda _p, **_k: {"executable": r"E:\SW\SLDWORKS.exe"}
    )
    assert list(sw_preflight._solidworks_exe_candidates()) == [r"E:\SW\SLDWORKS.exe"]
    monkeypatch.setattr(sw_preflight, "discover_installation", lambda _p, **_k: {"executable": None})
    assert list(sw_preflight._solidworks_exe_candidates()) == []


def test_solidworks_installed_matrix(monkeypatch, tmp_path):
    """@brief 非 Windows 直接 False；COM 注册或 exe 真实存在都算已安装。"""
    monkeypatch.setattr(sw_preflight, "_is_windows", lambda: False)
    assert sw_preflight.solidworks_installed() is False

    monkeypatch.setattr(sw_preflight, "_is_windows", lambda: True)
    monkeypatch.setattr(
        sw_preflight, "discover_installation", lambda _p, **_k: {"registered": True, "executable": None}
    )
    assert sw_preflight.solidworks_installed() is True

    real_exe = tmp_path / "SLDWORKS.exe"
    real_exe.write_bytes(b"MZ")
    monkeypatch.setattr(
        sw_preflight,
        "discover_installation",
        lambda _p, **_k: {"registered": False, "executable": str(real_exe)},
    )
    assert sw_preflight.solidworks_installed() is True

    monkeypatch.setattr(
        sw_preflight,
        "discover_installation",
        lambda _p, **_k: {"registered": False, "executable": str(tmp_path / "missing.exe")},
    )
    assert sw_preflight.solidworks_installed() is False


def test_ensure_solidworks_installed_raises_clear_error(monkeypatch):
    monkeypatch.setattr(sw_preflight, "solidworks_installed", lambda: True)
    assert sw_preflight.ensure_solidworks_installed() is None

    monkeypatch.setattr(sw_preflight, "solidworks_installed", lambda: False)
    with pytest.raises(sw_preflight.SolidWorksNotInstalledError, match="未检测到 SolidWorks"):
        sw_preflight.ensure_solidworks_installed()


def test_run_preflight_combines_checks(monkeypatch):
    """@brief SolidWorks 检测可跳过；依赖检查结果进入 PreflightResult。"""
    calls = []
    monkeypatch.setattr(sw_preflight, "ensure_solidworks_installed", lambda: calls.append("sw"))

    def fake_ensure_deps(*, allow_install=None, **_kwargs):
        return ["pywin32>=305"] if allow_install is None else []

    monkeypatch.setattr(sw_preflight, "ensure_com_dependencies", fake_ensure_deps)

    result = sw_preflight.run_preflight()
    assert calls == ["sw"]
    assert result.missing_packages == ["pywin32>=305"]
    assert result.dependencies_ready is True
    assert result.solidworks_ready is True

    result = sw_preflight.run_preflight(allow_install=False, check_solidworks=False)
    assert calls == ["sw"]
    assert result.missing_packages == []
    assert result.solidworks_ready is None


def test_main_reports_success_and_installed_packages(monkeypatch, capsys):
    """@brief --yes/--no-install/--no-solidworks-check 的组合映射与输出内容。"""
    recorded = []
    missing_states = [["pywin32>=305"], [], [], []]

    def fake_run_preflight(*, allow_install=None, check_solidworks=True):
        recorded.append((allow_install, check_solidworks))
        return sw_preflight.PreflightResult(
            missing_packages=missing_states.pop(0),
            dependencies_ready=True,
            solidworks_ready=True if check_solidworks else None,
        )

    monkeypatch.setattr(sw_preflight, "run_preflight", fake_run_preflight)

    monkeypatch.setattr(sys, "argv", ["sw_preflight.py"])
    assert sw_preflight.main() == 0
    assert recorded[-1] == (None, True)
    output = capsys.readouterr().out
    assert "自检通过" in output
    assert "已补齐依赖: pywin32>=305" in output

    monkeypatch.setattr(sys, "argv", ["sw_preflight.py", "--no-solidworks-check"])
    assert sw_preflight.main() == 0
    assert recorded[-1] == (None, False)
    output = capsys.readouterr().out
    assert "已补齐依赖" not in output

    monkeypatch.setattr(sys, "argv", ["sw_preflight.py", "--yes"])
    assert sw_preflight.main() == 0
    assert recorded[-1] == (True, True)

    monkeypatch.setattr(sys, "argv", ["sw_preflight.py", "--no-install"])
    assert sw_preflight.main() == 0
    assert recorded[-1] == (False, True)


def _run_script_as_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", list(argv))
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPTS / "sw_preflight.py"), run_name="__main__")
    return excinfo.value.code


def test_entrypoint_help(monkeypatch):
    """@brief __main__ 方式执行时 --help 无副作用退出。"""
    assert _run_script_as_main(monkeypatch, ["sw_preflight.py", "--help"]) == 0


def test_entrypoint_exits_2_when_install_declined(monkeypatch, capsys):
    """@brief 缺依赖且未授权时，__main__ 打印命令提示并以退出码 2 结束。"""
    real_import_module = importlib.import_module

    def block_comtypes(name, *args, **kwargs):
        if name == "comtypes":
            raise ModuleNotFoundError("No module named 'comtypes'", name="comtypes")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", block_comtypes)
    monkeypatch.setattr(
        cad_installation,
        "discover_installation",
        lambda product, **_kw: {"registered": True, "executable": r"E:\Fake\SLDWORKS.exe"},
    )

    assert _run_script_as_main(monkeypatch, ["sw_preflight.py", "--no-install"]) == 2
    assert "pip install" in capsys.readouterr().err


def test_entrypoint_exits_3_when_solidworks_missing(monkeypatch, capsys):
    """@brief 未检测到 SolidWorks 时 __main__ 打印提示并以退出码 3 结束。"""
    monkeypatch.setattr(
        cad_installation,
        "discover_installation",
        lambda product, **_kw: {"registered": False, "executable": None},
    )

    assert _run_script_as_main(monkeypatch, ["sw_preflight.py"]) == 3
    assert "未检测到 SolidWorks" in capsys.readouterr().err


def test_configure_stdio_utf8_handles_streams_without_reconfigure(monkeypatch):
    """@brief 旧代码页下尝试重配置；没有 reconfigure 能力的流直接跳过。"""

    class RecordingStream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    class PlainStream:
        pass

    recording = RecordingStream()
    monkeypatch.setattr(sw_preflight.sys, "stdout", recording)
    monkeypatch.setattr(sw_preflight.sys, "stderr", PlainStream())

    sw_preflight._configure_stdio_utf8()

    assert recording.calls == [{"encoding": "utf-8", "errors": "replace"}]
