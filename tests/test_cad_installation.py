"""cad_installation 本机 CAD 安装发现回归测试。

注册表读取全部通过伪造的 winreg 模块完成，绝不触碰真实注册表；
文件系统探测通过注入 exists 回调或 tmp_path 内的真实临时文件完成。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cad_installation  # noqa: E402
from cad_installation import (  # noqa: E402
    _common_candidates,
    _registry_paths,
    _shortcut_info,
    _shortcut_paths,
    _unique_paths,
    _uninstall_registry_candidates,
    _version_metadata,
    discover_all,
    discover_installation,
    discover_installations,
    resolve_shortcut_target,
)


class FakeRegKey:
    """@brief 模拟注册表键：values 为值表，children 为子键表，可注入失败。"""

    def __init__(self, values=None, children=None, *, fail_open=False, fail_query_info=False):
        self.values = dict(values or {})
        self.children = dict(children or {})
        self.fail_open = fail_open
        self.fail_query_info = fail_query_info
        self.closed = False


def make_fake_winreg(roots, query_value=None):
    """@brief 构造可注入 sys.modules 的 winreg 伪模块。roots 以 (hive, path) 为键。"""
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = "HKLM"
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.HKEY_CLASSES_ROOT = "HKCR"

    def open_key(hive, path):
        if isinstance(hive, FakeRegKey):
            entry = hive.children.get(path)
        else:
            entry = roots.get((hive, path))
        if entry is None or isinstance(entry, Exception) or entry.fail_open:
            raise OSError(f"无法打开 {path}")
        return entry

    def query_info_key(key):
        if key.fail_query_info:
            raise OSError("QueryInfoKey 失败")
        return (len(key.children), 0, 0)

    def enum_key(key, index):
        return list(key.children)[index]

    def query_value_ex(key, name):
        if name not in key.values:
            raise OSError(2, f"没有值 {name}")
        value = key.values[name]
        if isinstance(value, Exception):
            raise value
        return value, 1

    fake.OpenKey = open_key
    fake.CloseKey = lambda key: setattr(key, "closed", True)
    fake.QueryInfoKey = query_info_key
    fake.EnumKey = enum_key
    fake.QueryValueEx = query_value_ex
    fake.QueryValue = query_value or (lambda hive, path: "fake-clsid")
    return fake


def test_resolve_shortcut_target_prefers_matching_target_and_workdir():
    """@brief 目标即主程序时三个候选会按去重规则收敛为一个。"""
    candidates = resolve_shortcut_target(
        r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe", r"E:\Solidworks\SOLIDWORKS", "SLDWORKS.exe"
    )
    assert candidates == [Path(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe")]


def test_resolve_shortcut_target_recovers_from_installer_target():
    """@brief 安装器目标不是主程序时，必须从工作目录补出真正的 SLDWORKS.exe。"""
    candidates = resolve_shortcut_target(
        r"C:\WINDOWS\Installer\{demo}\i386_SldWorks.exe", r"E:\Solidworks\SOLIDWORKS", "SLDWORKS.exe"
    )
    assert candidates == [
        Path(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe"),
        Path(r"C:\WINDOWS\Installer\{demo}\SLDWORKS.exe"),
    ]


def test_resolve_shortcut_target_handles_missing_inputs():
    assert resolve_shortcut_target(None, None, "SLDWORKS.exe") == []
    assert resolve_shortcut_target(None, r"D:\AutoCAD 2024", "acad.exe") == [
        Path(r"D:\AutoCAD 2024\acad.exe")
    ]
    assert resolve_shortcut_target(r"D:\setup\launcher.exe", None, "acad.exe") == [
        Path(r"D:\setup\acad.exe")
    ]
    assert resolve_shortcut_target(r"D:\AutoCAD 2024\acad.exe", None, "acad.exe") == [
        Path(r"D:\AutoCAD 2024\acad.exe")
    ]


def test_unique_paths_normalizes_duplicates():
    """@brief normcase/normpath 归一后重复的候选只保留首次出现的那个。"""
    paths = [
        Path(r"C:\Tools\SLDWORKS.exe"),
        Path(r"c:\tools\SLDWORKS.exe"),
        Path(r"C:\Tools\..\Tools\SLDWORKS.exe"),
        Path(r"C:\Other\SLDWORKS.exe"),
    ]
    assert _unique_paths(paths) == [Path(r"C:\Tools\SLDWORKS.exe"), Path(r"C:\Other\SLDWORKS.exe")]


def test_shortcut_info_reads_target_and_workdir(monkeypatch, tmp_path):
    """@brief 能读取 .lnk 的目标和工作目录。"""
    import win32com.client as client_module

    lnk = tmp_path / "SOLIDWORKS 2026.lnk"
    lnk.write_bytes(b"lnk")

    class FakeShortcut:
        TargetPath = r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe"
        WorkingDirectory = r"E:\Solidworks\SOLIDWORKS"

    class FakeShell:
        def CreateShortcut(self, path):
            assert path == str(lnk)
            return FakeShortcut()

    monkeypatch.setattr(client_module, "Dispatch", lambda _name: FakeShell(), raising=False)
    assert _shortcut_info(lnk) == (r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe", r"E:\Solidworks\SOLIDWORKS")


def test_shortcut_info_defaults_missing_attributes_to_empty(monkeypatch, tmp_path):
    """@brief TargetPath/WorkingDirectory 为空时返回空字符串而不是 None。"""
    import win32com.client as client_module

    lnk = tmp_path / "plain.lnk"
    lnk.write_bytes(b"lnk")

    class FakeShortcut:
        TargetPath = None
        WorkingDirectory = None

    class FakeShell:
        def CreateShortcut(self, _path):
            return FakeShortcut()

    monkeypatch.setattr(client_module, "Dispatch", lambda _name: FakeShell(), raising=False)
    assert _shortcut_info(lnk) == ("", "")


def test_shortcut_info_fails_quietly(monkeypatch, tmp_path):
    """@brief 非文件、非 Windows、Dispatch 抛错时都必须安静返回 (None, None)。"""
    assert _shortcut_info(tmp_path / "missing.lnk") == (None, None)

    lnk = tmp_path / "real.lnk"
    lnk.write_bytes(b"lnk")

    monkeypatch.setattr("os.name", "posix")
    assert _shortcut_info(lnk) == (None, None)

    monkeypatch.setattr("os.name", "nt")
    import win32com.client as client_module

    def broken_dispatch(_name):
        raise RuntimeError("COM 不可用")

    monkeypatch.setattr(client_module, "Dispatch", broken_dispatch, raising=False)
    assert _shortcut_info(lnk) == (None, None)


def test_shortcut_paths_combines_fixed_entry_and_glob(monkeypatch, tmp_path):
    """@brief 除固定的 2024 快捷方式外，还要枚举公共桌面上任意版本的快捷方式。"""
    public = tmp_path / "public-desktop"
    public.mkdir()
    (public / "SOLIDWORKS 2026.lnk").write_bytes(b"lnk")
    (public / "AutoCAD 2025.lnk").write_bytes(b"lnk")
    monkeypatch.setattr(cad_installation, "PUBLIC_DESKTOP", public)

    sw_paths = _shortcut_paths("solidworks")
    assert cad_installation.SHORTCUTS["solidworks"] in sw_paths
    assert public / "SOLIDWORKS 2026.lnk" in sw_paths

    acad_paths = _shortcut_paths("autocad")
    assert cad_installation.SHORTCUTS["autocad"] in acad_paths
    assert public / "AutoCAD 2025.lnk" in acad_paths

    monkeypatch.setattr(cad_installation, "PUBLIC_DESKTOP", tmp_path / "missing")
    assert _shortcut_paths("solidworks") == [cad_installation.SHORTCUTS["solidworks"]]


def test_version_metadata_extracts_year_and_service_pack():
    assert _version_metadata("SOLIDWORKS 2024 SP03.1") == ("2024", "SP03.1")
    assert _version_metadata("SOLIDWORKS 2026 SP1") == ("2026", "SP01")
    assert _version_metadata("SP 5.2") == (None, "SP05.2")
    assert _version_metadata(None, "AutoCAD 2025 - 简体中文") == ("2025", None)
    assert _version_metadata("没有版本信息") == (None, None)
    assert _version_metadata("2024 SP3", "2025 备份") == ("2024", "SP03")


def test_uninstall_registry_candidates_solidworks(monkeypatch):
    """@brief 卸载注册表各异常分支都要安静跳过，只有匹配项进入结果。"""
    sw_root = FakeRegKey(
        children={
            "Broken": OSError("无法打开子键"),
            "NoDisplayName": FakeRegKey(),
            "EmptyDisplayName": FakeRegKey(values={"DisplayName": ""}),
            "DeltaV": FakeRegKey(values={"DisplayName": "DeltaV 12.3"}),
            "SOLIDWORKS 2024 SP03.1": FakeRegKey(
                values={
                    "DisplayName": "SOLIDWORKS 2024 SP03.1",
                    "InstallLocation": r"E:\Solidworks\SOLIDWORKS",
                }
            ),
            "SOLIDWORKS 2026 SP01.1": FakeRegKey(
                values={
                    "DisplayName": "SOLIDWORKS 2026 SP01.1",
                    "InstallLocation": OSError("拒绝访问"),
                }
            ),
        }
    )
    hkcu_root = FakeRegKey(fail_query_info=True)
    fake = make_fake_winreg(
        {
            ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"): FakeRegKey(fail_open=True),
            ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"): sw_root,
            ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"): hkcu_root,
        }
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)

    result = _uninstall_registry_candidates("solidworks")

    assert len(result) == 1
    item = result[0]
    assert item["path"] == Path(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe")
    assert item["source"] == "uninstall-registry"
    assert item["version"] == "2024"
    assert item["service_pack"] == "SP03.1"
    assert item["display_name"] == "SOLIDWORKS 2024 SP03.1"
    assert item["shortcut"] is None
    assert sw_root.closed is True
    assert hkcu_root.closed is True


def test_uninstall_registry_candidates_autocad(monkeypatch):
    """@brief AutoCAD 显示名带语言后缀时也要能匹配，并解析出 acad.exe。"""
    root = FakeRegKey(
        children={
            "AutoCAD 2024 - 简体中文 (Simplified Chinese)": FakeRegKey(
                values={
                    "DisplayName": "AutoCAD 2024 - 简体中文 (Simplified Chinese)",
                    "InstallLocation": r"D:\AutoCAD 2024",
                }
            ),
            "AutoCAD": FakeRegKey(values={"DisplayName": "AutoCAD", "InstallLocation": r"C:\Autodesk"}),
        }
    )
    fake = make_fake_winreg(
        {("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"): root}
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)

    result = _uninstall_registry_candidates("autocad")

    assert [str(item["path"]) for item in result] == [str(Path(r"D:\AutoCAD 2024\acad.exe"))]
    assert result[0]["version"] == "2024"
    assert result[0]["service_pack"] is None


def test_uninstall_registry_candidates_guard_branches(monkeypatch):
    """@brief 非 Windows 或缺少 winreg 时直接返回空列表。"""
    monkeypatch.setattr("os.name", "posix")
    assert _uninstall_registry_candidates("solidworks") == []

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert _uninstall_registry_candidates("autocad") == []


def test_registry_paths_solidworks(monkeypatch):
    """@brief 注册表值带 .exe 后缀时直接使用，否则补上 SLDWORKS.exe；递归有深度上限。"""
    deep = FakeRegKey()
    bin_key = FakeRegKey(children={"deep": deep})
    child_2024 = FakeRegKey(
        values={"Path": r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe"},
        children={"bin": bin_key, "Broken": OSError("无法打开")},
    )
    no_info = FakeRegKey(fail_query_info=True)
    root2 = FakeRegKey(
        values={
            "InstallDir": r"E:\Solidworks\SOLIDWORKS\\",
            "Path": r"E:\bin\SLDWORKS.exe",
        },
        children={"2024": child_2024, "NoInfo": no_info},
    )
    fake = make_fake_winreg(
        {
            ("HKLM", r"SOFTWARE\SolidWorks"): FakeRegKey(fail_open=True),
            ("HKLM", r"SOFTWARE\WOW6432Node\SolidWorks"): root2,
        }
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)

    result = _registry_paths("solidworks")

    assert sorted(str(path) for path in result) == sorted(
        [
            str(Path(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe")),
            str(Path(r"E:\bin\SLDWORKS.exe")),
            str(Path(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe")),
        ]
    )
    assert root2.closed is True


def test_registry_paths_autocad(monkeypatch):
    """@brief AutoCAD 注册表键用 AcadLocation 值并补上 acad.exe。"""
    root = FakeRegKey(values={"AcadLocation": r"D:\AutoCAD 2024"})
    fake = make_fake_winreg({("HKLM", r"SOFTWARE\Autodesk\AutoCAD"): root})
    monkeypatch.setitem(sys.modules, "winreg", fake)

    assert [str(path) for path in _registry_paths("autocad")] == [str(Path(r"D:\AutoCAD 2024\acad.exe"))]


def test_registry_paths_guard_branches(monkeypatch):
    monkeypatch.setattr("os.name", "posix")
    assert _registry_paths("solidworks") == []

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert _registry_paths("solidworks") == []


def test_common_candidates_covers_program_files_and_glob(monkeypatch):
    """@brief ProgramFiles 默认目录与 D:/E: 盘 glob 候选都要参与发现。"""
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")

    def fake_glob(pattern):
        if pattern == r"D:/Solidworks*/SOLIDWORKS*/SLDWORKS.exe":
            return [r"D:\Solidworks2026\SOLIDWORKS\SLDWORKS.exe"]
        if pattern == r"E:/AutoCAD*/acad.exe":
            return [r"E:\AutoCAD 2025\acad.exe"]
        return []

    monkeypatch.setattr(cad_installation.glob, "glob", fake_glob)

    sw = _common_candidates("solidworks")
    assert Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe") in sw
    assert Path(r"D:/Solidworks/SOLIDWORKS/SLDWORKS.exe") in sw
    assert Path(r"E:/SOLIDWORKS Corp/SOLIDWORKS/SLDWORKS.exe") in sw
    assert Path(r"D:\Solidworks2026\SOLIDWORKS\SLDWORKS.exe") in sw

    acad = _common_candidates("autocad")
    assert Path(r"C:\Program Files\Autodesk\AutoCAD 2024\acad.exe") in acad
    assert Path(r"D:/AutoCAD 2024/acad.exe") in acad
    assert Path(r"D:/Autodesk/AutoCAD 2024/acad.exe") in acad
    assert Path(r"E:\AutoCAD 2025\acad.exe") in acad


def test_discover_installations_rejects_unknown_product():
    with pytest.raises(ValueError, match="不支持的 CAD 产品"):
        discover_installations("rhino")


def test_discover_installations_pipeline(monkeypatch):
    """@brief 多来源候选按版本新到旧排序，路径去重且过滤不存在的安装。"""
    monkeypatch.setattr(cad_installation, "_shortcut_paths", lambda _product: [Path("SOLIDWORKS 2026.lnk")])
    monkeypatch.setattr(
        cad_installation,
        "_shortcut_info",
        lambda _path: (r"E:\Solidworks2026\SOLIDWORKS\SLDWORKS.exe", r"E:\Solidworks2026\SOLIDWORKS"),
    )
    monkeypatch.setattr(
        cad_installation,
        "_uninstall_registry_candidates",
        lambda _product: [
            {
                "path": Path(r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe"),
                "source": "uninstall-registry",
                "version": "2024",
                "service_pack": "SP03.1",
                "display_name": "SOLIDWORKS 2024 SP03.1",
                "shortcut": None,
            },
            {
                "path": Path(r"E:\Legacy\SOLIDWORKS\SLDWORKS.exe"),
                "source": "uninstall-registry",
                "version": "abc",
                "service_pack": None,
                "display_name": "SOLIDWORKS Beta",
                "shortcut": None,
            },
        ],
    )
    monkeypatch.setattr(
        cad_installation,
        "_registry_paths",
        lambda _product: [Path(r"D:\Solidworks 2025\SOLIDWORKS\SLDWORKS.exe"), Path(r"C:\missing\SLDWORKS.exe")],
    )
    monkeypatch.setattr(
        cad_installation,
        "_common_candidates",
        lambda _product: [
            Path(r"e:\solidworks\SOLIDWORKS\sldworks.exe"),
            Path(r"E:\Nonexistent\SLDWORKS.exe"),
            Path(r"D:\SWBackup\SLDWORKS.exe"),
        ],
    )
    monkeypatch.setattr(cad_installation, "_com_registered", lambda _product: True)

    existing = {
        r"E:\Solidworks2026\SOLIDWORKS\SLDWORKS.exe",
        r"E:\Solidworks\SOLIDWORKS\SLDWORKS.exe",
        r"D:\Solidworks 2025\SOLIDWORKS\SLDWORKS.exe",
        r"D:\SWBackup\SLDWORKS.exe",
    }
    result = discover_installations("solidworks", exists=lambda path: str(path).lower() in {p.lower() for p in existing})

    # 版本从新到旧排序；注册表候选的版本在候选阶段就从路径里解析出来了。
    assert [item["version"] for item in result] == ["2026", "2025", "2024", None]
    assert [item["source"] for item in result] == ["shortcut", "registry", "uninstall-registry", "common-path"]
    assert result[0]["displayName"] == "SOLIDWORKS 2026"
    assert result[0]["shortcut"] == str(Path("SOLIDWORKS 2026.lnk"))
    assert result[1]["displayName"] is None
    assert result[2]["servicePack"] == "SP03.1"
    assert result[3]["executable"] == str(Path(r"D:\SWBackup\SLDWORKS.exe"))
    assert all(item["registered"] is True and item["installed"] is True for item in result)
    assert all(item["product"] == "solidworks" for item in result)


def test_discover_installations_default_exists_uses_real_files(monkeypatch, tmp_path):
    """@brief 不注入 exists 时按真实文件存在性过滤。"""
    exe_dir = tmp_path / "SW2026"
    exe_dir.mkdir()
    exe = exe_dir / "SLDWORKS.exe"
    exe.write_bytes(b"MZ")
    missing = tmp_path / "SW2024" / "SLDWORKS.exe"

    monkeypatch.setattr(cad_installation, "_shortcut_paths", lambda _product: [])
    monkeypatch.setattr(cad_installation, "_uninstall_registry_candidates", lambda _product: [])
    monkeypatch.setattr(cad_installation, "_registry_paths", lambda _product: [exe, missing])
    monkeypatch.setattr(cad_installation, "_common_candidates", lambda _product: [])
    monkeypatch.setattr(cad_installation, "_com_registered", lambda _product: False)

    result = discover_installations("solidworks")

    assert [item["executable"] for item in result] == [str(exe)]
    assert result[0]["registered"] is False
    assert result[0]["shortcut"] is None


def test_discover_installation_prefers_newest_and_falls_back_to_placeholder(monkeypatch):
    monkeypatch.setattr(
        cad_installation,
        "discover_installations",
        lambda product, exists=None: [
            {"installed": True, "executable": "new", "version": "2026"},
            {"installed": True, "executable": "old", "version": "2024"},
        ],
    )
    assert discover_installation("solidworks")["executable"] == "new"

    monkeypatch.setattr(cad_installation, "discover_installations", lambda product, exists=None: [])
    monkeypatch.setattr(cad_installation, "_com_registered", lambda _product: False)
    assert discover_installation("autocad") == {
        "product": "autocad",
        "installed": False,
        "executable": None,
        "source": None,
        "version": None,
        "shortcut": None,
        "registered": False,
    }


def test_discover_all_queries_both_products(monkeypatch):
    seen = []

    def fake_installation(product, *, exists=None):
        seen.append(product)
        return {"product": product, "installed": False}

    monkeypatch.setattr(cad_installation, "discover_installation", fake_installation)

    result = discover_all()

    assert seen == ["solidworks", "autocad"]
    assert set(result) == {"solidworks", "autocad"}


def test_com_registered_branches(monkeypatch):
    """@brief COM 注册探测：非 Windows、查询成功、查询失败、缺 winreg 四条路径。"""
    monkeypatch.setattr("os.name", "posix")
    assert cad_installation._com_registered("solidworks") is False

    monkeypatch.setattr("os.name", "nt")
    fake = make_fake_winreg({})
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert cad_installation._com_registered("solidworks") is True
    assert cad_installation._com_registered("autocad") is True

    def broken_query(_hive, _path):
        raise OSError("类未注册")

    fake.QueryValue = broken_query
    assert cad_installation._com_registered("solidworks") is False

    monkeypatch.setitem(sys.modules, "winreg", None)
    assert cad_installation._com_registered("autocad") is False
