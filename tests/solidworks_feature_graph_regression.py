"""真机回归: Feature Graph IR — 参数化件双路构建对比 (v0.3: 8 场景)。

需要 Windows + SolidWorks + pywin32; 不被 pytest 收集 (无 test_ 前缀)::

    python tests/solidworks_feature_graph_regression.py --output-dir <目录>

每个部件: 直调库函数构建参照件 -> build_from_ir 构建试点件 ->
compare_documents 必须 verified -> 闭式解体积独立校验 (防止双路同错)。
v0.2: revolve 阶梯轴 / fillet+chamfer 块 / 特征级 linear_pattern /
反向门 collect_ir 往返校验。
v0.3: linear_pattern 方向 2 (mark 2) 双向网格 + D2/D4 往返校验
(方向实体仅中心线段; 基准轴/边线真机否定见 tests/probe_dir2_axis.py)。
"""
from __future__ import annotations

import argparse
import json
import math
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "scripts"))

from sw_compare import compare_documents, collect_model_fingerprint  # noqa: E402
from sw_connect import (  # noqa: E402
    connect_solidworks,
    get_com_member,
    new_document,
    open_document,
    save_document,
)
import feature_graph as feature_graph_module  # noqa: E402
from sw_part import (  # noqa: E402
    end_sketch,
    extrude_boss,
    extrude_cut,
    fillet,
    chamfer,
    find_centerline_segment,
    linear_pattern,
    revolve_boss,
    sketch_circle,
    sketch_corner_rectangle,
    start_sketch,
    _select_com_object,
)
from sw_selection import resolve_selection  # noqa: E402


def _sketch_on_face(model, point_mm):
    model.ClearSelection2(True)
    _handle, evidence = resolve_selection(model, {"kind": "coordinate", "point_mm": point_mm}, mark=1)
    if evidence.get("status") != "resolved":
        raise RuntimeError(f"面锚点选择失败: {evidence}")
    model.SketchManager.InsertSketch(True)


def _volume_of(sw, path: Path) -> float:
    model = open_document(sw, str(path), silent=True, raise_on_error=False)
    if model is None:
        raise RuntimeError(f"无法打开: {path}")
    try:
        fingerprint = collect_model_fingerprint(model)
    finally:
        sw.CloseDoc(model.GetTitle)
    volume = (fingerprint.get("metrics") or {}).get("volume_mm3")
    if volume is None:
        raise RuntimeError(f"体积不可用: {path}")
    return float(volume)


def _assert_volume(sw, path: Path, expected_mm3: float, label: str) -> float:
    volume = _volume_of(sw, path)
    delta = abs(volume - expected_mm3) / expected_mm3
    if delta > 0.02:
        raise RuntimeError(f"{label} 体积偏离闭式解: got {volume:.1f}, expect {expected_mm3:.1f} ({delta:.2%})")
    return volume


def _box_with_pattern_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    sketch_corner_rectangle(model, -0.025, -0.025, 0.025, 0.025)
    ref = end_sketch(model)
    boss = extrude_boss(model, ref.name, 0.01)
    if boss is None:
        raise RuntimeError("参照件基体拉伸失败")
    _sketch_on_face(model, [0, 0, 10])
    sketch_circle(model, -0.015, -0.015, 0.0025)
    sketch_circle(model, -0.015, 0.015, 0.0025)
    sketch_circle(model, 0.015, -0.015, 0.0025)
    sketch_circle(model, 0.015, 0.015, 0.0025)
    cut_ref = end_sketch(model)
    cut = extrude_cut(model, cut_ref.name, 0)
    if cut is None:
        raise RuntimeError("参照件孔切除失败")
    path = out_dir / "ref_box_pattern.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def _flange_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    sketch_circle(model, 0, 0, 0.05)
    ref = end_sketch(model)
    boss = extrude_boss(model, ref.name, 0.012)
    if boss is None:
        raise RuntimeError("参照件法兰盘失败")
    _sketch_on_face(model, [30, 0, 12])
    sketch_circle(model, 0, 0, 0.015)
    for index in range(6):
        angle = 2 * math.pi * index / 6
        sketch_circle(model, 0.035 * math.cos(angle), 0.035 * math.sin(angle), 0.004)
    cut_ref = end_sketch(model)
    cut = extrude_cut(model, cut_ref.name, 0)
    if cut is None:
        raise RuntimeError("参照件法兰孔切除失败")
    path = out_dir / "ref_flange.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def _bracket_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    ref = end_sketch(model)
    if extrude_boss(model, ref.name, 0.008) is None:
        raise RuntimeError("参照件底板失败")
    _sketch_on_face(model, [0, 0, 8])
    sketch_corner_rectangle(model, 0.02, -0.02, 0.03, 0.02)
    wall_ref = end_sketch(model)
    # 真机实测: 面上草图默认拉伸方向朝材料内, 参照件同样翻转保持双路一致。
    if extrude_boss(model, wall_ref.name, 0.03, direction=False) is None:
        raise RuntimeError("参照件立壁失败")
    _sketch_on_face(model, [0, 0, 8])
    sketch_circle(model, -0.015, 0, 0.003)
    hole_ref = end_sketch(model)
    if extrude_cut(model, hole_ref.name, 0) is None:
        raise RuntimeError("参照件底板孔失败")
    path = out_dir / "ref_bracket.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


IR_BOX_PATTERN = {
    "schemaVersion": "1.2",
    "name": "ir_box_pattern",
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
            "id": "holes",
            "op": "extrude_cut",
            "sketch": {
                "face_anchor": {"kind": "coordinate", "point_mm": [0, 0, 10]},
                "shapes": [
                    {"type": "circle", "cx": -15, "cy": -15, "r": 2.5},
                    {"type": "circle", "cx": -15, "cy": 15, "r": 2.5},
                    {"type": "circle", "cx": 15, "cy": -15, "r": 2.5},
                    {"type": "circle", "cx": 15, "cy": 15, "r": 2.5},
                ],
            },
            "through_all": True,
        },
    ],
}

IR_FLANGE = {
    "schemaVersion": "1.2",
    "name": "ir_flange",
    "features": [
        {
            "id": "disc",
            "op": "extrude_boss",
            "sketch": {"plane": "Front Plane", "shapes": [{"type": "circle", "cx": 0, "cy": 0, "r": 50}]},
            "depth_mm": 12,
        },
        {
            "id": "bore_and_bolts",
            "op": "extrude_cut",
            "sketch": {
                "face_anchor": {"kind": "coordinate", "point_mm": [30, 0, 12]},
                "shapes": [
                    {"type": "circle", "cx": 0, "cy": 0, "r": 15},
                    {
                        "type": "circle_array",
                        "cx": 0,
                        "cy": 0,
                        "orbit_mm": 35,
                        "r_hole_mm": 4,
                        "count": 6,
                        "start_angle_deg": 0,
                    },
                ],
            },
            "through_all": True,
        },
    ],
}

IR_BRACKET = {
    "schemaVersion": "1.2",
    "name": "ir_bracket",
    "features": [
        {
            "id": "base",
            "op": "extrude_boss",
            "sketch": {
                "plane": "Front Plane",
                "shapes": [{"type": "rectangle", "x1": -30, "y1": -20, "x2": 30, "y2": 20}],
            },
            "depth_mm": 8,
        },
        {
            "id": "wall",
            "op": "extrude_boss",
            "sketch": {
                "face_anchor": {"kind": "coordinate", "point_mm": [0, 0, 8]},
                "shapes": [{"type": "rectangle", "x1": 20, "y1": -20, "x2": 30, "y2": 20}],
            },
            "depth_mm": 30,
            "flip": True,
        },
        {
            "id": "base_hole",
            "op": "extrude_cut",
            "sketch": {
                "face_anchor": {"kind": "coordinate", "point_mm": [0, 0, 8]},
                "shapes": [{"type": "circle", "cx": -15, "cy": 0, "r": 3}],
            },
            "through_all": True,
        },
    ],
}


def _ir_scenario(sw, out_dir: Path, label: str, ir: dict, reference_path: Path, expected_mm3: float) -> dict:
    ir_path = out_dir / f"{ir['name']}.SLDPRT"
    build = feature_graph_module.build_from_ir(
        sw, ir, str(ir_path), overwrite=True, record_evidence_dir=out_dir
    )
    if build.get("status") != "ok":
        raise RuntimeError(f"IR 构建失败: {build.get('error')}")
    comparison = compare_documents(sw, str(reference_path), str(ir_path))
    if comparison.get("verdict") != "verified":
        raise RuntimeError(f"{label} 对比未 verified: {comparison.get('verdict')} {comparison.get('error')}")
    volume = _assert_volume(sw, ir_path, expected_mm3, label)
    return {"compare": "verified", "volume_mm3": round(volume, 1)}


def scenario_box_pattern(sw, out_dir: Path) -> dict:
    reference = _box_with_pattern_reference(sw, out_dir)
    expected = 50 * 50 * 10 - 4 * math.pi * 2.5 * 2.5 * 10
    detail = _ir_scenario(sw, out_dir, "box_pattern", IR_BOX_PATTERN, reference, expected)
    _assert_volume(sw, reference, expected, "box_pattern 参照件")
    return detail


def scenario_flange(sw, out_dir: Path) -> dict:
    reference = _flange_reference(sw, out_dir)
    expected = math.pi * 12 * (50**2 - 15**2 - 6 * 4**2)
    detail = _ir_scenario(sw, out_dir, "flange", IR_FLANGE, reference, expected)
    _assert_volume(sw, reference, expected, "flange 参照件")
    return detail


def scenario_bracket(sw, out_dir: Path) -> dict:
    reference = _bracket_reference(sw, out_dir)
    expected = 60 * 40 * 8 + 10 * 40 * 30 - math.pi * 3 * 3 * 8
    detail = _ir_scenario(sw, out_dir, "bracket", IR_BRACKET, reference, expected)
    _assert_volume(sw, reference, expected, "bracket 参照件")
    return detail


# ---------------------------------------------------------------------------
# v0.2 场景: revolve / fillet+chamfer / 特征级 linear_pattern / 反向门往返
# ---------------------------------------------------------------------------

IR_SHAFT = {
    "schemaVersion": "1.2",
    "name": "ir_shaft",
    "features": [
        {
            # 真机实测: 共边双矩形的草图有两个闭合区域, FeatureRevolve2 拒绝多轮廓;
            # 阶梯轴拆成两段独立 revolve (轴向区间不相交, 各含唯一 centerline)。
            "id": "seg_small",
            "op": "revolve_boss",
            "sketch": {
                "plane": "Front Plane",
                "shapes": [
                    {"type": "rectangle", "x1": 0, "y1": 0, "x2": 43, "y2": 28},
                    {"type": "centerline", "x1": -5, "y1": 0, "x2": 48, "y2": 0},
                ],
            },
        },
        {
            "id": "seg_large",
            "op": "revolve_boss",
            "sketch": {
                "plane": "Front Plane",
                "shapes": [
                    {"type": "rectangle", "x1": 43, "y1": 0, "x2": 113, "y2": 32.5},
                    {"type": "centerline", "x1": 38, "y1": 0, "x2": 118, "y2": 0},
                ],
            },
        },
    ],
}

IR_FILLET_CHAMFER = {
    "schemaVersion": "1.2",
    "name": "ir_treated",
    "features": [
        {
            "id": "block",
            "op": "extrude_boss",
            "sketch": {
                "plane": "Front Plane",
                "shapes": [{"type": "rectangle", "x1": -30, "y1": -20, "x2": 30, "y2": 20}],
            },
            "depth_mm": 8,
        },
        {
            "id": "round",
            "op": "fillet",
            "radius_mm": 5,
            "edges": [
                {"kind": "edge", "point_mm": [-30, -20, -4]},
                {"kind": "edge", "point_mm": [-30, 20, -4]},
            ],
        },
        {
            "id": "cham",
            "op": "chamfer",
            "distance_mm": 2,
            "angle_deg": 45,
            "edges": [
                {"kind": "edge", "point_mm": [30, -20, -4]},
                {"kind": "edge", "point_mm": [30, 20, -4]},
            ],
        },
    ],
}

IR_PATTERN_PLATE = {
    "schemaVersion": "1.2",
    "name": "ir_pattern_plate",
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
                "shapes": [{"type": "rectangle", "x1": -27.5, "y1": -5, "x2": -17.5, "y2": 5}],
            },
            "depth_mm": 5,
            "flip": True,
        },
        {
            "id": "pat",
            "op": "linear_pattern",
            "target": "boss",
            "direction": {"sketch": "plate"},
            "spacing_mm": 15,
            "count": 4,
        },
    ],
}

IR_ROUNDTRIP = {
    "schemaVersion": "1.2",
    "name": "ir_roundtrip",
    "features": IR_PATTERN_PLATE["features"]
    + [
        {
            "id": "round",
            "op": "fillet",
            "radius_mm": 3,
            "edges": [
                {"kind": "edge", "point_mm": [-30, -20, -4]},
                {"kind": "edge", "point_mm": [-30, 20, -4]},
                {"kind": "edge", "point_mm": [30, -20, -4]},
                {"kind": "edge", "point_mm": [30, 20, -4]},
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# v0.3 场景: linear_pattern 方向 2 (mark 2) 双向网格 + D2/D4 往返校验
# ---------------------------------------------------------------------------

IR_PATTERN_GRID = {
    "schemaVersion": "1.2",
    "name": "ir_pattern_grid",
    "features": [
        {
            # 底板草图: 矩形 + 水平 centerline (方向 1 实体)。
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
            # 凸台草图: 矩形 + 竖直 centerline (方向 2 实体; 必须引用另一个草图)。
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
            # 4x3 网格: 方向 1 沿板中心线 (X) 4 实例, 方向 2 沿凸台草图中心线 (Y) 3 实例。
            "id": "grid",
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


def _shaft_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    model.SketchManager.CreateCenterLine(-0.005, 0, 0, 0.048, 0, 0)
    sketch_corner_rectangle(model, 0, 0, 0.043, 0.028)
    ref1 = end_sketch(model)
    if revolve_boss(model, ref1.name, 2 * math.pi) is None:
        raise RuntimeError("参照件阶梯轴小段旋转失败")
    start_sketch(model, "Front Plane")
    model.SketchManager.CreateCenterLine(0.038, 0, 0, 0.118, 0, 0)
    sketch_corner_rectangle(model, 0.043, 0, 0.113, 0.0325)
    ref2 = end_sketch(model)
    if revolve_boss(model, ref2.name, 2 * math.pi) is None:
        raise RuntimeError("参照件阶梯轴大段旋转失败")
    path = out_dir / "ref_shaft.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def _vertical_edges(model):
    """竖直直线边谓词 (z 向, 端点坐标), 返回 [(边对象, x_mm, y_mm)]。"""
    model.ForceRebuild3(False)
    edges = []
    for body in model.GetBodies2(0, False):
        for edge in body.GetEdges():
            curve = get_com_member(edge, "GetCurve")
            if not bool(get_com_member(curve, "IsLine")):
                continue
            start_vertex = get_com_member(edge, "GetStartVertex")
            end_vertex = get_com_member(edge, "GetEndVertex")
            if not start_vertex or not end_vertex:
                continue
            start = tuple(float(v) for v in get_com_member(start_vertex, "GetPoint"))
            end = tuple(float(v) for v in get_com_member(end_vertex, "GetPoint"))
            deltas = [abs(end[i] - start[i]) for i in range(3)]
            if deltas[2] > 0.005 and deltas[0] < 1e-6 and deltas[1] < 1e-6:
                edges.append((edge, start[0] * 1000.0, start[1] * 1000.0))
    return edges


def _block_reference(model):
    start_sketch(model, "Front Plane")
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    ref = end_sketch(model)
    if extrude_boss(model, ref.name, 0.008) is None:
        raise RuntimeError("参照件块体失败")


def _apply_treatment(model, fillet_set, chamfer_set):
    model.ClearSelection2(True)
    for index, edge in enumerate(fillet_set):
        if not _select_com_object(edge, append=index > 0, mark=1):
            raise RuntimeError("参照件圆角选边失败")
    if fillet(model, 0.005) is None:
        raise RuntimeError("参照件圆角失败")
    model.ClearSelection2(True)
    for index, edge in enumerate(chamfer_set):
        if not _select_com_object(edge, append=index > 0, mark=1):
            raise RuntimeError("参照件倒角选边失败")
    if chamfer(model, 0.002, 45) is None:
        raise RuntimeError("参照件倒角失败")


def _fillet_chamfer_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    _block_reference(model)
    edges = _vertical_edges(model)
    fillet_set = [edge for edge, x, _y in edges if abs(x + 30) < 0.5]
    chamfer_set = [edge for edge, x, _y in edges if abs(x - 30) < 0.5]
    if len(fillet_set) != 2 or len(chamfer_set) != 2:
        raise RuntimeError(f"参照件边过滤数量异常: {len(fillet_set)}/{len(chamfer_set)}")
    _apply_treatment(model, fillet_set, chamfer_set)
    path = out_dir / "ref_treated.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def _plate_boss_pattern_core(model):
    """底板(含方向中心线) + 顶面凸台 + 特征级线性阵列 (直调路径)。"""
    start_sketch(model, "Front Plane")
    model.SketchManager.CreateCenterLine(-0.04, 0, 0, 0.04, 0, 0)
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    plate_ref = end_sketch(model)
    if extrude_boss(model, plate_ref.name, 0.008) is None:
        raise RuntimeError("参照件底板失败")
    _sketch_on_face(model, [0, 0, 8])
    sketch_corner_rectangle(model, -0.0275, -0.005, -0.0175, 0.005)
    boss_ref = end_sketch(model)
    # 真机实测: 面上草图默认拉伸方向朝材料内, 参照件同样翻转保持双路一致。
    boss = extrude_boss(model, boss_ref.name, 0.005, direction=False)
    if boss is None:
        raise RuntimeError("参照件凸台失败")
    boss_name = str(get_com_member(boss, "Name"))
    segment = find_centerline_segment(model, plate_ref)
    if linear_pattern(model, boss_name, segment, 0.015, 4) is None:
        raise RuntimeError("参照件阵列失败")


def _pattern_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    _plate_boss_pattern_core(model)
    path = out_dir / "ref_pattern_plate.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def _grid_reference(sw, out_dir: Path) -> Path:
    """底板(水平中心线) + 凸台(竖直中心线) + 4x3 双向网格 (直调路径, mark 2)。"""
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    model.SketchManager.CreateCenterLine(-0.04, 0, 0, 0.04, 0, 0)
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    plate_ref = end_sketch(model)
    if extrude_boss(model, plate_ref.name, 0.008) is None:
        raise RuntimeError("参照件底板失败")
    _sketch_on_face(model, [0, 0, 8])
    model.SketchManager.CreateCenterLine(-0.0225, -0.025, 0, -0.0225, 0.025, 0)
    sketch_corner_rectangle(model, -0.0275, -0.02, -0.0175, -0.01)
    boss_ref = end_sketch(model)
    # 真机实测: 面上草图默认拉伸方向朝材料内, 参照件同样翻转保持双路一致。
    boss = extrude_boss(model, boss_ref.name, 0.005, direction=False)
    if boss is None:
        raise RuntimeError("参照件凸台失败")
    boss_name = str(get_com_member(boss, "Name"))
    dir1_segment = find_centerline_segment(model, plate_ref)
    dir2_segment = find_centerline_segment(model, boss_ref)
    if (
        linear_pattern(
            model,
            boss_name,
            dir1_segment,
            0.015,
            4,
            direction2_segment=dir2_segment,
            spacing2=0.015,
            count2=3,
        )
        is None
    ):
        raise RuntimeError("参照件双向网格阵列失败")
    path = out_dir / "ref_pattern_grid.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def _roundtrip_reference(sw, out_dir: Path) -> Path:
    model = new_document(sw, "part")
    _plate_boss_pattern_core(model)
    edges = _vertical_edges(model)
    edge_map = {(round(x, 2), round(y, 2)): edge for edge, x, y in edges}
    corners = [(-30.0, -20.0), (-30.0, 20.0), (30.0, -20.0), (30.0, 20.0)]
    fillet_set = [edge_map[corner] for corner in corners if corner in edge_map]
    if len(fillet_set) != 4:
        raise RuntimeError(f"参照件圆角边数量异常: {len(fillet_set)}")
    model.ClearSelection2(True)
    for index, edge in enumerate(fillet_set):
        if not _select_com_object(edge, append=index > 0, mark=1):
            raise RuntimeError("参照件圆角选边失败")
    if fillet(model, 0.003) is None:
        raise RuntimeError("参照件圆角失败")
    path = out_dir / "ref_roundtrip.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("参照件保存失败")
    return path


def scenario_revolve_shaft(sw, out_dir: Path) -> dict:
    reference = _shaft_reference(sw, out_dir)
    expected = math.pi * 28 * 28 * 43 + math.pi * 32.5 * 32.5 * 70
    detail = _ir_scenario(sw, out_dir, "revolve_shaft", IR_SHAFT, reference, expected)
    _assert_volume(sw, reference, expected, "revolve_shaft 参照件")
    return detail


def scenario_fillet_chamfer(sw, out_dir: Path) -> dict:
    reference = _fillet_chamfer_reference(sw, out_dir)
    expected = 60 * 40 * 8 - 2 * (25 - 25 * math.pi / 4) * 8 - 2 * (2.0 * 2.0 / 2) * 8
    detail = _ir_scenario(sw, out_dir, "fillet_chamfer", IR_FILLET_CHAMFER, reference, expected)
    _assert_volume(sw, reference, expected, "fillet_chamfer 参照件")
    return detail


def scenario_linear_pattern(sw, out_dir: Path) -> dict:
    reference = _pattern_reference(sw, out_dir)
    expected = 60 * 40 * 8 + 4 * 10 * 10 * 5
    detail = _ir_scenario(sw, out_dir, "linear_pattern", IR_PATTERN_PLATE, reference, expected)
    _assert_volume(sw, reference, expected, "linear_pattern 参照件")
    return detail


def scenario_linear_pattern_dir2(sw, out_dir: Path) -> dict:
    reference = _grid_reference(sw, out_dir)
    expected = 60 * 40 * 8 + 4 * 3 * 10 * 10 * 5
    detail = _ir_scenario(sw, out_dir, "linear_pattern_dir2", IR_PATTERN_GRID, reference, expected)
    _assert_volume(sw, reference, expected, "linear_pattern_dir2 参照件")
    # 反向门: 方向 2 的数量/间距走 D2/D4 (真机实测后缀), 随主回归一起校验。
    ops = _assert_roundtrip(
        sw,
        out_dir / f"{IR_PATTERN_GRID['name']}.SLDPRT",
        ["extrude_boss", "extrude_boss", "linear_pattern"],
        {
            0: {"depth_mm": 8},
            1: {"depth_mm": 5},
            2: {"count": 4, "spacing_mm": 15, "count2": 3, "spacing2_mm": 15},
        },
    )
    detail["collected_ops"] = ops
    return detail


def _assert_roundtrip(sw, ir_path: Path, expect_ops: list[str], expect_params: dict[int, dict[str, float]]) -> list[str]:
    """反向门往返: collect_ir 的 op 序列与回读参数必须与 IR 语义一致 (容差 1%)。"""
    model = open_document(sw, str(ir_path), silent=True, raise_on_error=False)
    if model is None:
        raise RuntimeError("反向门: 无法打开试点件")
    try:
        collected = feature_graph_module.collect_ir(model)
    finally:
        sw.CloseDoc(model.GetTitle)
    nodes = [node for node in collected.get("features", []) if node.get("op") != "unknown"]
    ops = [node["op"] for node in nodes]
    if ops != expect_ops:
        raise RuntimeError(f"反向门 op 序列不符: {ops} != {expect_ops}")
    for index, node in enumerate(nodes):
        for key, value in (expect_params.get(index) or {}).items():
            actual = (node.get("params") or {}).get(key)
            if actual is None or abs(actual - value) > max(0.01, abs(value) * 0.01):
                raise RuntimeError(f"反向门参数不符: 节点[{index}] {key}: {actual!r} != {value}")
    return ops


def scenario_reverse_gate(sw, out_dir: Path) -> dict:
    reference = _roundtrip_reference(sw, out_dir)
    expected = 60 * 40 * 8 - 4 * (9 - 9 * math.pi / 4) * 8 + 4 * 10 * 10 * 5
    detail = _ir_scenario(sw, out_dir, "reverse_gate", IR_ROUNDTRIP, reference, expected)
    _assert_volume(sw, reference, expected, "reverse_gate 参照件")
    ops = _assert_roundtrip(
        sw,
        out_dir / f"{IR_ROUNDTRIP['name']}.SLDPRT",
        ["extrude_boss", "extrude_boss", "linear_pattern", "fillet"],
        {0: {"depth_mm": 8}, 1: {"depth_mm": 5}, 2: {"count": 4, "spacing_mm": 15}, 3: {"radius_mm": 3}},
    )
    detail["collected_ops"] = ops
    return detail


SCENARIOS = (
    scenario_box_pattern,
    scenario_flange,
    scenario_bracket,
    scenario_revolve_shaft,
    scenario_fillet_chamfer,
    scenario_linear_pattern,
    scenario_linear_pattern_dir2,
    scenario_reverse_gate,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Feature Graph IR 真机回归")
    parser.add_argument("--output-dir", default=str(Path(tempfile.gettempdir()) / "solidworks_feature_graph_regression"))
    args = parser.parse_args()

    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    run_dir = out_root / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    sw, _model = connect_solidworks()
    results = []
    for scenario in SCENARIOS:
        name = scenario.__name__
        try:
            detail = scenario(sw, run_dir)
            results.append({"scenario": name, "status": "pass", **detail})
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "scenario": name,
                    "status": "fail",
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=4),
                }
            )
            print(f"[FAIL] {name}: {exc}")
        finally:
            try:
                model = sw.ActiveDoc
                if model is not None:
                    sw.CloseDoc(model.GetTitle)
            except Exception:  # noqa: BLE001
                pass

    summary = run_dir / "feature_graph_regression_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总: {summary}")
    return 0 if all(item["status"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
