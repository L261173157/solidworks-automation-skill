"""@brief sw_appearance 外观/材质容错封装的无 COM 回归测试。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from scripts import sw_appearance


def as_floats(values):
    """@brief 从真实 VARIANT 或普通序列中提取浮点列表。"""
    return [float(item) for item in getattr(values, "value", values)]


class FakeDocTarget:
    """@brief 文档级桩：成员是可调用方法，经 GetMaterialPropertyValues 回读。"""

    def __init__(self, readback=None):
        self.readback = list(readback) if readback is not None else None

    def MaterialPropertyValues(self, values):
        self.readback = as_floats(values)
        return True

    def GetMaterialPropertyValues(self):
        return self.readback


class FakeAttributeTarget:
    """@brief 以普通属性形式直接暴露材质数组。"""

    def __init__(self, values):
        self.MaterialPropertyValues = values


class FakeShortAttributeTarget(FakeAttributeTarget):
    """@brief 属性回读长度不足 9，必须退到方法回读。"""

    def GetMaterialPropertyValues(self):
        return [0.9] * 9


class FakeNullTarget:
    """@brief 两个读取接口都返回 None。"""

    MaterialPropertyValues = None

    def GetMaterialPropertyValues(self):
        return None


class FakeReadOnlyDoc:
    """@brief MaterialPropertyValues 是只读属性，写入必然抛异常。"""

    def __init__(self):
        self._values = [0.0] * 9

    @property
    def MaterialPropertyValues(self):
        return self._values


class FakeConfiguredDoc:
    """@brief 通过 Extension 按配置写入、经 GetMaterialPropertyValues 回读。"""

    def __init__(self):
        self.readback = None
        self.Extension = FakeExtension(self)

    def GetMaterialPropertyValues(self):
        return self.readback


class FakeExtension:
    """@brief 记录配置写入参数的文档扩展桩。"""

    def __init__(self, model):
        self.model = model
        self.calls = []

    def SetMaterialPropertyValues(self, values, option, names):
        self.calls.append((option, names))
        self.model.readback = as_floats(values)
        return True


class FakeVoidFeature:
    """@brief SetMaterialPropertyValues2 是无返回值的 void 方法。"""

    def __init__(self):
        self.readback = None
        self.option = None
        self.names = None

    def SetMaterialPropertyValues2(self, values, option, names):
        self.option = option
        self.names = names
        self.readback = as_floats(values)
        return None

    def GetMaterialPropertyValues(self):
        return self.readback


class FakeZeroFeature:
    """@brief SetMaterialPropertyValues2 返回 0 表示写入失败。"""

    def SetMaterialPropertyValues2(self, _values, _option, _names):
        return 0


class FakeComponentTarget:
    """@brief 组件级桩：SetMaterialPropertyValues2 写入 + 属性回读。"""

    def __init__(self):
        self._values = [0.0] * 9
        self.option = None
        self.names = None

    def SetMaterialPropertyValues2(self, values, option, names):
        self._values = as_floats(values)
        self.option = option
        self.names = names
        return True

    @property
    def MaterialPropertyValues(self):
        return self._values


class FakePropertyOnlyComponent:
    """@brief 没有 SetMaterialPropertyValues2，只能靠属性写入。"""

    def __init__(self):
        self._values = [0.5] * 9

    @property
    def MaterialPropertyValues(self):
        return self._values

    @MaterialPropertyValues.setter
    def MaterialPropertyValues(self, values):
        self._values = as_floats(values)


class FakeFeatureOnlyTarget:
    """@brief 文档与组件路径都失败，仅特征回退 SetMaterialPropertyValues 可用。"""

    def __init__(self):
        self.readback = None

    def SetMaterialPropertyValues(self, values):
        self.readback = as_floats(values)
        return True

    def GetMaterialPropertyValues(self):
        return self.readback


class FakeLyingComponent:
    """@brief 写入报告成功但回读颜色与预期不符。"""

    def SetMaterialPropertyValues2(self, _values, _option, _names):
        return True

    def GetMaterialPropertyValues(self):
        return [0.0, 0.0, 1.0, 0.35, 0.75, 0.45, 0.35, 0.0, 0.0]


class Recorder:
    """@brief 记录调用参数并返回预设结果的可调用成员。"""

    def __init__(self, result):
        self.calls = []
        self.result = result

    def method(self, *args):
        self.calls.append(args)
        return self.result


def test_rgb01_accepts_preset_and_hex_colors():
    assert sw_appearance.rgb01("iron_red") == pytest.approx((0x8A / 255, 0x0E / 255, 0x0E / 255))
    assert sw_appearance.rgb01("#2AA8FF") == pytest.approx((0x2A / 255, 0xA8 / 255, 0xFF / 255))
    assert sw_appearance.rgb01("D99A22") == pytest.approx((0xD9 / 255, 0x9A / 255, 0x22 / 255))


def test_rgb01_rejects_unknown_color():
    with pytest.raises(ValueError, match="未知颜色"):
        sw_appearance.rgb01("not_a_color")


@pytest.mark.parametrize("color", [(1, 2), (1, 2, 3, 4)])
def test_rgb01_rejects_wrong_length(color):
    with pytest.raises(ValueError, match="3 个值"):
        sw_appearance.rgb01(color)


def test_rgb01_converts_0_255_tuple():
    assert sw_appearance.rgb01((255, 128, 0)) == pytest.approx((1.0, 128 / 255, 0.0))


def test_rgb01_keeps_0_1_tuple():
    assert sw_appearance.rgb01((0.5, 0.25, 0.0)) == pytest.approx((0.5, 0.25, 0.0))


def test_material_values_uses_default_material_parameters():
    assert sw_appearance.material_values("#FFFFFF") == pytest.approx(
        [1.0, 1.0, 1.0, 0.35, 0.75, 0.45, 0.35, 0.0, 0.0]
    )


def test_material_values_accepts_custom_material_parameters():
    assert sw_appearance.material_values(
        "#FFFFFF",
        ambient=0.1,
        diffuse=0.2,
        specular=0.3,
        shininess=0.4,
        transparency=0.5,
        emission=0.6,
    ) == pytest.approx([1.0, 1.0, 1.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])


def test_material_variant_packs_nine_doubles():
    values = sw_appearance.material_variant([1, 0.5, 0.25, 0.35, 0.75, 0.45, 0.35, 0.0, 0.0])
    assert values.varianttype == sw_appearance.pythoncom.VT_ARRAY | sw_appearance.pythoncom.VT_R8
    assert list(values.value) == pytest.approx([1.0, 0.5, 0.25, 0.35, 0.75, 0.45, 0.35, 0.0, 0.0])


def test_material_variant_rejects_wrong_length():
    with pytest.raises(ValueError, match="9 个值"):
        sw_appearance.material_variant([0.0] * 8)


def test_configuration_args_default_configuration():
    option, names = sw_appearance._configuration_args("")
    assert option == sw_appearance.SW_THIS_CONFIGURATION == 1
    assert names is None


def test_configuration_args_specified_configuration():
    option, names = sw_appearance._configuration_args("默认")
    assert option == sw_appearance.SW_SPECIFY_CONFIGURATION == 3
    assert names.varianttype == sw_appearance.pythoncom.VT_ARRAY | sw_appearance.pythoncom.VT_BSTR
    assert list(names.value) == ["默认"]


def test_try_set_calls_callable_member_with_values_and_args():
    target = Recorder(True)
    assert sw_appearance._try_set(target, "method", "packed", "opt", "names") is True
    assert target.calls == [("packed", "opt", "names")]


def test_try_set_treats_none_result_as_success():
    assert sw_appearance._try_set(Recorder(None), "method", "packed") is True


def test_try_set_treats_falsy_result_as_failure():
    assert sw_appearance._try_set(Recorder(0), "method", "packed") is False


def test_try_set_assigns_non_callable_member():
    class Holder:
        value = None

    holder = Holder()
    assert sw_appearance._try_set(holder, "value", "packed") is True
    assert holder.value == "packed"


def test_try_set_returns_false_when_member_missing():
    assert sw_appearance._try_set(object(), "missing", "packed") is False


def test_try_set_returns_false_when_assignment_raises():
    assert sw_appearance._try_set(FakeReadOnlyDoc(), "MaterialPropertyValues", "packed") is False


def test_get_material_property_values_reads_attribute_first():
    target = FakeAttributeTarget([0.1] * 10)
    assert sw_appearance.get_material_property_values(target) == pytest.approx((0.1,) * 9)


def test_get_material_property_values_falls_back_to_method():
    target = FakeShortAttributeTarget([0.1, 0.2, 0.3])
    assert sw_appearance.get_material_property_values(target) == pytest.approx((0.9,) * 9)


def test_get_material_property_values_returns_method_values_without_attribute():
    target = FakeDocTarget([0.25] * 9)
    assert sw_appearance.get_material_property_values(target) == pytest.approx((0.25,) * 9)


def test_get_material_property_values_returns_none_when_readers_return_none():
    assert sw_appearance.get_material_property_values(FakeNullTarget()) is None


def test_get_material_property_values_returns_none_when_readers_raise():
    assert sw_appearance.get_material_property_values(object()) is None


def test_verify_appearance_reports_match():
    report = sw_appearance.verify_appearance(
        FakeDocTarget([1.0, 0.0, 0.0, 0.35, 0.75, 0.45, 0.35, 0.0, 0.0]), "#FF0000"
    )
    assert report["ok"] is True
    assert report["expected_rgb"] == pytest.approx((1.0, 0.0, 0.0))
    assert report["actual_rgb"] == pytest.approx((1.0, 0.0, 0.0))
    assert report["max_error"] == pytest.approx(0.0)


def test_verify_appearance_reports_mismatch_beyond_tolerance():
    report = sw_appearance.verify_appearance(FakeLyingComponent(), "signal_red", tolerance=0.01)
    assert report["ok"] is False
    assert report["expected_rgb"] == pytest.approx((227 / 255, 27 / 255, 53 / 255))
    assert report["actual_rgb"] == pytest.approx((0.0, 0.0, 1.0))
    assert report["max_error"] == pytest.approx(227 / 255)


def test_verify_appearance_handles_unreadable_target():
    report = sw_appearance.verify_appearance(object(), "#FF0000")
    assert report["ok"] is False
    assert report["actual_rgb"] is None
    assert report["max_error"] is None


def test_set_document_appearance_default_configuration_succeeds():
    model = FakeDocTarget()
    assert sw_appearance.set_document_appearance(model, "arc_blue") is True
    assert model.readback[:3] == pytest.approx((0x2A / 255, 0xA8 / 255, 0xFF / 255))


def test_set_document_appearance_with_configuration_uses_extension():
    model = FakeConfiguredDoc()
    assert sw_appearance.set_document_appearance(model, "white", "默认") is True
    option, names = model.Extension.calls[0]
    assert option == sw_appearance.SW_SPECIFY_CONFIGURATION
    assert list(names.value) == ["默认"]
    assert model.readback[:3] == pytest.approx((242 / 255,) * 3)


def test_set_document_appearance_without_extension_fails():
    class NoExtension:
        pass

    assert sw_appearance.set_document_appearance(NoExtension(), "white", "默认") is False


def test_set_document_appearance_fails_when_readback_missing():
    class WriteOnly:
        def MaterialPropertyValues(self, _values):
            return True

    assert sw_appearance.set_document_appearance(WriteOnly(), "white") is False


def test_set_document_appearance_fails_when_write_raises():
    assert sw_appearance.set_document_appearance(FakeReadOnlyDoc(), "white") is False


def test_set_feature_appearance_uses_version2_api_with_void_result():
    feature = FakeVoidFeature()
    assert sw_appearance.set_feature_appearance(feature, "armor_gold") is True
    assert feature.option == sw_appearance.SW_THIS_CONFIGURATION
    assert feature.names is None
    assert feature.readback[:3] == pytest.approx((0xD9 / 255, 0x9A / 255, 0x22 / 255))


def test_set_feature_appearance_falls_back_without_configuration():
    feature = FakeFeatureOnlyTarget()
    assert sw_appearance.set_feature_appearance(feature, "white") is True
    assert feature.readback[:3] == pytest.approx((242 / 255,) * 3)


def test_set_feature_appearance_with_configuration_needs_version2():
    assert sw_appearance.set_feature_appearance(FakeFeatureOnlyTarget(), "white", "默认") is False


def test_set_feature_appearance_fails_when_version2_returns_zero():
    assert sw_appearance.set_feature_appearance(FakeZeroFeature(), "white") is False


def test_set_feature_appearance_fails_when_readback_missing():
    class Silent:
        def SetMaterialPropertyValues2(self, *_args):
            return True

    assert sw_appearance.set_feature_appearance(Silent(), "white") is False


def test_set_component_appearance_uses_version2_api():
    component = FakeComponentTarget()
    assert sw_appearance.set_component_appearance(component, "signal_red") is True
    assert component.option == sw_appearance.SW_THIS_CONFIGURATION
    assert component.names is None
    assert component.MaterialPropertyValues[:3] == pytest.approx((227 / 255, 27 / 255, 53 / 255))


def test_set_component_appearance_falls_back_to_property():
    component = FakePropertyOnlyComponent()
    assert sw_appearance.set_component_appearance(component, "black") is True
    assert component.MaterialPropertyValues[:3] == pytest.approx((5 / 255,) * 3)


def test_set_component_appearance_with_configuration_needs_version2():
    assert sw_appearance.set_component_appearance(FakePropertyOnlyComponent(), "black", "M6") is False


def test_set_component_appearance_fails_when_readback_missing():
    class Silent:
        def SetMaterialPropertyValues2(self, *_args):
            return True

    assert sw_appearance.set_component_appearance(Silent(), "white") is False


def test_apply_component_palette_reports_each_assignment():
    assignments = [(FakeComponentTarget(), "white"), (FakeComponentTarget(), "black")]
    reports = sw_appearance.apply_component_palette(assignments)
    assert [report["index"] for report in reports] == [0, 1]
    assert [report["color"] for report in reports] == ["white", "black"]
    assert all(report["ok"] for report in reports)


def test_apply_component_palette_accepts_empty_assignments():
    assert sw_appearance.apply_component_palette([]) == []


def test_apply_component_palette_strict_raises_with_evidence():
    with pytest.raises(RuntimeError, match="组件 0 外观验证失败"):
        sw_appearance.apply_component_palette([(FakeLyingComponent(), "signal_red")])


def test_apply_component_palette_lenient_collects_failures():
    reports = sw_appearance.apply_component_palette(
        [(FakeLyingComponent(), "signal_red"), (FakeComponentTarget(), "white")], strict=False
    )
    assert [report["ok"] for report in reports] == [False, True]
    assert reports[0]["index"] == 0
    assert reports[1]["index"] == 1


def test_apply_named_appearance_prefers_document_path():
    assert sw_appearance.apply_named_appearance(FakeDocTarget(), "white") is True


def test_apply_named_appearance_falls_back_to_component_path():
    assert sw_appearance.apply_named_appearance(FakeComponentTarget(), "white") is True


def test_apply_named_appearance_falls_back_to_feature_path():
    assert sw_appearance.apply_named_appearance(FakeFeatureOnlyTarget(), "white") is True


def test_apply_named_appearance_returns_false_when_all_paths_fail():
    assert sw_appearance.apply_named_appearance(object(), "white") is False


def test_module_import_falls_back_to_top_level_preflight(monkeypatch):
    """@brief 失去包上下文时必须能按顶层模块回退导入 sw_preflight。"""
    fake_preflight = types.ModuleType("sw_preflight")
    sentinel = (object(), object(), object())
    fake_preflight.import_com_dependencies = lambda: sentinel
    monkeypatch.setitem(sys.modules, "sw_preflight", fake_preflight)

    spec = importlib.util.spec_from_file_location(
        "sw_appearance_top_level_probe", Path(sw_appearance.__file__)
    )
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "sw_appearance_top_level_probe", probe)
    spec.loader.exec_module(probe)

    assert probe.import_com_dependencies() is sentinel
    assert probe.pythoncom is sentinel[0]
    assert probe.VARIANT is sentinel[2]
    assert probe.PRESET_COLORS["iron_red"] == "#8A0E0E"
