"""@brief sw_inspect 覆盖率补充测试: 导入回退、COM 错误分支与边界解析路径。

所有用例离线且确定性: COM 边界一律使用假文档 + monkeypatch 注入,
不触发真实 SolidWorks;整体尺寸测量经 monkeypatch 替换 open/new/scale 依赖。
"""
from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

import pytest

import scripts.sw_inspect as sw_inspect
from scripts import sw_connect, sw_preflight
from scripts.sw_inspect import (
    _basename_no_ext,
    bounding_box,
    count_components,
    enumerate_features,
    overall_dimensions,
)


# ---------------------------------------------------------------------------
# 顶层导入回退分支 (try/except ImportError)
# ---------------------------------------------------------------------------

def _install_top_level_import_stubs(monkeypatch) -> dict:
    """@brief 注入顶层同名桩模块, 使 runpy 直接执行源文件时命中 ImportError 回退。"""
    stubs = {
        name: types.ModuleType(name)
        for name in ("sw_connect", "sw_drawing", "sw_preflight", "sw_assembly")
    }
    stubs["sw_connect"].connect_solidworks = lambda version=None: (None, None)
    stubs["sw_connect"].get_com_member = sw_connect.get_com_member
    stubs["sw_connect"].open_document = sw_connect.open_document
    stubs["sw_connect"].new_document = sw_connect.new_document
    stubs["sw_connect"].classify_sw_file_load_errors = sw_connect.classify_sw_file_load_errors
    stubs["sw_connect"].SolidWorksDocumentOpenError = sw_connect.SolidWorksDocumentOpenError
    stubs["sw_preflight"].import_com_dependencies = sw_preflight.import_com_dependencies
    stubs["sw_drawing"].force_view_scale = lambda view, scale, drawing_model=None: {"status": "pass"}
    stubs["sw_drawing"].inspect_drawing_structure = lambda *args, **kwargs: {}
    stubs["sw_assembly"].get_interference_detection = lambda model: {}
    for name, stub in stubs.items():
        monkeypatch.setitem(sys.modules, name, stub)
    return stubs


def test_module_body_supports_top_level_import_fallback(monkeypatch):
    """@brief 以脚本方式执行源文件时, 相对导入失败必须回退到顶层导入。"""
    _install_top_level_import_stubs(monkeypatch)
    namespace = runpy.run_path(
        str(Path(sw_inspect.__file__).resolve()), run_name="sw_inspect_script"
    )
    assert namespace["InspectionError"].__name__ == "InspectionError"
    assert callable(namespace["overall_dimensions"])


# ---------------------------------------------------------------------------
# _basename_no_ext
# ---------------------------------------------------------------------------

def test_basename_no_ext_edge_cases():
    """@brief 空路径返回占位符, 非 SW 扩展名回退 splitext。"""
    assert _basename_no_ext(None) == "?"
    assert _basename_no_ext("") == "?"
    assert _basename_no_ext("C:/lib/plugin.dll") == "plugin"
    assert _basename_no_ext(r"D:\x\part.SLDPRT") == "part"
    assert _basename_no_ext("asm.sldasm") == "asm"
    assert _basename_no_ext("drawing.slddrw") == "drawing"


# ---------------------------------------------------------------------------
# overall_dimensions
# ---------------------------------------------------------------------------

class _OutlineView:
    def __init__(self, outline):
        self._outline = outline

    def GetOutline(self):
        return self._outline


class _TempDrawing:
    """@brief 可配置的一次性测量工程图假对象。"""

    def __init__(self, front_outline=None, top_outline=None, *,
                 create_raises=False, rebuild_raises=False, views_none=False):
        self._front = front_outline
        self._top = top_outline
        self._create_raises = create_raises
        self._rebuild_raises = rebuild_raises
        self._views_none = views_none
        self.created = []

    def CreateDrawViewFromModelView3(self, path, name, x, y, scale):
        if self._create_raises:
            raise RuntimeError("view create boom")
        self.created.append(name)
        if self._views_none:
            return None
        return _OutlineView(self._front if name == "*Front" else self._top)

    def EditRebuild3(self):
        if self._rebuild_raises:
            raise RuntimeError("rebuild boom")
        return True

    def GetTitle(self):
        return "throwaway.SLDDRW"


class _FakeSwApp:
    """@brief 会话假对象: 可控制 GetOpenDocumentByName 与 CloseDoc 行为。"""

    def __init__(self, pre_open=None, close_raises=False, with_lookup=True):
        self._pre_open = pre_open
        self._close_raises = close_raises
        self._with_lookup = with_lookup
        self.closed = []

    def GetOpenDocumentByName(self, path):
        if not self._with_lookup:
            raise RuntimeError("lookup boom")
        return self._pre_open

    def CloseDoc(self, title):
        if self._close_raises:
            raise RuntimeError("close boom")
        self.closed.append(title)
        return 1


class _FakeSwAppNoLookup:
    """@brief 完全缺失 GetOpenDocumentByName 成员的会话 (getattr 抛 AttributeError)。"""

    def __init__(self, close_raises=False):
        self.closed = []
        self._close_raises = close_raises

    def CloseDoc(self, title):
        if self._close_raises:
            raise RuntimeError("close boom")
        self.closed.append(title)
        return 1


def _patch_measurement(monkeypatch, drawing):
    monkeypatch.setattr(sw_inspect, "open_document", lambda *args, **kwargs: object())
    monkeypatch.setattr(sw_inspect, "new_document", lambda session, doc_type: drawing)
    monkeypatch.setattr(
        sw_inspect, "force_view_scale",
        lambda view, scale, drawing_model=None: {"status": "pass"},
    )


_GOOD_FRONT = [0.0, 0.0, 0.060, 0.010]   # W=60mm H=10mm
_GOOD_TOP = [0.0, 0.0, 0.060, 0.040]     # W=60mm D=40mm
_PART = r"C:\tmp\block.sldprt"


def test_overall_dimensions_reuses_already_open_part(monkeypatch):
    """@brief 零件已加载时不得重复 open, 且不关闭调用方的既有零件。"""
    drawing = _TempDrawing(_GOOD_FRONT, _GOOD_TOP)
    sw = _FakeSwApp(pre_open=object())
    opened = []

    def _record_open(*args, **kwargs):
        opened.append(args)
        return object()

    monkeypatch.setattr(sw_inspect, "open_document", _record_open)
    monkeypatch.setattr(sw_inspect, "new_document", lambda session, doc_type: drawing)
    monkeypatch.setattr(
        sw_inspect, "force_view_scale",
        lambda view, scale, drawing_model=None: {"status": "pass"},
    )

    result = overall_dimensions(sw, _PART)
    assert result["status"] == "pass"
    assert opened == []
    assert abs(result["width_mm"] - 60.0) < 1e-9
    assert sw.closed == ["throwaway.SLDDRW"]


def test_overall_dimensions_missing_drawing_reports_error(monkeypatch):
    """@brief 新建临时图失败时返回 MEASURE_NO_DRAWING, 但仍关闭新开的零件。"""
    sw = _FakeSwApp()
    monkeypatch.setattr(sw_inspect, "open_document", lambda *args, **kwargs: object())
    monkeypatch.setattr(sw_inspect, "new_document", lambda session, doc_type: None)
    monkeypatch.setattr(
        sw_inspect, "force_view_scale",
        lambda view, scale, drawing_model=None: {"status": "pass"},
    )

    result = overall_dimensions(sw, _PART)
    assert result["status"] == "failed"
    assert result["error_code"] == "MEASURE_NO_DRAWING"
    assert sw.closed == ["block.sldprt"]


def test_overall_dimensions_missing_views(monkeypatch):
    """@brief 前/俯视图创建失败时返回 MEASURE_NO_VIEWS。"""
    drawing = _TempDrawing(views_none=True)
    sw = _FakeSwApp()
    _patch_measurement(monkeypatch, drawing)

    result = overall_dimensions(sw, _PART)
    assert result["error_code"] == "MEASURE_NO_VIEWS"
    assert drawing.created == ["*Front", "*Top"]
    assert "throwaway.SLDDRW" in sw.closed


def test_overall_dimensions_rebuild_failure_is_ignored(monkeypatch):
    """@brief EditRebuild3 抛错只跳过重建, 不影响测量结果。"""
    drawing = _TempDrawing(_GOOD_FRONT, _GOOD_TOP, rebuild_raises=True)
    sw = _FakeSwApp()
    _patch_measurement(monkeypatch, drawing)

    result = overall_dimensions(sw, _PART)
    assert result["status"] == "pass"
    assert result["outline_front"] == _GOOD_FRONT


def test_overall_dimensions_short_outlines(monkeypatch):
    """@brief 轮廓值不足 4 个时返回 MEASURE_NO_OUTLINE。"""
    drawing = _TempDrawing([0.0, 0.0, 0.010], _GOOD_TOP)
    sw = _FakeSwApp()
    _patch_measurement(monkeypatch, drawing)

    result = overall_dimensions(sw, _PART)
    assert result["status"] == "failed"
    assert result["error_code"] == "MEASURE_NO_OUTLINE"


def test_overall_dimensions_creation_failure_is_reported(monkeypatch):
    """@brief 视图创建抛错进入 MEASURE_FAILED 兜底, 临时图与零件仍被关闭。"""
    drawing = _TempDrawing(create_raises=True)
    sw = _FakeSwApp()
    _patch_measurement(monkeypatch, drawing)

    result = overall_dimensions(sw, _PART)
    assert result["status"] == "failed"
    assert result["error_code"] == "MEASURE_FAILED"
    assert result["retryable"] is True
    assert "view create boom" in result["error"]
    assert sw.closed == ["throwaway.SLDDRW", "block.sldprt"]


def test_overall_dimensions_close_errors_are_swallowed(monkeypatch):
    """@brief 会话查询与 CloseDoc 抛错都不允许逃逸出函数。"""
    sw = _FakeSwAppNoLookup(close_raises=True)
    drawing = _TempDrawing(_GOOD_FRONT, _GOOD_TOP)
    _patch_measurement(monkeypatch, drawing)

    result = overall_dimensions(sw, _PART)
    assert result["status"] == "pass"
    assert sw.closed == []


def test_overall_dimensions_close_part_disabled(monkeypatch):
    """@brief close_part=False 时保留新开零件, 只关闭临时图。"""
    sw = _FakeSwApp(with_lookup=False)
    drawing = _TempDrawing(_GOOD_FRONT, _GOOD_TOP)
    _patch_measurement(monkeypatch, drawing)

    result = overall_dimensions(sw, _PART, close_part=False)
    assert result["status"] == "pass"
    assert sw.closed == ["throwaway.SLDDRW"]


# ---------------------------------------------------------------------------
# bounding_box
# ---------------------------------------------------------------------------

class _BoxBody:
    def __init__(self, box=None, raises=False):
        self._box = box
        self._raises = raises

    def GetBodyBox(self):
        if self._raises:
            raise RuntimeError("bodybox boom")
        return self._box


class _BoxModel:
    def __init__(self, box=None, box_raises=False, bodies=(), bodies_raises=False):
        self._box = box
        self._box_raises = box_raises
        self._bodies = bodies
        self._bodies_raises = bodies_raises

    def GetBox(self, arg):
        if self._box_raises:
            raise RuntimeError("GetBox boom")
        return self._box

    def GetBodies2(self, code, visible):
        if self._bodies_raises:
            raise RuntimeError("bodies boom")
        return self._bodies


def test_bounding_box_requires_model():
    """@brief model 为 None 时必须显式失败而不是抛异常。"""
    result = bounding_box(None)
    assert result["status"] == "failed"
    assert result["error_code"] == "BBOX_NO_MODEL"
    assert result["manual_review_required"] is True


def test_bounding_box_short_getbox_falls_back_to_bodies():
    """@brief GetBox 返回不足 6 个值时回退到实体并集。"""
    bodies = [_BoxBody([0.0, 0.0, 0.0, 0.05, 0.05, 0.05])]
    model = _BoxModel(box=[0.0, 0.0, 0.0], bodies=bodies)

    result = bounding_box(model)
    assert result["status"] == "pass"
    assert result["method"] == "GetBodies2+GetBodyBox"
    assert result["size_mm"] == [50.0, 50.0, 50.0]


def test_bounding_box_union_skips_bad_bodies():
    """@brief 个别实体 GetBodyBox 抛错或值不足时跳过, 其余实体仍取并集。"""
    bodies = [
        _BoxBody(raises=True),
        _BoxBody([0.0, 0.0, 0.0]),
        _BoxBody([0.0, 0.0, 0.0, 0.02, 0.04, 0.06]),
        _BoxBody([0.01, -0.01, 0.01, 0.03, 0.01, 0.07]),
    ]
    model = _BoxModel(box_raises=True, bodies=bodies)

    result = bounding_box(model)
    assert result["status"] == "pass"
    assert result["method"] == "GetBodies2+GetBodyBox"
    assert result["range_mm"] == [0.0, -10.0, 0.0, 30.0, 40.0, 70.0]
    assert result["size_mm"] == [30.0, 50.0, 70.0]


def test_bounding_box_unresolvable_when_bodies_raise():
    """@brief GetBox 与 GetBodies2 同时不可解析时返回 review_required。"""
    model = _BoxModel(box_raises=True, bodies_raises=True)

    result = bounding_box(model)
    assert result["status"] == "review_required"
    assert result["error_code"] == "BBOX_UNRESOLVABLE"
    assert result["limitations"]


def test_bounding_box_unresolvable_when_all_bodies_bad():
    """@brief 所有实体都读不出包围盒时同样返回 review_required。"""
    model = _BoxModel(box_raises=True, bodies=[_BoxBody(raises=True), _BoxBody([0.0])])

    result = bounding_box(model)
    assert result["status"] == "review_required"
    assert result["error_code"] == "BBOX_UNRESOLVABLE"


def test_bounding_box_reports_parse_failure():
    """@brief 实体包围盒值无法转 float 时进入 BBOX_FAILED 兜底。"""
    model = _BoxModel(box_raises=True, bodies=[_BoxBody(["a", "b", "c", "d", "e", "f"])])

    result = bounding_box(model)
    assert result["status"] == "failed"
    assert result["error_code"] == "BBOX_FAILED"
    assert result["retryable"] is True
    assert "could not convert" in result["error"]


# ---------------------------------------------------------------------------
# count_components
# ---------------------------------------------------------------------------

class _PathComponent:
    def __init__(self, path=None, raises=False):
        self._path = path
        self._raises = raises

    def GetPathName(self):
        if self._raises:
            raise RuntimeError("path boom")
        return self._path


class _ComponentsModel:
    def __init__(self, comps=None, raises=False):
        self._comps = comps
        self._raises = raises

    def GetComponents(self, flat):
        assert isinstance(flat, bool)
        if self._raises:
            raise RuntimeError("comps boom")
        return self._comps


def test_count_components_requires_model():
    """@brief 装配体模型缺失时显式失败。"""
    result = count_components(None)
    assert result["status"] == "failed"
    assert result["error_code"] == "COUNT_NO_MODEL"


def test_count_components_handles_path_errors_and_null_lists():
    """@brief 单个组件取路径抛错记为 ?, 组件列表为 None 时判空。"""
    result = count_components(_ComponentsModel([_PathComponent(raises=True)]))
    assert result["status"] == "pass"
    assert result["counts"] == {"?": 1}
    assert result["total"] == 1

    result = count_components(_ComponentsModel(comps=None))
    assert result["status"] == "review_required"
    assert result["error_code"] == "COUNT_EMPTY"


def test_count_components_reports_failure():
    """@brief GetComponents 抛错进入 COUNT_FAILED 兜底。"""
    result = count_components(_ComponentsModel(raises=True))
    assert result["status"] == "failed"
    assert result["error_code"] == "COUNT_FAILED"
    assert result["retryable"] is True
    assert "comps boom" in result["error"]


# ---------------------------------------------------------------------------
# enumerate_features
# ---------------------------------------------------------------------------

class _DimObject:
    def __init__(self, name="D1@Sketch1", system_value=0.08, name_raises=False, value_raises=False):
        self._name = name
        self._system_value = system_value
        self._name_raises = name_raises
        self._value_raises = value_raises

    @property
    def FullName(self):
        if self._name_raises:
            raise RuntimeError("name boom")
        return self._name

    @property
    def SystemValue(self):
        if self._value_raises:
            raise RuntimeError("value boom")
        return self._system_value


class _NamelessFeature:
    """@brief 所有成员访问都会抛错的异常特征。"""

    @property
    def Name(self):
        raise RuntimeError("name boom")

    def GetTypeName2(self):
        raise RuntimeError("type boom")

    def GetDimensions(self):
        raise RuntimeError("dims boom")

    def GetNextFeature(self):
        return None


class _FeatureNode:
    def __init__(self, name="Sketch1", ftype="ProfileFeature", dims=None, nxt=None):
        self.Name = name
        self._ftype = ftype
        self._dims = dims if dims is not None else []
        self._nxt = nxt

    def GetTypeName2(self):
        return self._ftype

    def GetDimensions(self):
        return self._dims

    def GetNextFeature(self):
        return self._nxt


class _SimpleFeatureModel:
    def __init__(self, first):
        self._first = first

    def FirstFeature(self):
        return self._first


class _BrokenChainFeature:
    Name = "F1"

    def GetTypeName2(self):
        return "T"

    def GetDimensions(self):
        return []

    def GetNextFeature(self):
        raise RuntimeError("next boom")


class _ParameterFeature:
    def __init__(self, name="Pad1"):
        self.Name = name

    def GetTypeName2(self):
        return "Extrusion"

    def GetDimensions(self):
        return []          # 触发 model.Parameter 探测

    def GetNextFeature(self):
        return None


class _ParameterModel:
    """@brief 用 model.Parameter 提供驱动尺寸的假零件。"""

    def __init__(self, feature):
        self._feature = feature
        self._values = {f"D1@{feature.Name}": 0.05, f"D2@{feature.Name}": None}

    def FirstFeature(self):
        return self._feature

    def Parameter(self, name):
        if name not in self._values:
            return None
        value = self._values[name]
        if value is None:
            return None
        return _DimObject(name=name, system_value=value)


def test_enumerate_features_requires_model():
    """@brief 模型缺失时显式失败。"""
    result = enumerate_features(None)
    assert result["status"] == "failed"
    assert result["error_code"] == "FEAT_NO_MODEL"


def test_enumerate_features_without_feature_chain():
    """@brief 两个特征入口成员都不可用时安全返回空枚举。"""
    result = enumerate_features(object())
    assert result["status"] == "pass"
    assert result["count"] == 0
    assert result["truncated"] is False
    assert result["features"] == []


def test_enumerate_features_tolerates_member_errors():
    """@brief 名称/类型/尺寸成员抛错时记录为空值而不是失败。"""
    result = enumerate_features(_SimpleFeatureModel(_NamelessFeature()))
    assert result["count"] == 1
    assert result["features"][0] == {"index": 0, "name": "", "type": "", "dimensions": []}
    assert result["truncated"] is False


def test_enumerate_features_dimension_member_errors():
    """@brief 尺寸成员 FullName/SystemValue 抛错时逐项降级为空值。"""
    feature = _FeatureNode(dims=[
        _DimObject(name_raises=True),
        _DimObject(name="D2@Sketch1", value_raises=True),
    ])
    result = enumerate_features(_SimpleFeatureModel(feature))
    dims = result["features"][0]["dimensions"]
    assert dims[0] == {"name": "", "value_mm": 80.0, "system_value": 0.08}
    assert dims[1]["name"] == "D2@Sketch1"
    assert dims[1]["value_mm"] is None
    assert dims[1]["system_value"] is None


def test_enumerate_features_parameter_probe():
    """@brief GetDimensions 为空时按 D1..D8 逐个探测 model.Parameter。"""
    feature = _ParameterFeature()
    result = enumerate_features(_ParameterModel(feature))
    entry = result["features"][0]
    assert entry["dimensions"] == [
        {"name": "D1@Pad1", "value_mm": 50.0, "system_value": 0.05}
    ]
    assert result["status"] == "pass"


def test_enumerate_features_stops_when_chain_breaks():
    """@brief GetNextFeature 抛错时终止枚举且不标记 truncated。"""
    result = enumerate_features(_SimpleFeatureModel(_BrokenChainFeature()))
    assert result["status"] == "pass"
    assert result["count"] == 1
    assert result["truncated"] is False


def test_enumerate_features_reports_invalid_max_features():
    """@brief max_features 非数值时进入 FEAT_FAILED 兜底。"""
    result = enumerate_features(
        _SimpleFeatureModel(_BrokenChainFeature()), max_features="abc"
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "FEAT_FAILED"
    assert result["retryable"] is True
