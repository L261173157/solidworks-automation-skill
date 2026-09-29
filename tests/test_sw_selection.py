"""声明式选择引擎单元测试 (伪 COM 对象, 无需 SolidWorks)。"""
import pytest

from scripts.sw_selection import (
    SelectionSpec,
    resolve_pair_for_mate,
    resolve_selection,
)

from fakes.sw import FakeComponent, FakeModelDoc, FakeSwApp, new_fake_session


@pytest.fixture()
def model():
    app, journal = new_fake_session()
    return app.NewDocument("C:/fake/templates/gb_part.prtdot", 0, 0, 0), journal


def test_coerce_accepts_string_dict_and_spec():
    spec = SelectionSpec.coerce("Front Plane")
    assert spec.kind == "named" and spec.name == "Front Plane"

    spec = SelectionSpec.coerce({"kind": "coordinate", "point_mm": [1, 2, 3]})
    assert spec.point_mm == (1.0, 2.0, 3.0)

    spec2 = SelectionSpec.coerce(spec)
    assert spec2 == spec

    with pytest.raises(ValueError):
        SelectionSpec.coerce({"kind": "teleport"})
    with pytest.raises(ValueError):
        SelectionSpec.coerce({"kind": "coordinate", "point_mm": [1, 2], "surprise": 1})
    with pytest.raises(ValueError):
        SelectionSpec.coerce({"kind": "coordinate", "point_mm": [1, 2]})


def test_plane_resolution_tries_alias_list(model):
    model_obj, _journal = model
    handle, evidence = resolve_selection(model_obj, {"kind": "plane"}, mark=1)
    assert evidence["status"] == "resolved"
    assert evidence["matched"] == "Front Plane"
    assert evidence["entity_type"] == "PLANE"


def test_named_legacy_string_with_hint(model):
    model_obj, _journal = model
    handle, evidence = resolve_selection(model_obj, "Front Plane", entity_type_hint="PLANE")
    assert evidence["status"] == "resolved"
    assert evidence["entity_type"] == "PLANE"


def test_coordinate_converts_mm_to_m(model):
    model_obj, _journal = model
    handle, evidence = resolve_selection(
        model_obj, {"kind": "coordinate", "point_mm": [10, 20, 30]}
    )
    assert evidence["status"] == "resolved"
    assert evidence["point_m"] == [0.01, 0.02, 0.03]


def test_component_unique_match_and_ambiguity():
    app, _journal = new_fake_session()
    asm = app.NewDocument("C:/fake/templates/gb_assembly.asmdot", 0, 0, 0)
    asm._add_component("box.SLDPRT")
    unique = asm.components[0]

    handle, evidence = resolve_selection(asm, {"kind": "component", "name": "box"})
    assert evidence["status"] == "resolved_object_only"
    assert handle is unique

    asm._add_component("box-lid.SLDPRT")
    handle, evidence = resolve_selection(asm, {"kind": "component", "name": "box"})
    assert evidence["status"] == "ambiguous"
    assert len(evidence["candidates"]) == 2


def test_face_pilot_without_bodies_returns_not_found(model):
    model_obj, _journal = model
    handle, evidence = resolve_selection(model_obj, {"kind": "face", "nth": 0})
    assert evidence["status"] == "not_found"
    assert "face" in evidence["message"]


def test_pair_for_mate_reports_selection_count(model):
    model_obj, _journal = model
    evidence1, evidence2, count = resolve_pair_for_mate(
        model_obj,
        {"kind": "plane", "name": "Front Plane"},
        {"kind": "plane", "name": "Top Plane"},
        mark=1,
    )
    assert evidence1["status"] == "resolved"
    assert evidence2["status"] == "resolved"
    assert count == 2
    assert evidence1["selection_count"] == 2


def test_pair_for_mate_second_failure_keeps_count_evidence(model):
    model_obj, _journal = model
    evidence1, evidence2, count = resolve_pair_for_mate(
        model_obj,
        {"kind": "plane", "name": "Front Plane"},
        {"kind": "component", "name": "不存在"},
        mark=1,
    )
    assert evidence1["status"] == "resolved"
    assert evidence2["status"] == "not_found"
