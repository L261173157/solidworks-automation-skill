"""comtypes Pack and Go 子进程 worker 的无 COM 离线回归测试。

worker 通过 stdin 读 JSON 载荷、stdout 回写单行 JSON 结果；测试用
``io.StringIO`` 替换标准流，并把 ``sw_delivery`` 替换为桩模块，从而在不触碰
真实 COM 的前提下覆盖成功、异常与 ``__main__`` 入口全部分支。
"""
import io
import json
import os
import runpy
import sys
import types
from pathlib import Path

import pytest

from scripts import sw_delivery_comtypes_worker as worker

# worker 模块在导入时设置 CADSTUDIO_PACK_WORKER=1, 还原以免影响同会话其它测试。
os.environ.pop("CADSTUDIO_PACK_WORKER", None)


FULL_PAYLOAD = {
    "source_path": "C:/temp/assembly.sldasm",
    "target": "C:/temp/package",
    "existing_files": {
        "C:/temp/assembly.sldasm": [2048, 0],
        "C:/temp/part.sldprt": None,
    },
    "include_drawings": True,
    "include_simulation_results": False,
    "include_toolbox_components": True,
    "include_suppressed": False,
    "flatten": True,
    "dependencies": ["C:/temp/part.sldprt"],
}


def _install_impl_stub(monkeypatch, impl):
    """用桩模块替换 sw_delivery, 拦截 worker 内的延迟导入。"""
    stub = types.ModuleType("sw_delivery")
    stub._comtypes_pack_and_go_impl = impl
    monkeypatch.setitem(sys.modules, "sw_delivery", stub)


def _patch_stdio(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload, ensure_ascii=False)))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    return stdout


def test_main_success_passes_payload_to_impl_and_prints_result(monkeypatch):
    seen = {}

    def impl(source_path, target, existing_files, **kwargs):
        seen["source_path"] = source_path
        seen["target"] = target
        seen["existing_files"] = existing_files
        seen.update(kwargs)
        return {"backend": "comtypes", "document_count": 2}

    _install_impl_stub(monkeypatch, impl)
    stdout = _patch_stdio(monkeypatch, FULL_PAYLOAD)

    assert worker.main() == 0
    assert json.loads(stdout.getvalue()) == {"result": {"backend": "comtypes", "document_count": 2}}
    assert seen["source_path"] == "C:/temp/assembly.sldasm"
    assert seen["target"] == Path("C:/temp/package")
    # 列表转元组, None 保持 None, 供父进程做文件审计比对。
    assert seen["existing_files"] == {
        "C:/temp/assembly.sldasm": (2048, 0),
        "C:/temp/part.sldprt": None,
    }
    assert seen["include_drawings"] is True
    assert seen["include_simulation_results"] is False
    assert seen["include_toolbox_components"] is True
    assert seen["include_suppressed"] is False
    assert seen["flatten"] is True
    assert seen["dependencies"] == ["C:/temp/part.sldprt"]


def test_main_defaults_missing_existing_files_to_empty(monkeypatch):
    seen = {}

    def impl(_source_path, _target, existing_files, **_kwargs):
        seen["existing_files"] = existing_files
        return {"document_count": 0}

    _install_impl_stub(monkeypatch, impl)
    payload = {key: value for key, value in FULL_PAYLOAD.items() if key != "existing_files"}
    stdout = _patch_stdio(monkeypatch, payload)

    assert worker.main() == 0
    assert seen["existing_files"] == {}
    assert json.loads(stdout.getvalue())["result"] == {"document_count": 0}


def test_main_reports_exception_payload_and_returns_one(monkeypatch):
    def impl(*_args, **_kwargs):
        raise ValueError("comtypes 无法创建 SolidWorks 应用")

    _install_impl_stub(monkeypatch, impl)
    stdout = _patch_stdio(monkeypatch, FULL_PAYLOAD)

    assert worker.main() == 1
    payload = json.loads(stdout.getvalue())
    assert payload["error"] == "ValueError: comtypes 无法创建 SolidWorks 应用"
    assert "ValueError: comtypes 无法创建 SolidWorks 应用" in payload["traceback"]


def test_main_catches_base_exception_like_keyboard_interrupt(monkeypatch):
    """@brief 父进程终止 (KeyboardInterrupt) 也必须回传而不是裸崩。"""
    def impl(*_args, **_kwargs):
        raise KeyboardInterrupt("模拟父进程终止")

    _install_impl_stub(monkeypatch, impl)
    stdout = _patch_stdio(monkeypatch, FULL_PAYLOAD)

    assert worker.main() == 1
    payload = json.loads(stdout.getvalue())
    assert payload["error"] == "KeyboardInterrupt: 模拟父进程终止"


def test_main_reports_missing_payload_key_as_error(monkeypatch):
    _install_impl_stub(monkeypatch, lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应到达")))
    payload = {"source_path": "C:/temp/a.sldasm", "target": "C:/temp/out"}
    stdout = _patch_stdio(monkeypatch, payload)

    assert worker.main() == 1
    body = json.loads(stdout.getvalue())
    assert body["error"].startswith("KeyError: 'include_drawings'")


@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # runpy 复执行已导入模块时的固有提示
def test_main_guard_success_exits_zero(monkeypatch):
    _install_impl_stub(monkeypatch, lambda *_args, **_kwargs: {"document_count": 1})
    monkeypatch.delenv("CADSTUDIO_PACK_WORKER", raising=False)
    stdout = _patch_stdio(monkeypatch, FULL_PAYLOAD)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("scripts.sw_delivery_comtypes_worker", run_name="__main__")

    assert excinfo.value.code == 0
    assert json.loads(stdout.getvalue())["result"] == {"document_count": 1}


@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # runpy 复执行已导入模块时的固有提示
def test_main_guard_error_exits_one(monkeypatch):
    def impl(*_args, **_kwargs):
        raise RuntimeError("worker 内部失败")

    _install_impl_stub(monkeypatch, impl)
    monkeypatch.delenv("CADSTUDIO_PACK_WORKER", raising=False)
    stdout = _patch_stdio(monkeypatch, FULL_PAYLOAD)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("scripts.sw_delivery_comtypes_worker", run_name="__main__")

    assert excinfo.value.code == 1
    assert "RuntimeError: worker 内部失败" in stdout.getvalue()
