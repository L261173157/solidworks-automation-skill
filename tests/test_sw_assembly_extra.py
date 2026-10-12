"""@brief sw_assembly 全分支补充测试（离线假 COM，绝不触发真实 SolidWorks）。"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pywintypes
import pytest

from scripts import sw_assembly

SCRIPTS_DIR = Path(sw_assembly.__file__).resolve().parent


def _exec_standalone(source: Path, module_name: str):
    """@brief 以非包成员身份重新执行模块源码，触发相对导入 ImportError 回退分支。"""
    spec = importlib.util.spec_from_file_location(module_name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_module_import_fallback_paths_covered(monkeypatch):
    """@brief 覆盖模块头部 try/except 双导入回退分支 (13-15, 19-20)。"""
    import scripts.sw_connect as real_connect
    import scripts.sw_preflight as real_preflight
    import scripts.sw_selection as real_selection

    monkeypatch.setitem(sys.modules, "sw_preflight", real_preflight)
    monkeypatch.setitem(sys.modules, "sw_connect", real_connect)
    monkeypatch.setitem(sys.modules, "sw_selection", real_selection)

    module = _exec_standalone(SCRIPTS_DIR / "sw_assembly.py", "_sw_assembly_toplevel_probe")

    assert module.safe_get_com_member is real_connect.safe_get_com_member
    assert module.open_document is real_connect.open_document
    assert module.resolve_selection is real_selection.resolve_selection


# ---------------------------------------------------------------------------
# 选择封装与别名规范化
# ---------------------------------------------------------------------------
class _RecordingExtension:
    """@brief 记录 SelectByID2 调用参数的 Extension 桩。"""

    def __init__(self, result=True):
        self.calls = []
        self._result = result

    def SelectByID2(self, name, entity_type, x, y, z, append, mark, callout, options):
        self.calls.append((name, entity_type, append, mark, callout, options))
        return self._result


def test_empty_callout_and_select_by_id_defaults():
    ext = _RecordingExtension()
    assert sw_assembly._select_by_id(ext, "Face1", "FACE") is True
    name, entity_type, append, mark, callout, options = ext.calls[0]
    assert (name, entity_type, append, mark, options) == ("Face1", "FACE", False, 0, 0)
    assert callout.varianttype == sw_assembly.pythoncom.VT_DISPATCH
    assert callout.value is None

    assert sw_assembly._select_by_id(ext, "Face2", "EDGE", append=True, mark=2) is True
    assert ext.calls[1][2] is True and ext.calls[1][3] == 2


def test_as_alias_list_normalization_and_empty_guard():
    assert sw_assembly._as_alias_list("Front Plane") == ["Front Plane"]
    assert sw_assembly._as_alias_list(["A", "", "B"]) == ["A", "B"]
    with pytest.raises(ValueError, match="aliases 不能为空"):
        sw_assembly._as_alias_list([])
    with pytest.raises(ValueError, match="aliases 不能为空"):
        sw_assembly._as_alias_list(["", ""])


def test_active_solidworks_app_returns_object_or_none(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(sw_assembly._win32com, "GetActiveObject", lambda prog_id: sentinel)
    assert sw_assembly._active_solidworks_app() is sentinel

    def _boom(_prog_id):
        raise pywintypes.com_error(-2147221005, "类未注册", None, None)

    monkeypatch.setattr(sw_assembly._win32com, "GetActiveObject", _boom)
    assert sw_assembly._active_solidworks_app() is None


class _TitleModel:
    def __init__(self, title):
        self._title = title

    def GetTitle(self):
        return self._title


class _FakeSwApp:
    """@brief 支持 ActivateDoc3/ActivateDoc2 降级链的应用桩。"""

    def __init__(self, doc3=None, doc3_error=False, doc2_error=False):
        self._doc3 = doc3
        self._doc3_error = doc3_error
        self._doc2_error = doc2_error
        self.doc3_titles = []
        self.doc2_titles = []

    def ActivateDoc3(self, title, silent, option, errors):
        self.doc3_titles.append(title)
        if self._doc3_error:
            raise RuntimeError("ActivateDoc3 不兼容")
        return self._doc3

    def ActivateDoc2(self, title, silent, errors):
        self.doc2_titles.append(title)
        if self._doc2_error:
            raise RuntimeError("ActivateDoc2 也失败")
        return None


def test_activate_model_document_guard_and_success_paths():
    assert sw_assembly._activate_model_document(None, _TitleModel("A")) is False
    assert sw_assembly._activate_model_document(_FakeSwApp(), None) is False
    assert sw_assembly._activate_model_document(_FakeSwApp(), _TitleModel("")) is False

    sw = _FakeSwApp(doc3=object())
    assert sw_assembly._activate_model_document(sw, _TitleModel("装配体1")) is True
    assert sw.doc3_titles == ["装配体1"]


def test_activate_model_document_falls_back_to_activate_doc2():
    sw = _FakeSwApp(doc3_error=True)
    assert sw_assembly._activate_model_document(sw, _TitleModel("装配体1")) is True
    assert sw.doc2_titles == ["装配体1"]


def test_activate_model_document_all_paths_fail_returns_false():
    sw = _FakeSwApp(doc3_error=True, doc2_error=True)
    assert sw_assembly._activate_model_document(sw, _TitleModel("装配体1")) is False


class _QueueAsm:
    """@brief AddComponent5 按队列依次返回值、AddComponent4 固定返回值的装配体桩。"""

    def __init__(self, add5_results, add4_result=None, title=""):
        self._add5_results = list(add5_results)
        self._add4_result = add4_result
        self.GetTitle = title
        self.add5_calls = []

    def AddComponent5(self, path, options, config_option_name, use_config, config_name, x, y, z):
        self.add5_calls.append((path, options, config_option_name, use_config, config_name, x, y, z))
        if len(self._add5_results) > 1:
            return self._add5_results.pop(0)
        return self._add5_results[0]

    def AddComponent4(self, path, config_name, x, y, z):
        return self._add4_result


def _touch_part(tmp_path):
    part = tmp_path / "part.sldprt"
    part.write_bytes(b"x")
    return part


def test_add_component_success_via_add_component5(tmp_path, capsys):
    part = _touch_part(tmp_path)
    asm = _QueueAsm(["component"])
    assert sw_assembly.add_component(asm, str(part), 1, 2, 3, "Default") == "component"
    assert asm.add5_calls[0][0] == str(part.resolve())
    assert asm.add5_calls[0][1] == 0
    assert asm.add5_calls[0][4] == "Default"
    assert "已添加组件" in capsys.readouterr().out


def test_add_component_falls_back_to_add_component4(tmp_path, capsys):
    part = _touch_part(tmp_path)

    class _Add5Rejects:
        def AddComponent5(self, *args):
            raise RuntimeError("AddComponent5 拒绝")

        def AddComponent4(self, path, config_name, x, y, z):
            return "component4"

    assert sw_assembly.add_component(_Add5Rejects(), str(part)) == "component4"
    assert "已添加组件" in capsys.readouterr().out


def test_add_component_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="组件文件不存在"):
        sw_assembly.add_component(_QueueAsm(["x"]), str(tmp_path / "missing.sldprt"))


def test_add_component_wrapper_without_add_component5_api():
    """AddComponent5 缺失时包装层直接返回 None。"""
    assert sw_assembly._add_component5(object(), "part.sldprt", "", 0, 0, 0) is None


def test_add_component_reports_add_component4_exception(tmp_path, capsys, monkeypatch):
    """AddComponent5 返回 None 且 AddComponent4 抛异常时，异常写入错误清单。"""
    monkeypatch.setattr(sw_assembly, "_active_solidworks_app", lambda: None)

    class _Add4Rejects:
        def AddComponent5(self, *args):
            return None

        def AddComponent4(self, *args):
            raise RuntimeError("AddComponent4 崩溃")

    result = sw_assembly.add_component(_Add4Rejects(), str(_touch_part(tmp_path)))
    assert result is None
    out = capsys.readouterr().out
    assert "AddComponent4: AddComponent4 崩溃" in out
    assert "GetActiveObject(SldWorks.Application): 返回 None" in out


def test_add_component_reports_active_object_unavailable(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sw_assembly, "_active_solidworks_app", lambda: None)
    result = sw_assembly.add_component(_QueueAsm([None]), str(_touch_part(tmp_path)))
    assert result is None
    out = capsys.readouterr().out
    assert "添加组件失败" in out
    assert "GetActiveObject(SldWorks.Application): 返回 None" in out


def test_add_component_reports_open_doc6_none(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sw_assembly, "open_document", lambda *args, **kwargs: None)
    result = sw_assembly.add_component(_QueueAsm([None]), str(_touch_part(tmp_path)), sw=object())
    assert result is None
    assert "OpenDoc6: 返回 None" in capsys.readouterr().out


def test_add_component_reports_activate_failure(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sw_assembly, "open_document", lambda *args, **kwargs: object())
    asm = _QueueAsm([None], title="")
    result = sw_assembly.add_component(asm, str(_touch_part(tmp_path)), sw=object())
    assert result is None
    assert "ActivateDoc: 无法重新激活装配体" in capsys.readouterr().out


def test_add_component_open_then_add_component5_returns_none(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sw_assembly, "open_document", lambda *args, **kwargs: object())
    sw = _FakeSwApp(doc3=object())
    asm = _QueueAsm([None, None], title="装配体1")
    result = sw_assembly.add_component(asm, str(_touch_part(tmp_path)), sw=sw)
    assert result is None
    assert "open_then_AddComponent5: 返回 None" in capsys.readouterr().out


def test_add_component_open_then_add_component5_succeeds(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sw_assembly, "open_document", lambda *args, **kwargs: object())
    sw = _FakeSwApp(doc3=object())
    asm = _QueueAsm([None, "component"], title="装配体1")
    result = sw_assembly.add_component(asm, str(_touch_part(tmp_path)), sw=sw)
    assert result == "component"
    assert "已添加组件" in capsys.readouterr().out


def test_add_component_reports_open_document_exception(tmp_path, capsys, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("打开文档失败")

    monkeypatch.setattr(sw_assembly, "open_document", _boom)
    result = sw_assembly.add_component(_QueueAsm([None]), str(_touch_part(tmp_path)), sw=object())
    assert result is None
    assert "open_then_AddComponent5: 打开文档失败" in capsys.readouterr().out


def test_add_component_falsy_component_uses_default_detail(tmp_path, capsys):
    """AddComponent5 返回假值但非 None 时，错误清单为空，走默认提示分支。"""

    class _FalsyAsm:
        def AddComponent5(self, *args):
            return False

    result = sw_assembly.add_component(_FalsyAsm(), str(_touch_part(tmp_path)))
    assert result is False
    out = capsys.readouterr().out
    assert "AddComponent5/AddComponent4 均返回 None" in out


# ---------------------------------------------------------------------------
# 组件解析与模型/特征获取
# ---------------------------------------------------------------------------
class _Suppressible:
    def __init__(self, fail=False, fail_always=False):
        self.states = []
        self._fail = fail
        self._fail_always = fail_always

    def SetSuppression2(self, state):
        self.states.append(state)
        if self._fail_always or (self._fail and state != sw_assembly.SW_COMPONENT_RESOLVED):
            raise RuntimeError("轻化组件不支持该状态")
        return f"state={state}"


def test_resolve_component_rejects_none_and_retries_with_resolved_state():
    with pytest.raises(ValueError, match="component 不能为空"):
        sw_assembly.resolve_component(None)

    ok = _Suppressible()
    assert sw_assembly.resolve_component(ok) == "state=2"
    assert ok.states == [2]

    failing = _Suppressible(fail=True)
    assert sw_assembly.resolve_component(failing) == "state=3"
    assert failing.states == [2, 3]

    hopeless = _Suppressible(fail_always=True)
    with pytest.raises(RuntimeError, match="轻化组件不支持该状态"):
        sw_assembly.resolve_component(hopeless, sw_assembly.SW_COMPONENT_RESOLVED)
    assert hopeless.states == [3]


class _ModelComponent:
    def __init__(self, model=None, name="part-1", corresponding=None):
        self._model = model
        self.Name2 = name
        self._corresponding = corresponding
        self.suppressions = []

    def SetSuppression2(self, state):
        self.suppressions.append(state)
        return state

    def GetModelDoc2(self):
        return self._model

    def GetCorresponding(self, obj):
        return self._corresponding


def test_get_component_model_guard_and_resolution():
    with pytest.raises(ValueError, match="component 不能为空"):
        sw_assembly.get_component_model(None)
    assert sw_assembly.get_component_model(None, raise_on_error=False) is None

    model = object()
    component = _ModelComponent(model=model)
    assert sw_assembly.get_component_model(component) is model
    assert component.suppressions == [2]

    lazy = _ModelComponent(model=model)
    assert sw_assembly.get_component_model(lazy, resolve=False) is model
    assert lazy.suppressions == []


def test_get_component_model_unresolved_component_error_paths():
    component = _ModelComponent(model=None, name="轻化-1")
    with pytest.raises(RuntimeError, match="组件未解析或未加载.*轻化-1"):
        sw_assembly.get_component_model(component)
    assert sw_assembly.get_component_model(component, raise_on_error=False) is None


class _FeatureModel:
    def __init__(self, mapping):
        self._mapping = mapping

    def FeatureByName(self, name):
        return self._mapping.get(name)


def _component_with_feature_model(mapping, name="part-1"):
    return _ModelComponent(model=_FeatureModel(mapping), name=name)


def test_get_component_feature_alias_expansion_and_dedup():
    plane = object()
    component = _component_with_feature_model({"上视基准面": plane})
    assert sw_assembly.get_component_feature(component, "Top Plane") is plane

    front = object()
    component = _component_with_feature_model({"Front Plane": front})
    assert sw_assembly.get_component_feature(component, ["Front Plane", "Front Plane"]) is front

    custom = object()
    component = _component_with_feature_model({"MyFace": custom})
    assert sw_assembly.get_component_feature(component, "MyFace") is custom


def test_get_component_feature_missing_feature_raises_with_candidates():
    component = _component_with_feature_model({}, name="part-1")
    with pytest.raises(RuntimeError, match="组件 part-1 缺少特征"):
        sw_assembly.get_component_feature(component, ["A", "B"])
    with pytest.raises(ValueError, match="aliases 不能为空"):
        sw_assembly.get_component_feature(component, [])


def test_get_assembly_entity_success_and_failure():
    entity = object()
    assert sw_assembly.get_assembly_entity(_CorrespondingComponent(entity), "face") is entity
    with pytest.raises(RuntimeError, match="无法映射装配体上下文实体: slide-1"):
        sw_assembly.get_assembly_entity(_CorrespondingComponent(None), "face")


class _CorrespondingComponent:
    Name2 = "slide-1"

    def __init__(self, entity):
        self._entity = entity

    def GetCorresponding(self, obj):
        return self._entity


def test_get_component_feature_entity_maps_feature_to_assembly_context():
    feature = object()
    entity = object()
    component = _ModelComponent(
        model=_FeatureModel({"前视基准面": feature}),
        name="part-1",
        corresponding=entity,
    )
    assert sw_assembly.get_component_feature_entity(component, "前视基准面") is entity


# ---------------------------------------------------------------------------
# 圆柱面查找与 Mate 选择
# ---------------------------------------------------------------------------
class _Surface:
    def __init__(self, is_cylinder=True, params=None, params_error=False):
        self._is_cylinder = is_cylinder
        self._params = params
        self._params_error = params_error

    def IsCylinder(self):
        return self._is_cylinder

    def CylinderParams(self):
        if self._params_error:
            raise RuntimeError("CylinderParams 失败")
        return self._params


class _Face:
    def __init__(self, surface, area):
        self._surface = surface
        self._area = area

    def GetSurface(self):
        return self._surface

    def GetArea(self):
        return self._area


class _Body:
    def __init__(self, faces):
        self._faces = faces

    def GetFaces(self):
        return tuple(self._faces)


class _Part:
    def __init__(self, bodies):
        self._bodies = bodies

    def GetBodies2(self, body_type, flag):
        if self._bodies is None:
            return None
        return tuple(self._bodies)


class _CylComponent:
    Name2 = "gear-1"

    def __init__(self, part):
        self._part = part

    def SetSuppression2(self, state):
        return state

    def GetModelDoc2(self):
        return self._part

    def GetCorresponding(self, obj):
        return ("ctx", obj)


def _cyl_params(radius):
    return (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, radius)


def test_find_largest_cylinder_face_picks_largest_in_range():
    skip_plane = _Face(_Surface(is_cylinder=False), 50.0)
    skip_no_surface = _Face(None, 60.0)
    below_min = _Face(_Surface(params=_cyl_params(0.005)), 40.0)
    above_max = _Face(_Surface(params=_cyl_params(0.02)), 30.0)
    broken = _Face(_Surface(params_error=True), 25.0)
    winner = _Face(_Surface(params=_cyl_params(0.012)), 10.0)
    smaller = _Face(_Surface(params=_cyl_params(0.012)), 5.0)
    part = _Part([
        _Body([skip_plane, skip_no_surface]),
        _Body([below_min, above_max]),
        _Body([broken, winner, smaller]),
    ])
    component = _CylComponent(part)

    entity = sw_assembly.find_largest_cylinder_face(component, min_radius=0.01, max_radius=0.015)
    assert entity == ("ctx", winner)


def test_find_largest_cylinder_face_without_bodies_raises():
    component = _CylComponent(_Part(None))
    with pytest.raises(RuntimeError, match="未找到圆柱面: gear-1"):
        sw_assembly.find_largest_cylinder_face(component)


def test_find_largest_cylinder_face_no_matching_face_raises():
    empty = _Face(_Surface(is_cylinder=False), 1.0)
    component = _CylComponent(_Part([_Body([empty])]))
    with pytest.raises(RuntimeError, match="未找到圆柱面"):
        sw_assembly.find_largest_cylinder_face(component)


class _MateEntity:
    def __init__(self, result=True):
        self._result = result
        self.calls = []

    def Select2(self, append, mark):
        self.calls.append((append, mark))
        return self._result


class _SelectionManager:
    def __init__(self, model):
        self._model = model

    def GetSelectedObjectCount2(self, mark):
        return self._model.count


class _AsmSelectionModel:
    def __init__(self, count=2):
        self.count = count
        self.clears = 0
        self.SelectionManager = _SelectionManager(self)

    def ClearSelection2(self, flag):
        self.clears += 1


def test_select_entities_for_mate_success_and_selection_count_guard():
    model = _AsmSelectionModel(count=2)
    entity1, entity2 = _MateEntity(), _MateEntity()
    assert sw_assembly.select_entities_for_mate(model, entity1, entity2) is True
    assert entity1.calls == [(False, 1)]
    assert entity2.calls == [(True, 1)]

    bad_count = _AsmSelectionModel(count=5)
    with pytest.raises(RuntimeError, match="Mate 选择数量错误: selected=5, expected=2"):
        sw_assembly.select_entities_for_mate(bad_count, _MateEntity(), _MateEntity())
    assert bad_count.clears == 2


def test_select_entities_for_mate_first_and_second_failure():
    model = _AsmSelectionModel()
    with pytest.raises(RuntimeError, match="选择第一个 Mate 实体失败"):
        sw_assembly.select_entities_for_mate(model, _MateEntity(result=False), _MateEntity())
    with pytest.raises(RuntimeError, match="选择第二个 Mate 实体失败"):
        sw_assembly.select_entities_for_mate(model, _MateEntity(), _MateEntity(result=False))


class _Mate:
    def __init__(self):
        self.Name = None


class _BrokenNameMate:
    @property
    def Name(self):
        return "locked"

    @Name.setter
    def Name(self, value):
        raise RuntimeError("COM 属性只读")


class _AsmMateModel:
    """@brief 带 Extension/AddMate5/ClearSelection2/SelectionManager 的装配体 Mate 桩。"""

    def __init__(self, mate=None, status=1, error=None, selected_count=2):
        self.Extension = _RecordingExtension()
        self.count = selected_count
        self.SelectionManager = _SelectionManager(self)
        self.clears = 0
        self.mate_args = None
        self._mate = mate
        self._status = status
        self._error = error

    def ClearSelection2(self, flag):
        self.clears += 1

    def AddMate5(self, mate_type, align, flip, distance, distance_upper, distance_lower,
                 gear_num, gear_den, angle, angle_upper, angle_lower,
                 for_positioning_only, lock_rotation, width_mate_option, error_status):
        self.mate_args = {
            "mate_type": mate_type,
            "align": align,
            "flip": flip,
            "distance": distance,
            "distance_upper": distance_upper,
            "distance_lower": distance_lower,
            "gear_num": gear_num,
            "gear_den": gear_den,
            "angle": angle,
            "angle_upper": angle_upper,
            "angle_lower": angle_lower,
            "for_positioning_only": for_positioning_only,
            "lock_rotation": lock_rotation,
            "width_mate_option": width_mate_option,
        }
        if self._error is not None:
            raise self._error
        error_status.value = self._status
        return self._mate


def test_add_mate5_checked_defaults_and_success():
    mate = _Mate()
    asm = _AsmMateModel(mate=mate)
    assert sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_CONCENTRIC) is mate
    args = asm.mate_args
    assert args["distance"] == args["distance_upper"] == args["distance_lower"] == 0.0
    assert args["angle"] == args["angle_upper"] == args["angle_lower"] == 0.0
    assert args["gear_num"] == args["gear_den"] == 0.0
    assert args["for_positioning_only"] is False
    assert args["lock_rotation"] is False
    assert args["width_mate_option"] == 0
    assert asm.clears == 1


def test_add_mate5_checked_accepts_unknown_error_status_with_mate():
    asm = _AsmMateModel(mate=_Mate(), status=0)
    assert sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_DISTANCE) is asm._mate


def test_add_mate5_checked_explicit_parameters_forwarded():
    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_mate5_checked(
        asm,
        sw_assembly.SW_MATE_GEAR,
        align=1,
        flip=True,
        distance=0.5,
        distance_upper=0.6,
        distance_lower=0.4,
        gear_num=2.0,
        gear_den=3.0,
        angle=0.1,
        angle_upper=0.2,
        angle_lower=0.05,
        for_positioning_only=True,
        lock_rotation=True,
        width_mate_option=1,
        name="齿轮配合",
    )
    args = asm.mate_args
    assert args["mate_type"] == sw_assembly.SW_MATE_GEAR
    assert args["align"] == 1
    assert args["flip"] is True
    assert (args["distance"], args["distance_upper"], args["distance_lower"]) == (0.5, 0.6, 0.4)
    assert (args["gear_num"], args["gear_den"]) == (2.0, 3.0)
    assert (args["angle"], args["angle_upper"], args["angle_lower"]) == (0.1, 0.2, 0.05)
    assert args["for_positioning_only"] is True
    assert args["lock_rotation"] is True
    assert args["width_mate_option"] == 1
    assert asm._mate.Name == "齿轮配合"
    assert asm.clears == 1


def test_add_mate5_checked_name_assignment_failure_is_ignored():
    mate = _BrokenNameMate()
    asm = _AsmMateModel(mate=mate)
    assert sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_COINCIDENT, name="重合") is mate
    assert asm.clears == 1


def test_add_mate5_checked_failure_statuses():
    asm = _AsmMateModel(mate=None, status=1)
    with pytest.raises(RuntimeError, match="AddMate5 失败: type=0, error_status=1"):
        sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_COINCIDENT)

    asm = _AsmMateModel(mate=_Mate(), status=5)
    with pytest.raises(RuntimeError, match="error_status=5"):
        sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_COINCIDENT)

    asm = _AsmMateModel(error=RuntimeError("COM 崩溃"))
    with pytest.raises(RuntimeError, match="COM 崩溃"):
        sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_COINCIDENT)


def test_add_mate5_checked_clear_selection_disabled():
    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_mate5_checked(asm, sw_assembly.SW_MATE_PARALLEL, clear_selection=False)
    assert asm.clears == 0


def test_add_concentric_mate_by_cylinders_forwards_radius_ranges(monkeypatch):
    ranges = []

    def _fake_find(component, min_radius, max_radius):
        ranges.append((component, min_radius, max_radius))
        return _MateEntity()

    monkeypatch.setattr(sw_assembly, "find_largest_cylinder_face", _fake_find)
    asm = _AsmMateModel(mate=_Mate())
    result = sw_assembly.add_concentric_mate_by_cylinders(
        asm, "comp-a", "comp-b", radius_a=(0.01, 0.2), radius_b=(0.05, None), name="同心1"
    )
    assert result is asm._mate
    assert ranges == [("comp-a", 0.01, 0.2), ("comp-b", 0.05, None)]
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_CONCENTRIC
    assert asm.mate_args["lock_rotation"] is False


def test_add_concentric_mate_by_cylinders_defaults_and_lock_rotation(monkeypatch):
    ranges = []

    def _fake_find(component, min_radius, max_radius):
        ranges.append((min_radius, max_radius))
        return _MateEntity()

    monkeypatch.setattr(sw_assembly, "find_largest_cylinder_face", _fake_find)
    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_concentric_mate_by_cylinders(asm, "comp-a", "comp-b", lock_rotation=True)
    assert ranges == [(0.0, None), (0.0, None)]
    assert asm.mate_args["lock_rotation"] is True


def test_add_gear_mate_by_cylinders_rejects_zero_teeth():
    asm = _AsmSelectionModel()
    with pytest.raises(ValueError, match="齿数/传动比不能为 0"):
        sw_assembly.add_gear_mate_by_cylinders(asm, "comp-a", "comp-b", 0, 12)
    with pytest.raises(ValueError, match="齿数/传动比不能为 0"):
        sw_assembly.add_gear_mate_by_cylinders(asm, "comp-a", "comp-b", 12, 0)


def test_add_gear_mate_by_cylinders_success(monkeypatch):
    monkeypatch.setattr(sw_assembly, "find_largest_cylinder_face", lambda c, lo, hi: _MateEntity())
    asm = _AsmMateModel(mate=_Mate())
    result = sw_assembly.add_gear_mate_by_cylinders(asm, "comp-a", "comp-b", 17, 34, name="齿轮")
    assert result is asm._mate
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_GEAR
    assert (asm.mate_args["gear_num"], asm.mate_args["gear_den"]) == (17.0, 34.0)


def test_add_revolute_joint_by_cylinders_uses_concentric_with_free_rotation(monkeypatch):
    monkeypatch.setattr(sw_assembly, "find_largest_cylinder_face", lambda c, lo, hi: _MateEntity())
    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_revolute_joint_by_cylinders(asm, "comp-a", "comp-b", name="铰接")
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_CONCENTRIC
    assert asm.mate_args["lock_rotation"] is False


def test_build_transform_data_x_identity_and_rotation():
    data = sw_assembly.build_transform_data_x(0.1, 0.2, 0.3)
    assert data == (1.0, 0.0, 0.0, 0.0, 1.0, -0.0, 0.0, 0.0, 1.0, 0.1, 0.2, 0.3, 1.0, 0.0, 0.0, 0.0)

    rotated = sw_assembly.build_transform_data_x(0.0, 0.0, 0.0, angle_rad=math.pi / 2)
    assert rotated[4] == pytest.approx(0.0, abs=1e-12)
    assert rotated[5] == pytest.approx(-1.0)
    assert rotated[7] == pytest.approx(1.0)


class _Transform:
    def __init__(self):
        self.ArrayData = None


def _transform_component(solve_result=True, solve_error=False):
    transform = _Transform()

    class _Component:
        Transform2 = transform

        def SetTransformAndSolve2(self, value):
            if solve_error:
                raise RuntimeError("SetTransformAndSolve2 失败")
            return solve_result

    return _Component(), transform


def test_apply_component_transform_x_solve_paths():
    component, transform = _transform_component(solve_result=True)
    assert sw_assembly.apply_component_transform_x(component, 0.1, 0.2, 0.3, 0.5) is True
    assert transform.ArrayData == sw_assembly.build_transform_data_x(0.1, 0.2, 0.3, 0.5)

    component, transform = _transform_component(solve_result=False)
    assert sw_assembly.apply_component_transform_x(component, 0, 0, 0) is True
    assert component.Transform2 is transform

    component, transform = _transform_component(solve_error=True)
    assert sw_assembly.apply_component_transform_x(component, 0, 0, 0) is True
    assert component.Transform2 is transform


def test_apply_component_transform_x_missing_transform_raises():
    class _NoTransform:
        Transform2 = None

    with pytest.raises(RuntimeError, match="组件没有可用 Transform2"):
        sw_assembly.apply_component_transform_x(_NoTransform(), 0, 0, 0)


def test_apply_component_transform_x_assignment_failure_returns_false():
    transform = _Transform()

    class _Component:
        @property
        def Transform2(self):
            return transform

        @Transform2.setter
        def Transform2(self, value):
            raise RuntimeError("Transform2 只读")

        def SetTransformAndSolve2(self, value):
            return False

    assert sw_assembly.apply_component_transform_x(_Component(), 0, 0, 0) is False


# ---------------------------------------------------------------------------
# 特征树遍历与 Mate 摘要
# ---------------------------------------------------------------------------
class _TreeNode:
    def __init__(self, name, type_name="ProfileFeature"):
        self.name = name
        self.type_name = type_name
        self._next = None
        self._first_sub = None

    def GetNextFeature(self):
        return self._next

    def GetFirstSubFeature(self):
        return self._first_sub

    def GetNextSubFeature(self):
        return None

    def Name(self):
        return self.name

    def GetTypeName2(self):
        return self.type_name


def _link_chain(features):
    for current, following in zip(features, features[1:]):
        current._next = following
    return features


def _tree_model(first_feature):
    class _Model:
        def FirstFeature(self):
            return first_feature

    return _Model()


def test_iter_feature_tree_with_and_without_subfeatures():
    sub = _TreeNode("同心1", "Mate")
    root1 = _TreeNode("MateGroup1", "MateGroup")
    root1._first_sub = sub
    root2 = _TreeNode("Boss1")
    _link_chain([root1, root2])
    model = _tree_model(root1)

    assert [(f.name, depth) for f, depth in sw_assembly.iter_feature_tree(model)] == [
        ("MateGroup1", 0),
        ("同心1", 1),
        ("Boss1", 0),
    ]
    assert [(f.name, depth) for f, depth in sw_assembly.iter_feature_tree(model, include_subfeatures=False)] == [
        ("MateGroup1", 0),
        ("Boss1", 0),
    ]


def test_iter_feature_tree_empty_model():
    assert list(sw_assembly.iter_feature_tree(_tree_model(None))) == []


def test_iter_feature_tree_walk_subfeatures_skips_recursion_when_disabled():
    """walk_subfeatures 仅在外层 include_subfeatures=True 时被调用,
    其内部的假值分支需要直接驱动嵌套生成器并翻转闭包变量才能真实执行。"""
    sub = _TreeNode("同心1", "Mate")
    root = _TreeNode("MateGroup1", "MateGroup")
    root._first_sub = sub
    gen = sw_assembly.iter_feature_tree(_tree_model(root))
    assert next(gen) == (root, 0)  # 首个 yield 后 walk_subfeatures 已在帧内定义

    walk_factory = gen.gi_frame.f_locals["walk_subfeatures"]
    include_cell = walk_factory.__closure__[0]
    assert include_cell.cell_contents is True

    walk_gen = walk_factory(root, 1)
    assert next(walk_gen) == (sub, 1)
    include_cell.cell_contents = False  # 运行中翻转闭包变量, 命中假值分支
    assert list(walk_gen) == []  # 不再下钻子特征的子特征, 仅推进同级链
    gen.close()


def test_collect_mate_feature_summary_matches_all_mate_patterns():
    sub = _TreeNode("同心配合1", "Mate")
    group = _TreeNode("MateGroup1", "MateGroup")
    group._first_sub = sub
    prefix = _TreeNode("其他", "MateFolder")
    english = _TreeNode("gear-mate-1", "ProfileFeature")
    chinese = _TreeNode("配合2", "ProfileFeature")
    noise = _TreeNode("垫片", "ProfileFeature")
    _link_chain([group, prefix, english, chinese, noise])

    summary = sw_assembly.collect_mate_feature_summary(_tree_model(group))
    assert [(item["name"], item["type"], item["depth"]) for item in summary] == [
        ("MateGroup1", "MateGroup", 0),
        ("同心配合1", "Mate", 1),
        ("其他", "MateFolder", 0),
        ("gear-mate-1", "ProfileFeature", 0),
        ("配合2", "ProfileFeature", 0),
    ]


# ---------------------------------------------------------------------------
# 声明式选择与快捷 Mate 封装
# ---------------------------------------------------------------------------
def test_select_mate_entity_string_uses_select_by_id():
    asm = SimpleNamespace(Extension=_RecordingExtension())
    assert sw_assembly._select_mate_entity(asm, "Face1", "EDGE", append=True, mark=1) is True
    assert asm.Extension.calls[0][:4] == ("Face1", "EDGE", True, 1)


def test_select_mate_entity_declarative_resolved(monkeypatch):
    recorded = {}

    def _fake_resolve(model, spec, *, entity_type_hint=None, append=False, mark=1):
        recorded.update({
            "model": model,
            "spec": spec,
            "entity_type_hint": entity_type_hint,
            "append": append,
            "mark": mark,
        })
        return None, {"status": "resolved"}

    monkeypatch.setattr(sw_assembly, "resolve_selection", _fake_resolve)
    asm = object()
    spec = {"kind": "named", "name": "Face1"}
    assert sw_assembly._select_mate_entity(asm, spec, "FACE") is True
    assert recorded["spec"] is spec
    assert recorded["entity_type_hint"] == "FACE"
    assert recorded["append"] is False
    assert recorded["mark"] == 1


def test_select_mate_entity_declarative_failure_raises(monkeypatch):
    monkeypatch.setattr(
        sw_assembly,
        "resolve_selection",
        lambda model, spec, **kwargs: (None, {"status": "not_found"}),
    )
    with pytest.raises(ValueError, match="声明式选择失败"):
        sw_assembly._select_mate_entity(object(), {"kind": "named", "name": "缺"}, "FACE")


def test_add_mate_coincident_selects_then_creates_mate():
    asm = _AsmMateModel(mate=_Mate())
    result = sw_assembly.add_mate_coincident(asm, "面1", "FACE", "面2", "PLANE")
    assert result is asm._mate
    assert [(c[0], c[1], c[2]) for c in asm.Extension.calls] == [("面1", "FACE", False), ("面2", "PLANE", True)]
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_COINCIDENT
    assert asm.clears == 2


def test_add_mate_concentric_and_parallel_use_face_types():
    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_mate_concentric(asm, "孔1", "孔2")
    assert [(c[0], c[1], c[2]) for c in asm.Extension.calls] == [("孔1", "FACE", False), ("孔2", "FACE", True)]
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_CONCENTRIC

    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_mate_parallel(asm, "面A", "面B")
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_PARALLEL


def test_add_mate_distance_forwards_distance():
    asm = _AsmMateModel(mate=_Mate())
    sw_assembly.add_mate_distance(asm, "面1", "FACE", "面2", "PLANE", 0.01)
    assert asm.mate_args["mate_type"] == sw_assembly.SW_MATE_DISTANCE
    assert asm.mate_args["distance"] == 0.01


# ---------------------------------------------------------------------------
# 组件枚举/查找与压缩替换
# ---------------------------------------------------------------------------
class _ListedComponent:
    def __init__(self, name, path):
        self.Name2 = name
        self._path = path
        self.Visible = 1

    def GetPathName(self):
        return self._path

    def IsSuppressed(self):
        return False


class _AsmComponents:
    def __init__(self, components):
        self._components = components

    def GetComponents(self, top_level_only):
        if self._components is None:
            return None
        return tuple(self._components)


def test_get_components_returns_info_dicts():
    comp = _ListedComponent("base-1", r"C:\parts\base.sldprt")
    result = sw_assembly.get_components(_AsmComponents([comp]), top_level_only=False)
    assert result == [{
        "name": "base-1",
        "path": r"C:\parts\base.sldprt",
        "suppressed": False,
        "visible": 1,
    }]
    assert sw_assembly.get_components(_AsmComponents(None)) == []


def test_find_component_by_name_case_handling():
    asm = _AsmComponents([_ListedComponent("Impeller-1", "p")])
    assert sw_assembly.find_component_by_name(asm, "impeller") is asm.GetComponents(True)[0]
    assert sw_assembly.find_component_by_name(asm, "Impeller", case_sensitive=True).Name2 == "Impeller-1"
    with pytest.raises(ValueError, match="keyword 不能为空"):
        sw_assembly.find_component_by_name(asm, "")
    with pytest.raises(RuntimeError, match="未找到组件: impeller"):
        sw_assembly.find_component_by_name(asm, "impeller", case_sensitive=True)


class _AsmEditModel:
    def __init__(self):
        self.Extension = _RecordingExtension()
        self.suppress_calls = 0
        self.unsuppress_calls = 0
        self.replace_calls = []

    def EditSuppress(self):
        self.suppress_calls += 1

    def EditUnsuppress(self):
        self.unsuppress_calls += 1

    def ReplaceComponents2(self, new_path, config, options, flags, try_owner):
        self.replace_calls.append((new_path, config, options, flags, try_owner))
        return "replaced"


def test_suppress_and_unsuppress_component_select_first():
    asm = _AsmEditModel()
    sw_assembly.suppress_component(asm, "blk-1")
    assert asm.Extension.calls[0][:4] == ("blk-1", "COMPONENT", False, 0)
    assert asm.suppress_calls == 1

    sw_assembly.unsuppress_component(asm, "blk-1")
    assert asm.unsuppress_calls == 1


def test_replace_component_selects_then_replaces():
    asm = _AsmEditModel()
    assert sw_assembly.replace_component(asm, "old-1", r"C:\parts\new.sldprt") == "replaced"
    assert asm.Extension.calls[0][:4] == ("old-1", "COMPONENT", False, 0)
    assert asm.replace_calls == [(r"C:\parts\new.sldprt", "", False, 0, True)]


# ---------------------------------------------------------------------------
# 干涉检查
# ---------------------------------------------------------------------------
class _CompNoName2:
    Name = "b"


class _CompWithName2:
    def __init__(self, name):
        self.Name2 = name


class _InterferenceItemMixed:
    def IGetComponents(self):
        return (_CompWithName2("a"), _CompNoName2())


class _InterferenceItemBroken:
    def IGetComponents(self):
        raise RuntimeError("IGetComponents 不可用")


def test_read_component_names_mixed_and_broken():
    assert sw_assembly._read_component_names(_InterferenceItemMixed()) == ["a", "b"]
    assert sw_assembly._read_component_names(_InterferenceItemBroken()) == []


def test_interference_report_blocked_when_manager_is_none():
    class _ModelManagerNone:
        InterferenceDetectionManager = None

    report = sw_assembly.get_interference_detection(_ModelManagerNone())
    assert report["status"] == "blocked"
    assert report["api"] is None
    assert "未取得干涉检查管理器" in report["error"]


class _ModernManagerBase:
    TreatSubAssembliesAsComponents = True
    TreatCoincidenceAsInterference = True

    def Done(self):
        return None

    def GetInterferenceCount(self):
        return 1


def test_interference_modern_items_without_component_names():
    class _Manager(_ModernManagerBase):
        def GetInterferences(self):
            return [_InterferenceItemBroken()]

        def GetInterferenceComponents(self):
            return (_CompNoName2(),)

    class _Model:
        InterferenceDetectionManager = _Manager()

    report = sw_assembly.get_interference_detection(_Model())
    assert report["status"] == "warn"
    assert report["items"] == [{"index": 0}]
    assert report["interfering_components"] == ["b"]


def test_interference_modern_component_enumeration_failure_is_ignored():
    class _Manager(_ModernManagerBase):
        def GetInterferences(self):
            return []

        def GetInterferenceComponents(self):
            raise RuntimeError("GetInterferenceComponents 不可用")

    class _Model:
        InterferenceDetectionManager = _Manager()

    report = sw_assembly.get_interference_detection(_Model())
    assert report["items"] == []
    assert report["interfering_components"] == []


def test_interference_legacy_item_failure_recorded_as_error_entry():
    class _LegacyManager:
        TreatSubAssembliesAsComponents = True
        TreatCoincidenceAsInterference = True

        def Done(self):
            return None

        def GetInterferenceCount(self):
            return 1

        def GetInterference(self, index):
            raise RuntimeError("GetInterference 失败")

    class _Model:
        InterferenceDetection = _LegacyManager()

    report = sw_assembly.get_interference_detection(_Model())
    assert report["status"] == "warn"
    assert report["api"] == "legacy"
    assert report["items"] == [{"index": 0, "error": "GetInterference 失败"}]
    assert report["manual_review_required"] is True
