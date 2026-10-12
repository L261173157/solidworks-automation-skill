"""@brief sw_weldment 全分支补充测试（离线假 COM，绝不触发真实 SolidWorks）。"""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

from scripts import sw_weldment
from scripts.sw_weldment import (
    SW_CONNECTED_SEGMENTS_SIMPLE_CUT,
    SW_END_CONDITION_MITER,
    _active_configuration_name,
    _as_tuple,
    _body_cut_list_key,
    _body_length_key,
    _iter_feature_chain,
    create_structural_member,
    create_weldment_profile,
    ensure_cut_list,
    export_cut_list_csv,
    iter_features_recursive,
    read_property_manager,
    set_cut_list_properties,
    update_cut_list,
    weldment_evidence,
)

SCRIPTS_DIR = Path(sw_weldment.__file__).resolve().parent


def _variant_payload(variant):
    """@brief 兼容真实 pywin32 (_value) 与 fake COM 桩 (.value) 的取值。"""
    if hasattr(variant, "_value"):
        return variant._value
    return variant.value


@pytest.fixture(autouse=True)
def _ensure_vt_array_constant(monkeypatch):
    """@brief fake COM 桩的 pythoncom 缺少 VT_ARRAY, 与真实值 0x2000 对齐。"""
    if not hasattr(sw_weldment.pythoncom, "VT_ARRAY"):
        monkeypatch.setattr(sw_weldment.pythoncom, "VT_ARRAY", 0x2000, raising=False)


def test_module_import_fallback_paths_covered(monkeypatch):
    """@brief 以非包身份重新执行模块源码，覆盖 try/except 双导入回退分支 (13-17)。"""
    import scripts.sw_connect as real_connect
    import scripts.sw_document_data as real_document_data
    import scripts.sw_part as real_part
    import scripts.sw_preflight as real_preflight

    monkeypatch.setitem(sys.modules, "sw_connect", real_connect)
    monkeypatch.setitem(sys.modules, "sw_document_data", real_document_data)
    monkeypatch.setitem(sys.modules, "sw_part", real_part)
    monkeypatch.setitem(sys.modules, "sw_preflight", real_preflight)

    spec = importlib.util.spec_from_file_location("_sw_weldment_toplevel_probe", SCRIPTS_DIR / "sw_weldment.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module.__name__, None)

    assert module.get_com_member is real_connect.get_com_member
    assert module.save_document is real_connect.save_document
    assert module.set_custom_properties is real_document_data.set_custom_properties
    assert module._ensure_sketch_selected is real_part._ensure_sketch_selected


# ---------------------------------------------------------------------------
# 通用假对象
# ---------------------------------------------------------------------------
class _FakeConfiguration:
    def __init__(self, name="Default", cut_list_error=False, has_cut_list_items=True):
        self.Name = name
        self._cut_list_error = cut_list_error
        self._has_cut_list_items = has_cut_list_items

    def GetCutListItems(self):
        if self._cut_list_error:
            raise RuntimeError("GetCutListItems 失败")
        if not self._has_cut_list_items:
            raise AttributeError("GetCutListItems")
        return ()


class _FakeConfigurationManager:
    def __init__(self, configuration):
        self.ActiveConfiguration = configuration


class _FakeFeature:
    """@brief 支持特征链、子特征与属性管理器的最小特征桩。"""

    def __init__(self, name, type_name, specific=None, manager=None,
                 first_sub=None, sub_error=False):
        self.Name = name
        self._type_name = type_name
        self._next = None
        self._specific = specific
        self.CustomPropertyManager = manager
        self._first_sub = first_sub
        self._sub_error = sub_error

    def GetTypeName2(self):
        return self._type_name

    def GetNextFeature(self):
        return self._next

    def GetFirstSubFeature(self):
        if self._sub_error:
            raise RuntimeError("GetFirstSubFeature 失败")
        return self._first_sub

    def GetNextSubFeature(self):
        return None

    def GetSpecificFeature2(self):
        return self._specific


def _link_chain(features):
    for current, following in zip(features, features[1:]):
        current._next = following
    return features


class _FakeBodyFolder:
    def __init__(self, update_ok=True, body_count=3):
        self._update_ok = update_ok
        self._body_count = body_count

    def UpdateCutList(self):
        return self._update_ok

    def GetBodyCount(self):
        return self._body_count


class _FakeCutListPropertyManager:
    """@brief Add3 写入后 Get6 可回读（或强制失配）的属性管理器桩。"""

    def __init__(self, names=("长度",), echo=True):
        self.names = tuple(names)
        self.echo = echo
        self.added = []
        self.values = {}

    def GetNames(self):
        return self.names

    def Add3(self, name, prop_type, value, flag):
        self.added.append((name, prop_type, value, flag))
        if self.echo:
            self.values[name] = value
        return 1

    def Get6(self, name, cached, raw, resolved, was_resolved, linked):
        value = self.values.get(name, "MISMATCH" if not self.echo else "")
        raw.value = value
        resolved.value = value
        was_resolved.value = True
        linked.value = False
        return 1


class _FakeBody:
    def __init__(self, box=(0.0, 0.0, 0.0, 0.0254, 0.2054, 0.0254)):
        self._box = box

    def GetBodyBox(self):
        return self._box


class _FakeWeldModel:
    """@brief 支撑焊件封装的最小 ModelDoc2 桩。"""

    def __init__(self, features=(), bodies=None, configuration=None, rebuild_result=True):
        self.features = list(features)
        self._bodies = bodies
        self.ConfigurationManager = _FakeConfigurationManager(configuration or _FakeConfiguration())
        self._rebuild_result = rebuild_result
        self.clear_calls = 0
        self.FeatureManager = None

    def FirstFeature(self):
        return self.features[0] if self.features else None

    def ClearSelection2(self, flag):
        self.clear_calls += 1

    def GetBodies2(self, body_type, flag):
        return self._bodies

    def EditRebuild3(self):
        return self._rebuild_result


# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------
def test_as_tuple_normalization():
    assert _as_tuple(None) == ()
    assert _as_tuple("abc") == ("abc",)
    assert _as_tuple(()) == ()
    assert _as_tuple(["a", None, "b"]) == ("a", "b")


def test_active_configuration_name_reads_back():
    model = _FakeWeldModel(configuration=_FakeConfiguration("默认"))
    assert _active_configuration_name(model) == "默认"


def test_active_configuration_name_falls_back_to_empty():
    model = _FakeWeldModel(configuration=_FakeConfiguration(None))
    assert _active_configuration_name(model) == ""


def test_body_box_validation_requires_six_values():
    class _NoneBody:
        def GetBodyBox(self):
            return None

    class _ShortBody:
        def GetBodyBox(self):
            return (1.0, 2.0)

    with pytest.raises(RuntimeError, match="结构构件实体未返回 6 值包围盒"):
        _body_length_key(_NoneBody())
    with pytest.raises(RuntimeError, match="结构构件实体未返回 6 值包围盒"):
        _body_cut_list_key(_ShortBody())


def test_iter_feature_chain_guards_none_and_4096_limit():
    assert list(_iter_feature_chain(None, "GetNextFeature")) == []

    head = None
    for _ in range(4100):
        head = _ChainNode(head)
    assert len(list(_iter_feature_chain(head, "GetNextFeature"))) == 4096


class _ChainNode:
    def __init__(self, nxt):
        self._next = nxt

    def GetNextFeature(self):
        return self._next


def test_iter_features_recursive_walks_subfeatures_and_survives_errors():
    deep = _FakeFeature("深层", "CutListFolder")
    sub = _FakeFeature("子1", "CutListFolder", first_sub=deep)
    root1 = _FakeFeature("根部1", "WeldmentFeature", first_sub=sub)
    root2 = _FakeFeature("根部2", "ProfileFeature", sub_error=True)
    model = _FakeWeldModel(features=_link_chain([root1, root2]))

    assert [f.Name for f in iter_features_recursive(model)] == ["根部1", "子1", "深层", "根部2"]


# ---------------------------------------------------------------------------
# 焊件型材导出
# ---------------------------------------------------------------------------
def test_create_weldment_profile_success_with_properties(tmp_path, monkeypatch):
    target = tmp_path / "profiles" / "rect.sldlfp"
    selected = []
    prop_calls = []

    monkeypatch.setattr(
        sw_weldment, "_ensure_sketch_selected",
        lambda model, ref: (selected.append(ref) or "轮廓草图"),
    )

    def _fake_set_properties(model, properties, *, configuration_name="", save=False):
        prop_calls.append((properties, configuration_name, save))
        return {}

    monkeypatch.setattr(sw_weldment, "set_custom_properties", _fake_set_properties)

    def _fake_save(model, path):
        target.write_bytes(b"lfp-data")
        return True

    monkeypatch.setattr(sw_weldment, "save_document", _fake_save)

    model = _FakeWeldModel(configuration=_FakeConfiguration("Default"))
    result = create_weldment_profile(model, "轮廓草图", target, properties={"材料": "Q235"})

    assert result == {
        "path": str(target),
        "configuration_name": "Default",
        "size_bytes": 8,
        "properties": {"材料": "Q235"},
    }
    assert selected == ["轮廓草图", "轮廓草图"]  # 写属性后重新选中草图
    assert prop_calls == [({"材料": "Q235"}, "Default", False)]


def test_create_weldment_profile_without_properties(tmp_path, monkeypatch):
    target = tmp_path / "rect.sldlfp"
    calls = []

    def _fake_save(model, path):
        target.write_bytes(b"lfp")
        return True

    monkeypatch.setattr(sw_weldment, "_ensure_sketch_selected", lambda model, ref: calls.append(ref))
    monkeypatch.setattr(sw_weldment, "save_document", _fake_save)

    result = create_weldment_profile(_FakeWeldModel(), "草图1", target)
    assert result["properties"] == {}
    assert result["configuration_name"] == "Default"
    assert len(calls) == 1


def test_create_weldment_profile_rejects_wrong_suffix(tmp_path):
    with pytest.raises(ValueError, match="焊件型材输出必须使用 .sldlfp 扩展名"):
        create_weldment_profile(_FakeWeldModel(), "草图1", tmp_path / "rect.txt")


def test_create_weldment_profile_save_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sw_weldment, "_ensure_sketch_selected", lambda model, ref: ref)
    monkeypatch.setattr(sw_weldment, "save_document", lambda model, path: False)
    with pytest.raises(RuntimeError, match="自定义焊件型材保存失败"):
        create_weldment_profile(_FakeWeldModel(), "草图1", tmp_path / "rect.sldlfp")


def test_create_weldment_profile_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sw_weldment, "_ensure_sketch_selected", lambda model, ref: ref)
    monkeypatch.setattr(sw_weldment, "save_document", lambda model, path: True)
    with pytest.raises(RuntimeError, match="未生成 .sldlfp 文件"):
        create_weldment_profile(_FakeWeldModel(), "草图1", tmp_path / "rect.sldlfp")


def test_create_weldment_profile_zero_size_file_raises(tmp_path, monkeypatch):
    target = tmp_path / "rect.sldlfp"
    target.write_bytes(b"")
    monkeypatch.setattr(sw_weldment, "_ensure_sketch_selected", lambda model, ref: ref)
    monkeypatch.setattr(sw_weldment, "save_document", lambda model, path: True)
    with pytest.raises(RuntimeError, match="未生成 .sldlfp 文件"):
        create_weldment_profile(_FakeWeldModel(), "草图1", target)


# ---------------------------------------------------------------------------
# 结构构件创建
# ---------------------------------------------------------------------------
class _FakeGroup:
    def __init__(self):
        self.Segments = None
        self.ApplyCornerTreatment = None
        self.CornerTreatmentType = None
        self.MiterMergeCondition = None


class _FakeStructuralManager:
    def __init__(self, weldment_feature="weldment", structural_feature="feature"):
        self._weldment_feature = weldment_feature
        self._structural_feature = structural_feature
        self.groups_created = []
        self.insert_calls = []
        self.weldment_insert_calls = 0

    def InsertWeldmentFeature(self):
        self.weldment_insert_calls += 1
        return self._weldment_feature

    def CreateStructuralMemberGroup(self):
        group = _FakeGroup()
        self.groups_created.append(group)
        return group

    def InsertStructuralWeldment5(self, profile, connected_option, allow_protrusion, groups, configuration_name):
        raw_groups = _variant_payload(groups)
        self.insert_calls.append((profile, connected_option, allow_protrusion, tuple(raw_groups), configuration_name))
        return self._structural_feature


def _profile(tmp_path, name="rect.sldlfp"):
    profile = tmp_path / name
    profile.write_bytes(b"lfp")
    return profile


def test_create_structural_member_success_creates_weldment_feature(tmp_path):
    profile = _profile(tmp_path)
    seg1, seg2 = object(), object()
    manager = _FakeStructuralManager(structural_feature="feature")
    model = _FakeWeldModel(features=[_FakeFeature("Boss1", "ProfileFeature")])
    model.FeatureManager = manager

    feature = create_structural_member(model, profile, [[seg1], [seg2, None]], configuration_name="custom")

    assert feature == "feature"
    call = manager.insert_calls[0]
    assert call[0] == str(profile)
    assert call[1] == SW_CONNECTED_SEGMENTS_SIMPLE_CUT
    assert call[2] is False
    assert call[4] == "custom"
    assert len(call[3]) == 2
    first_group = call[3][0]
    assert first_group.ApplyCornerTreatment is True
    assert first_group.CornerTreatmentType == SW_END_CONDITION_MITER
    assert first_group.MiterMergeCondition is False
    assert first_group.Segments.varianttype == sw_weldment.pythoncom.VT_ARRAY | sw_weldment.pythoncom.VT_DISPATCH
    assert _variant_payload(call[3][1].Segments) == (seg2,)  # None 段被过滤
    assert manager.weldment_insert_calls == 1  # 原无焊件特征, 需先创建
    assert model.clear_calls == 2
    assert model.EditRebuild3() is True


def test_create_structural_member_skips_weldment_feature_when_present(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager()
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")])
    model.FeatureManager = manager

    create_structural_member(model, profile, [["seg"]], allow_protrusion=True)

    assert manager.weldment_insert_calls == 0
    assert model.clear_calls == 1  # 仅特性创建完成后的清理
    assert manager.insert_calls[0][2] is True


def test_create_structural_member_without_corner_treatment(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager()
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")])
    model.FeatureManager = manager

    create_structural_member(model, profile, [["seg"]], apply_corner_treatment=False)

    group = manager.insert_calls[0][3][0]
    assert group.ApplyCornerTreatment is False
    assert group.CornerTreatmentType is None
    assert group.MiterMergeCondition is None


def test_create_structural_member_accepts_empty_groups(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager()
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")])
    model.FeatureManager = manager

    create_structural_member(model, profile, [])

    assert manager.insert_calls[0][3] == ()


def test_create_structural_member_missing_or_bad_profile_raises(tmp_path):
    model = _FakeWeldModel()
    model.FeatureManager = _FakeStructuralManager()
    with pytest.raises(FileNotFoundError, match="焊件型材不存在或格式错误"):
        create_structural_member(model, tmp_path / "missing.sldlfp", [["s"]])
    with pytest.raises(FileNotFoundError, match="焊件型材不存在或格式错误"):
        create_structural_member(model, _profile(tmp_path, "rect.txt"), [["s"]])


def test_create_structural_member_empty_group_raises(tmp_path):
    profile = _profile(tmp_path)
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")])
    model.FeatureManager = _FakeStructuralManager()

    with pytest.raises(ValueError, match="结构构件第 2 组没有路径段"):
        create_structural_member(model, profile, [["seg"], [None]])


def test_create_structural_member_group_creation_failure_raises(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager()
    manager.CreateStructuralMemberGroup = lambda: None
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")])
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="CreateStructuralMemberGroup 未返回对象"):
        create_structural_member(model, profile, [["seg"]])


def test_create_structural_member_weldment_feature_failure_raises(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager(weldment_feature=None)
    model = _FakeWeldModel(features=[_FakeFeature("Boss1", "ProfileFeature")])
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="InsertWeldmentFeature 创建失败"):
        create_structural_member(model, profile, [["seg"]])


def test_create_structural_member_insert_failure_raises(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager(structural_feature=None)
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")])
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="InsertStructuralWeldment5 创建失败"):
        create_structural_member(model, profile, [["seg"]])


def test_create_structural_member_rebuild_failure_raises(tmp_path):
    profile = _profile(tmp_path)
    manager = _FakeStructuralManager()
    model = _FakeWeldModel(features=[_FakeFeature("焊件1", "WeldmentFeature")], rebuild_result=False)
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="结构构件创建后的模型重建失败"):
        create_structural_member(model, profile, [["seg"]])


# ---------------------------------------------------------------------------
# 切割清单创建
# ---------------------------------------------------------------------------
class _FakeCutListManager:
    def __init__(self, method_results=None, has_method=True):
        self.method_calls = []
        self._method_results = list(method_results or [])
        self._oleobj_ = None
        if has_method:
            self.InsertWeldmentCutList2 = self._insert

    def _insert(self, body_array):
        self.method_calls.append(body_array)
        if self._method_results:
            return self._method_results.pop(0)
        return "folder"


class _FakeOleObject:
    def __init__(self, result="folder"):
        self.calls = []
        self._result = result

    def Invoke(self, *args):
        self.calls.append(args)
        return self._result


def test_ensure_cut_list_returns_existing_folders_deduplicated():
    folder = _FakeFeature("切割清单项目1", "CutListFolder")
    duplicate = _FakeFeature("切割清单项目1", "CutListFolder")
    noise = _FakeFeature("Boss1", "ProfileFeature")
    model = _FakeWeldModel(features=_link_chain([noise, folder, duplicate]))

    assert ensure_cut_list(model) == [folder]


def test_ensure_cut_list_without_bodies_raises():
    model = _FakeWeldModel(bodies=None)
    with pytest.raises(RuntimeError, match="没有可写入切割清单的结构构件实体"):
        ensure_cut_list(model)


def test_ensure_cut_list_groups_bodies_by_box_key():
    short = _FakeBody(box=(0.0, 0.0, 0.0, 0.0254, 0.1, 0.0254))
    long_a = _FakeBody()
    long_b = _FakeBody()
    manager = _FakeCutListManager(method_results=["folder-1", "folder-2"])
    model = _FakeWeldModel(bodies=(long_a, short, long_b))
    model.FeatureManager = manager

    created = ensure_cut_list(model)

    assert created == ["folder-1", "folder-2"]
    assert _variant_payload(manager.method_calls[0]) == (short,)  # 排序靠前的短料先建清单
    assert _variant_payload(manager.method_calls[1]) == (long_a, long_b)


def test_ensure_cut_list_method_returns_none_raises():
    manager = _FakeCutListManager(method_results=[None])
    model = _FakeWeldModel(bodies=(_FakeBody(),))
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="InsertWeldmentCutList2 创建失败: length_key="):
        ensure_cut_list(model)


def test_ensure_cut_list_without_insert_api_and_ole_object_raises():
    model = _FakeWeldModel(bodies=(_FakeBody(),))
    model.FeatureManager = object()  # 既无方法也无 _oleobj_

    with pytest.raises(RuntimeError, match="FeatureManager 不支持 InsertWeldmentCutList2"):
        ensure_cut_list(model)


def test_ensure_cut_list_uses_ole_invoke_for_dispid_174():
    body = _FakeBody()
    ole = _FakeOleObject(result="folder")
    manager = _FakeCutListManager(has_method=False)
    manager._oleobj_ = ole
    model = _FakeWeldModel(bodies=(body,))
    model.FeatureManager = manager

    created = ensure_cut_list(model)

    assert created == ["folder"]
    dispid, flags, dispatch, want_result, body_array = ole.calls[0]
    assert (dispid, flags, dispatch, want_result) == (174, 0, sw_weldment.pythoncom.DISPATCH_METHOD, True)
    assert _variant_payload(body_array) == (body,)


def test_ensure_cut_list_ole_invoke_returns_none_raises():
    ole = _FakeOleObject(result=None)
    manager = _FakeCutListManager(has_method=False)
    manager._oleobj_ = ole
    model = _FakeWeldModel(bodies=(_FakeBody(),))
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="InsertWeldmentCutList2 创建失败"):
        ensure_cut_list(model)


def test_ensure_cut_list_rebuild_failure_raises():
    manager = _FakeCutListManager()
    model = _FakeWeldModel(bodies=(_FakeBody(),), rebuild_result=False)
    model.FeatureManager = manager

    with pytest.raises(RuntimeError, match="切割清单创建后的模型重建失败"):
        ensure_cut_list(model)


# ---------------------------------------------------------------------------
# 属性读写与证据回读
# ---------------------------------------------------------------------------
def test_read_property_manager_core_only_false_keeps_noise():
    class _Manager:
        def GetNames(self):
            return ("长度", "设计")

        def Get6(self, name, cached, raw, resolved, was_resolved, linked):
            raw.value = resolved.value = "v"
            was_resolved.value = linked.value = False
            return 1

    assert set(read_property_manager(_Manager(), core_only=False)) == {"长度", "设计"}


def test_read_property_manager_empty_names_returns_empty_dict():
    class _Manager:
        def GetNames(self):
            return ()

        def Get6(self, name, cached, raw, resolved, was_resolved, linked):
            raise AssertionError("不应读取任何属性")

    assert read_property_manager(_Manager()) == {}


def test_set_cut_list_properties_requires_properties():
    model = _FakeWeldModel()
    with pytest.raises(ValueError, match="切割清单属性不能为空"):
        set_cut_list_properties(model, {})


def test_set_cut_list_properties_writes_and_verifies_each_folder():
    manager_a = _FakeCutListPropertyManager()
    manager_b = _FakeCutListPropertyManager()
    folder_a = _FakeFeature("清单A", "CutListFolder", manager=manager_a)
    folder_b = _FakeFeature("清单B", "CutListFolder", manager=manager_b)
    duplicate = _FakeFeature("清单A", "CutListFolder")
    noise = _FakeFeature("Boss1", "ProfileFeature")
    model = _FakeWeldModel(features=_link_chain([noise, folder_a, duplicate, folder_b]))

    results = set_cut_list_properties(model, {"材料": "Q235"})

    # 同名文件夹去重, 不同名文件夹各自写入并回读。
    assert [item["name"] for item in results] == ["清单A", "清单B"]
    for item in results:
        assert item["properties"]["材料"]["raw"] == "Q235"
        assert item["properties"]["材料"]["add_status"] == 1
    assert manager_a.added == [("材料", 30, "Q235", 2)]
    assert manager_b.added == [("材料", 30, "Q235", 2)]


def test_set_cut_list_properties_empty_name_raises():
    folder = _FakeFeature("清单A", "CutListFolder", manager=_FakeCutListPropertyManager())
    model = _FakeWeldModel(features=[folder])

    with pytest.raises(ValueError, match="切割清单属性名不能为空"):
        set_cut_list_properties(model, {"  ": "x"})


def test_set_cut_list_properties_readback_mismatch_raises():
    manager = _FakeCutListPropertyManager(echo=False)
    folder = _FakeFeature("清单A", "CutListFolder", manager=manager)
    model = _FakeWeldModel(features=[folder])

    with pytest.raises(RuntimeError, match="切割清单属性回读失败: 清单A / 材料"):
        set_cut_list_properties(model, {"材料": "Q235"})


def test_update_cut_list_collects_folder_evidence():
    specific = _FakeBodyFolder(update_ok=True, body_count=3)
    manager = _FakeCutListPropertyManager()
    folder = _FakeFeature("清单A", "CutListFolder", specific=specific, manager=manager)
    duplicate = _FakeFeature("清单A", "CutListFolder")
    noise = _FakeFeature("Boss1", "ProfileFeature")
    model = _FakeWeldModel(features=_link_chain([noise, folder, duplicate]))

    folders = update_cut_list(model)

    assert folders == [{
        "name": "清单A",
        "type": "CutListFolder",
        "body_count": 3,
        "update_ok": True,
        "properties": {"长度": {
            "name": "长度",
            "status": 1,
            "raw": "",
            "resolved": "",
            "was_resolved": True,
            "linked": False,
        }},
    }]


def test_update_cut_list_without_body_folder_or_manager():
    folder = _FakeFeature("清单B", "CutListFolder", specific=None, manager=None)
    model = _FakeWeldModel(features=[folder])

    folders = update_cut_list(model)

    assert folders == [{
        "name": "清单B",
        "type": "CutListFolder",
        "body_count": 0,
        "update_ok": False,
        "properties": {},
    }]


def test_update_cut_list_update_failure_reported():
    specific = _FakeBodyFolder(update_ok=False, body_count=1)
    folder = _FakeFeature("清单C", "CutListFolder", specific=specific, manager=_FakeCutListPropertyManager())
    model = _FakeWeldModel(features=[folder])

    folders = update_cut_list(model)
    assert folders[0]["update_ok"] is False
    assert folders[0]["body_count"] == 1


class _CutListItem:
    def __init__(self, manager=None):
        self.CustomPropertyManager = manager


def test_weldment_evidence_collects_features_items_and_bodies():
    specific = _FakeBodyFolder(update_ok=True, body_count=2)
    manager = _FakeCutListPropertyManager()
    manager.values["长度"] = "205.4"
    folder = _FakeFeature("清单A", "CutListFolder", specific=specific, manager=manager)
    duplicate = _FakeFeature("清单A", "CutListFolder", specific=specific, manager=manager)
    member = _FakeFeature("结构构件1", "StructuralMember")
    weldment = _FakeFeature("焊件1", "WeldmentFeature")

    class _ConfigurationWithItems(_FakeConfiguration):
        def GetCutListItems(self):
            return (_CutListItem(manager=manager),)

    model = _FakeWeldModel(
        features=_link_chain([weldment, member, folder, duplicate]),
        bodies=(object(), object()),
        configuration=_ConfigurationWithItems("Default"),
    )

    evidence = weldment_evidence(model)

    assert evidence["configuration"] == "Default"
    assert evidence["features"] == [
        {"name": "焊件1", "type": "WeldmentFeature"},
        {"name": "结构构件1", "type": "StructuralMember"},
        {"name": "清单A", "type": "CutListFolder"},
    ]
    assert evidence["feature_types"] == ["WeldmentFeature", "StructuralMember", "CutListFolder"]
    assert evidence["solid_body_count"] == 2
    assert evidence["has_weldment_feature"] is True
    assert evidence["has_structural_member"] is True
    assert evidence["cut_list_folders"][0]["body_count"] == 2
    assert evidence["cut_list_items"] == [{
        "index": 1,
        "properties": {"长度": {
            "name": "长度", "status": 1, "raw": "205.4", "resolved": "205.4",
            "was_resolved": True, "linked": False,
        }},
    }]


def test_weldment_evidence_creates_missing_cut_list_when_requested():
    folder = _FakeFeature("清单A", "CutListFolder")
    model = _FakeWeldModel(features=[folder])

    evidence = weldment_evidence(model, create_missing_cut_list=True)

    assert evidence["cut_list_folders"][0]["name"] == "清单A"
    assert evidence["cut_list_folders"][0]["update_ok"] is False
    assert evidence["has_weldment_feature"] is False
    assert evidence["has_structural_member"] is False
    assert evidence["solid_body_count"] == 0


def test_weldment_evidence_handles_missing_cut_list_items_and_manager():
    member = _FakeFeature("结构构件1", "WeldMemberFeat")
    folder = _FakeFeature("清单B", "CutListFolder")
    model = _FakeWeldModel(
        features=_link_chain([member, folder]),
        configuration=_FakeConfiguration("Default", cut_list_error=True),
    )

    evidence = weldment_evidence(model)

    assert evidence["cut_list_items"] == []
    assert evidence["has_structural_member"] is True  # WeldMemberFeat 也算结构构件


def test_weldment_evidence_cut_list_items_without_manager():
    class _ConfigurationPlainItems(_FakeConfiguration):
        def GetCutListItems(self):
            return (_CutListItem(manager=None),)

    folder = _FakeFeature("清单B", "CutListFolder")
    model = _FakeWeldModel(
        features=[folder],
        configuration=_ConfigurationPlainItems("Default"),
        bodies=(_FakeBody(),),
    )

    evidence = weldment_evidence(model)

    assert evidence["cut_list_items"] == [{"index": 1, "properties": {}}]
    assert evidence["solid_body_count"] == 1


# ---------------------------------------------------------------------------
# CSV 导出
# ---------------------------------------------------------------------------
def test_export_cut_list_csv_falls_back_to_cut_list_items_and_raw_values(tmp_path):
    evidence = {
        "cut_list_folders": [],
        "cut_list_items": [
            {"index": 2, "properties": {"SOURCE_SKU": {"raw": "01760", "resolved": ""}}},
        ],
    }
    path = export_cut_list_csv(evidence, tmp_path / "items.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{"index": "2", "name": "", "body_count": "", "SOURCE_SKU": "01760"}]


def test_export_cut_list_csv_empty_evidence_writes_header_only(tmp_path):
    path = export_cut_list_csv({}, tmp_path / "empty.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["index", "name", "body_count"]
        assert list(reader) == []
