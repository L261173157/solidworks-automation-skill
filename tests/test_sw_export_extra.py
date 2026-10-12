"""sw_export 补充覆盖测试：导出封装、批量导出分支与导入回退。

覆盖策略：
- 导出封装用假模型/假应用（离线，绝不触发真实 COM）；
- 批量导出分支通过 monkeypatch 替换 open_document / _activate_source_document /
  _export_for_format，聚焦 batch_export_formats 自身的编排与证据逻辑；
- 模块级 ImportError 回退分支用 sys.modules 注入 None 后 reload 覆盖。
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

from scripts import sw_export
from scripts.sw_export import (
    SW_EXPORT_TO_DWG_SHEET_METAL,
    _ensure_parent_dir,
    _export_for_format,
    _normalize_format,
    _sheet_metal_alignment_variant,
    batch_export,
    batch_export_formats,
    export_flat_pattern_dxf,
    export_to_dxf,
    export_to_iges,
    export_to_parasolid,
    export_to_pdf,
    export_to_step,
    export_to_stl,
)


# ---- 通用假对象：覆盖全部导出 API 面 ----

class _PdfDataFake:
    def __init__(self):
        self.set_sheets = None

    def SetSheets(self, flag, names):
        self.set_sheets = (flag, names)


class _UniversalSw:
    def __init__(self, pdf_data=None):
        self._pdf_data = _PdfDataFake() if pdf_data is None else pdf_data

    def GetExportFileData(self, kind):
        assert kind == 1
        return self._pdf_data


class _UniversalExtension:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    def SaveAs(self, path, version, options, data, errors, warnings):
        self.calls.append(str(path))
        if hasattr(errors, "value"):
            errors.value = 0 if self.success else 9
        if hasattr(warnings, "value"):
            warnings.value = 0
        if self.success:
            Path(path).write_bytes(b"exported")
        return self.success


class _UniversalModel:
    """@brief 支持 STEP/STL/IGES/Parasolid/PDF/DXF 全部导出面的假模型。"""

    pref = None

    def __init__(self, success=True, doc_type=3):
        self.Extension = _UniversalExtension(success)
        self._sw = _UniversalSw()
        self._doc_type = doc_type
        self.cleared = None

    def ClearSelection2(self, flag):
        self.cleared = flag

    def SetUserPreferenceIntegerValue(self, key, value):
        self.pref = (key, value)
        return True

    def GetSldWorksObject(self):
        return self._sw

    def GetSheetNames(self):
        return ["Sheet1"]

    def GetType(self):
        return self._doc_type


# ---- 模块级 ImportError 回退 ----

def test_sw_export_falls_back_to_top_level_imports():
    """@brief 包内导入失败时回退到顶层模块导入。"""
    import scripts.sw_preflight as preflight_module
    import scripts.sw_connect as connect_module
    saved = {
        name: sys.modules.get(name)
        for name in ("scripts.sw_preflight", "scripts.sw_connect", "sw_preflight", "sw_connect")
    }
    try:
        sys.modules["scripts.sw_preflight"] = None
        sys.modules["scripts.sw_connect"] = None
        sys.modules["sw_preflight"] = preflight_module
        sys.modules["sw_connect"] = connect_module
        importlib.reload(sw_export)
        assert ".step" in sw_export.SUPPORTED_BATCH_FORMATS
        assert callable(sw_export.batch_export_formats)
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        importlib.reload(sw_export)


# ---- _ensure_parent_dir / _normalize_format ----

def test_ensure_parent_dir_creates_dirs_and_tolerates_empty_parent(monkeypatch, tmp_path):
    target = tmp_path / "lvl1" / "lvl2" / "out.step"
    _ensure_parent_dir(str(target))
    assert target.parent.is_dir()

    monkeypatch.setattr(os.path, "dirname", lambda _path: "")
    _ensure_parent_dir(str(target))  # parent 为空时跳过创建
    assert target.parent.is_dir()


def test_normalize_format_adds_dot_and_rejects_unknown():
    assert _normalize_format("STEP") == ".step"       # 无点 → 补点
    assert _normalize_format(" .Iges ") == ".iges"    # 有点 → 直接校验
    with pytest.raises(ValueError, match="暂不支持批量导出格式"):
        _normalize_format(".docx")


# ---- 单格式导出封装 ----

def test_export_to_step_writes_file_and_prints_success(tmp_path, capsys):
    model = _UniversalModel()
    out = tmp_path / "deep" / "part.step"
    assert export_to_step(model, str(out)) is True
    assert model.cleared is True
    assert out.is_file()
    assert "STEP 导出成功" in capsys.readouterr().out


def test_export_to_stl_sets_quality_preference_for_known_levels(tmp_path):
    fine = _UniversalModel()
    assert export_to_stl(fine, str(tmp_path / "p.stl"), quality="fine") is True
    assert fine.pref == (78, 0)

    coarse = _UniversalModel()
    assert export_to_stl(coarse, str(tmp_path / "q.stl"), quality="coarse") is True
    assert coarse.pref == (78, 1)

    custom = _UniversalModel()
    assert export_to_stl(custom, str(tmp_path / "r.stl"), quality="custom") is True
    assert custom.pref is None  # 未知质量档不写偏好


def test_export_to_iges_and_parasolid(tmp_path, capsys):
    assert export_to_iges(_UniversalModel(), str(tmp_path / "p.igs")) is True
    assert export_to_parasolid(_UniversalModel(), str(tmp_path / "p.x_t")) is True
    out = capsys.readouterr().out
    assert ".IGS 导出成功" in out
    assert ".X_T 导出成功" in out


def test_export_generic_failure_reports_error_code(tmp_path, capsys):
    model = _UniversalModel(success=False)
    assert export_to_step(model, str(tmp_path / "bad.step")) is False
    captured = capsys.readouterr().out
    assert ".STEP 导出失败" in captured
    assert "错误码: 9" in captured


def test_export_to_pdf_uses_export_file_data_and_default_sheets(tmp_path, capsys):
    model = _UniversalModel()
    pdf_data = model._sw._pdf_data
    out = tmp_path / "drawing.pdf"  # PDF 导出不负责创建父目录
    assert export_to_pdf(model, str(out)) is True
    assert pdf_data.set_sheets == (0, ["Sheet1"])   # sheet_names=None → GetSheetNames
    assert out.is_file()
    assert "PDF 导出成功" in capsys.readouterr().out


def test_export_to_pdf_without_pdf_data_reports_failure(tmp_path, capsys):
    model = _UniversalModel(success=False)
    model._sw._pdf_data = None
    assert export_to_pdf(model, str(tmp_path / "drawing.pdf"), sheet_names=["Sheet1"]) is False
    captured = capsys.readouterr().out
    assert "PDF 导出失败" in captured
    assert "错误码: 9" in captured


def test_export_to_dxf_routes_drawing_to_generic(tmp_path):
    model = _UniversalModel(doc_type=3)
    assert export_to_dxf(model, str(tmp_path / "drw.dxf")) is True


def test_export_to_dxf_routes_part_to_flat_pattern(monkeypatch, tmp_path):
    captured = {}

    def _fake_flat(model, output_path, **kwargs):
        captured["model"] = model
        captured["path"] = output_path
        return "flat-result"

    monkeypatch.setattr(sw_export, "export_flat_pattern_dxf", _fake_flat)
    model = _UniversalModel(doc_type=2)
    assert export_to_dxf(model, str(tmp_path / "part.dxf")) == "flat-result"
    assert captured["model"] is model
    assert captured["path"] == str(tmp_path / "part.dxf")


def test_export_for_format_routes_every_supported_extension(tmp_path):
    stem = tmp_path / "model"
    for extension in (".step", ".stp", ".stl", ".igs", ".iges", ".x_t", ".pdf", ".dxf", ".dwg"):
        model = _UniversalModel()
        assert _export_for_format(model, str(stem.with_suffix(extension)), extension, "fine") is True
    with pytest.raises(ValueError, match="没有导出器"):
        _export_for_format(_UniversalModel(), str(stem) + ".xyz", ".xyz", "fine")


# ---- 钣金展开 DXF ----

class _FlatModel:
    def __init__(self, path=str(Path(__file__).resolve()), result=True):
        self._path = path
        self._result = result
        self.args = None

    def GetPathName(self):
        return self._path

    def ExportToDWG2(self, *args):
        self.args = args
        return self._result


def test_export_flat_pattern_dxf_requires_saved_part():
    with pytest.raises(ValueError, match="必须先保存钣金零件"):
        export_flat_pattern_dxf(_FlatModel(path=""), "out.dxf")


def test_export_flat_pattern_dxf_applies_all_option_bits(tmp_path):
    model = _FlatModel(path=str(tmp_path / "bracket.sldprt"))
    out = tmp_path / "sub" / "flat.dxf"
    alignment = tuple(float(index) for index in range(12))
    assert export_flat_pattern_dxf(
        model,
        str(out),
        include_bend_lines=True,
        include_sketches=True,
        include_hidden_edges=True,
        include_bounding_box=True,
        merge_coplanar_faces=True,
        alignment=alignment,
    ) is True
    args = model.args
    assert args[0] == str(out)
    assert args[1] == str(tmp_path / "bracket.sldprt")
    assert args[2] == SW_EXPORT_TO_DWG_SHEET_METAL
    assert args[3] is True
    assert args[7] == 2079  # GEOMETRY|HIDDEN|BEND|SKETCHES|MERGE|BOUNDING_BOX
    assert out.parent.is_dir()


def test_export_flat_pattern_dxf_defaults_and_false_result(tmp_path):
    model = _FlatModel(path=str(tmp_path / "bracket.sldprt"), result=False)
    assert export_flat_pattern_dxf(model, str(tmp_path / "flat.dxf")) is False
    args = model.args
    assert args[7] == 5  # GEOMETRY + BEND_LINES（默认）
    assert args[4].value == (0.0,) * 12  # alignment=None → 12 个 0.0
    assert args[5] is False and args[6] is False and args[8] is None

    no_bend = _FlatModel(path=str(tmp_path / "bracket.sldprt"))
    assert export_flat_pattern_dxf(no_bend, str(tmp_path / "flat.dxf"), include_bend_lines=False) is True
    assert no_bend.args[7] == 1  # 仅几何，不含折弯线


def test_sheet_metal_alignment_variant_rejects_wrong_length(tmp_path):
    with pytest.raises(ValueError, match="12 个双精度数值"):
        _sheet_metal_alignment_variant((0.0,) * 11)
    model = _FlatModel(path=str(tmp_path / "bracket.sldprt"))
    with pytest.raises(ValueError, match="12 个双精度数值"):
        export_flat_pattern_dxf(model, "out.dxf", alignment=(0.0,) * 13)
    assert model.args is None  # 参数校验先于 ExportToDWG2


# ---- 源文档激活 ----

class _TitleModel:
    def __init__(self, title):
        self._title = title

    def GetTitle(self):
        return self._title


def test_activate_source_document_requires_title():
    class _NoTitle:
        def GetTitle(self):
            return ""

    with pytest.raises(RuntimeError, match="没有可激活的标题"):
        sw_export._activate_source_document(object(), _NoTitle(), "whatever.sldprt")


def test_activate_source_document_reraises_type_error_without_oleobj():
    class _Strict:
        def ActivateDoc3(self, *_args):
            raise TypeError("int() argument must not be VARIANT")

    with pytest.raises(TypeError):
        sw_export._activate_source_document(_Strict(), _TitleModel("part"), "src.sldprt")


def test_activate_source_document_falls_back_to_activate_doc2(tmp_path):
    source = tmp_path / "p.sldprt"
    source.write_text("x", encoding="utf-8")

    class _Active:
        def GetPathName(self):
            return str(source)

    class _Sw:
        def __init__(self):
            self.active = _Active()
            self.activate2_calls = []

        def ActivateDoc3(self, *_args):
            raise RuntimeError("doc3 exploded")

        def ActivateDoc2(self, title, flag, errors):
            self.activate2_calls.append(title)
            if hasattr(errors, "value"):
                errors.value = 0
            return True

        @property
        def ActiveDoc(self):
            return self.active

    sw = _Sw()
    active = sw_export._activate_source_document(sw, _TitleModel(source.name), source)
    assert active.GetPathName() == str(source)
    assert sw.activate2_calls == [source.name]


def test_activate_source_document_activate_doc2_typeerror_uses_dynamic_proxy(tmp_path, monkeypatch):
    source = tmp_path / "p.sldprt"
    source.write_text("x", encoding="utf-8")

    class _Active:
        def GetPathName(self):
            return str(source)

    class _Sw:
        _oleobj_ = object()

        def __init__(self):
            self.active = _Active()

        def ActivateDoc3(self, *_args):
            raise RuntimeError("doc3 exploded")

        def ActivateDoc2(self, *_args):
            raise TypeError("must not be VARIANT")

        @property
        def ActiveDoc(self):
            return self.active

    class _Dynamic:
        def ActivateDoc2(self, title, _flag, errors):
            if hasattr(errors, "value"):
                errors.value = 0
            return True

    monkeypatch.setattr(
        sw_export._win32com.dynamic,
        "DumbDispatch",
        lambda ole_object, _name: _Dynamic() if ole_object is _Sw._oleobj_ else None,
    )
    active = sw_export._activate_source_document(_Sw(), _TitleModel(source.name), source)
    assert active.GetPathName() == str(source)


def test_activate_source_document_activate_doc2_reraises_without_oleobj():
    class _Sw:
        def ActivateDoc3(self, *_args):
            raise RuntimeError("doc3 exploded")

        def ActivateDoc2(self, *_args):
            raise TypeError("must not be VARIANT")

    with pytest.raises(TypeError):
        sw_export._activate_source_document(_Sw(), _TitleModel("part"), "src.sldprt")


def test_activate_source_document_rejects_none_and_mismatched_active(tmp_path):
    source = tmp_path / "real.sldprt"
    source.write_text("x", encoding="utf-8")

    class _NoneSw:
        def ActivateDoc3(self, _title, _flag, _option, _errors):
            return None

    with pytest.raises(RuntimeError, match="无法激活源文档"):
        sw_export._activate_source_document(_NoneSw(), _TitleModel(source.name), source)

    class _Other:
        def GetPathName(self):
            return str(tmp_path / "other.sldprt")

    class _Sw2:
        def ActivateDoc3(self, _title, _flag, _option, _errors):
            return _Other()

    with pytest.raises(RuntimeError, match="活动文档与导出源不一致"):
        sw_export._activate_source_document(_Sw2(), _TitleModel(source.name), source)


# ---- batch_export 包装层 ----

def test_batch_export_wrapper_summarizes_documents(monkeypatch):
    canned = {
        "documents": [
            {"source": "a.sldprt", "success": True, "outputs": [{"path": "out/a.step", "error": None}]},
            {"source": "b.sldprt", "success": False, "outputs": [{"path": None, "error": "导出失败"}]},
            {"source": "c.sldprt", "success": False, "outputs": [{}], "error": "无法打开"},
        ]
    }
    monkeypatch.setattr(sw_export, "batch_export_formats", lambda *_args, **_kwargs: canned)
    results = batch_export(object(), ["a.sldprt"], "out", ".step")
    assert results[0] == {"file": "a.sldprt", "success": True, "output": "out/a.step", "error": None}
    assert results[1]["error"] == "导出失败"      # 文档级无错误 → 读取输出级错误
    assert results[2]["error"] == "无法打开"      # 文档级错误优先
    assert results[2]["output"] is None


# ---- batch_export_formats 分支 ----

class _BatchModel:
    def __init__(self, title):
        self._title = title

    def GetTitle(self):
        return self._title


class _BatchSw:
    def __init__(self, open_paths=(), lookup_boom=False):
        self.open_paths = {str(Path(path).resolve()).casefold() for path in open_paths}
        self.lookup_boom = lookup_boom
        self.closed = []

    def GetOpenDocumentByName(self, path):
        if self.lookup_boom:
            raise RuntimeError("lookup exploded")
        if str(Path(path).resolve()).casefold() in self.open_paths:
            return _BatchModel(Path(path).stem)
        return None

    def CloseDoc(self, title):
        self.closed.append(title)


def test_batch_export_formats_rejects_empty_inputs(tmp_path):
    source = tmp_path / "a.sldprt"
    source.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="file_paths 不能为空"):
        batch_export_formats(_BatchSw(), [], tmp_path / "out", [".step"])
    with pytest.raises(ValueError, match="formats 不能为空"):
        batch_export_formats(_BatchSw(), [source], tmp_path / "out", ())


def test_batch_export_formats_reports_missing_source(tmp_path):
    sw = _BatchSw()
    report = batch_export_formats(sw, [tmp_path / "ghost.sldprt"], tmp_path / "out", [".step"])
    assert report["success"] is False
    assert report["documents"][0]["error"] == "源文件不存在"
    assert report["summary"] == {"documents": 1, "outputs": 0, "succeeded": 0}
    assert sw.closed == []


def test_batch_export_formats_survives_lookup_failure_and_open_failure(tmp_path, monkeypatch):
    source = tmp_path / "a.sldprt"
    source.write_text("x", encoding="utf-8")
    sw = _BatchSw(lookup_boom=True)
    monkeypatch.setattr(sw_export, "open_document", lambda *_args, **_kwargs: None)
    report = batch_export_formats(sw, [source], tmp_path / "out", [".step"])
    assert report["documents"][0]["error"] == "SolidWorks 无法打开源文件"
    assert report["documents"][0]["outputs"] == []
    assert sw.closed == []  # 打开失败时不进入关闭流程


def test_batch_export_formats_detects_missing_artifact_after_api_success(tmp_path, monkeypatch):
    source = tmp_path / "a.sldprt"
    source.write_text("x", encoding="utf-8")
    sw = _BatchSw()
    monkeypatch.setattr(sw_export, "open_document", lambda *_args, **_kwargs: _BatchModel("a"))
    monkeypatch.setattr(sw_export, "_activate_source_document", lambda *_args: True)
    monkeypatch.setattr(sw_export, "_export_for_format", lambda *_args, **_kwargs: True)
    report = batch_export_formats(
        sw, [source], tmp_path / "out", [".step"], overwrite=True, close_documents=False
    )
    output = report["documents"][0]["outputs"][0]
    assert output["api_success"] is True
    assert output["produced_this_run"] is False
    assert output["exists"] is False
    assert output["error"] == "SolidWorks 返回成功，但没有检测到本轮新产物"
    assert report["success"] is False
    assert sw.closed == []  # close_documents=False 不关闭文档


def test_batch_export_formats_captures_export_exception(tmp_path, monkeypatch):
    source = tmp_path / "a.sldprt"
    source.write_text("x", encoding="utf-8")
    sw = _BatchSw()
    monkeypatch.setattr(sw_export, "open_document", lambda *_args, **_kwargs: _BatchModel("a"))

    def _boom(*_args):
        raise RuntimeError("激活失败")

    monkeypatch.setattr(sw_export, "_activate_source_document", _boom)
    report = batch_export_formats(sw, [source], tmp_path / "out", [".step"])
    output = report["documents"][0]["outputs"][0]
    assert output["error"] == "激活失败"
    assert output["success"] is False
    assert report["success"] is False
    assert sw.closed == ["a"]  # 非既开文档在 finally 中关闭


def test_batch_export_formats_records_failed_api_call(tmp_path, monkeypatch):
    source = tmp_path / "a.sldprt"
    source.write_text("x", encoding="utf-8")
    sw = _BatchSw()
    monkeypatch.setattr(sw_export, "open_document", lambda *_args, **_kwargs: _BatchModel("a"))
    monkeypatch.setattr(sw_export, "_activate_source_document", lambda *_args: True)
    monkeypatch.setattr(sw_export, "_export_for_format", lambda *_args, **_kwargs: False)
    report = batch_export_formats(sw, [source], tmp_path / "out", [".step"])
    output = report["documents"][0]["outputs"][0]
    assert output["success"] is False
    assert output["api_success"] is False
    assert output["produced_this_run"] is False
    assert "error" not in output  # API 明确失败且无新产物 → 不追加二次错误
    assert report["summary"]["succeeded"] == 0
