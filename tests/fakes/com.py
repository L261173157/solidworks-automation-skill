"""Fake COM 模块: 无 SolidWorks/pywin32 环境下的最小语义仿真 (CI fake 模式)。

设计约束:
- 只仿真库代码真正调用的 COM 表面 (以真机回归脚本为契约锚点);
- 所有对象带调用日志 (journal), 供冒烟测试断言调用序列;
- 通过 :data:`FAKE_ENV_VAR` 环境变量启用, 由 tests/conftest.py 注入 sys.modules。
"""
from __future__ import annotations

import importlib.machinery
import sys
import types

FAKE_ENV_VAR = "CADSTUDIO_FAKE_COM"

# win32com.client.VARIANT 的最小替代: 记录 vt 与值, 出参写回直接对 .value 赋值。
class FakeVARIANT:
    def __init__(self, varianttype=0, value=None):
        self.varianttype = varianttype
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"FakeVARIANT(vt=0x{self.varianttype:x}, value={self.value!r})"


# pythoncom 常量 (与真实值一致, 库代码用它们做位运算)
VT_EMPTY = 0
VT_NULL = 1
VT_I2 = 2
VT_I4 = 3
VT_R4 = 4
VT_R8 = 5
VT_BSTR = 8
VT_DISPATCH = 9
VT_BOOL = 11
VT_VARIANT = 12
VT_UNKNOWN = 13
VT_BYREF = 0x4000
DISPATCH_METHOD = 1
DISPATCH_PROPERTYGET = 2
DISPATCH_PROPERTYPUT = 4

# 伪 COM 表面在真实类型库缺失时抛出的统一错误
class FakeComUnavailable(RuntimeError):
    pass


class ComJournal:
    """记录 (owner, method, args) 调用序列, 供冒烟测试断言。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []

    def record(self, owner: str, method: str, *args) -> None:
        self.calls.append((owner, method, args))

    def names(self) -> list[str]:
        return [method for _, method, _ in self.calls]

    def count(self, method: str) -> int:
        return sum(1 for _, name, _ in self.calls if name == method)


def _make_module(name: str, **attrs):
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def make_fake_pythoncom():
    def _unavailable(*_args, **_kwargs):
        raise FakeComUnavailable("fake 模式无真实 COM 类型库")

    return _make_module(
        "pythoncom",
        CoInitialize=lambda *a, **k: None,
        CoUninitialize=lambda *a, **k: None,
        LoadTypeLib=_unavailable,
        LoadRegTypeLib=_unavailable,
        VT_EMPTY=VT_EMPTY,
        VT_NULL=VT_NULL,
        VT_I2=VT_I2,
        VT_I4=VT_I4,
        VT_R4=VT_R4,
        VT_R8=VT_R8,
        VT_BSTR=VT_BSTR,
        VT_DISPATCH=VT_DISPATCH,
        VT_BOOL=VT_BOOL,
        VT_VARIANT=VT_VARIANT,
        VT_UNKNOWN=VT_UNKNOWN,
        VT_BYREF=VT_BYREF,
        DISPATCH_METHOD=DISPATCH_METHOD,
        DISPATCH_PROPERTYGET=DISPATCH_PROPERTYGET,
        DISPATCH_PROPERTYPUT=DISPATCH_PROPERTYPUT,
    )


def make_fake_pywintypes():
    return _make_module(
        "pywintypes",
        IID=lambda value: value,
        com_error=type("com_error", (Exception,), {}),
    )


def make_fake_win32com_client():
    def _raise_unavailable(*_args, **_kwargs):
        raise FakeComUnavailable("fake 模式无真实 COM 连接")

    client = _make_module(
        "win32com.client",
        VARIANT=FakeVARIANT,
        Dispatch=_raise_unavailable,
        DispatchEx=_raise_unavailable,
        GetActiveObject=_raise_unavailable,
        Constant=_raise_unavailable,
        gencache=_make_module("win32com.client.gencache", EnsureModule=_raise_unavailable, EnsureDispatch=_raise_unavailable),
    )
    client.dynamic = _make_module(
        "win32com.client.dynamic",
        Dispatch=lambda obj, *a, **k: obj,
        DumbDispatch=_raise_unavailable,
    )
    client.constants = _make_module("win32com.client.constants")
    return client


def make_fake_win32com(client_module):
    return _make_module("win32com", client=client_module)


INSTALLABLE_MODULES = ("pythoncom", "pywintypes", "win32com", "win32com.client", "comtypes")


def install() -> dict:
    """把伪 COM 模块注入 sys.modules (替换已存在的真实模块), 幂等。"""
    client = make_fake_win32com_client()
    stubs = {
        "comtypes": _make_module("comtypes"),
        "pythoncom": make_fake_pythoncom(),
        "pywintypes": make_fake_pywintypes(),
        "win32com": make_fake_win32com(client),
        "win32com.client": client,
        "win32com.client.dynamic": client.dynamic,
        "win32com.client.gencache": client.gencache,
        "win32com.client.constants": client.constants,
    }
    for name, module in stubs.items():
        sys.modules[name] = module
    # 明确启用的 fake 模式也隔离 comtypes，避免非 Windows 测试触发安装提示。
    return stubs
