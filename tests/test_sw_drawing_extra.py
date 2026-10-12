"""sw_drawing 补充覆盖测试：COM 执行路径、制造零件图编排与导入回退。

覆盖策略：
- 纯函数与证据字典用最小假 COM 对象（离线，绝不触发真实 SolidWorks）；
- generate_manufacturing_drawing 编排层用假 sw/假工程图 + monkeypatch 边界函数，
  覆盖计划校验、文档/视图失败、存盘证据、scan/model_only 双策略与 PDF 导出失败；
- 模块级 ImportError 回退、子技能 spec 缺失、声明式 __all__ 分支用 reload 覆盖。
"""
import importlib
import sys
from pathlib import Path

import pytest

import scripts.sw_drawing as sw_drawing
from scripts.sw_drawing import (
    SW_TOL_BILATERAL,
    SW_TOL_SYMMETRIC,
    _as_sequence,
    _finite_positive,
    _iter_drawing_notes,
    _plan_tolerance_resolver,
    _safe_member,
    _scale_ratio,
    _scan_dim_entry,
    _worst_status,
    add_dimension_between,
    add_text_note,
    find_edge,
    find_solid_x,
    fill_title_block,
    force_view_scale,
    gb1804m_band,
    generate_manufacturing_drawing,
    make_picktype,
    pickability_ok,
    probe_title_block_links,
    scan_view_dimensions,
    set_dimension_tolerance,
    set_drawing_document_preferences,
    setup_sheet_format,
)


# ---- 基础辅助函数（COM 兼容读取 / 序列化 / 数值防护）----

def test_safe_member_none_and_error_paths():
    assert _safe_member(None, "Anything", default="d") == "d"

    class _Boom:
        @property
        def Value(self):
            raise RuntimeError("member exploded")

    assert _safe_member(_Boom(), "Value", default="fallback") == "fallback"


def test_as_sequence_normalizes_com_returns():
    assert _as_sequence(None) == []
    assert _as_sequence((1, 2)) == [1, 2]
    assert _as_sequence(7) == [7]


def test_finite_positive_converts_or_falls_back():
    assert _finite_positive(0.42, 0.297) == (0.42, True)
    assert _finite_positive(None, 0.297) == (0.297, False)
    assert _finite_positive("abc", 0.297) == (0.297, False)
    assert _finite_positive(float("nan"), 0.297) == (0.297, False)
    assert _finite_positive(float("inf"), 0.297) == (0.297, False)
    assert _finite_positive(-1.0, 0.297) == (0.297, False)
    assert _finite_positive(0, 0.297) == (0.297, False)


def test_scale_ratio_converts_float_to_integer_ratio():
    assert _scale_ratio(0.5) == (1, 2)
    assert _scale_ratio(1.0) == (1, 1)


def test_gb1804m_band_nan_falls_back_to_symmetric_08():
    assert gb1804m_band(float("nan")) == 0.8


# ---- 尺寸公差 ----

class _FakeIDimension:
    def __init__(self):
        self.tol_type = None
        self.plus_m = None
        self.minus_m = None

    def SetToleranceType(self, tol_type):
        self.tol_type = tol_type
        return True

    def SetToleranceValues(self, plus_m, minus_m):
        self.plus_m = plus_m
        self.minus_m = minus_m
        return True


class _FakeDisplayDimension:
    def __init__(self):
        self._dim = _FakeIDimension()

    def GetDimension(self):
        return self._dim


def test_set_dimension_tolerance_reports_com_exception():
    class _ExplodingDisplayDimension:
        def GetDimension(self):
            raise RuntimeError("COM exploded")

    result = set_dimension_tolerance(_ExplodingDisplayDimension(), 45.0)
    assert result["status"] == "failed"
    assert result["error_code"] == "DRAWING_TOLERANCE_SET_FAILED"
    assert result["retryable"] is True
    assert "COM exploded" in result["error"]


# ---- 视图比例门禁 ----

def test_pickability_ok_reports_unreadable_scale():
    class _NoRatio:
        pass

    blocked = pickability_ok(_NoRatio())
    assert blocked["status"] == "blocked"
    assert blocked["scale"] is None
    assert blocked["reason"] == "无法读取视图 ScaleRatio"


def test_pickability_ok_handles_non_numeric_and_zero_denominator():
    weird = pickability_ok(type("V", (), {"ScaleRatio": ("abc", 2)})())
    assert weird["scale"] is None
    assert weird["status"] == "blocked"

    zero_den = pickability_ok(type("V", (), {"ScaleRatio": (1.0, 0)})())
    assert zero_den["scale"] is None


# ---- 强制视图比例 ----

class _PlainScaleView:
    """@brief 无 UseSheetScale/UseParentScale/ScaleRatio 成员的裸视图。"""


def test_force_view_scale_plain_view_reports_readback_failure():
    view = _PlainScaleView()
    result = force_view_scale(view, 0.5)
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_SCALE_READBACK_FAILED"
    assert result["manual_review_required"] is True
    assert view.ScaleDecimal == 0.5  # ScaleDecimal 直接落属性


class _DecimalRaisesView:
    def __init__(self, ratio):
        self.UseSheetScale = True
        self.UseParentScale = True
        self.ScaleRatio = ratio

    @property
    def ScaleDecimal(self):
        return None

    @ScaleDecimal.setter
    def ScaleDecimal(self, value):
        raise RuntimeError("ScaleDecimal unsupported")


def test_force_view_scale_falls_back_to_integer_ratio():
    view = _DecimalRaisesView((9.0, 1.0))
    result = force_view_scale(view, 0.5)
    assert result["status"] == "pass"
    assert result["verified"] is True
    assert result["scale_ratio"] == [1, 2]  # 0.5 → 1:2 整数比
    assert view.ScaleRatio == (1, 2)


class _RebuildBoom:
    @property
    def EditRebuild3(self):
        raise RuntimeError("rebuild exploded")


def test_force_view_scale_survives_rebuild_failure():
    view = _DecimalRaisesView((1.0, 2.0))
    result = force_view_scale(view, 0.5, drawing_model=_RebuildBoom())
    assert result["status"] == "pass"
    assert result["verified"] is True


class _ReadOnlySheetScale:
    @property
    def UseSheetScale(self):
        return True


def test_force_view_scale_reports_force_failure():
    result = force_view_scale(_ReadOnlySheetScale(), 0.5)
    assert result["status"] == "failed"
    assert result["error_code"] == "DRAWING_SCALE_FORCE_FAILED"
    assert result["retryable"] is True
    assert "UseSheetScale" in result["error"] or "attribute" in result["error"]


# ---- 坐标扫描纯函数边界 ----

def _always(value):
    return lambda c, axis, fixed: value


def test_find_edge_degenerate_outline_returns_none():
    assert find_edge([0.05, 0.0, 0.05, 0.08], 0, "min", 0.04, _always(2)) is None


def test_find_edge_face_everywhere_returns_boundary_center():
    outline = [0.0, 0.0, 0.1, 0.08]
    left = find_edge(outline, 0, "min", 0.04, _always(2))
    right = find_edge(outline, 0, "max", 0.04, _always(2))
    assert left == pytest.approx(0.0, abs=1e-5)   # side=min → c_empty 回退 lo
    assert right == pytest.approx(0.1, abs=1e-5)  # side=max → c_empty 回退 hi


def test_find_solid_x_degenerate_outline_returns_midpoint():
    assert find_solid_x([0.05, 0.0, 0.05, 0.08], 0.04, _always(2)) == pytest.approx(0.05)


# ---- make_picktype：类型方法降级与选择异常 ----

class _FlakyTypeManager:
    """@brief GetSelectedObjectTypeN 系列按调用次数退化的假 SelectionManager。"""

    def __init__(self):
        self.count_value = 1
        self.max5_ok = 2
        self.all_fail = False
        self.calls = {"6": 0, "5": 0, "4": 0, "3": 0}

    def GetSelectedObjectCount2(self, mark=-1):
        return self.count_value

    def GetSelectedObjectType6(self, idx, mark):
        self.calls["6"] += 1
        raise RuntimeError("type6 unsupported")

    def GetSelectedObjectType5(self, idx, mark):
        self.calls["5"] += 1
        if self.all_fail or self.calls["5"] > self.max5_ok:
            raise RuntimeError("type5 gone")
        return 2

    def GetSelectedObjectType4(self, idx, mark):
        self.calls["4"] += 1
        if self.all_fail:
            raise RuntimeError("type4 gone")
        return 1

    def GetSelectedObjectType3(self, idx, mark):
        self.calls["3"] += 1
        raise RuntimeError("type3 unsupported")


class _PickDrawing:
    def __init__(self, manager):
        self.Extension = self
        self.SelectionManager = manager

    def ClearSelection(self):
        return None

    def SelectByID2(self, *_args):
        return True


def test_make_picktype_caches_then_falls_through_type_methods():
    manager = _FlakyTypeManager()
    picktype = make_picktype(_PickDrawing(manager))

    assert picktype(0.1, 0, 0.05) == 2        # type6 失败 → type5 成功并缓存
    manager.count_value = "abc"
    assert picktype(0.1, 0, 0.05) == 0        # 选中计数不可整型化 → 0
    manager.count_value = 1
    assert picktype(0.1, 0, 0.05) == 1        # 缓存的 type5 失效 → 清缓存 → type4
    manager.all_fail = True
    assert picktype(0.1, 0, 0.05) == 0        # 全部类型方法失败 → 0
    assert manager.calls["3"] >= 1


def test_make_picktype_survives_selection_errors():
    class _BrokenPickDrawing:
        def __init__(self):
            self.Extension = self
            self.SelectionManager = self

        def ClearSelection(self):
            raise RuntimeError("clear exploded")

        def SelectByID2(self, *_args):
            raise RuntimeError("select exploded")

        def GetSelectedObjectCount2(self, mark=-1):
            return 0

    picktype = make_picktype(_BrokenPickDrawing())
    assert picktype(0.05, 0, 0.02) == 0


# ---- add_dimension_between：重试与耗尽 ----

class _RetryDimDrawing:
    def __init__(self):
        self.Extension = self
        self.add_calls = 0
        self.clear_calls = 0

    def ClearSelection(self):
        self.clear_calls += 1
        raise RuntimeError("clear flaky")

    def SelectByID2(self, *_args):
        return True

    def AddDimension2(self, x, y, z):
        self.add_calls += 1
        return None if self.add_calls == 1 else object()


def test_add_dimension_between_retries_until_dimension_appears():
    drawing = _RetryDimDrawing()
    display_dimension = add_dimension_between(drawing, (0.01, 0.02), (0.06, 0.02), 0.03, 0.0)
    assert display_dimension is not None
    assert drawing.add_calls == 2
    assert drawing.clear_calls == 3  # 每轮开始 1 次 + 成功后 1 次（均异常但被吞掉）


class _NeverDimDrawing:
    def __init__(self):
        self.Extension = self

    def ClearSelection(self):
        return None

    def SelectByID2(self, *_args):
        return True

    def AddDimension2(self, *_args):
        return None


def test_add_dimension_between_exhausts_retries():
    assert add_dimension_between(_NeverDimDrawing(), (0, 0), (1, 1), 0.5, 0.5, retries=2) is None


# ---- _scan_dim_entry：公差策略分支 ----

def test_scan_dim_entry_skips_tolerance_when_disabled():
    entry = _scan_dim_entry("W", _FakeDisplayDimension(), 45.0, apply_tolerance=False)
    assert entry["tolerance_report"] == {"status": "skipped", "reason": "apply_tolerance=False"}


def test_scan_dim_entry_uses_plan_resolver():
    display_dimension = _FakeDisplayDimension()
    calls = []

    def resolver(dim_id, nominal_mm):
        calls.append((dim_id, nominal_mm))
        return {"tol_type": SW_TOL_BILATERAL, "plus_mm": 0.2, "minus_mm": 0.1}

    entry = _scan_dim_entry("H", display_dimension, 45.0, True, tolerance_resolver=resolver)
    assert calls == [("H", 45.0)]
    assert entry["display_dimension_set"] is True
    assert entry["tolerance_report"]["status"] == "pass"
    assert entry["tolerance_report"]["tolerance_type"] == SW_TOL_BILATERAL
    assert entry["tolerance_report"]["plus_mm"] == 0.2


def test_scan_dim_entry_resolver_none_falls_back_to_gb1804m():
    entry = _scan_dim_entry(
        "W", _FakeDisplayDimension(), 45.0, True, tolerance_resolver=lambda dim_id, nominal: None
    )
    assert entry["tolerance_report"]["band_source"] == "gb1804m_m"


def test_scan_dim_entry_without_display_dimension():
    entry = _scan_dim_entry("D", None, 30.0, True)
    assert entry["display_dimension_set"] is False
    assert entry["tolerance_report"] is None


# ---- scan_view_dimensions：异常降级 ----

class _ScanSelDrawing:
    def __init__(self, clear_boom=False):
        self.Extension = self
        self.SelectionManager = self
        self.clear_boom = clear_boom

    def GetSelectedObjectCount2(self, mark=-1):
        return 0

    def GetSelectedObjectType6(self, idx, mark=-1):
        return 0

    def ClearSelection(self):
        if self.clear_boom:
            raise RuntimeError("clear exploded")

    def SelectByID2(self, *_args):
        return True


class _OutlineView:
    def __init__(self, outline):
        self._outline = outline

    def GetOutline(self):
        return self._outline


def test_scan_view_dimensions_survives_clear_selection_failure():
    report = scan_view_dimensions(
        _ScanSelDrawing(clear_boom=True),
        _OutlineView([0.0, 0.0, 0.1, 0.08]),
        _OutlineView([0.0, 0.1, 0.1, 0.18]),
        nominal_width_mm=60.0, nominal_height_mm=60.0, nominal_depth_mm=60.0,
    )
    assert report["status"] == "review_required"
    assert report["error_code"] == "DRAWING_SCAN_PARTIAL"
    assert all(entry["tolerance_report"] is None for entry in report["dimensions"])
    assert all(edge is None for pair in report["edges"].values() for edge in pair)


def test_scan_view_dimensions_reports_failure_on_bad_outline_values():
    report = scan_view_dimensions(
        _ScanSelDrawing(),
        _OutlineView(("a", "b", "c", "d")),
        _OutlineView([0.0, 0.1, 0.1, 0.18]),
        nominal_width_mm=1.0, nominal_height_mm=1.0, nominal_depth_mm=1.0,
    )
    assert report["status"] == "failed"
    assert report["error_code"] == "DRAWING_SCAN_FAILED"
    assert report["retryable"] is True


# ---- 文档偏好 ----

class _PrefModel:
    def __init__(self, result=True, boom=False):
        self.calls = []
        self.result = result
        self.boom = boom

    def SetUserPreferenceIntegerValue(self, key, value):
        if self.boom:
            raise RuntimeError("pref exploded")
        self.calls.append((key, value))
        return self.result


def test_set_drawing_document_preferences_applies_units_projection_decimals():
    model = _PrefModel()
    result = set_drawing_document_preferences(model, decimals=2)
    assert result["status"] == "pass"
    assert result["applied"] == ["units_mmgs", "first_angle", "decimals_2"]
    assert model.calls == [(263, 5), (79, 1), (49, 2)]


def test_set_drawing_document_preferences_third_angle_and_no_decimals():
    model = _PrefModel()
    result = set_drawing_document_preferences(model, projection="third_angle")
    assert result["status"] == "pass"
    assert result["projection"]["value"] is None
    assert (79, 1) not in model.calls
    assert result["decimals"]["value"] is None


def test_set_drawing_document_preferences_units_not_applied():
    model = _PrefModel(result=False)
    result = set_drawing_document_preferences(model, decimals=2)
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_PREF_UNITS_NOT_APPLIED"
    assert result["applied"] == []  # 偏好调用全部返回 False，包括小数位


def test_set_drawing_document_preferences_com_failure():
    result = set_drawing_document_preferences(_PrefModel(boom=True))
    assert result["error_code"] == "DRAWING_PREF_SET_FAILED"
    assert "pref exploded" in result["error"]


# ---- 图框套用 ----

def test_setup_sheet_format_unknown_paper_blocks_without_com():
    result = setup_sheet_format(object(), "B5", "x.slddrt")
    assert result["status"] == "blocked"
    assert result["error_code"] == "DRAWING_FORMAT_UNKNOWN_PAPER"
    assert result["retryable"] is False


def test_setup_sheet_format_missing_file_blocks_with_retry(tmp_path):
    result = setup_sheet_format(object(), "a3", tmp_path / "missing.slddrt")
    assert result["status"] == "blocked"
    assert result["error_code"] == "DRAWING_FORMAT_FILE_MISSING"
    assert result["retryable"] is True
    assert result["paper_size"] == "A3"  # 大小写规范化


class _SheetFormatSheet:
    def GetName(self):
        return "Sheet1"


class _SheetFormatModel:
    def __init__(self, result=True, boom=False):
        self.result = result
        self.boom = boom
        self.args = None

    def GetCurrentSheet(self):
        return _SheetFormatSheet()

    def SetupSheet5(self, *args):
        self.args = args
        if self.boom:
            raise RuntimeError("setup exploded")
        return self.result


def test_setup_sheet_format_applies_custom_template(tmp_path):
    sheet_format = tmp_path / "a3 - gb.slddrt"
    sheet_format.write_text("gb", encoding="utf-8")
    model = _SheetFormatModel()
    result = setup_sheet_format(model, "a3", sheet_format)
    assert result["status"] == "pass"
    assert result["configured"] is True
    assert result["paper_code"] == 6
    assert result["sheet_name"] == "Sheet1"
    assert model.args[0] == "Sheet1"
    assert model.args[1] == 6
    assert model.args[2] == 12  # swDwgTemplateCustom
    assert model.args[6] == str(sheet_format)


def test_setup_sheet_format_setup_rejected(tmp_path):
    sheet_format = tmp_path / "a3.slddrt"
    sheet_format.write_text("x", encoding="utf-8")
    result = setup_sheet_format(_SheetFormatModel(result=False), "A3", sheet_format)
    assert result["status"] == "failed"
    assert result["error_code"] == "DRAWING_FORMAT_SETUP_FAILED"


def test_setup_sheet_format_setup_explodes(tmp_path):
    sheet_format = tmp_path / "a3.slddrt"
    sheet_format.write_text("x", encoding="utf-8")
    result = setup_sheet_format(_SheetFormatModel(boom=True), "A3", sheet_format)
    assert result["status"] == "failed"
    assert result["error_code"] == "DRAWING_FORMAT_SETUP_FAILED"
    assert "setup exploded" in result["error"]


# ---- 文字注释 ----

class _NoteWithReadback:
    def GetText(self):
        return "注释文本"

    def GetX(self):
        return 0.045

    def GetY(self):
        return 0.262


class _NoteModel:
    _AUTO = object()

    def __init__(self, note=_AUTO, boom=False):
        self.note = note
        self.boom = boom
        self.args = None

    def CreateText2(self, *args):
        self.args = args
        if self.boom:
            raise RuntimeError("note exploded")
        return _NoteWithReadback() if self.note is _NoteModel._AUTO else self.note


def test_add_text_note_success_and_readback():
    model = _NoteModel()
    result = add_text_note(model, "技术要求:去毛刺", 0.02, 0.25, height=0.004)
    assert result["status"] == "pass"
    assert result["created"] is True
    assert result["readback"]["text"] == "注释文本"
    assert model.args == ("技术要求:去毛刺", 0.02, 0.25, 0.0, 0.004, 0.0)


def test_add_text_note_create_returns_none():
    result = add_text_note(_NoteModel(note=None), "x", 0, 0)
    assert result["status"] == "failed"
    assert result["error_code"] == "DRAWING_NOTE_CREATE_FAILED"
    assert result["created"] is False


def test_add_text_note_com_failure():
    result = add_text_note(_NoteModel(boom=True), "x", 0, 0)
    assert result["error_code"] == "DRAWING_NOTE_CREATE_FAILED"
    assert "note exploded" in result["error"]


# ---- 注释遍历与标题栏联动探测 ----

class _ProbeNote:
    def __init__(self, text):
        self.Text = text


class _ProbeView:
    def __init__(self, name, notes=(), nxt=None):
        self.Name = name
        self._notes = list(notes)
        self._nxt = nxt

    def GetNotes(self):
        return self._notes

    def GetNextView(self):
        return self._nxt


class _ProbeModel:
    def __init__(self, first):
        self._first = first

    def GetFirstView(self):
        return self._first


def test_iter_drawing_notes_walks_view_chain():
    second = _ProbeView("V2", [_ProbeNote("备注")])
    first = _ProbeView("V1", [_ProbeNote('$PRP:"图号"')], nxt=second)
    pairs = _iter_drawing_notes(_ProbeModel(first))
    assert [(name, note.Text) for name, note in pairs] == [
        ("V1", '$PRP:"图号"'),
        ("V2", "备注"),
    ]


def test_iter_drawing_notes_stops_on_self_loop():
    view = _ProbeView("Sheet1", [])
    view._nxt = view  # GetNextView 返回自身 → 终止
    assert _iter_drawing_notes(_ProbeModel(view)) == []


def test_iter_drawing_notes_without_first_view_returns_empty():
    assert _iter_drawing_notes(_ProbeModel(None)) == []
    result = probe_title_block_links(_ProbeModel(None))
    assert result["linked"] is False
    assert result["note_count"] == 0


def test_probe_title_block_links_extracts_prp_fields():
    model = _ProbeModel(_ProbeView("图纸1", [
        _ProbeNote('$PRP:"图号"'),
        _ProbeNote(""),              # 空文本跳过
        _ProbeNote("$PRPSHEET:零件号"),
        _ProbeNote('$PRP:"图号"'),   # 重复字段去重
    ]))
    result = probe_title_block_links(model)
    assert result["status"] == "pass"
    assert result["linked"] is True
    assert result["linked_fields"] == ["图号", "零件号"]
    assert result["note_count"] == 3


# ---- 标题栏填充 ----

class _TitleSheet:
    def __init__(self, height=0.297):
        self.Height = height

    def GetName(self):
        return "Sheet1"


class _TitleProps:
    def __init__(self, add_result=True, set_result=False, boom=False):
        self.add_result = add_result
        self.set_result = set_result
        self.boom = boom
        self.added = []

    def Add3(self, name, data_type, value, overwrite):
        if self.boom:
            raise RuntimeError("Add3 exploded")
        self.added.append((name, data_type, value, overwrite))
        return self.add_result

    def Set2(self, name, value):
        return self.set_result


class _TitleModel:
    """@brief fill_title_block 专用假工程图：联动探测 + 属性管理器 + CreateText2。"""

    def __init__(self, *, prp_note=True, manager="ok", height=0.297, with_extension=True):
        if with_extension:
            self.Extension = self
        self.sheet = _TitleSheet(height)
        self.first_view = _ProbeView("图纸格式1", [_ProbeNote('$PRP:"图号"')] if prp_note else [])
        self.props = None
        if manager is not None:
            self.props = _TitleProps(
                add_result=(manager == "ok"),
                set_result=(manager == "set"),
                boom=(manager == "boom"),
            )
        self.text_args = None

    def GetFirstView(self):
        return self.first_view

    def GetCurrentSheet(self):
        return self.sheet

    def CustomPropertyManager(self, _name):
        return self.props

    def CreateText2(self, *args):
        self.text_args = args
        return _NoteWithReadback()


def test_fill_title_block_requires_something_to_write():
    result = fill_title_block(object(), {})
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_TITLE_BLOCK_NO_FIELDS"


def test_fill_title_block_writes_custom_properties():
    model = _TitleModel()
    result = fill_title_block(model, {"图号": "GB-001", "备注": None})
    assert result["status"] == "pass"
    assert result["method_used"] == "custom_properties"
    assert result["fields_written"] == ["图号"]  # None 值字段被过滤
    assert model.props.added == [("图号", 30, "GB-001", 1)]


def test_fill_title_block_merges_tech_lines_into_single_note():
    model = _TitleModel()
    result = fill_title_block(model, {"图号": "GB-001"}, tech_lines=["  ", "去毛刺"])
    assert result["status"] == "pass"
    assert result["method_used"] == "custom_properties"
    assert result["tech_lines"] == ["去毛刺"]  # 空白行被过滤
    assert result["note_evidence"]["status"] == "pass"
    assert model.text_args[0] == "技术要求:去毛刺"


def test_fill_title_block_falls_back_when_property_write_rejected():
    model = _TitleModel(manager="fail")
    result = fill_title_block(model, {"图号": "GB-001"})
    assert result["error_code"] == "DRAWING_TITLE_BLOCK_PROPERTY_WRITE_FAILED"
    assert result["method_used"] == "text_note_fallback"
    assert result["fields_written"] == ["图号"]
    assert result["status"] == "pass"
    assert model.text_args[0] == "图号:GB-001"


def test_fill_title_block_records_property_write_exception():
    """@brief Add3/Set2 经 _safe_member 调用异常被吞为 False；value.__str__ 爆炸
    才能抵达 try/except 记录分支，随后注释兜底的 f-string 会再次触发同一异常。"""

    class _BrokenStr:
        def __str__(self):
            raise RuntimeError("str exploded")

    model = _TitleModel()
    with pytest.raises(RuntimeError, match="str exploded"):
        fill_title_block(model, {"图号": _BrokenStr()})


def test_fill_title_block_falls_back_when_property_manager_missing():
    """@brief 拿不到 CustomPropertyManager 时直接退回唯一单行注释兜底。"""
    model = _TitleModel(manager=None, with_extension=False)
    result = fill_title_block(model, {"图号": "GB-001"})
    assert result["probe"]["linked"] is True
    assert result["method_used"] == "text_note_fallback"
    assert result["fields_written"] == ["图号"]
    assert result["status"] == "pass"
    assert model.text_args[0] == "图号:GB-001"


def test_fill_title_block_falls_back_without_linked_probe():
    model = _TitleModel(prp_note=False)
    result = fill_title_block(model, {"材料": "Q235"}, tech_lines=["去毛刺"])
    assert result["probe"]["linked"] is False
    assert result["method_used"] == "text_note_fallback"
    assert result["status"] == "pass"
    assert model.text_args[0] == "材料:Q235   技术要求:去毛刺"


def test_fill_title_block_tech_only_uses_height_fallback():
    model = _TitleModel(prp_note=False, height=None, with_extension=False)
    result = fill_title_block(model, {}, tech_lines=["去毛刺"])
    assert result["method_used"] == "text_note_fallback"
    assert result["fields_written"] == []
    assert result["status"] == "pass"
    assert model.text_args[0] == "技术要求:去毛刺"
    # Height 回读失败 → A3 高度兜底 0.297，注释 y = 0.297 - 0.035
    assert abs(model.text_args[2] - 0.262) < 1e-12


# ---- 计划公差解析器与状态汇总 ----

def test_plan_tolerance_resolver_resolves_explicit_and_bilateral_entries():
    plan = {
        "dimensioning": {
            "overall_dimensions": [
                {"id": "W", "tolerance": {"source": "explicit", "plus_mm": 0.2, "minus_mm": 0.1}},
                {"id": "H", "tolerance": {"type": "bilateral", "plus_mm": 0.05}},
                {"id": "T", "tolerance": {}},
            ]
        }
    }
    resolver = _plan_tolerance_resolver(plan)
    assert resolver("W", 60.0) == {"tol_type": SW_TOL_SYMMETRIC, "plus_mm": 0.2, "minus_mm": 0.1}
    bilateral = resolver("H", 40.0)
    assert bilateral["tol_type"] == SW_TOL_BILATERAL
    assert bilateral["plus_mm"] == 0.05
    assert resolver("T", 30.0) is None     # 无显式公差 → 走默认分档
    assert resolver("D", 30.0) is None     # 计划缺失 → 走默认分档


def test_plan_tolerance_resolver_without_dimensioning_section():
    resolver = _plan_tolerance_resolver({})
    assert resolver("W", 60.0) is None


def test_worst_status_picks_most_severe_stage():
    assert _worst_status(["pass"]) == "pass"
    assert _worst_status(["pass", "review_required"]) == "review_required"
    assert _worst_status(["pass", "review_required", "blocked"]) == "blocked"
    assert _worst_status(["blocked", "failed"]) == "failed"
    assert _worst_status(["pass", "未知状态"]) == "未知状态"  # 未知状态按最高严重级处理
    assert _worst_status([]) == "pass"


# ---- generate_manufacturing_drawing 编排层 ----

def _write_plan_files(tmp_path, *, template=True, sheet_format=True):
    part = tmp_path / "bracket.sldprt"
    part.write_text("part", encoding="utf-8")
    paths = {"part": part}
    if template:
        dot = tmp_path / "gb-a3.drwdot"
        dot.write_text("drwdot", encoding="utf-8")
        paths["template"] = dot
    if sheet_format:
        fmt = tmp_path / "a3 - gb.slddrt"
        fmt.write_text("slddrt", encoding="utf-8")
        paths["sheet_format"] = fmt
    return paths


def _build_plan(paths, *, strategy=None, title_fields=True, tech=False):
    plan = {
        "part": {
            "path": str(paths["part"]),
            "nominal": {"width_mm": 60.0, "height_mm": 40.0, "depth_mm": 25.0},
        },
        "views": [
            {"name": "*Front", "center": [0.10, 0.10]},
            {"name": "*Top", "center": [0.10, 0.22]},
            {"name": "*Right", "center": [0.24, 0.10]},
        ],
        "frame": {"paper_size": "A3"},
        "scale": 1.0,
    }
    if "template" in paths:
        plan["frame"]["template_path"] = str(paths["template"])
    if "sheet_format" in paths:
        plan["frame"]["sheet_format_path"] = str(paths["sheet_format"])
    if strategy:
        plan["dimensioning"] = {"strategy": strategy}
    if title_fields:
        plan["title_block"] = {"fields": {"图号": "GB-001"}}
    if tech:
        plan["technical_requirements"] = {"lines": ["去毛刺"]}
    return plan


class _ExecNote:
    Text = '$PRP:"图号"'


class _ExecFrameView:
    Name = "图纸格式1"

    def GetNotes(self):
        return [_ExecNote()]

    def GetNextView(self):
        return None


class _ExecSheet:
    Height = 0.297

    def GetName(self):
        return "Sheet1"

    def GetViews(self):
        return []


class _ExecView:
    def __init__(self, name, scale_ratio=(1.0, 1.0)):
        self.Name = name
        self.UseSheetScale = True
        self.UseParentScale = True
        self.ScaleRatio = scale_ratio
        self._outline = (0.05, 0.05, 0.15, 0.15)

    def GetOutline(self):
        return self._outline


class _ExecProps:
    def __init__(self, add_result=True):
        self.add_result = add_result
        self.added = []

    def Add3(self, name, data_type, value, overwrite):
        self.added.append((name, data_type, value, overwrite))
        return self.add_result

    def Set2(self, name, value):
        return False


class _ExecDrawing:
    """@brief 编排层假工程图：建视图/偏好/图框/存盘/属性/模型尺寸全可打桩。"""

    def __init__(self, *, with_insert=True, save_writes=True, view_ratio=(1.0, 1.0)):
        self.Extension = self
        self.SelectionManager = self
        self.sheet = _ExecSheet()
        self.save_writes = save_writes
        self.view_ratio = view_ratio
        self.views = []
        self.props = _ExecProps()
        if with_insert:
            self.InsertModelAnnotations3 = lambda *args: True

    def GetCurrentSheet(self):
        return self.sheet

    def GetFirstView(self):
        return _ExecFrameView()

    def CustomPropertyManager(self, _name):
        return self.props

    def SetUserPreferenceIntegerValue(self, _key, _value):
        return True

    def SetupSheet5(self, *_args):
        return True

    def CreateDrawViewFromModelView3(self, _path, name, _x, _y, _scale):
        view = _ExecView(name, self.view_ratio)
        self.views.append(view)
        return view

    def EditRebuild3(self):
        return True

    def SaveAs3(self, path, _flag, _option):
        if self.save_writes:
            Path(path).write_bytes(b"drawing-data")

    def GetTitle(self):
        return "bracket.SLDDRW"

    def ClearSelection(self):
        return None

    def SelectByID2(self, *_args):
        return True

    def GetSelectedObjectCount2(self, _mark=-1):
        return 0


class _NoViewDrawing(_ExecDrawing):
    def CreateDrawViewFromModelView3(self, *_args):
        return None


class _ExecSw:
    """@brief 不暴露 ActivateDoc3 的假应用（激活异常被编排层吞掉）。"""

    def __init__(self, model=None):
        self._model = model
        self.documents = []

    def NewDocument(self, _template, _paper_size, _width, _height):
        model = self._model if self._model is not None else _ExecDrawing()
        self.documents.append(model)
        return model


class _ActivateSw:
    """@brief 暴露 ActivateDoc3 的假应用（成功激活路径）。"""

    def ActivateDoc3(self, _title, _set_current, _option, errors):
        if hasattr(errors, "value"):
            errors.value = 0
        return 1


def _patch_pdf(monkeypatch, ok=True):
    monkeypatch.setattr(
        sw_drawing,
        "export_sheet_to_pdf",
        lambda model, output_path, sheet_names=None, sw_app=None: ok,
    )


def _patch_open(monkeypatch, model=object()):
    monkeypatch.setattr(sw_drawing, "open_document", lambda *_args, **_kwargs: model)


def test_generate_manufacturing_drawing_rejects_invalid_plans(tmp_path):
    assert generate_manufacturing_drawing(object(), None, tmp_path)["error_code"] == \
        "DRAWING_EXEC_PLAN_INVALID"
    thin_plan = {"part": {"path": "p"}, "views": [{"name": "a"}, {"name": "b"}]}
    result = generate_manufacturing_drawing(object(), thin_plan, tmp_path)
    assert result["error_code"] == "DRAWING_EXEC_PLAN_INVALID"
    assert result["stages"] == {}


def test_generate_manufacturing_drawing_reports_open_and_new_document_failures(tmp_path, monkeypatch):
    _patch_open(monkeypatch, model=None)
    monkeypatch.setattr(sw_drawing, "new_document", lambda *_args, **_kwargs: None)
    plan = _build_plan(_write_plan_files(tmp_path, template=False, sheet_format=False))
    result = generate_manufacturing_drawing(object(), plan, tmp_path / "out")
    assert result["error_code"] == "DRAWING_EXEC_NEW_DOCUMENT_FAILED"
    assert result["stages"]["open_part"]["status"] == "review_required"
    assert result["stages"]["new_document"] == {"status": "failed", "template_path": None}
    assert result["artifacts"] == {}


def test_generate_manufacturing_drawing_reports_view_creation_failure(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path)
    sw = _ExecSw(model=_NoViewDrawing())
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch)
    result = generate_manufacturing_drawing(sw, _build_plan(paths, strategy="model_only"), tmp_path / "out")
    assert result["error_code"] == "DRAWING_EXEC_VIEW_CREATE_FAILED"
    assert result["stages"]["create_views"] == {"status": "failed", "failed_view": "*Front"}
    assert result["artifacts"] == {}


def test_generate_manufacturing_drawing_full_pass_downgrades_to_review(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path)
    sw = _ExecSw()
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch, ok=True)
    plan = _build_plan(paths, strategy="model_only", title_fields=True)

    result = generate_manufacturing_drawing(sw, plan, tmp_path / "out")

    # 执行层永不自动 pass 交付：全阶段 pass 仍降级为 review_required。
    assert result["status"] == "review_required"
    assert result["error_code"] is None
    assert result["stages"]["new_document"]["template_path"] == str(paths["template"])
    assert result["stages"]["sheet_format"]["status"] == "pass"
    assert result["stages"]["preferences"]["status"] == "pass"
    assert result["stages"]["create_views"]["status"] == "pass"
    assert result["stages"]["commit"]["status"] == "pass"
    assert result["stages"]["commit"]["evidence"] == "file_on_disk"
    assert result["stages"]["dimensioning"]["strategy"] == "model_only"
    assert result["stages"]["dimensioning"]["status"] == "pass"
    assert result["stages"]["title_block"]["status"] == "pass"
    assert result["stages"]["technical_requirements"]["merged_into_single_note"] is True
    assert result["stages"]["export_pdf"]["status"] == "pass"
    drawing_model = sw.documents[0]
    assert all(view.UseSheetScale is False for view in drawing_model.views)
    assert drawing_model.props.added == [("图号", 30, "GB-001", 1)]
    assert Path(result["artifacts"]["slddrw"]).is_file()
    assert result["artifacts"]["pdf"] == str(tmp_path / "out" / "bracket.pdf")


def test_generate_manufacturing_drawing_scan_strategy_with_format_candidates(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path, template=False, sheet_format=False)
    drawing_model = _ExecDrawing()
    monkeypatch.setattr(sw_drawing, "new_document", lambda *_args, **_kwargs: drawing_model)
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch, ok=True)

    plan = _build_plan(paths, title_fields=False)  # 默认 scan 策略
    # 非空候选但文件不存在 → 无可用图框，仍需显式告警而非静默跳过。
    result = generate_manufacturing_drawing(
        _ActivateSw(), plan, tmp_path / "out",
        sheet_format_candidates=[tmp_path / "missing-a3.slddrt"],
    )

    assert result["stages"]["select_format"]["status"] == "blocked"
    assert result["stages"]["sheet_format"]["status"] == "review_required"
    assert result["stages"]["sheet_format"]["error_code"] == "DRAWING_FORMAT_NOT_SPECIFIED"
    assert result["stages"]["dimensioning"]["strategy"] == "scan"
    scan_report = result["stages"]["dimensioning"]["report"]
    assert scan_report["status"] == "review_required"
    assert scan_report["error_code"] == "DRAWING_SCAN_PARTIAL"
    assert result["stages"]["commit"]["saved"] is True
    assert result["stages"]["title_block"]["status"] == "review_required"
    # 图框候选全被拒（blocked）比 review_required 更严重，成为整体状态与错误码。
    assert result["status"] == "blocked"
    assert result["error_code"] == "DRAWING_GBT_TEMPLATE_MISSING"


def test_generate_manufacturing_drawing_model_only_without_dimensions(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path, template=False, sheet_format=False)
    drawing_model = _ExecDrawing(with_insert=False, save_writes=False)
    monkeypatch.setattr(sw_drawing, "new_document", lambda *_args, **_kwargs: drawing_model)
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch, ok=True)

    plan = _build_plan(paths, strategy="model_only", title_fields=False)
    result = generate_manufacturing_drawing(_ActivateSw(), plan, tmp_path / "out")

    dimensioning = result["stages"]["dimensioning"]
    assert dimensioning["status"] == "review_required"
    assert dimensioning["inserted_count"] == 0
    assert dimensioning["error_code"] == "DRAWING_MODEL_DIMENSIONS_EMPTY"
    assert result["stages"]["commit"]["status"] == "review_required"
    assert result["stages"]["commit"]["evidence"] == "file_missing"
    assert result["status"] == "review_required"


def test_generate_manufacturing_drawing_pdf_export_failure_sets_error_code(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path, template=False, sheet_format=False)
    drawing_model = _ExecDrawing()
    monkeypatch.setattr(sw_drawing, "new_document", lambda *_args, **_kwargs: drawing_model)
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch, ok=False)

    plan = _build_plan(paths, strategy="model_only", title_fields=False)
    result = generate_manufacturing_drawing(_ActivateSw(), plan, tmp_path / "out")

    assert result["stages"]["export_pdf"]["status"] == "failed"
    assert result["stages"]["export_pdf"]["error_code"] == "DRAWING_EXEC_PDF_EXPORT_FAILED"
    assert result["artifacts"]["pdf"] is None
    assert result["error_code"] == "DRAWING_EXEC_PDF_EXPORT_FAILED"
    assert result["status"] == "failed"


def test_generate_manufacturing_drawing_tolerates_non_dict_stage_result(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path)
    sw = _ExecSw()
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch, ok=True)
    monkeypatch.setattr(sw_drawing, "set_drawing_document_preferences", lambda _model, **_kw: "bogus")

    plan = _build_plan(paths, strategy="model_only")
    result = generate_manufacturing_drawing(sw, plan, tmp_path / "out")

    assert result["stages"]["preferences"] == "bogus"  # 非字典阶段结果按 failed 处理
    assert result["status"] == "failed"
    assert result["error_code"] is None


def test_generate_manufacturing_drawing_failed_stage_without_error_code(tmp_path, monkeypatch):
    paths = _write_plan_files(tmp_path)
    sw = _ExecSw()
    _patch_open(monkeypatch)
    _patch_pdf(monkeypatch, ok=True)
    monkeypatch.setattr(
        sw_drawing, "set_drawing_document_preferences", lambda _model, **_kw: {"status": "failed"}
    )

    plan = _build_plan(paths, strategy="model_only")
    result = generate_manufacturing_drawing(sw, plan, tmp_path / "out")

    assert result["stages"]["preferences"]["status"] == "failed"
    assert result["status"] == "failed"
    assert result["error_code"] is None  # 失败阶段未携带 error_code


def test_generate_manufacturing_drawing_wraps_unexpected_exceptions(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    paths = _write_plan_files(tmp_path, template=False, sheet_format=False)
    plan = _build_plan(paths, strategy="model_only")

    result = generate_manufacturing_drawing(object(), plan, blocker)  # out_dir 是文件 → mkdir 抛错

    assert result["error_code"] == "DRAWING_EXEC_FAILED"
    assert "not-a-dir" in result["error"] or "FileExistsError" in result["error"] or result["error"]


# ---- 模块级导入回退 / 子技能加载分支（放最后，避免影响其它用例的模块状态）----

def test_sw_drawing_falls_back_to_top_level_imports():
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
        importlib.reload(sw_drawing)
        assert callable(sw_drawing.set_dimension_tolerance)
        assert sw_drawing.PAPER_SIZES["A3"]["code"] == 6
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        importlib.reload(sw_drawing)


def test_sw_drawing_raises_when_subskill_spec_missing():
    """@brief 子技能实现文件缺失时导入失败并给出可诊断错误。"""
    real_spec_fn = importlib.util.spec_from_file_location

    def _no_spec(name, location, *args, **kwargs):
        if name == "solidworks_drawing_workflow":
            return None
        return real_spec_fn(name, location, *args, **kwargs)

    importlib.util.spec_from_file_location = _no_spec
    try:
        with pytest.raises(ImportError, match="无法加载工程图子技能实现"):
            importlib.reload(sw_drawing)
    finally:
        importlib.util.spec_from_file_location = real_spec_fn
        importlib.reload(sw_drawing)


def test_sw_drawing_honors_declared_all_from_implementation():
    """@brief 子技能声明 __all__ 时按声明白名单再导出。"""
    real_spec_fn = importlib.util.spec_from_file_location
    implementation = (
        Path(sw_drawing.__file__).resolve().parents[1]
        / "subskills"
        / "solidworks-engineering-drawing"
        / "scripts"
        / "drawing_workflow.py"
    )

    class _ProxyLoader:
        def __init__(self, inner):
            self._inner = inner

        def create_module(self, spec):
            return self._inner.create_module(spec)

        def exec_module(self, module):
            self._inner.exec_module(module)
            module.__all__ = ["plan_standard_view_layout"]

    class _ProxySpec:
        def __init__(self, inner_spec):
            self._inner = inner_spec
            self.loader = _ProxyLoader(inner_spec.loader)

        def __getattr__(self, item):
            return getattr(self._inner, item)

    def _patched(name, location, *args, **kwargs):
        spec = real_spec_fn(name, location, *args, **kwargs)
        if name == "solidworks_drawing_workflow" and spec is not None:
            return _ProxySpec(spec)
        return spec

    importlib.util.spec_from_file_location = _patched
    try:
        importlib.reload(sw_drawing)
        declared_all = list(sw_drawing.__all__)
        reexported = callable(getattr(sw_drawing, "plan_standard_view_layout", None))
    finally:
        importlib.util.spec_from_file_location = real_spec_fn
        importlib.reload(sw_drawing)

    assert declared_all == ["plan_standard_view_layout"]
    assert reexported is True
    # 恢复后回到"未声明 __all__ → 全量再导出"模式。
    assert "math" in sw_drawing.__all__
    assert callable(sw_drawing.set_dimension_tolerance)
