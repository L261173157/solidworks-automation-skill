"""sw_selection 全分支补充测试 (伪对象, 无真实 COM/SolidWorks)。

覆盖: SelectionSpec 校验、拓扑枚举/元数据/评分、全部选择形态的
成功/失败/降级路径, 以及配合对选择的计数证据。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import sw_selection
from scripts.sw_entity_reference import geometry_signature
from scripts.sw_selection import (
    SelectionSpec,
    _candidate_metadata,
    _enumerate_topology,
    resolve_pair_for_mate,
    resolve_selection,
)


# --- 桩对象体系 ---


class _Vertex:
    """@brief 顶点桩 (仅 GetPoint)。"""

    def __init__(self, point):
        self._point = point

    def GetPoint(self):
        return self._point


_MISSING = object()


class _TopologyEntity:
    """@brief 可配置面积/中点/顶点/选择结果的拓扑候选桩。

    传 :data:`_MISSING` (默认) 表示成员不存在 (访问抛错); 传 None 表示
    成员存在但返回空值。
    """

    def __init__(
        self,
        *,
        area=None,
        center=None,
        start=_MISSING,
        end=_MISSING,
        point_at=_MISSING,
        select_result=True,
        fail_area=False,
    ):
        self._area = area
        self._center = center
        self._start = start
        self._end = end
        self._point_at = point_at
        self._select_result = select_result
        self._fail_area = fail_area
        self.select_calls = []

    def GetArea(self):
        if self._fail_area:
            raise RuntimeError("面积读取失败")
        return self._area

    def GetPointAtPoint(self):
        if self._point_at is _MISSING:
            raise RuntimeError("成员缺失")
        return self._point_at

    def GetCenterPoint(self):
        if self._center is _MISSING:
            raise RuntimeError("成员缺失")
        return self._center

    def GetStartVertex(self):
        if self._start is _MISSING:
            raise RuntimeError("成员缺失")
        return self._start

    def GetEndVertex(self):
        if self._end is _MISSING:
            raise RuntimeError("成员缺失")
        return self._end

    def Select2(self, append, mark):
        self.select_calls.append((append, mark))
        return self._select_result


class _Body:
    """@brief 实体桩 (仅 GetFaces)。"""

    def __init__(self, faces):
        self._faces = tuple(faces)

    def GetFaces(self):
        return self._faces


class _BodiesModel:
    """@brief 提供 Extension 与 GetBodies2 的最小模型桩。"""

    def __init__(self, bodies):
        self._bodies = tuple(bodies)
        self.Extension = object()

    def GetBodies2(self, _flag, _created):
        return self._bodies


class _StaticProbeBody:
    """@brief 首次 GetFaces 访问失败的桩 (模拟静态代理尚未动态化)。"""

    def __init__(self, faces):
        self._faces = tuple(faces)
        self._probed = False

    def __getattribute__(self, name):
        if name == "GetFaces" and not object.__getattribute__(self, "_probed"):
            object.__setattr__(self, "_probed", True)
            raise AttributeError("静态代理")
        return object.__getattribute__(self, name)

    def GetFaces(self):
        return self._faces


class _SelectExtension:
    """@brief SelectByID2 按预置结果序列应答的 Extension 桩。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def SelectByID2(self, name, entity_type, x, y, z, append, mark, callout, options):
        self.calls.append((name, entity_type, append, mark))
        if not self._results:
            return False
        return self._results.pop(0)


class _Segment:
    """@brief 草图段桩 (仅 Select2)。"""

    def __init__(self, select_result=True):
        self.select_result = select_result
        self.select_calls = []

    def Select2(self, append, mark):
        self.select_calls.append((append, mark))
        return self.select_result


# --- SelectionSpec 校验与导入回退 ---


def test_spec_rejects_unknown_kind():
    """@brief 直接构造时未知形态抛 ValueError。"""
    with pytest.raises(ValueError, match="未知选择形态"):
        SelectionSpec(kind="teleport")


def test_spec_rejects_negative_nth():
    """@brief nth 为负抛 ValueError。"""
    with pytest.raises(ValueError, match="nth 必须 >= 0"):
        SelectionSpec(kind="named", nth=-1)


def test_coerce_rejects_non_sequence_point_mm():
    """@brief 字典形态 point_mm 不是序列时抛 ValueError。"""
    with pytest.raises(ValueError, match="三元组"):
        SelectionSpec.coerce({"kind": "coordinate", "point_mm": 5})


def test_coerce_rejects_unrecognized_input():
    """@brief 字符串/字典/SelectionSpec 以外的输入抛 ValueError。"""
    with pytest.raises(ValueError, match="无法解释选择描述"):
        SelectionSpec.coerce(42)


def test_direct_import_falls_back_to_flat_modules(monkeypatch):
    """@brief 顶层导入走 sw_connect/sw_entity_reference 平铺回退分支。"""
    scripts_dir = Path(sw_selection.__file__).resolve().parent
    monkeypatch.syspath_prepend(str(scripts_dir))
    for name in ("sw_selection", "sw_connect", "sw_entity_reference"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    flat = importlib.import_module("sw_selection")
    assert flat.__name__ == "sw_selection"
    assert callable(flat.resolve_selection)
    assert flat.SelectionSpec(kind="named").name is None


# --- 候选元数据提取 ---


def test_candidate_metadata_prefers_point_at_point():
    """@brief GetPointAtPoint 优先于 GetCenterPoint。"""
    entity = _TopologyEntity(point_at=(0.1, 0.2, 0.3), center=(0.9, 0.9, 0.9))
    assert _candidate_metadata(entity)["midpoint"] == [0.1, 0.2, 0.3]


def test_candidate_metadata_uses_center_point():
    """@brief 无 GetPointAtPoint 时退回 GetCenterPoint。"""
    entity = _TopologyEntity(center=(0.1, 0.2, 0.3))
    assert _candidate_metadata(entity)["midpoint"] == [0.1, 0.2, 0.3]


def test_candidate_metadata_area_rounding_and_failure():
    """@brief 面积四舍五入; 读取失败仅跳过该字段。"""
    entity = _TopologyEntity(area=0.1234565)
    assert _candidate_metadata(entity)["area"] == round(0.1234565, 6)

    broken = _TopologyEntity(area=None, fail_area=True)
    metadata = _candidate_metadata(broken)
    assert "area" not in metadata


def test_candidate_metadata_midpoint_from_end_vertices():
    """@brief 无中点成员时用起止顶点均值近似。"""
    entity = _TopologyEntity(start=_Vertex((0.0, 0.0, 0.0)), end=_Vertex((0.2, 0.4, 0.6)))
    assert _candidate_metadata(entity)["midpoint"] == [0.1, 0.2, 0.3]


def test_candidate_metadata_skips_non_iterable_point():
    """@brief 中点成员返回不可迭代值时丢弃 midpoint 字段。"""
    entity = _TopologyEntity(center=3.14)
    metadata = _candidate_metadata(entity)
    assert "midpoint" not in metadata
    assert "area" not in metadata


def test_candidate_metadata_tolerates_missing_members():
    """@brief 全部成员缺失时元数据为空。"""
    assert _candidate_metadata(_TopologyEntity(area=None)) == {}


def test_candidate_metadata_skips_when_vertex_value_missing():
    """@brief 起止顶点存在但返回空值时不计算中点。"""
    entity = _TopologyEntity(start=None, end=None)
    metadata = _candidate_metadata(entity)
    assert "midpoint" not in metadata


# --- 拓扑枚举 ---


def test_enumerate_topology_retuples_static_bodies():
    """@brief 首元素尚为静态代理时重新收拢实体元组。"""
    face = _TopologyEntity(area=1.0)
    model = _BodiesModel([_StaticProbeBody([face])])
    assert _enumerate_topology(model, "face") == [face]


def test_enumerate_topology_unknown_kind_returns_empty():
    """@brief 未知形态无枚举器, 直接返回空列表。"""
    assert _enumerate_topology(_BodiesModel([]), "coordinate") == []


def test_enumerate_topology_collects_faces_from_all_bodies():
    """@brief 汇总多实体面并跳过空实体。"""
    face_a = _TopologyEntity(area=1.0)
    face_b = _TopologyEntity(area=2.0)
    model = _BodiesModel([_Body([face_a]), _Body([]), _Body([face_b])])
    assert _enumerate_topology(model, "face") == [face_a, face_b]


# --- 命名/基准面选择 ---


def test_named_with_entity_type_override():
    """@brief 显式 entity_type 覆盖按 kind 推断的结果。"""
    extension = _SelectExtension([True])
    model = SimpleNamespace(Extension=extension)
    _handle, evidence = resolve_selection(
        model, {"kind": "named", "name": "Front Plane", "entity_type": "PLANE"}
    )
    assert evidence["status"] == "resolved"
    assert evidence["entity_type"] == "PLANE"
    assert extension.calls == [("Front Plane", "PLANE", False, 1)]


def test_named_select_failure_reports_not_found():
    """@brief 名称选择失败返回 not_found 证据。"""
    extension = _SelectExtension([])
    model = SimpleNamespace(Extension=extension)
    _handle, evidence = resolve_selection(model, {"kind": "named", "name": "Edge5"})
    assert evidence["status"] == "not_found"
    assert evidence["matched"] == "Edge5"
    assert evidence["entity_type"] == "EDGE"  # 无 hint 时缺省
    assert extension.calls == [("Edge5", "EDGE", False, 1)]


def test_missing_extension_reports_failed():
    """@brief 模型无 Extension 对象时返回 failed 证据。"""
    model = SimpleNamespace(Extension=None)
    _handle, evidence = resolve_selection(model, {"kind": "named", "name": "x"})
    assert evidence["status"] == "failed"
    assert "Extension" in evidence["message"]


def test_plane_alias_fallback_all_fail():
    """@brief 显式名与全部别名都失败时返回候选清单。"""
    extension = _SelectExtension([])
    model = SimpleNamespace(Extension=extension)
    _handle, evidence = resolve_selection(model, {"kind": "plane"})
    assert evidence["status"] == "not_found"
    assert evidence["candidates"][0] == "Front Plane"
    assert len(evidence["candidates"]) == 6
    assert len(extension.calls) == 6


def test_plane_dedups_explicit_name():
    """@brief 显式名与别名重复时只尝试一次。"""
    extension = _SelectExtension([True])
    model = SimpleNamespace(Extension=extension)
    _handle, evidence = resolve_selection(model, {"kind": "plane", "name": "Top Plane"})
    assert evidence["matched"] == "Top Plane"
    assert [call[0] for call in extension.calls] == ["Top Plane"]


# --- 坐标选择 ---


def test_coordinate_without_point_fails():
    """@brief coordinate 形态缺少 point_mm 返回 failed。"""
    model = SimpleNamespace(Extension=_SelectExtension([True]))
    _handle, evidence = resolve_selection(model, {"kind": "coordinate"})
    assert evidence["status"] == "failed"
    assert "point_mm" in evidence["message"]


def test_coordinate_retries_after_redraw():
    """@brief 首次坐标选择失败后刷新视图重试一次。"""
    extension = _SelectExtension([False, True])
    redraws = []
    model = SimpleNamespace(
        Extension=extension,
        ViewZoomtofit2=lambda: redraws.append("zoom"),
        GraphicsRedraw2=lambda: redraws.append("redraw"),
    )
    _handle, evidence = resolve_selection(
        model, {"kind": "coordinate", "point_mm": [5, 5, 5]}
    )
    assert evidence["status"] == "resolved"
    assert len(extension.calls) == 2
    assert redraws == ["zoom", "redraw"]


# --- 组件选择 ---


class _Component:
    """@brief 组件名称桩 (Name2 可缺失)。"""

    def __init__(self, name2, name):
        self.Name2 = name2
        self.Name = name


def test_component_matches_by_name_fallback():
    """@brief Name2 缺失时回退 Name 匹配。"""
    component = _Component(None, "bracket-1")
    model = SimpleNamespace(
        Extension=object(), GetComponents=lambda _toplevel: (component,)
    )
    handle, evidence = resolve_selection(model, {"kind": "component", "name": "bracket"})
    assert evidence["status"] == "resolved_object_only"
    assert evidence["matched"] == "bracket-1"
    assert handle is component


def test_component_without_match_reports_not_found():
    """@brief 名称不含关键字或全空的组件被跳过。"""
    model = SimpleNamespace(
        Extension=object(),
        GetComponents=lambda _toplevel: (_Component("lid-1", "lid-1"), _Component(None, None)),
    )
    _handle, evidence = resolve_selection(model, {"kind": "component", "name": "box"})
    assert evidence["status"] == "not_found"
    assert evidence["candidates"] == []


# --- 草图段选择 ---


def test_sketch_segment_without_manager_reports_not_found():
    """@brief 无草图管理器时返回 not_found。"""
    model = SimpleNamespace(Extension=object(), SketchManager=None)
    _handle, evidence = resolve_selection(model, {"kind": "sketch_segment"})
    assert evidence["status"] == "not_found"
    assert "无活动草图段" in evidence["message"]


def test_sketch_segment_empty_active_sketch_reports_not_found():
    """@brief 活动草图无段时返回 not_found。"""
    sketch = SimpleNamespace(GetSketchSegments=lambda: ())
    model = SimpleNamespace(
        Extension=object(), SketchManager=SimpleNamespace(ActiveSketch=sketch)
    )
    _handle, evidence = resolve_selection(model, {"kind": "sketch_segment"})
    assert evidence["status"] == "not_found"
    assert "无活动草图段" in evidence["message"]


def test_sketch_segment_selects_nth_segment():
    """@brief 按序号选中草图段。"""
    seg_a, seg_b = _Segment(), _Segment()
    sketch = SimpleNamespace(GetSketchSegments=lambda: (seg_a, seg_b))
    model = SimpleNamespace(
        Extension=object(), SketchManager=SimpleNamespace(ActiveSketch=sketch)
    )
    handle, evidence = resolve_selection(model, {"kind": "sketch_segment", "nth": 1})
    assert handle is seg_b
    assert evidence["status"] == "resolved"
    assert seg_b.select_calls == [(False, 1)]
    assert evidence["candidates"][0]["index"] == 0


def test_sketch_segment_nth_out_of_range():
    """@brief 序号越界返回候选清单。"""
    sketch = SimpleNamespace(GetSketchSegments=lambda: (_Segment(),))
    model = SimpleNamespace(
        Extension=object(), SketchManager=SimpleNamespace(ActiveSketch=sketch)
    )
    _handle, evidence = resolve_selection(model, {"kind": "sketch_segment", "nth": 5})
    assert evidence["status"] == "not_found"
    assert len(evidence["candidates"]) == 1


def test_sketch_segment_select_failure_reports_failed():
    """@brief 草图段 Select2 失败返回 failed。"""
    seg = _Segment(select_result=False)
    sketch = SimpleNamespace(GetSketchSegments=lambda: (seg,))
    model = SimpleNamespace(
        Extension=object(), SketchManager=SimpleNamespace(ActiveSketch=sketch)
    )
    handle, evidence = resolve_selection(model, {"kind": "sketch_segment"})
    assert handle is None
    assert evidence["status"] == "failed"


# --- 面/边/顶点: 评分与唯一性 ---


def _face_model(faces):
    """@brief 构造带面的模型桩。"""
    return _BodiesModel([_Body(faces)])


def test_face_selects_by_nth_without_constraints():
    """@brief 无签名/坐标约束时按序号直接选中。"""
    face = _TopologyEntity(area=2.0)
    handle, evidence = resolve_selection(_face_model([face]), {"kind": "face", "nth": 0})
    assert handle is face
    assert evidence["status"] == "resolved"
    assert evidence["candidates"][0]["score"] == 0


def test_face_nth_out_of_range_reports_not_found():
    """@brief 面序号越界返回候选清单。"""
    model = _face_model([_TopologyEntity(), _TopologyEntity()])
    _handle, evidence = resolve_selection(model, {"kind": "face", "nth": 7})
    assert evidence["status"] == "not_found"
    assert len(evidence["candidates"]) == 2


def test_face_select_failure_reports_failed():
    """@brief 面 Select2 失败返回 failed。"""
    face = _TopologyEntity(select_result=False)
    handle, evidence = resolve_selection(_face_model([face]), {"kind": "face"})
    assert handle is None
    assert evidence["status"] == "failed"


def test_face_signature_unique_match_selects():
    """@brief 几何签名唯一命中时按匹配分选中。"""
    face = _TopologyEntity(area=0.5, center=(0.1, 0.2, 0.3))
    decoy = _TopologyEntity(area=9.0, center=(9.0, 9.0, 9.0))
    expected = geometry_signature([0.5, [0.1, 0.2, 0.3]])
    handle, evidence = resolve_selection(
        _face_model([decoy, face]), {"kind": "face", "geometry_signature": expected}
    )
    assert handle is face
    assert evidence["status"] == "resolved"
    assert evidence["candidates"][0]["match_score"] == 5
    assert evidence["candidates"][1]["match_score"] == 0


def test_face_signature_mismatch_reports_not_found():
    """@brief 签名不匹配且无歧义命中时返回 not_found。"""
    expected = geometry_signature([123.0, [4.0, 5.0, 6.0]])
    model = _face_model([_TopologyEntity(area=0.5, center=(0.1, 0.2, 0.3))])
    _handle, evidence = resolve_selection(
        model, {"kind": "face", "geometry_signature": expected}
    )
    assert evidence["status"] == "not_found"
    assert len(evidence["candidates"]) == 1


def test_face_duplicate_signature_reports_ambiguous():
    """@brief 多个候选签名相同且命中时绝不猜测。"""
    expected = geometry_signature([0.5, [0.1, 0.2, 0.3]])
    faces = [_TopologyEntity(area=0.5, center=(0.1, 0.2, 0.3)) for _ in range(2)]
    _handle, evidence = resolve_selection(
        _face_model(faces), {"kind": "face", "geometry_signature": expected}
    )
    assert evidence["status"] == "ambiguous"
    assert len(evidence["candidates"]) == 2


def test_face_midpoint_hint_selects_nearest():
    """@brief 中点提示命中 (<=1mm) 的候选得唯一高分。"""
    near = _TopologyEntity(area=1.0, center=(0.1, 0.2, 0.3))
    far = _TopologyEntity(area=1.0, center=(0.5, 0.5, 0.5))
    handle, evidence = resolve_selection(
        _face_model([far, near]), {"kind": "face", "point_mm": (100, 200, 300)}
    )
    assert handle is near
    assert evidence["status"] == "resolved"
    assert evidence["candidates"][0]["midpoint_distance_m"] == 0.0


def test_face_midpoint_hint_without_hit_reports_not_found():
    """@brief 中点提示距离超容差时返回 not_found。"""
    model = _face_model([_TopologyEntity(area=1.0, center=(0.9, 0.9, 0.9))])
    _handle, evidence = resolve_selection(
        model, {"kind": "face", "point_mm": (100, 200, 300)}
    )
    assert evidence["status"] == "not_found"
    assert evidence["candidates"][0].get("midpoint_distance_m", 0) > 0.001


def test_face_midpoint_hint_ignores_missing_midpoint():
    """@brief 候选无中点信息时不参与距离评分。"""
    face = _TopologyEntity(area=1.0)
    _handle, evidence = resolve_selection(
        _face_model([face]), {"kind": "face", "point_mm": (100, 200, 300)}
    )
    assert evidence["status"] == "not_found"
    assert "midpoint_distance_m" not in evidence["candidates"][0]


# --- 配合对选择 ---


def test_pair_for_mate_first_failure_short_circuits():
    """@brief 第一次选择失败时保留计数证据并提前返回。"""
    model = SimpleNamespace(
        Extension=_SelectExtension([]),
        GetComponents=lambda _toplevel: (),
        ClearSelection2=lambda _flag: None,
        SelectionManager=SimpleNamespace(GetSelectedObjectCount2=lambda _mark: 0),
    )
    evidence1, evidence2, count = resolve_pair_for_mate(
        model,
        {"kind": "component", "name": "不存在"},
        {"kind": "plane", "name": "Front Plane"},
    )
    assert evidence1["status"] == "not_found"
    assert evidence1["selection_count"] == 0
    assert evidence2["status"] == "not_found"
    assert count == 0
