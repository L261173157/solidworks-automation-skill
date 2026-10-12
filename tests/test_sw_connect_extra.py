"""sw_connect 全分支补充测试 (伪对象, 无真实 COM/SolidWorks)。

覆盖: 连接中枢各路径、启动互斥锁、模板查找、新建/打开/保存文档、
版本 ProgID 解析、COM 成员兼容读取与单位换算。
"""
from __future__ import annotations

import ctypes
import importlib
import json
import math
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts import sw_connect
from scripts.sw_connect import (
    SolidWorksConnectionError,
    SolidWorksDocumentOpenError,
    _ensure_parent_dir,
    _LaunchGuard,
    _prog_id_for_version,
    _read_active_document,
    _solidworks_process_running,
    _wait_until_ready,
    close_owned_solidworks,
    connect_solidworks,
    deg,
    ensure_default_templates,
    find_template,
    get_com_member,
    get_sw_version,
    new_document,
    normalize_doc_type,
    open_document,
    save_document,
)

from fakes.sw import new_fake_session


def _raise(message):
    """@brief 生成总是抛错的伪 COM 边界。"""

    def _inner(*_args, **_kwargs):
        raise RuntimeError(message)

    return _inner


class _ActiveSw:
    """@brief 已附着实例的最小应用桩 (属性式成员)。"""

    def __init__(self, model=None):
        self.RevisionNumber = "32.5.0"
        self._model = model

    @property
    def ActiveDoc(self):
        return self._model


def _patch_client(monkeypatch, *, get_active_object=None, dispatch=None):
    """@brief 替换 sw_connect 命名空间内的 win32com.client 引用。"""
    fake_client = types.SimpleNamespace(
        GetActiveObject=get_active_object or _raise("ROT 中无运行实例"),
        Dispatch=dispatch or _raise("COM 类工厂不可用"),
        dynamic=types.SimpleNamespace(
            DumbDispatch=_raise("open_document 路径不应触发 DumbDispatch")
        ),
    )
    monkeypatch.setattr(sw_connect, "win32com_client", fake_client)
    return fake_client


def _posix_os(monkeypatch, kill=None):
    """@brief 把 sw_connect 命名空间内的 os 换成 posix 视角桩。

    不替换全局 os 模块, 避免 pathlib 在 Windows 上实例化 PosixPath。
    """
    stub = types.SimpleNamespace(
        name="posix",
        open=os.open,
        write=os.write,
        close=os.close,
        getpid=os.getpid,
        O_CREAT=os.O_CREAT,
        O_EXCL=os.O_EXCL,
        O_WRONLY=os.O_WRONLY,
        kill=kill or (lambda pid, sig: None),
    )
    monkeypatch.setattr(sw_connect, "os", stub)
    return stub


# --- 连接错误类型与 COM 成员兼容读取 ---


def test_connection_error_carries_code_and_stage():
    """@brief 连接错误保留阶段与错误码。"""
    exc = SolidWorksConnectionError("SW_X", "launch", "失败")
    assert exc.code == "SW_X"
    assert exc.stage == "launch"
    assert "[SW_X] launch: 失败" in str(exc)


class _MemberRaiser:
    """@brief 成员调用即抛错的桩。"""

    def __init__(self, exc):
        self._exc = exc

    def m(self):
        raise self._exc


def test_get_com_member_returns_member_on_member_not_found():
    """@brief 成员不可调用错误返回成员本身而非抛错。"""
    obj = _MemberRaiser(RuntimeError("找不到成员 -2147352573"))
    result = get_com_member(obj, "m")
    assert callable(result)
    assert result.__self__ is obj


def test_get_com_member_reraises_other_errors():
    """@brief 其它异常原样上抛。"""
    with pytest.raises(RuntimeError, match="boom"):
        get_com_member(_MemberRaiser(RuntimeError("boom")), "m")


# --- 启动互斥锁 ---


def test_launch_guard_windows_mutex_lifecycle():
    """@brief Windows 命名互斥锁进入/退出配对。"""
    with _LaunchGuard("CADStudio.Test.Mutex.Lifecycle") as guard:
        assert guard.handle
    assert guard.handle is None


def test_launch_guard_mutex_creation_failure(monkeypatch):
    """@brief 互斥锁创建失败立即抛 SW_MUTEX。"""
    kernel32 = ctypes.windll.kernel32
    monkeypatch.setattr(kernel32, "CreateMutexW", lambda *args: 0)
    with pytest.raises(SolidWorksConnectionError) as excinfo:
        with _LaunchGuard("CADStudio.Test.Mutex.Create"):
            pass
    assert excinfo.value.code == "SW_MUTEX"


def test_launch_guard_wait_timeout(monkeypatch):
    """@brief 等待其它启动任务超时抛 SW_MUTEX_TIMEOUT 并释放句柄。"""
    kernel32 = ctypes.windll.kernel32
    closed = []
    monkeypatch.setattr(kernel32, "CreateMutexW", lambda *args: 0x1234)
    monkeypatch.setattr(kernel32, "WaitForSingleObject", lambda handle, ms: 0x5)
    monkeypatch.setattr(kernel32, "CloseHandle", lambda handle: closed.append(handle) or 1)
    with pytest.raises(SolidWorksConnectionError) as excinfo:
        with _LaunchGuard("CADStudio.Test.Mutex.Wait"):
            pass
    assert excinfo.value.code == "SW_MUTEX_TIMEOUT"
    assert closed == [0x1234]


def test_launch_guard_posix_lock_file(monkeypatch):
    """@brief 非 Windows 平台使用独占锁文件。"""
    _posix_os(monkeypatch)
    guard = _LaunchGuard()
    with guard:
        assert guard.path.is_file()
        assert guard._owns_file is True
    assert not guard.path.exists()


def test_launch_guard_posix_stale_lock_is_broken(monkeypatch):
    """@brief 持锁进程已死亡时自动清理陈旧锁文件。"""

    def _dead_pid(pid, _sig):
        raise ProcessLookupError(pid)

    _posix_os(monkeypatch, kill=_dead_pid)
    guard = _LaunchGuard()
    guard.path.write_text("999999999", encoding="ascii")
    try:
        with guard:
            assert guard._owns_file is True
    finally:
        guard.path.unlink(missing_ok=True)


def test_launch_guard_posix_busy_lock(monkeypatch):
    """@brief 持锁进程仍存活时抛 SW_MUTEX_BUSY。"""
    _posix_os(monkeypatch, kill=lambda pid, sig: None)
    guard = _LaunchGuard()
    guard.path.write_text(str(os.getpid()), encoding="ascii")
    try:
        with pytest.raises(SolidWorksConnectionError) as excinfo:
            with guard:
                pass
        assert excinfo.value.code == "SW_MUTEX_BUSY"
    finally:
        guard.path.unlink(missing_ok=True)


def test_launch_guard_exit_tolerates_missing_lock_file(monkeypatch):
    """@brief 退出时锁文件已被清理不报错。"""
    _posix_os(monkeypatch)
    guard = _LaunchGuard()
    with guard:
        guard.path.unlink()


def test_launch_guard_exit_is_idempotent(monkeypatch):
    """@brief 已释放所有权后重复退出为无操作。"""
    _posix_os(monkeypatch)
    guard = _LaunchGuard()
    with guard:
        assert guard.path.is_file()
    assert not guard.path.exists()
    guard.__exit__(None, None, None)  # 第二次退出不应有任何副作用
    assert guard._owns_file is False


# --- 进程探测 ---


def test_process_probe_skipped_off_windows(monkeypatch):
    """@brief 非 Windows 平台按未运行处理。"""
    _posix_os(monkeypatch)
    assert _solidworks_process_running() is False


def test_process_probe_detects_sldworks(monkeypatch):
    """@brief tasklist 输出包含 SLDWORKS.exe 视为运行中。"""

    def _fake_run(*_args, **_kwargs):
        return types.SimpleNamespace(stdout="sldworks.exe  1234 Console ... SLDWORKS.exe\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _solidworks_process_running() is True


def test_process_probe_failure_means_not_running(monkeypatch):
    """@brief 探测失败按未运行处理。"""

    def _boom(*_args, **_kwargs):
        raise OSError("tasklist 不可用")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _solidworks_process_running() is False


# --- 连接中枢 ---


def test_connect_attaches_running_instance(monkeypatch, capsys):
    """@brief GetActiveObject 成功时直接附着且不获得所有权。"""
    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    sw = _ActiveSw()
    _patch_client(monkeypatch, get_active_object=lambda prog_id: sw)
    result_sw, model = connect_solidworks(version=None)
    assert result_sw is sw
    assert model is None
    out = capsys.readouterr().out
    assert "已连接到运行中的 SolidWorks 实例" in out
    assert "当前没有打开的文档" in out


def test_connect_attaches_with_active_document(monkeypatch, capsys):
    """@brief 附着实例后读取活动文档的类型与标题。"""

    class _Model:
        def GetType(self):
            return 1

        def GetTitle(self):
            return "Part1"

    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    sw = _ActiveSw(_Model())
    _patch_client(monkeypatch, get_active_object=lambda prog_id: sw)
    _result_sw, model = connect_solidworks(version=None)
    assert model is not None
    out = capsys.readouterr().out
    assert "Part1" in out and "零件" in out


def test_connect_reports_unknown_document_type(monkeypatch, capsys):
    """@brief 未知文档类型码按未知显示。"""

    class _Model:
        def GetType(self):
            return 99

        def GetTitle(self):
            return "Mystery"

    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    sw = _ActiveSw(_Model())
    _patch_client(monkeypatch, get_active_object=lambda prog_id: sw)
    connect_solidworks(version=None)
    assert "未知" in capsys.readouterr().out


def test_connect_launches_new_instance(monkeypatch, capsys):
    """@brief Dispatch 新建实例并记录所有权元数据。"""
    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    created = {}

    class _DispatchedSw(_ActiveSw):
        def __init__(self):
            super().__init__()
            self.visible = None

        @property
        def Visible(self):
            return self.visible

        @Visible.setter
        def Visible(self, value):
            self.visible = value

    sw = _DispatchedSw()

    def _dispatch(prog_id):
        created["prog_id"] = prog_id
        return sw

    _patch_client(monkeypatch, get_active_object=_raise("ROT 中无实例"), dispatch=_dispatch)
    monkeypatch.setattr(sw_connect, "_solidworks_process_running", lambda: False)
    result_sw, model, metadata = connect_solidworks(version=2024, wait_seconds=1, return_metadata=True)
    assert result_sw is sw and model is None
    assert created["prog_id"] == "SldWorks.Application.32"
    assert sw.visible is True
    assert metadata == {
        "prog_id": "SldWorks.Application.32",
        "requested_version": 2024,
        "started_by_cad_studio": True,
    }
    assert "启动了新的 SolidWorks 实例" in capsys.readouterr().out


def test_connect_dispatch_attaches_without_ownership(monkeypatch, capsys):
    """@brief Dispatch 附着既有实例时不标记所有权。"""
    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    sw = _ActiveSw()
    _patch_client(monkeypatch, get_active_object=_raise("ROT 中无实例"), dispatch=lambda prog_id: sw)
    monkeypatch.setattr(sw_connect, "_solidworks_process_running", lambda: True)
    _result_sw, _model, metadata = connect_solidworks(version=None, return_metadata=True)
    assert metadata["started_by_cad_studio"] is False
    assert "已连接到运行中的 SolidWorks 实例" in capsys.readouterr().out


def test_connect_tolerates_visible_failure(monkeypatch):
    """@brief 设置窗口可见性失败不阻断连接。"""

    class _VisibleHater(_ActiveSw):
        @property
        def Visible(self):
            raise RuntimeError("无法设置可见性")

    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    sw = _VisibleHater()
    _patch_client(monkeypatch, get_active_object=_raise("x"), dispatch=lambda prog_id: sw)
    monkeypatch.setattr(sw_connect, "_solidworks_process_running", lambda: False)
    result_sw, _model = connect_solidworks(version=None, wait_seconds=1)
    assert result_sw is sw


def test_connect_launch_failure_wraps_error(monkeypatch):
    """@brief Dispatch 失败包装为 SW_LAUNCH_FAILED 并带附着错误。"""
    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    _patch_client(
        monkeypatch,
        get_active_object=_raise("ROT 里没有实例"),
        dispatch=_raise("COM 类工厂失败"),
    )
    with pytest.raises(SolidWorksConnectionError) as excinfo:
        connect_solidworks(version=None)
    assert excinfo.value.code == "SW_LAUNCH_FAILED"
    assert "ROT 里没有实例" in str(excinfo.value)
    assert "COM 类工厂失败" in str(excinfo.value)


def test_connect_timeout_closes_launched_instance(monkeypatch):
    """@brief 启动超时只关闭本次启动的实例。"""
    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    clock = {"now": 0.0}

    def _monotonic():
        clock["now"] += 0.2
        return clock["now"]

    monkeypatch.setattr(sw_connect.time, "monotonic", _monotonic)
    monkeypatch.setattr(sw_connect.time, "sleep", lambda _s: None)

    class _BusySw:
        def __init__(self):
            self.visible = None
            self.exit_calls = 0

        @property
        def RevisionNumber(self):
            raise RuntimeError("COM 未就绪")

        @property
        def Visible(self):
            return self.visible

        @Visible.setter
        def Visible(self, value):
            self.visible = value

        def ExitApp(self):
            self.exit_calls += 1
            return True

    sw = _BusySw()
    _patch_client(monkeypatch, get_active_object=_raise("x"), dispatch=lambda prog_id: sw)
    monkeypatch.setattr(sw_connect, "_solidworks_process_running", lambda: False)
    with pytest.raises(SolidWorksConnectionError) as excinfo:
        connect_solidworks(version=None, wait_seconds=1)
    assert excinfo.value.code == "SW_START_TIMEOUT"
    assert sw.exit_calls == 1
    assert sw.visible is True


def test_connect_without_instance_raises(monkeypatch):
    """@brief 未获得 COM 实例时抛 SW_NO_INSTANCE。"""
    monkeypatch.setattr(sw_connect, "ensure_solidworks_installed", lambda: None)
    _patch_client(monkeypatch, get_active_object=lambda prog_id: None)
    with pytest.raises(SolidWorksConnectionError) as excinfo:
        connect_solidworks(version=None)
    assert excinfo.value.code == "SW_NO_INSTANCE"


def test_flat_import_fallback_for_sw_connect(monkeypatch):
    """@brief 顶层导入走 sw_preflight 平铺回退分支。"""
    scripts_dir = Path(sw_connect.__file__).resolve().parent
    monkeypatch.syspath_prepend(str(scripts_dir))
    monkeypatch.delitem(sys.modules, "sw_connect", raising=False)
    flat = importlib.import_module("sw_connect")
    assert flat.__name__ == "sw_connect"
    assert flat.DOC_TYPE_MAP == sw_connect.DOC_TYPE_MAP


# --- 版本与工具函数 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PART", ("part", 1)),
        (".prt", ("part", 1)),
        (" sldasm ", ("assembly", 2)),
        ("Drw", ("drawing", 3)),
    ],
)
def test_normalize_doc_type_aliases(raw, expected):
    """@brief 文档类型别名/扩展名归一化。"""
    assert normalize_doc_type(raw) == expected


def test_normalize_doc_type_unknown_raises():
    """@brief 未知文档类型抛 ValueError。"""
    with pytest.raises(ValueError, match="未知文档类型"):
        normalize_doc_type("xyz")


def test_prog_id_none_uses_version_independent():
    """@brief 不指定版本使用版本无关 ProgID。"""
    assert _prog_id_for_version(None) == "SldWorks.Application"


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2010, "SldWorks.Application.18"), (2024, "SldWorks.Application.32"), (2035, "SldWorks.Application.43")],
)
def test_prog_id_for_year(year, expected):
    """@brief 年份映射到 ProgID 子版本号。"""
    assert _prog_id_for_version(year) == expected


@pytest.mark.parametrize("version", [2009, 2036, "abc", 24])
def test_prog_id_invalid_versions_raise(version):
    """@brief 越界或非年份输入抛 ValueError。"""
    with pytest.raises(ValueError):
        _prog_id_for_version(version)


def test_read_active_document_returns_none_on_error():
    """@brief 读取活动文档失败返回 None。"""

    class _BrokenActiveDocSw:
        @property
        def ActiveDoc(self):
            raise RuntimeError("COM 未就绪")

    assert _read_active_document(_BrokenActiveDocSw()) is None


def test_read_active_document_returns_model():
    """@brief 读取活动文档成功返回模型。"""
    model = object()
    assert _read_active_document(_ActiveSw(model)) is model


class _FlakyRevisionSw:
    """@brief 前 N 次 RevisionNumber 读取失败的桩。"""

    def __init__(self, failures):
        self._remaining = failures

    @property
    def RevisionNumber(self):
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("COM 未就绪")
        return "32.5.0"


def test_wait_until_ready_retries_then_succeeds(monkeypatch):
    """@brief COM 未就绪时按轮询重试直至成功。"""
    clock = {"now": 0.0}

    def _monotonic():
        clock["now"] += 0.01
        return clock["now"]

    monkeypatch.setattr(sw_connect.time, "monotonic", _monotonic)
    monkeypatch.setattr(sw_connect.time, "sleep", lambda _s: None)
    sw = _FlakyRevisionSw(failures=1)
    _wait_until_ready(sw, 5)
    assert sw._remaining == 0


def test_wait_until_ready_times_out(monkeypatch):
    """@brief 超时抛 SW_START_TIMEOUT 并携带最后错误。"""
    clock = {"now": 0.0}

    def _monotonic():
        clock["now"] += 0.2
        return clock["now"]

    monkeypatch.setattr(sw_connect.time, "monotonic", _monotonic)
    monkeypatch.setattr(sw_connect.time, "sleep", lambda _s: None)
    with pytest.raises(SolidWorksConnectionError) as excinfo:
        _wait_until_ready(_FlakyRevisionSw(failures=10**9), 5)
    assert excinfo.value.code == "SW_START_TIMEOUT"


class _ExitAppSw:
    def __init__(self):
        self.exited = False

    def ExitApp(self):
        self.exited = True
        return True


class _QuitOnlySw:
    def __init__(self):
        self.quit = False

    def Quit(self):
        self.quit = True


def test_close_owned_solidworks_skips_foreign_instance():
    """@brief 非本会话启动的实例不退出。"""
    sw = _ExitAppSw()
    assert close_owned_solidworks(sw, False) is False
    assert sw.exited is False


def test_close_owned_solidworks_uses_exitapp():
    """@brief 优先调用 ExitApp。"""
    sw = _ExitAppSw()
    assert close_owned_solidworks(sw, True) is True
    assert sw.exited is True


def test_close_owned_solidworks_falls_back_to_quit():
    """@brief 无 ExitApp 时回退 Quit。"""
    sw = _QuitOnlySw()
    assert close_owned_solidworks(sw, True) is True
    assert sw.quit is True


def test_close_owned_solidworks_without_exit_members():
    """@brief 两个退出成员都缺失时返回 False。"""
    assert close_owned_solidworks(object(), True) is False


def test_get_sw_version_parses_revision():
    """@brief 版本号解析为年份与主版本。"""

    class _RevisionSw:
        def RevisionNumber(self):
            return "33.0.5"

    assert get_sw_version(_RevisionSw()) == {"revision": "33.0.5", "year": 2025, "major": 33}


def test_degree_conversion():
    """@brief 角度转弧度。"""
    assert deg(180) == pytest.approx(math.pi)


def test_ensure_parent_dir_skips_empty_parent(monkeypatch):
    """@brief 无父目录时不调用 makedirs (abspath("") 约定为空)。"""
    real_abspath = os.path.abspath
    monkeypatch.setattr(sw_connect.os.path, "abspath", lambda p: real_abspath(p) if p else "")
    monkeypatch.setattr(sw_connect.os, "makedirs", _raise("空父目录不应创建目录"))
    _ensure_parent_dir("")


# --- 模板查找与默认模板 ---


class _PrefSw:
    """@brief 用户偏好读写的最小应用桩。"""

    def __init__(self, value):
        self._value = value
        self.set_calls = []
        self.toggle_calls = []

    def GetUserPreferenceStringValue(self, pref):
        return self._value

    def SetUserPreferenceStringValue(self, pref, value):
        self.set_calls.append((pref, value))

    def SetUserPreferenceToggle(self, pref, on):
        self.toggle_calls.append((pref, on))


def test_find_template_prefers_existing_file(tmp_path):
    """@brief 偏好路径直接指向模板文件时原样返回。"""
    template = tmp_path / "gb_part.prtdot"
    template.write_bytes(b"template")
    assert find_template(_PrefSw(str(template)), "part") == str(template)


def test_find_template_globs_template_directory(tmp_path):
    """@brief 偏好路径为目录时按模式匹配模板。"""
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "gb_assembly.asmdot").write_bytes(b"t")
    found = find_template(_PrefSw(str(template_dir)), "assembly")
    assert Path(found) == template_dir / "gb_assembly.asmdot"


def test_find_template_search_dirs_and_empty_tokens(monkeypatch, tmp_path):
    """@brief 空段与引号被剔除后回退到系统模板目录搜索。"""
    real_abspath = os.path.abspath
    monkeypatch.setattr(sw_connect.os.path, "abspath", lambda p: real_abspath(p) if p else "")
    marker = tmp_path / "from-search.prtdot"
    monkeypatch.setattr(sw_connect.glob, "glob", lambda pattern: [str(marker)])
    sw = _PrefSw(' ;; "C:/nonexistent" ')
    assert find_template(sw, "part") == str(marker)


def test_find_template_raises_when_nothing_found(monkeypatch):
    """@brief 全部搜索落空时抛 FileNotFoundError。"""
    monkeypatch.setattr(sw_connect.glob, "glob", lambda pattern: [])
    with pytest.raises(FileNotFoundError):
        find_template(_PrefSw(""), "part")


def test_find_template_skips_dirs_without_matches(monkeypatch, tmp_path):
    """@brief 目录无匹配模板时继续搜索系统目录。"""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(sw_connect.glob, "glob", lambda pattern: [])
    with pytest.raises(FileNotFoundError):
        find_template(_PrefSw(str(empty_dir)), "drawing")


def test_ensure_default_templates_writes_missing_prefs(monkeypatch):
    """@brief 缺省模板未配置时写入偏好并打开默认模板开关。"""
    monkeypatch.setattr(sw_connect, "find_template", lambda sw, doc_type: f"C:/fake/{doc_type}.prtdot")
    sw = _PrefSw("")
    templates = ensure_default_templates(sw)
    assert templates == {
        "part": "C:/fake/part.prtdot",
        "assembly": "C:/fake/assembly.prtdot",
        "drawing": "C:/fake/drawing.prtdot",
    }
    assert sorted(sw.set_calls) == [
        (8, "C:/fake/part.prtdot"),
        (9, "C:/fake/assembly.prtdot"),
        (10, "C:/fake/drawing.prtdot"),
    ]
    assert sw.toggle_calls == [(111, True)]


def test_ensure_default_templates_skips_matching_prefs(monkeypatch):
    """@brief 偏好与已确认模板一致时不重复写入。"""
    monkeypatch.setattr(sw_connect, "find_template", lambda sw, doc_type: f"C:/fake/{doc_type}.prtdot")

    class _MatchedPrefSw(_PrefSw):
        def GetUserPreferenceStringValue(self, pref):
            return {
                8: "C:/fake/part.prtdot",
                9: "C:/fake/assembly.prtdot",
                10: "C:/fake/drawing.prtdot",
            }[pref]

    sw = _MatchedPrefSw("")
    templates = ensure_default_templates(sw)
    assert sw.set_calls == []
    assert set(templates) == {"part", "assembly", "drawing"}


def test_ensure_default_templates_survives_toggle_failure(monkeypatch, capsys):
    """@brief 开关设置失败仅提示不中断。"""

    class _ToggleHater(_PrefSw):
        def SetUserPreferenceToggle(self, pref, on):
            raise RuntimeError("COM 拒绝写入")

    monkeypatch.setattr(sw_connect, "find_template", lambda sw, doc_type: f"C:/fake/{doc_type}.prtdot")
    sw = _ToggleHater("")
    templates = ensure_default_templates(sw)
    assert set(templates) == {"part", "assembly", "drawing"}
    assert "无法设置始终使用默认模板" in capsys.readouterr().out


# --- 新建文档 ---


def test_new_document_with_explicit_template():
    """@brief 显式模板路径先展开再创建。"""

    class _NewSw:
        def __init__(self):
            self.calls = []

        def NewDocument(self, template, paper, width, height):
            self.calls.append(template)
            return object()

    sw = _NewSw()
    model = new_document(sw, "part", template_path="~/templates/gb_part.SLDPRT")
    assert model is not None
    assert sw.calls[0].endswith("gb_part.SLDPRT")


def test_new_document_finds_template_when_missing(monkeypatch):
    """@brief 未提供模板时自动查找。"""
    monkeypatch.setattr(sw_connect, "find_template", lambda sw, doc_type: "C:/fake/found.prtdot")
    model = object()
    captured = {}

    class _NewSw:
        def NewDocument(self, template, paper, width, height):
            captured["template"] = template
            return model

    sw = _NewSw()
    assert new_document(sw, "drawing") is model
    assert captured["template"] == "C:/fake/found.prtdot"


def test_new_document_waits_for_active_doc(monkeypatch):
    """@brief NewDocument 返回空时轮询活动文档。"""
    monkeypatch.setattr(sw_connect.time, "sleep", lambda _s: None)
    model = object()

    class _LateSw:
        def __init__(self):
            self._tries = 0

        def NewDocument(self, *_args):
            return None

        @property
        def ActiveDoc(self):
            self._tries += 1
            return model if self._tries >= 2 else None

    assert new_document(_LateSw(), "part", template_path="C:/t/gb_part.prtdot") is model


def test_new_document_raises_when_active_doc_never_appears(monkeypatch):
    """@brief 轮询耗尽仍无活动文档时抛错。"""
    monkeypatch.setattr(sw_connect.time, "sleep", lambda _s: None)

    class _DeadSw:
        def NewDocument(self, *_args):
            return None

        ActiveDoc = None

    with pytest.raises(RuntimeError, match="创建零件文档失败"):
        new_document(_DeadSw(), "part", template_path="C:/t/gb_part.prtdot")


# --- 打开文档 ---


def test_open_document_missing_file_raises(tmp_path):
    """@brief 文件不存在且要求抛错时抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="文件不存在"):
        open_document(object(), str(tmp_path / "missing.sldprt"), raise_on_error=True)


def test_open_document_missing_file_returns_none(capsys):
    """@brief 文件不存在且未要求抛错时打印并返回 None。"""
    assert open_document(object(), "C:/definitely-missing/x.sldprt") is None
    assert "文件不存在" in capsys.readouterr().out


def test_open_document_survives_get_open_document_failure(tmp_path):
    """@brief 复用查询抛错时继续正常打开。"""
    source = tmp_path / "part.sldprt"
    source.write_bytes(b"part")
    expected = object()

    class _FaultyReuseSw:
        def GetOpenDocumentByName(self, _path):
            raise RuntimeError("COM 翻车")

        def OpenDoc6(self, _path, _dt, _opt, _cfg, errors, warnings):
            errors.value = 0
            warnings.value = 0
            return expected

    assert open_document(_FaultyReuseSw(), source) is expected


def test_open_document_warns_foreign_options_and_loads(tmp_path, capsys, monkeypatch):
    """@brief 外来格式忽略 read_only/silent 并给出提示。"""
    monkeypatch.setattr(sw_connect, "ensure_default_templates", lambda _sw: True)
    source = tmp_path / "model.step"
    source.write_bytes(b"ISO-10303-21")
    expected = object()

    class _StepSw:
        def __init__(self):
            self.load_args = None

        def GetOpenDocumentByName(self, _path):
            return None

        def GetImportFileData(self, _path):
            return "import-data"

        def LoadFile4(self, path, argument, data, errors):
            self.load_args = (path, argument, data)
            errors.value = 0
            return expected

    sw = _StepSw()
    assert open_document(sw, source, read_only=True, silent=True) is expected
    out = capsys.readouterr().out
    assert "read_only" in out and "silent" in out
    assert sw.load_args[1] == "r"


def _no_import_sw():
    class _NoImportSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def GetImportFileData(self, _path):
            return None

    return _NoImportSw()


def test_open_document_import_data_failure_raises(tmp_path, monkeypatch):
    """@brief 导入数据获取失败且要求抛错时抛 RuntimeError。"""
    monkeypatch.setattr(sw_connect, "ensure_default_templates", lambda _sw: True)
    source = tmp_path / "model.igs"
    source.write_bytes(b"IGES")
    with pytest.raises(RuntimeError, match="获取导入数据失败"):
        open_document(_no_import_sw(), source, raise_on_error=True)


def test_open_document_import_data_failure_prints(tmp_path, capsys, monkeypatch):
    """@brief 导入数据获取失败未要求抛错时打印并返回 None。"""
    monkeypatch.setattr(sw_connect, "ensure_default_templates", lambda _sw: True)
    source = tmp_path / "model.igs"
    source.write_bytes(b"IGES")
    assert open_document(_no_import_sw(), source) is None
    assert "获取导入数据失败" in capsys.readouterr().out


def test_open_document_loadfile4_type_error_without_oleobj(tmp_path, monkeypatch):
    """@brief 无 _oleobj_ 时 LoadFile4 的 TypeError 原样上抛。"""
    monkeypatch.setattr(sw_connect, "ensure_default_templates", lambda _sw: True)
    source = tmp_path / "model.stp"
    source.write_bytes(b"ISO-10303-21")

    class _NoOleSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def GetImportFileData(self, _path):
            return "data"

        def LoadFile4(self, *_args):
            raise TypeError("int() argument must not be VARIANT")

    with pytest.raises(TypeError):
        open_document(_NoOleSw(), source)


def test_open_document_opendoc6_type_error_without_oleobj(tmp_path):
    """@brief 无 _oleobj_ 时 OpenDoc6 的 TypeError 原样上抛。"""
    source = tmp_path / "part.sldprt"
    source.write_bytes(b"part")

    class _NoOleSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def OpenDoc6(self, *_args):
            raise TypeError("int() argument must not be VARIANT")

    with pytest.raises(TypeError):
        open_document(_NoOleSw(), source)


def test_open_document_tuple_result_sets_codes(tmp_path):
    """@brief OpenDoc6 返回元组时提取模型与错误/警告码。"""
    source = tmp_path / "part.sldprt"
    source.write_bytes(b"part")
    expected = object()

    class _TupleSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def OpenDoc6(self, _path, _dt, options, _cfg, _errors, _warnings):
            assert options == 3  # read_only(2) + silent(1)
            return (expected, 2, 4)

    assert open_document(_TupleSw(), source, read_only=True, silent=True) is expected


def test_open_document_tuple_with_none_codes(tmp_path):
    """@brief 元组中空错误/警告码不回写出参。"""
    source = tmp_path / "part.sldprt"
    source.write_bytes(b"part")
    expected = object()

    class _TupleSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def OpenDoc6(self, _path, _dt, _opt, _cfg, _errors, _warnings):
            return (expected, None, None)

    assert open_document(_TupleSw(), source) is expected


def test_open_document_empty_tuple_fails_without_raise(tmp_path, capsys):
    """@brief 空元组结果按失败处理并打印分类错误。"""
    source = tmp_path / "part.sldprt"
    source.write_bytes(b"part")

    class _TupleSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def OpenDoc6(self, _path, _dt, _opt, _cfg, errors, warnings):
            errors.value = 2
            warnings.value = 8
            return ()

    assert open_document(_TupleSw(), source) is None
    out = capsys.readouterr().out
    assert "打开失败" in out and "swFileNotFoundError" in out and "swReadOnlyWarn" in out


def test_open_document_failure_raises_document_error(tmp_path):
    """@brief 打开失败且要求抛错时抛 SolidWorksDocumentOpenError。"""
    source = tmp_path / "missing-part.sldprt"
    source.write_bytes(b"part")

    class _FailSw:
        def GetOpenDocumentByName(self, _path):
            return None

        def OpenDoc6(self, _path, _dt, _opt, _cfg, errors, warnings):
            errors.value = 2
            warnings.value = 0
            return None

    with pytest.raises(SolidWorksDocumentOpenError) as excinfo:
        open_document(_FailSw(), source, raise_on_error=True)
    assert excinfo.value.error_code == 2
    assert "swFileNotFoundError" in str(excinfo.value)


# --- 保存文档 ---


def test_save_document_in_place_uses_save3():
    """@brief 不指定路径时走 Save3 原地保存。"""
    app, _journal = new_fake_session()
    model = app.NewDocument("C:/fake/templates/gb_part.prtdot", 0, 0, 0)
    assert save_document(model) is True


def test_save_document_in_place_failure_prints_codes(capsys):
    """@brief 原地保存失败打印错误/警告码。"""

    class _FailingSave:
        GetPathName = "C:/doc/part.SLDPRT"

        def Save3(self, options, errors, warnings):
            errors.value = 1
            warnings.value = 2
            return False

    assert save_document(_FailingSave()) is False
    assert "保存失败" in capsys.readouterr().out


def test_backup_manifest_rebuilds_from_non_list(tmp_path):
    """@brief 清单内容不是列表时重建为空列表再登记。"""
    app, _journal = new_fake_session()
    model = app.NewDocument("C:/fake/templates/gb_part.prtdot", 0, 0, 0)
    target = tmp_path / "doc.SLDPRT"
    target.write_bytes(b"OLD")
    backup_dir = tmp_path / ".cadstudio_backups"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text('{"not": "a list"}', encoding="utf-8")

    assert save_document(model, str(target), overwrite=True, backup=True) is True
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest, list) and len(manifest) == 1
    assert manifest[0]["original"] == str(target)


def test_backup_manifest_appends_to_existing_list(tmp_path):
    """@brief 合法清单被追加而非重建。"""
    app, _journal = new_fake_session()
    model = app.NewDocument("C:/fake/templates/gb_part.prtdot", 0, 0, 0)
    target = tmp_path / "doc.SLDPRT"
    target.write_bytes(b"OLD")
    backup_dir = tmp_path / ".cadstudio_backups"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text("[]", encoding="utf-8")

    assert save_document(model, str(target), overwrite=True, backup=True) is True
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 1
    assert manifest[0]["backup"].startswith(str(backup_dir))
