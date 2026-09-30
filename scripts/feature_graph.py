"""Feature Graph IR (v0.3): AI 意图与 COM 调用之间的确定性中间层。

借鉴 SolidPilot 的 Feature Graph IR 思想 (全部全新实现, 未复制其代码):
- AI 只产出符合 ``feature_graph.schema.json`` 的 IR (毫米单位, 特征以 id 引用);
- ``validate_ir`` 做结构校验, ``lower_to_calls`` 做确定性降级 (无 LLM 参与),
  ``build_from_ir`` 按计划执行并追踪特征名;
- 草图锚点复用 P2 声明式选择引擎 (基准面名或 face_anchor SelectionSpec);
- 圆周阵列在草图层确定性展开 (circle_array); 特征级线性阵列的方向实体是
  被引用草图中的唯一构造中心线 (centerline), 对象级预选 (方向 1 = mark 1,
  方向 2 = mark 2) 后传给 FeatureLinearPattern3 (DName 传字面量 "NULL",
  真机查证 2026-09-30);
- revolve 的旋转轴 = 同一草图中唯一 centerline。

v0.3 词表 (经真机验证, SW2024 SP5): extrude_boss (含 flip/midplane) /
extrude_cut (through_all) / revolve_boss / fillet / chamfer / 特征级
linear_pattern (方向 1 = {sketch: <先前特征id>}, 可选 direction2/count2/
spacing2_mm 双向网格; 方向 2 草图必须不同于方向 1, 各自恰含 1 条 centerline);
草图 shape: rectangle / circle / circle_array / centerline。

方向实体边界 (真机 2026-09-30, tests/probe_dir2_axis.py): 基准轴/模型边线
虽见诸 API 文档口径, 但 FeatureLinearPattern3 晚绑定路径下均被拒绝
(GetErrorCode=51 swSketchErrorExtRefFail, 实例坍缩为种子); 唯一可用方向
实体 = 已消费草图构造中心线段 (GetD1AxisType()=3)。IR 因此只接受
{sketch: id} 形态, 不引入 axis/edge 方向。

v0.3 已知限制 (记录于 capabilities.yaml):
- 引用锚点 (face_anchor / fillet-chamfer 边) 不保证上游编辑后存活,
  重建语义而非编辑语义;
- fillet/chamfer 的边引用是拓扑实体, 歧义或未命中即停, 绝不猜测;
- collect_ir 反向门对回读字段 (深度/半径/距离/角度/间距/数量, 含方向 2
  的数量/间距 D2/D4) 承诺往返等价, 草图轮廓与特征引用关系不回读,
  仍需人工判读。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

try:
    from .sw_connect import get_com_member, new_document, save_document
    from .sw_part import (
        chamfer,
        current_sketch_name,
        end_sketch,
        extrude_boss,
        extrude_cut,
        extrude_midplane,
        fillet,
        find_centerline_segment,
        linear_pattern,
        revolve_boss,
        sketch_centerline,
        sketch_circle,
        sketch_corner_rectangle,
        start_sketch,
    )
    from .sw_selection import resolve_selection
except ImportError:  # 直接以 scripts/ 为工作目录导入
    from sw_connect import get_com_member, new_document, save_document
    from sw_part import (
        chamfer,
        current_sketch_name,
        end_sketch,
        extrude_boss,
        extrude_cut,
        extrude_midplane,
        fillet,
        find_centerline_segment,
        linear_pattern,
        revolve_boss,
        sketch_centerline,
        sketch_circle,
        sketch_corner_rectangle,
        start_sketch,
    )
    from sw_selection import resolve_selection

FEATURE_GRAPH_SCHEMA_VERSION = "1.2"
KNOWN_OPS = {"extrude_boss", "extrude_cut", "revolve_boss", "fillet", "chamfer", "linear_pattern"}
SKETCH_OPS = {"extrude_boss", "extrude_cut", "revolve_boss"}  # 自带草图的特征 op
SHAPE_TYPES = {"rectangle", "circle", "circle_array", "centerline"}
SPEC_KINDS = {"coordinate", "plane", "face", "named", "edge"}

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps/desktop/cad_workbench/schemas/feature_graph.schema.json"
)


def _err(errors: list[str], message: str) -> None:
    errors.append(message)


def _validate_spec(spec: Any, where: str, errors: list[str]) -> None:
    if not isinstance(spec, dict):
        _err(errors, f"{where}: 必须是对象")
        return
    if spec.get("kind") not in SPEC_KINDS:
        _err(errors, f"{where}: kind 非法: {spec.get('kind')!r} (允许: {sorted(SPEC_KINDS)})")
    if spec.get("kind") == "coordinate":
        point = spec.get("point_mm")
        if not (isinstance(point, list) and len(point) == 3):
            _err(errors, f"{where}: coordinate 锚点必须提供 point_mm[x,y,z]")


def _validate_shape(shape: Any, where: str, errors: list[str]) -> None:
    if not isinstance(shape, dict) or shape.get("type") not in SHAPE_TYPES:
        _err(errors, f"{where}: 非法 shape 类型: {shape if not isinstance(shape, dict) else shape.get('type')!r}")
        return
    kind = shape["type"]
    if kind == "rectangle":
        for key in ("x1", "y1", "x2", "y2"):
            if not isinstance(shape.get(key), (int, float)):
                _err(errors, f"{where}: rectangle 缺少数值字段 {key}")
    elif kind == "centerline":
        for key in ("x1", "y1", "x2", "y2"):
            if not isinstance(shape.get(key), (int, float)):
                _err(errors, f"{where}: centerline 缺少数值字段 {key}")
                return
        if (shape["x1"], shape["y1"]) == (shape["x2"], shape["y2"]):
            _err(errors, f"{where}: centerline 长度为零")
    elif kind == "circle":
        for key in ("cx", "cy", "r"):
            if not isinstance(shape.get(key), (int, float)):
                _err(errors, f"{where}: circle 缺少数值字段 {key}")
    else:  # circle_array
        for key in ("cx", "cy", "orbit_mm", "r_hole_mm"):
            if not isinstance(shape.get(key), (int, float)):
                _err(errors, f"{where}: circle_array 缺少数值字段 {key}")
        count = shape.get("count")
        if not isinstance(count, int) or count < 2:
            _err(errors, f"{where}: circle_array.count 必须是 >=2 的整数")


def _validate_sketch(sketch: Any, where: str, errors: list[str]) -> None:
    if not isinstance(sketch, dict):
        _err(errors, f"{where}: sketch 必须是对象")
        return
    has_plane = isinstance(sketch.get("plane"), str)
    has_anchor = isinstance(sketch.get("face_anchor"), dict)
    if has_plane == has_anchor:
        _err(errors, f"{where}: sketch 必须且只能提供 plane 或 face_anchor 之一")
    if has_anchor:
        _validate_spec(sketch["face_anchor"], f"{where}.face_anchor", errors)
    shapes = sketch.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        _err(errors, f"{where}: sketch.shapes 必须是非空数组")
        return
    for index, shape in enumerate(shapes):
        _validate_shape(shape, f"{where}.shapes[{index}]", errors)


def _count_centerlines(shapes: list[Any]) -> int:
    return sum(1 for shape in shapes if isinstance(shape, dict) and shape.get("type") == "centerline")


def _validate_edges(edges: Any, where: str, errors: list[str]) -> None:
    if not isinstance(edges, list) or not edges:
        _err(errors, f"{where}: edges 必须是非空数组 (edge SelectionSpec)")
        return
    for index, spec in enumerate(edges):
        _validate_spec(spec, f"{where}[{index}]", errors)


def _validate_direction_sketch(
    direction: Any, where: str, errors: list[str], centerline_counts: dict[str, int]
) -> None:
    """direction / direction2 共用: {sketch: <先前特征id>} 且该草图恰含 1 条 centerline。

    axis / 模型边线形态被真机否定 (FeatureLinearPattern3 晚绑定拒绝, 见模块
    docstring), 词表不接受。"""
    if not (isinstance(direction, dict) and isinstance(direction.get("sketch"), str)):
        _err(errors, f"{where}: 必须是 {{sketch: <先前特征id>}}")
        return
    ref = direction.get("sketch")
    if ref not in centerline_counts:
        _err(errors, f"{where}.sketch 必须引用先前带草图特征的 id: {ref!r}")
    elif centerline_counts[ref] != 1:
        _err(errors, f"{where}.sketch 引用的草图必须恰含 1 条 centerline (实际 {centerline_counts[ref]})")


def validate_ir(ir: Any) -> list[str]:
    """结构校验, 返回错误列表 (空列表 = 通过)。不依赖 jsonschema 库。"""
    errors: list[str] = []
    if not isinstance(ir, dict):
        return ["IR 必须是 JSON 对象"]
    if ir.get("schemaVersion") != FEATURE_GRAPH_SCHEMA_VERSION:
        _err(errors, f"schemaVersion 必须是 '{FEATURE_GRAPH_SCHEMA_VERSION}'")
    if not isinstance(ir.get("name"), str) or not ir.get("name"):
        _err(errors, "name 必须是非空字符串")
    features = ir.get("features")
    if not isinstance(features, list) or not features:
        _err(errors, "features 必须是非空数组")
        return errors

    seen_ids: set[str] = set()
    centerline_counts: dict[str, int] = {}  # feature id -> 草图 centerline 数
    for index, feature in enumerate(features):
        where = f"features[{index}]"
        if not isinstance(feature, dict):
            _err(errors, f"{where}: 必须是对象")
            continue
        fid = feature.get("id")
        if not isinstance(fid, str) or not fid:
            _err(errors, f"{where}: id 必须是非空字符串")
            continue
        if fid in seen_ids:
            _err(errors, f"{where}: id 重复: {fid}")
        seen_ids.add(fid)
        op = feature.get("op")
        if op not in KNOWN_OPS:
            _err(errors, f"{where}: 未知 op: {op!r} (允许: {sorted(KNOWN_OPS)})")
            continue
        if op in ("extrude_boss", "extrude_cut"):
            _validate_sketch(feature.get("sketch"), f"{where}.sketch", errors)
            if op == "extrude_boss":
                depth = feature.get("depth_mm")
                if not isinstance(depth, (int, float)) or depth <= 0:
                    _err(errors, f"{where}: extrude_boss.depth_mm 必须为正数")
            else:
                through_all = feature.get("through_all", False)
                depth = feature.get("depth_mm")
                if not through_all and not (isinstance(depth, (int, float)) and depth > 0):
                    _err(errors, f"{where}: extrude_cut 需要 depth_mm>0 或 through_all=true")
            shapes = (feature.get("sketch") or {}).get("shapes")
            if isinstance(shapes, list):
                centerline_counts[fid] = _count_centerlines(shapes)
        elif op == "revolve_boss":
            _validate_sketch(feature.get("sketch"), f"{where}.sketch", errors)
            shapes = (feature.get("sketch") or {}).get("shapes")
            count = _count_centerlines(shapes) if isinstance(shapes, list) else 0
            if count != 1:
                _err(errors, f"{where}: revolve_boss 草图必须恰含 1 条 centerline (实际 {count})")
            centerline_counts[fid] = count
            angle = feature.get("angle_deg", 360.0)
            if not isinstance(angle, (int, float)) or not (0 < angle <= 360):
                _err(errors, f"{where}: revolve_boss.angle_deg 必须是 (0,360] 内的数")
        elif op == "fillet":
            radius = feature.get("radius_mm")
            if not isinstance(radius, (int, float)) or radius <= 0:
                _err(errors, f"{where}: fillet.radius_mm 必须为正数")
            _validate_edges(feature.get("edges"), f"{where}.edges", errors)
        elif op == "chamfer":
            distance = feature.get("distance_mm")
            if not isinstance(distance, (int, float)) or distance <= 0:
                _err(errors, f"{where}: chamfer.distance_mm 必须为正数")
            angle = feature.get("angle_deg", 45.0)
            if not isinstance(angle, (int, float)) or not (0 < angle < 90):
                _err(errors, f"{where}: chamfer.angle_deg 必须是 (0,90) 内的数")
            _validate_edges(feature.get("edges"), f"{where}.edges", errors)
        else:  # linear_pattern
            target = feature.get("target")
            if target not in seen_ids:
                _err(errors, f"{where}: linear_pattern.target 必须引用先前的特征 id: {target!r}")
            direction = feature.get("direction")
            _validate_direction_sketch(direction, f"{where}.direction", errors, centerline_counts)
            spacing = feature.get("spacing_mm")
            if not isinstance(spacing, (int, float)) or spacing <= 0:
                _err(errors, f"{where}: spacing_mm 必须为正数")
            count = feature.get("count")
            if not isinstance(count, int) or count < 2:
                _err(errors, f"{where}: count 必须是 >=2 的整数")
            direction2 = feature.get("direction2")
            if direction2 is not None:
                _validate_direction_sketch(
                    direction2, f"{where}.direction2", errors, centerline_counts
                )
                # 同一草图只有 1 条 centerline, 方向 2 必须引用另一个草图特征。
                if (
                    isinstance(direction2, dict)
                    and isinstance(direction2.get("sketch"), str)
                    and isinstance(direction, dict)
                    and direction2.get("sketch") == direction.get("sketch")
                ):
                    _err(errors, f"{where}: direction2.sketch 必须引用与 direction 不同的草图特征")
                spacing2 = feature.get("spacing2_mm")
                if not isinstance(spacing2, (int, float)) or spacing2 <= 0:
                    _err(errors, f"{where}: direction2 存在时 spacing2_mm 必须为正数")
                count2 = feature.get("count2")
                if not isinstance(count2, int) or count2 < 2:
                    _err(errors, f"{where}: direction2 存在时 count2 必须是 >=2 的整数")
            elif "count2" in feature or "spacing2_mm" in feature:
                _err(errors, f"{where}: count2/spacing2_mm 依赖 direction2, 后者缺失")
    return errors


def _expand_shapes(shapes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确定性展开: circle_array -> 逐个圆 (米坐标); 其余毫米转米。"""
    expanded: list[dict[str, Any]] = []
    for shape in shapes:
        kind = shape["type"]
        if kind == "rectangle":
            expanded.append(
                {
                    "type": "rectangle",
                    "args": [shape["x1"] / 1000.0, shape["y1"] / 1000.0, shape["x2"] / 1000.0, shape["y2"] / 1000.0],
                }
            )
        elif kind == "centerline":
            expanded.append(
                {
                    "type": "centerline",
                    "args": [shape["x1"] / 1000.0, shape["y1"] / 1000.0, shape["x2"] / 1000.0, shape["y2"] / 1000.0],
                }
            )
        elif kind == "circle":
            expanded.append(
                {"type": "circle", "args": [shape["cx"] / 1000.0, shape["cy"] / 1000.0, shape["r"] / 1000.0]}
            )
        else:  # circle_array
            cx = shape["cx"] / 1000.0
            cy = shape["cy"] / 1000.0
            orbit = shape["orbit_mm"] / 1000.0
            radius = shape["r_hole_mm"] / 1000.0
            count = int(shape["count"])
            start = math.radians(shape.get("start_angle_deg", 0.0))
            for k in range(count):
                angle = start + 2.0 * math.pi * k / count
                expanded.append(
                    {
                        "type": "circle",
                        "args": [cx + orbit * math.cos(angle), cy + orbit * math.sin(angle), radius],
                    }
                )
    return expanded


def lower_to_calls(ir: dict[str, Any]) -> list[dict[str, Any]]:
    """确定性降级: IR -> 有序调用计划 (无 LLM 参与)。计划同时作为交付证据返回。"""
    errors = validate_ir(ir)
    if errors:
        raise ValueError("IR 校验失败: " + "; ".join(errors))

    plan: list[dict[str, Any]] = []
    for feature in ir["features"]:
        op = feature["op"]
        if op in SKETCH_OPS:
            sketch = feature["sketch"]
            if sketch.get("plane"):
                plan.append({"op": "sketch_start", "plane": sketch["plane"]})
            else:
                plan.append({"op": "sketch_start", "face_anchor": sketch["face_anchor"]})
            for expanded in _expand_shapes(sketch["shapes"]):
                plan.append({"op": f"draw_{expanded['type']}", "args": expanded["args"]})
            plan.append({"op": "sketch_end"})
            if op == "extrude_boss":
                plan.append(
                    {
                        "op": op,
                        "id": feature["id"],
                        "depth_mm": feature["depth_mm"],
                        "midplane": bool(feature.get("midplane", False)),
                        # 真机实测: 面上草图默认拉伸方向可能与基准面草图相反, flip 显式控制。
                        "flip": bool(feature.get("flip", False)),
                    }
                )
            elif op == "extrude_cut":
                plan.append(
                    {
                        "op": op,
                        "id": feature["id"],
                        "depth_mm": feature.get("depth_mm"),
                        "through_all": bool(feature.get("through_all", False)),
                        "flip": bool(feature.get("flip", False)),
                    }
                )
            else:  # revolve_boss
                plan.append(
                    {
                        "op": op,
                        "id": feature["id"],
                        "angle_deg": float(feature.get("angle_deg", 360.0)),
                    }
                )
        elif op == "fillet":
            plan.append(
                {
                    "op": op,
                    "id": feature["id"],
                    "radius_mm": float(feature["radius_mm"]),
                    "edges": list(feature["edges"]),
                }
            )
        elif op == "chamfer":
            plan.append(
                {
                    "op": op,
                    "id": feature["id"],
                    "distance_mm": float(feature["distance_mm"]),
                    "angle_deg": float(feature.get("angle_deg", 45.0)),
                    "edges": list(feature["edges"]),
                }
            )
        else:  # linear_pattern
            plan_item = {
                "op": "linear_pattern",
                "id": feature["id"],
                "target": feature["target"],
                "direction": {"sketch": feature["direction"]["sketch"]},
                "spacing_mm": float(feature["spacing_mm"]),
                "count": int(feature["count"]),
            }
            if feature.get("direction2") is not None:
                plan_item["direction2"] = {"sketch": feature["direction2"]["sketch"]}
                plan_item["spacing2_mm"] = float(feature["spacing2_mm"])
                plan_item["count2"] = int(feature["count2"])
            plan.append(plan_item)
    return plan


def start_sketch_on_anchor(model, spec: dict[str, Any]) -> dict[str, Any]:
    """在 SelectionSpec 命中的面上开草图 (先清空选择集)。"""
    model.ClearSelection2(True)
    _handle, evidence = resolve_selection(model, spec, mark=1)
    if evidence.get("status") not in ("resolved", "resolved_object_only"):
        raise RuntimeError(f"草图锚点选择失败: {evidence}")
    model.SketchManager.InsertSketch(True)
    return evidence


def _select_edges(model, edges: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """按 edge SelectionSpec 逐条选边 (mark 1)。歧义/未命中即抛, 绝不猜测。"""
    model.ClearSelection2(True)
    evidences = []
    for index, spec in enumerate(edges):
        _handle, evidence = resolve_selection(model, spec, append=index > 0, mark=1)
        if evidence.get("status") not in ("resolved", "resolved_object_only"):
            raise RuntimeError(f"{label} 第 {index} 条边选择失败: {evidence}")
        evidences.append(evidence)
    return evidences


def build_from_ir(
    sw,
    ir: dict[str, Any],
    out_path: str,
    *,
    overwrite: bool = True,
    record_evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """执行 IR: 校验 -> 降级 -> 按计划建模 -> 保存。返回执行证据。"""
    errors = validate_ir(ir)
    if errors:
        raise ValueError("IR 校验失败: " + "; ".join(errors))
    plan = lower_to_calls(ir)
    model = new_document(sw, "part")
    feature_names: dict[str, str] = {}
    sketch_refs: dict[str, Any] = {}  # feature id -> SketchSelectionRef (centerline 解析用)
    executed: list[dict[str, Any]] = []
    active_sketch: str | None = None
    last_sketch_ref = None
    try:
        for step in plan:
            op = step["op"]
            if op == "sketch_start":
                if step.get("plane"):
                    start_sketch(model, step["plane"])
                else:
                    start_sketch_on_anchor(model, step["face_anchor"])
            elif op == "draw_rectangle":
                sketch_corner_rectangle(model, *step["args"])
            elif op == "draw_circle":
                sketch_circle(model, *step["args"])
            elif op == "draw_centerline":
                sketch_centerline(model, *step["args"])
            elif op == "sketch_end":
                sketch_ref = end_sketch(model)
                active_sketch = getattr(sketch_ref, "name", None) or current_sketch_name(model)
                last_sketch_ref = sketch_ref
                executed.append({"op": op, "sketch": active_sketch})
                continue
            elif op == "extrude_boss":
                if step.get("midplane"):
                    feature = extrude_midplane(model, active_sketch, step["depth_mm"] / 1000.0)
                else:
                    feature = extrude_boss(
                        model, active_sketch, step["depth_mm"] / 1000.0, direction=not step.get("flip", False)
                    )
                if feature is None:
                    raise RuntimeError(f"拉伸特征创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(feature, "Name"))
                sketch_refs[step["id"]] = last_sketch_ref
            elif op == "extrude_cut":
                depth = 0.0 if step.get("through_all") else step["depth_mm"] / 1000.0
                feature = extrude_cut(model, active_sketch, depth, flip=step.get("flip", False))
                if feature is None:
                    raise RuntimeError(f"切除特征创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(feature, "Name"))
                sketch_refs[step["id"]] = last_sketch_ref
            elif op == "revolve_boss":
                feature = revolve_boss(model, active_sketch, math.radians(step["angle_deg"]))
                if feature is None:
                    raise RuntimeError(f"旋转特征创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(feature, "Name"))
                sketch_refs[step["id"]] = last_sketch_ref
            elif op == "fillet":
                _select_edges(model, step["edges"], "fillet")
                feature = fillet(model, step["radius_mm"] / 1000.0)
                if feature is None:
                    raise RuntimeError(f"圆角特征创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(feature, "Name"))
            elif op == "chamfer":
                _select_edges(model, step["edges"], "chamfer")
                feature = chamfer(model, step["distance_mm"] / 1000.0, step["angle_deg"])
                if feature is None:
                    raise RuntimeError(f"倒角特征创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(feature, "Name"))
            elif op == "linear_pattern":
                target_name = feature_names.get(step["target"])
                if not target_name:
                    raise RuntimeError(f"linear_pattern 目标特征未追踪到: {step['target']}")
                direction_ref = sketch_refs.get(step["direction"]["sketch"])
                if direction_ref is None:
                    raise RuntimeError(f"linear_pattern 方向草图未追踪到: {step['direction']['sketch']}")
                direction_segment = find_centerline_segment(model, direction_ref)
                direction2_segment = None
                if step.get("direction2") is not None:
                    direction2_ref = sketch_refs.get(step["direction2"]["sketch"])
                    if direction2_ref is None:
                        raise RuntimeError(
                            f"linear_pattern 方向 2 草图未追踪到: {step['direction2']['sketch']}"
                        )
                    direction2_segment = find_centerline_segment(model, direction2_ref)
                created = linear_pattern(
                    model,
                    target_name,
                    direction_segment,
                    step["spacing_mm"] / 1000.0,
                    step["count"],
                    direction2_segment=direction2_segment,
                    spacing2=(step["spacing2_mm"] / 1000.0) if step.get("direction2") else 0.01,
                    count2=step.get("count2", 1),
                )
                if created is None:
                    raise RuntimeError(f"特征级阵列创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(created, "Name"))
            else:  # pragma: no cover - lower_to_calls 只产出已知 op
                raise RuntimeError(f"未知计划步骤: {op}")
            executed.append({"op": op, "ok": True})
    except Exception as exc:  # noqa: BLE001 - 执行证据要完整
        return {
            "status": "failed",
            "error": f"{exc.__class__.__name__}: {exc}",
            "executed": executed,
            "plan": plan,
        }
    saved = save_document(model, str(out_path), overwrite=overwrite)
    result = {
        "status": "ok" if saved else "save_failed",
        "path": str(out_path),
        "part_name": ir["name"],
        "feature_names": feature_names,
        "executed": executed,
        "plan": plan,
    }
    if record_evidence_dir is not None:
        evidence_path = Path(record_evidence_dir) / f"{ir['name']}_build.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


# 反向门: GetTypeName2 特征类型名 -> op (真机实测 SW2024 SP5, 2026-09-30)
_TYPE_NAME_TO_OP = {
    "Extrusion": "extrude_boss",  # 基准面草图拉伸凸台
    "ICE": "extrude_boss",        # 面上草图拉伸凸台
    "Cut": "extrude_cut",
    "Revolution": "revolve_boss",
    "Fillet": "fillet",
    "Chamfer": "chamfer",
    "LPattern": "linear_pattern",
}


def collect_ir(model) -> dict[str, Any]:
    """反向门: 枚举特征生成 IR 骨架 + 可回读参数。

    往返等价仅对回读字段承诺 (深度/半径/距离/角度/间距/数量, 单位已换算);
    草图轮廓与特征引用关系不回读。尺寸回读经 model.Parameter("Dn@特征名")
    (SW2024 类型库无 IFeature.GetDimensions, 见 sw_inspect.enumerate_features)。
    """
    try:
        from .sw_inspect import enumerate_features
    except ImportError:
        from sw_inspect import enumerate_features

    enumeration = enumerate_features(model, max_features=200)
    nodes = []
    for entry in enumeration.get("features", []):
        name = str(entry.get("name") or "")
        type_name = str(entry.get("type") or "")
        dims: dict[str, float | None] = {}
        for dim in entry.get("dimensions", []):
            dims[str(dim.get("name"))] = dim.get("system_value")

        def _dim(suffix: str) -> float | None:
            return dims.get(f"{suffix}@{name}")

        op = _TYPE_NAME_TO_OP.get(type_name, "unknown")
        node: dict[str, Any] = {"id": name, "op": op, "type_name": type_name, "params": {}}
        if op == "extrude_boss" or op == "extrude_cut":
            value = _dim("D1")
            node["params"]["depth_mm"] = None if value is None else value * 1000.0
        elif op == "revolve_boss":
            value = _dim("D1")
            node["params"]["angle_deg"] = None if value is None else math.degrees(value)
        elif op == "fillet":
            value = _dim("D1")
            node["params"]["radius_mm"] = None if value is None else value * 1000.0
        elif op == "chamfer":
            distance = _dim("D1")
            angle = _dim("D2")
            node["params"]["distance_mm"] = None if distance is None else distance * 1000.0
            node["params"]["angle_deg"] = None if angle is None else math.degrees(angle)
        elif op == "linear_pattern":
            # 真机实测 (2026-09-30): D1=数量1, D2=数量2, D3=间距1(米), D4=间距2(米);
            # 单方向阵列无 D2/D4, 回读为 None。
            count = _dim("D1")
            spacing = _dim("D3")
            count2 = _dim("D2")
            spacing2 = _dim("D4")
            node["params"]["count"] = None if count is None else int(round(count))
            node["params"]["spacing_mm"] = None if spacing is None else spacing * 1000.0
            node["params"]["count2"] = None if count2 is None else int(round(count2))
            node["params"]["spacing2_mm"] = None if spacing2 is None else spacing2 * 1000.0
        nodes.append(node)
    return {
        "schemaVersion": FEATURE_GRAPH_SCHEMA_VERSION,
        "name": "collected",
        "features": nodes,
        "limitations": [
            "往返等价仅对回读字段承诺 (深度/半径/距离/角度/间距/数量, 含方向 2 的 D2/D4)",
            "草图轮廓与特征引用关系不回读, unknown 节点需人工判读后手工改写",
        ],
    }


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
