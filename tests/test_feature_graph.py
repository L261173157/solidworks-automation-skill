"""Feature Graph IR 纯逻辑单元测试: 校验、circle_array 展开、降级计划。"""
import math

import pytest

from scripts.feature_graph import lower_to_calls, validate_ir


def _valid_ir():
    return {
        "schemaVersion": "1.0",
        "name": "plate",
        "features": [
            {
                "id": "base",
                "op": "extrude_boss",
                "sketch": {
                    "plane": "Front Plane",
                    "shapes": [{"type": "rectangle", "x1": -25, "y1": -25, "x2": 25, "y2": 25}],
                },
                "depth_mm": 10,
            },
            {
                "id": "bore",
                "op": "extrude_cut",
                "sketch": {
                    "face_anchor": {"kind": "coordinate", "point_mm": [0, 0, 10]},
                    "shapes": [{"type": "circle", "cx": 0, "cy": 0, "r": 15}],
                },
                "through_all": True,
            },
        ],
    }


def test_valid_ir_passes():
    assert validate_ir(_valid_ir()) == []


def test_validation_catches_structure_errors():
    ir = _valid_ir()
    ir["schemaVersion"] = "0.9"
    ir["features"][1]["id"] = "base"  # id 重复
    ir["features"][1]["op"] = "revolve"  # 未知 op (v0.1 词表外)
    errors = validate_ir(ir)
    assert any("schemaVersion" in item for item in errors)
    assert any("重复" in item for item in errors)
    assert any("未知 op" in item for item in errors)


def test_validation_catches_bad_anchor_and_pattern_target():
    ir = _valid_ir()
    ir["features"].append(
        {
            "id": "pat",
            "op": "linear_pattern",
            "target": "不存在",
            "direction": [0, 0, 0],
            "spacing_mm": -1,
            "count": 1,
        }
    )
    ir["features"][1]["sketch"]["face_anchor"] = {"kind": "teleport"}
    errors = validate_ir(ir)
    assert any("target" in item for item in errors)
    assert any("零向量" in item or "direction" in item for item in errors)
    assert any("face_anchor.kind" in item for item in errors)
    assert any("spacing_mm" in item for item in errors)
    assert any("count" in item for item in errors)


def test_lower_to_calls_plan_order():
    plan = lower_to_calls(_valid_ir())
    ops = [step["op"] for step in plan]
    assert ops == [
        "sketch_start",
        "draw_rectangle",
        "sketch_end",
        "extrude_boss",
        "sketch_start",
        "draw_circle",
        "sketch_end",
        "extrude_cut",
    ]
    assert plan[0]["plane"] == "Front Plane"
    assert plan[4]["face_anchor"] == {"kind": "coordinate", "point_mm": [0, 0, 10]}
    assert plan[3]["depth_mm"] == 10
    assert plan[7]["through_all"] is True


def test_lower_to_calls_rejects_invalid_ir():
    with pytest.raises(ValueError):
        lower_to_calls({"schemaVersion": "1.0", "name": "x", "features": []})


def test_circle_array_expansion_matches_closed_form():
    ir = {
        "schemaVersion": "1.0",
        "name": "flange",
        "features": [
            {
                "id": "bolts",
                "op": "extrude_cut",
                "sketch": {
                    "plane": "Front Plane",
                    "shapes": [
                        {
                            "type": "circle_array",
                            "cx": 0,
                            "cy": 0,
                            "orbit_mm": 35,
                            "r_hole_mm": 4,
                            "count": 6,
                            "start_angle_deg": 0,
                        }
                    ],
                },
                "through_all": True,
            }
        ],
    }
    plan = lower_to_calls(ir)
    circles = [step for step in plan if step["op"] == "draw_circle"]
    assert len(circles) == 6
    for index, step in enumerate(circles):
        cx, cy, r = step["args"]
        angle = 2 * math.pi * index / 6
        assert abs(cx - 0.035 * math.cos(angle)) < 1e-12
        assert abs(cy - 0.035 * math.sin(angle)) < 1e-12
        assert abs(r - 0.004) < 1e-12


def test_midplane_flag_reaches_plan():
    ir = _valid_ir()
    ir["features"][0]["midplane"] = True
    plan = lower_to_calls(ir)
    assert plan[3]["midplane"] is True
