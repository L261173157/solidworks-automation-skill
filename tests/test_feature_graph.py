"""Feature Graph IR 纯逻辑单元测试: 校验、circle_array 展开、降级计划、v0.3 词表。"""
import math

import pytest

from scripts.feature_graph import lower_to_calls, validate_ir


def _valid_ir():
    return {
        "schemaVersion": "1.2",
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


def _shaft_ir():
    """revolve 阶梯轴: 两个共边矩形 + 唯一 centerline 作轴。"""
    return {
        "schemaVersion": "1.2",
        "name": "shaft",
        "features": [
            {
                "id": "body",
                "op": "revolve_boss",
                "sketch": {
                    "plane": "Front Plane",
                    "shapes": [
                        {"type": "rectangle", "x1": 0, "y1": 0, "x2": 43, "y2": 28},
                        {"type": "rectangle", "x1": 43, "y1": 0, "x2": 113, "y2": 32.5},
                        {"type": "centerline", "x1": -5, "y1": 0, "x2": 120, "y2": 0},
                    ],
                },
            }
        ],
    }


def _treatment_ir():
    """fillet + chamfer + linear_pattern (方向 = base 草图唯一 centerline)。"""
    return {
        "schemaVersion": "1.2",
        "name": "treated",
        "features": [
            {
                "id": "base",
                "op": "extrude_boss",
                "sketch": {
                    "plane": "Front Plane",
                    "shapes": [
                        {"type": "rectangle", "x1": -30, "y1": -20, "x2": 30, "y2": 20},
                        {"type": "centerline", "x1": -40, "y1": 0, "x2": 40, "y2": 0},
                    ],
                },
                "depth_mm": 8,
            },
            {
                "id": "round",
                "op": "fillet",
                "radius_mm": 5,
                "edges": [
                    {"kind": "edge", "point_mm": [-30, -20, 4]},
                    {"kind": "edge", "point_mm": [-30, 20, 4]},
                ],
            },
            {
                "id": "cham",
                "op": "chamfer",
                "distance_mm": 2,
                "angle_deg": 45,
                "edges": [
                    {"kind": "edge", "point_mm": [30, -20, 4]},
                    {"kind": "edge", "point_mm": [30, 20, 4]},
                ],
            },
            {
                "id": "pat",
                "op": "linear_pattern",
                "target": "base",
                "direction": {"sketch": "base"},
                "spacing_mm": 15,
                "count": 4,
            },
        ],
    }


def _grid_ir():
    """v0.3 双向网格: 底板草图(水平 centerline=方向1) + 凸台草图(竖直 centerline=方向2)。"""
    return {
        "schemaVersion": "1.2",
        "name": "grid",
        "features": [
            {
                "id": "plate",
                "op": "extrude_boss",
                "sketch": {
                    "plane": "Front Plane",
                    "shapes": [
                        {"type": "rectangle", "x1": -30, "y1": -20, "x2": 30, "y2": 20},
                        {"type": "centerline", "x1": -40, "y1": 0, "x2": 40, "y2": 0},
                    ],
                },
                "depth_mm": 8,
            },
            {
                "id": "boss",
                "op": "extrude_boss",
                "sketch": {
                    "face_anchor": {"kind": "coordinate", "point_mm": [0, 0, 8]},
                    "shapes": [
                        {"type": "rectangle", "x1": -27.5, "y1": -20, "x2": -17.5, "y2": -10},
                        {"type": "centerline", "x1": -22.5, "y1": -25, "x2": -22.5, "y2": 25},
                    ],
                },
                "depth_mm": 5,
                "flip": True,
            },
            {
                "id": "pat",
                "op": "linear_pattern",
                "target": "boss",
                "direction": {"sketch": "plate"},
                "direction2": {"sketch": "boss"},
                "spacing_mm": 15,
                "count": 4,
                "spacing2_mm": 15,
                "count2": 3,
            },
        ],
    }


def test_valid_ir_passes():
    assert validate_ir(_valid_ir()) == []
    assert validate_ir(_shaft_ir()) == []
    assert validate_ir(_treatment_ir()) == []
    assert validate_ir(_grid_ir()) == []


def test_validation_catches_structure_errors():
    ir = _valid_ir()
    ir["schemaVersion"] = "1.0"
    ir["features"][1]["id"] = "base"  # id 重复
    ir["features"][1]["op"] = "loft"  # 未知 op (词表外)
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
            "direction": [0, 0, 0],  # v0.2: direction 必须是 {sketch: ...}
            "spacing_mm": -1,
            "count": 1,
        }
    )
    ir["features"][1]["sketch"]["face_anchor"] = {"kind": "teleport"}
    errors = validate_ir(ir)
    assert any("target" in item for item in errors)
    assert any("direction" in item for item in errors)
    assert any("face_anchor" in item and "kind 非法" in item for item in errors)
    assert any("spacing_mm" in item for item in errors)
    assert any("count" in item for item in errors)


def test_revolve_requires_exactly_one_centerline():
    ir = _shaft_ir()
    ir["features"][0]["sketch"]["shapes"] = ir["features"][0]["sketch"]["shapes"][:2]  # 0 条
    assert any("centerline" in item for item in validate_ir(ir))

    ir = _shaft_ir()
    ir["features"][0]["sketch"]["shapes"].append(
        {"type": "centerline", "x1": 0, "y1": 10, "x2": 100, "y2": 10}
    )  # 2 条
    assert any("centerline" in item for item in validate_ir(ir))

    ir = _shaft_ir()
    ir["features"][0]["angle_deg"] = 361
    errors = validate_ir(ir)
    assert any("angle_deg" in item for item in errors)


def test_fillet_chamfer_validation():
    ir = _treatment_ir()
    ir["features"][1]["radius_mm"] = 0
    assert any("radius_mm" in item for item in validate_ir(ir))

    ir = _treatment_ir()
    ir["features"][1]["edges"] = []
    assert any("edges" in item for item in validate_ir(ir))

    ir = _treatment_ir()
    ir["features"][1]["edges"] = [{"kind": "face"}]  # face 是合法 kind, 结构层不报错
    assert not any("edges[0]" in item for item in validate_ir(ir))
    ir["features"][1]["edges"] = [{"kind": "teleport"}]
    assert any("kind 非法" in item for item in validate_ir(ir))

    ir = _treatment_ir()
    ir["features"][2]["angle_deg"] = 90
    assert any("angle_deg" in item for item in validate_ir(ir))

    ir = _treatment_ir()
    del ir["features"][2]["edges"]
    assert any("edges" in item for item in validate_ir(ir))


def test_linear_pattern_direction_validation():
    ir = _treatment_ir()
    ir["features"][3]["direction"] = {"sketch": "不存在"}
    assert any("direction.sketch" in item for item in validate_ir(ir))

    # 引用的草图必须恰含 1 条 centerline: 用 bore (无 centerline) 作方向。
    ir = _treatment_ir()
    ir["features"][3]["direction"] = {"sketch": "round"}  # fillet 无草图
    assert any("direction.sketch" in item for item in validate_ir(ir))

    # 前向引用禁止: direction 指向自己。
    ir = _treatment_ir()
    ir["features"][3]["direction"] = {"sketch": "pat"}
    assert any("direction.sketch" in item for item in validate_ir(ir))


def test_linear_pattern_direction2_validation():
    # 方向 2 与方向 1 引用同一草图 -> 拒绝 (每个草图只有 1 条 centerline)。
    ir = _grid_ir()
    ir["features"][2]["direction2"] = {"sketch": "plate"}
    errors = validate_ir(ir)
    assert any("direction2.sketch 必须引用与 direction 不同的草图特征" in item for item in errors)

    # 方向 2 引用无 centerline 的草图 -> 拒绝。
    ir = _grid_ir()
    ir["features"][2]["direction2"] = {"sketch": "不存在"}
    assert any("direction2.sketch" in item for item in validate_ir(ir))

    # direction2 存在但缺 count2/spacing2_mm -> 拒绝。
    ir = _grid_ir()
    del ir["features"][2]["count2"]
    del ir["features"][2]["spacing2_mm"]
    errors = validate_ir(ir)
    assert any("count2" in item for item in errors)
    assert any("spacing2_mm" in item for item in errors)

    # count2/spacing2_mm 存在但 direction2 缺失 -> 拒绝 (依赖关系)。
    ir = _grid_ir()
    del ir["features"][2]["direction2"]
    errors = validate_ir(ir)
    assert any("依赖 direction2" in item for item in errors)

    # axis 形态被词表拒绝 (真机否定: FeatureLinearPattern3 不消费基准轴)。
    ir = _grid_ir()
    ir["features"][2]["direction2"] = {"axis": "plate"}
    assert any("direction2" in item and "{sketch" in item for item in validate_ir(ir))

    # count2 < 2 -> 拒绝。
    ir = _grid_ir()
    ir["features"][2]["count2"] = 1
    assert any("count2" in item for item in validate_ir(ir))


def test_grid_lowering():
    plan = lower_to_calls(_grid_ir())
    pattern_step = plan[-1]
    assert pattern_step["op"] == "linear_pattern"
    assert pattern_step["direction"] == {"sketch": "plate"}
    assert pattern_step["direction2"] == {"sketch": "boss"}
    assert pattern_step["spacing2_mm"] == 15.0
    assert pattern_step["count2"] == 3

    # 单方向阵列的计划项不带 direction2 键。
    plan = lower_to_calls(_treatment_ir())
    assert "direction2" not in plan[-1]


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


def test_revolve_and_centerline_lowering():
    plan = lower_to_calls(_shaft_ir())
    ops = [step["op"] for step in plan]
    assert ops == [
        "sketch_start",
        "draw_rectangle",
        "draw_rectangle",
        "draw_centerline",
        "sketch_end",
        "revolve_boss",
    ]
    centerline_step = plan[3]
    assert centerline_step["args"] == [-0.005, 0.0, 0.12, 0.0]
    revolve_step = plan[5]
    assert revolve_step["angle_deg"] == 360.0  # 默认 360

    ir = _shaft_ir()
    ir["features"][0]["angle_deg"] = 270.5
    assert lower_to_calls(ir)[5]["angle_deg"] == 270.5


def test_treatment_lowering():
    plan = lower_to_calls(_treatment_ir())
    ops = [step["op"] for step in plan]
    assert ops == [
        "sketch_start",
        "draw_rectangle",
        "draw_centerline",
        "sketch_end",
        "extrude_boss",
        "fillet",
        "chamfer",
        "linear_pattern",
    ]
    assert plan[5]["radius_mm"] == 5.0
    assert plan[5]["edges"] == [
        {"kind": "edge", "point_mm": [-30, -20, 4]},
        {"kind": "edge", "point_mm": [-30, 20, 4]},
    ]
    assert plan[6]["distance_mm"] == 2.0
    assert plan[6]["angle_deg"] == 45.0
    assert plan[7]["direction"] == {"sketch": "base"}
    assert plan[7]["spacing_mm"] == 15.0
    assert plan[7]["count"] == 4


def test_lower_to_calls_rejects_invalid_ir():
    with pytest.raises(ValueError):
        lower_to_calls({"schemaVersion": "1.2", "name": "x", "features": []})


def test_circle_array_expansion_matches_closed_form():
    ir = {
        "schemaVersion": "1.2",
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
