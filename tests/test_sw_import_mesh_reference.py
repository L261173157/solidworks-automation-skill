"""OBJ/STL 网格参考模型导入流程的无 COM 离线单元测试。

COM 边界 (connect_solidworks / save_document / run_review) 全部以 monkeypatch
替换; LoadFile4 的 by-ref VARIANT 走真实 win32com 对象, 不初始化任何 COM 会话。
"""
import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

from scripts import sw_connect, sw_import_mesh_reference as mesh_module
from scripts.sw_import_mesh_reference import (
    SW_IMPORT_STL_VRML_MODEL_TYPE,
    SW_IMPORT_STL_VRML_UNITS,
    SW_IMPORT_UNIT_PREFERENCE,
    SW_IMPORT_STL_VRML_TEXTURE_INFORMATION,
    SW_VRML_STL_IMPORT_AS_PS_MESH,
    SW_VRML_STL_IMPORT_SEGMENTED,
    configure_mesh_import_preferences,
    import_mesh_reference,
    main,
)


class FakeModel:
    """@brief LoadFile4 返回的模型桩: 记录后处理调用序列。"""

    def __init__(self):
        self.calls = []

    def GetTitle(self):
        return "mesh.stl"

    GetType = 1  # 不可调用属性, 覆盖 get_com_member 的属性形态

    def ClearSelection2(self, flag):
        self.calls.append(("ClearSelection2", flag))

    def ForceRebuild3(self, flag):
        self.calls.append(("ForceRebuild3", flag))

    def ViewZoomtofit2(self):
        self.calls.append(("ViewZoomtofit2",))

    def GraphicsRedraw2(self):
        self.calls.append(("GraphicsRedraw2",))


class FakeSwApp:
    """@brief 最小 SolidWorks 应用桩: 偏好设置 + LoadFile4 记录。"""

    def __init__(self, load_result=None):
        self.RevisionNumber = "32.5.0"
        self.load_result = load_result
        self.load_calls = []
        self.int_prefs = {}
        self.toggles = []

    def SetUserPreferenceIntegerValue(self, preference, value):
        self.int_prefs[preference] = value
        return 1

    def SetUserPreferenceToggle(self, preference, value):
        self.toggles.append((preference, value))
        return None  # 部分版本返回 void

    def LoadFile4(self, path, flags, empty_dispatch, errors):
        self.load_calls.append((path, flags, empty_dispatch, errors.value))
        return self.load_result


def patch_solidworks(monkeypatch, sw):
    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return sw, None

    monkeypatch.setattr(mesh_module, "connect_solidworks", fake_connect)
    return seen


# ---------------------------------------------------------------------------
# configure_mesh_import_preferences
# ---------------------------------------------------------------------------


def test_configure_mesh_import_preferences_records_audit_operations():
    sw = FakeSwApp()

    report = configure_mesh_import_preferences(sw)

    assert report["units"] == "m"
    assert report["model_type"] == "graphics"
    kinds = [operation["kind"] for operation in report["operations"]]
    assert kinds == ["integer", "integer", "integer", "toggle", "toggle", "toggle"]
    assert report["operations"][0] == {
        "kind": "integer",
        "preference": SW_IMPORT_STL_VRML_MODEL_TYPE,
        "value": 0,
        "ok": True,
    }
    assert report["operations"][1] == {
        "kind": "integer",
        "preference": SW_IMPORT_STL_VRML_UNITS,
        "value": 2,
        "ok": True,
    }
    assert report["operations"][2]["preference"] == SW_IMPORT_UNIT_PREFERENCE
    assert sw.toggles == [
        (SW_IMPORT_STL_VRML_TEXTURE_INFORMATION, True),
        (SW_VRML_STL_IMPORT_AS_PS_MESH, True),
        (SW_VRML_STL_IMPORT_SEGMENTED, False),
    ]
    assert report["operations"][3] == {
        "kind": "toggle",
        "preference": SW_IMPORT_STL_VRML_TEXTURE_INFORMATION,
        "value": True,
        "returned": None,
    }


def test_configure_mesh_import_preferences_normalizes_units_and_type():
    sw = FakeSwApp()

    report = configure_mesh_import_preferences(sw, units=" MM ", model_type=" Solid ")

    assert report["units"] == "mm"
    assert report["model_type"] == "solid"
    assert sw.int_prefs[SW_IMPORT_STL_VRML_UNITS] == 0
    assert sw.int_prefs[SW_IMPORT_STL_VRML_MODEL_TYPE] == 2


def test_configure_mesh_import_preferences_rejects_unknown_units():
    sw = FakeSwApp()

    with pytest.raises(ValueError, match="不支持的单位: mm2"):
        configure_mesh_import_preferences(sw, units="mm2")


def test_configure_mesh_import_preferences_rejects_unknown_model_type():
    sw = FakeSwApp()

    with pytest.raises(ValueError, match="不支持的导入类型: wireframe"):
        configure_mesh_import_preferences(sw, model_type="wireframe")


# ---------------------------------------------------------------------------
# import_mesh_reference
# ---------------------------------------------------------------------------


def test_import_mesh_reference_rejects_non_mesh_suffix(tmp_path):
    with pytest.raises(ValueError, match="当前脚本只处理 OBJ/STL"):
        import_mesh_reference(tmp_path / "model.glb", tmp_path / "model.sldprt")


def test_import_mesh_reference_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="ghost.stl"):
        import_mesh_reference(tmp_path / "ghost.stl", tmp_path / "model.sldprt")


def test_import_mesh_reference_happy_path_saves_without_review(tmp_path, monkeypatch):
    mesh = tmp_path / "mesh.stl"
    mesh.write_bytes(b"stl-bytes")
    output = tmp_path / "out" / "mesh.sldprt"
    model = FakeModel()
    sw = FakeSwApp(load_result=model)
    connect_kwargs = patch_solidworks(monkeypatch, sw)
    save_calls = []

    def fake_save_document(model_arg, file_path=None):
        save_calls.append((model_arg, file_path))
        Path(file_path).write_bytes(b"saved-bytes")
        return True

    monkeypatch.setattr(mesh_module, "save_document", fake_save_document)

    result = import_mesh_reference(mesh, output)

    assert connect_kwargs == {"visible": True}
    assert result["source"] == str(mesh.resolve())
    assert result["source_size_bytes"] == len(b"stl-bytes")
    assert result["output_sldprt"] == str(output.resolve())
    assert result["solidworks_revision"] == "32.5.0"
    assert result["loadfile4_errors"] == 0
    assert result["opened"] is True
    assert result["title"] == "mesh.stl"
    assert result["doc_type"] == 1
    assert result["saved"] is True
    assert result["output_size_bytes"] == len(b"saved-bytes")
    assert "review_report" not in result
    assert save_calls == [(model, str(output))]
    assert sw.load_calls == [(str(mesh.resolve()), "r", sw.load_calls[0][2], 0)]
    assert model.calls == [
        ("ClearSelection2", True),
        ("ForceRebuild3", False),
        ("ViewZoomtofit2",),
        ("GraphicsRedraw2",),
    ]
    assert output.parent.is_dir()


def test_import_mesh_reference_raises_when_loadfile4_returns_none(tmp_path, monkeypatch):
    mesh = tmp_path / "mesh.stl"
    mesh.write_bytes(b"stl-bytes")
    sw = FakeSwApp(load_result=None)
    patch_solidworks(monkeypatch, sw)
    monkeypatch.setattr(mesh_module, "save_document", lambda *_args, **_kwargs: True)

    with pytest.raises(RuntimeError, match="OBJ/STL 导入失败，LoadFile4 errors=0"):
        import_mesh_reference(mesh, tmp_path / "out.sldprt")


def test_import_mesh_reference_raises_when_save_fails(tmp_path, monkeypatch):
    mesh = tmp_path / "mesh.stl"
    mesh.write_bytes(b"stl-bytes")
    output = tmp_path / "missing" / "mesh.sldprt"
    sw = FakeSwApp(load_result=FakeModel())
    patch_solidworks(monkeypatch, sw)
    monkeypatch.setattr(mesh_module, "save_document", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="保存失败"):
        import_mesh_reference(mesh, output)
    # 输出不存在时 output_size_bytes 走 0 分支, 且不抛 FileNotFoundError。
    assert not output.exists()


def test_import_mesh_reference_runs_review_when_requested(tmp_path, monkeypatch):
    mesh = tmp_path / "mesh.obj"
    mesh.write_bytes(b"obj-bytes")
    output = tmp_path / "mesh.sldprt"
    output.write_bytes(b"saved-bytes")
    review_dir = tmp_path / "deep" / "review"
    model = FakeModel()
    sw = FakeSwApp(load_result=model)
    connect_kwargs = patch_solidworks(monkeypatch, sw)
    review_calls = []

    def fake_save_document(_model, file_path=None):
        Path(file_path).write_bytes(b"saved-bytes")
        return True

    def fake_run_review(model_arg, output_dir, basename="review", expected_outputs=None):
        review_calls.append((model_arg, output_dir, basename, expected_outputs))
        return {"evaluation": "pass", "previews": ["front.bmp"]}, Path(output_dir) / "review_report.json"

    monkeypatch.setattr(mesh_module, "save_document", fake_save_document)
    monkeypatch.setattr(mesh_module, "run_review", fake_run_review)

    result = import_mesh_reference(
        mesh, output, review_dir=review_dir, units="mm", model_type="surface", visible=False
    )

    assert connect_kwargs == {"visible": False}
    assert review_calls == [
        (
            model,
            str(review_dir.expanduser().resolve()),
            "mesh",
            [str(output.resolve()), str(mesh.resolve())],
        )
    ]
    assert review_dir.is_dir()
    assert result["review_report"] == review_dir.resolve() / "review_report.json"
    assert result["review_evaluation"] == "pass"
    assert result["review_previews"] == ["front.bmp"]
    assert result["import_preferences"]["units"] == "mm"
    assert result["import_preferences"]["model_type"] == "surface"


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def test_main_prints_result_json_with_defaults(monkeypatch, capsys, tmp_path):
    mesh = tmp_path / "mesh.stl"
    output = tmp_path / "mesh.sldprt"
    seen = {}

    def fake_import(mesh_path, output_sldprt, **kwargs):
        seen["args"] = (mesh_path, output_sldprt)
        seen.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(mesh_module, "import_mesh_reference", fake_import)

    assert main([str(mesh), str(output)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "ok"}
    assert seen == {
        "args": (str(mesh), str(output)),
        "review_dir": None,
        "units": "m",
        "model_type": "graphics",
        "visible": True,
    }


def test_main_forwards_cli_options(monkeypatch, capsys, tmp_path):
    mesh = tmp_path / "mesh.stl"
    output = tmp_path / "mesh.sldprt"
    review_dir = tmp_path / "review"
    seen = {}

    def fake_import(mesh_path, output_sldprt, **kwargs):
        seen["args"] = (mesh_path, output_sldprt)
        seen.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(mesh_module, "import_mesh_reference", fake_import)

    argv = [
        str(mesh), str(output),
        "--review-dir", str(review_dir),
        "--units", "mm",
        "--model-type", "solid",
        "--hidden",
    ]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "ok"}
    assert seen == {
        "args": (str(mesh), str(output)),
        "review_dir": str(review_dir),
        "units": "mm",
        "model_type": "solid",
        "visible": False,
    }


@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # runpy 复执行已导入模块时的固有提示
def test_module_main_entrypoint_imports_and_saves(tmp_path, monkeypatch, capsys):
    """@brief ``python scripts/sw_import_mesh_reference.py`` 入口全流程 (COM 全 mock)。"""
    mesh = tmp_path / "mesh.stl"
    mesh.write_bytes(b"stl-bytes")
    output = tmp_path / "out" / "mesh.sldprt"
    sw = FakeSwApp(load_result=FakeModel())

    def fake_connect(**_kwargs):
        return sw, None

    def fake_save_document(_model, file_path=None, **_kwargs):
        Path(file_path).write_bytes(b"saved-bytes")
        return True

    monkeypatch.setattr(sw_connect, "connect_solidworks", fake_connect)
    monkeypatch.setattr(sw_connect, "save_document", fake_save_document)
    monkeypatch.setattr(sys, "argv", ["sw_import_mesh_reference.py", str(mesh), str(output)])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("scripts.sw_import_mesh_reference", run_name="__main__")

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["saved"] is True
    assert payload["output_size_bytes"] == len(b"saved-bytes")
    assert output.exists()


def test_fallback_import_path_without_package(monkeypatch):
    """@brief 以文件方式导入时, 相对导入失败须回退到顶级模块导入。"""
    module_path = Path(mesh_module.__file__)
    monkeypatch.syspath_prepend(str(module_path.parent))
    spec = importlib.util.spec_from_file_location("_mesh_reference_fallback", module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_mesh_reference_fallback", module)
    spec.loader.exec_module(module)

    assert module.SW_IMPORT_STL_VRML_MODEL_TYPE == 208
    assert module.configure_mesh_import_preferences is not configure_mesh_import_preferences
