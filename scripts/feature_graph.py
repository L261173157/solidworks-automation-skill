"""Feature Graph IR (v0.1): AI 意图与 COM 调用之间的确定性中间层。

借鉴 SolidPilot 的 Feature Graph IR 思想 (全部全新实现, 未复制其代码):
- AI 只产出符合 ``feature_graph.schema.json`` 的 IR (毫米单位, 特征以 id 引用);
- ``validate_ir`` 做结构校验, ``lower_to_calls`` 做确定性降级 (无 LLM 参与),
  ``build_from_ir`` 按计划执行并追踪特征名;
- 草图锚点复用 P2 声明式选择引擎 (基准面名或 face_anchor SelectionSpec);
- 圆周阵列在草图层确定性展开 (circle_array), 特征级线性阵列走
  ``sw_part.linear_pattern``。

v0.1 已知限制 (记录于 capabilities.yaml, 与 SolidPilot v0 相同):
- 词表仅 extrude_boss / extrude_cut / linear_pattern;
  revolve/fillet/chamfer 在 v0.2 路线;
- 引用锚点不保证上游编辑后存活;
- collect_ir 反向门是尽力而为的骨架, 不承诺往返等价。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

try:
    from .sw_connect import get_com_member, new_document, save_document
    from .sw_part import (
        current_sketch_name,
        end_sketch,
        extrude_boss,
        extrude_cut,
        extrude_midplane,
        linear_pattern,
        sketch_circle,
        sketch_corner_rectangle,
        start_sketch,
    )
    from .sw_selection import resolve_selection
except ImportError:  # 直接以 scripts/ 为工作目录导入
    from sw_connect import get_com_member, new_document, save_document
    from sw_part import (
        current_sketch_name,
        end_sketch,
        extrude_boss,
        extrude_cut,
        extrude_midplane,
        linear_pattern,
        sketch_circle,
        sketch_corner_rectangle,
        start_sketch,
    )
    from sw_selection import resolve_selection

FEATURE_GRAPH_SCHEMA_VERSION = "1.0"
KNOWN_OPS = {"extrude_boss", "extrude_cut", "linear_pattern"}
SHAPE_TYPES = {"rectangle", "circle", "circle_array"}
SPEC_KINDS = {"coordinate", "plane", "face", "named"}

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps/desktop/cad_workbench/schemas/feature_graph.schema.json"
)


def _err(errors: list[str], message: str) -> None:
    errors.append(message)


def _validate_spec(spec: Any, where: str, errors: list[str]) -> None:
    if not isinstance(spec, dict):
        _err(errors, f"{where}: face_anchor 必须是对象")
        return
    if spec.get("kind") not in SPEC_KINDS:
        _err(errors, f"{where}: face_anchor.kind 非法: {spec.get('kind')!r}")
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
        _validate_spec(sketch["face_anchor"], where, errors)
    shapes = sketch.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        _err(errors, f"{where}: sketch.shapes 必须是非空数组")
        return
    for index, shape in enumerate(shapes):
        _validate_shape(shape, f"{where}.shapes[{index}]", errors)


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
        else:  # linear_pattern
            target = feature.get("target")
            if target not in seen_ids:
                _err(errors, f"{where}: linear_pattern.target 必须引用先前的特征 id: {target!r}")
            direction = feature.get("direction")
            if not (isinstance(direction, list) and len(direction) == 3):
                _err(errors, f"{where}: direction 必须是 [x,y,z] 三元组")
            elif all(not component for component in direction):
                _err(errors, f"{where}: direction 不能是零向量")
            spacing = feature.get("spacing_mm")
            if not isinstance(spacing, (int, float)) or spacing <= 0:
                _err(errors, f"{where}: spacing_mm 必须为正数")
            count = feature.get("count")
            if not isinstance(count, int) or count < 2:
                _err(errors, f"{where}: count 必须是 >=2 的整数")
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
        if op in ("extrude_boss", "extrude_cut"):
            sketch = feature["sketch"]
            if sketch.get("plane"):
                plan.append({"op": "sketch_start", "plane": sketch["plane"]})
            else:
                plan.append({"op": "sketch_start", "face_anchor": sketch["face_anchor"]})
            for expanded in _expand_shapes(sketch["shapes"]):
                plan.append({"op": f"draw_{expanded['type']}", "args": expanded["args"]})
            plan.append({"op": "sketch_end"})
            entry: dict[str, Any] = {"op": op, "id": feature["id"]}
            if op == "extrude_boss":
                entry["depth_mm"] = feature["depth_mm"]
                entry["midplane"] = bool(feature.get("midplane", False))
                # 真机实测: 面上草图默认拉伸方向可能与基准面草图相反, flip 显式控制。
                entry["flip"] = bool(feature.get("flip", False))
            else:
                entry["depth_mm"] = feature.get("depth_mm")
                entry["through_all"] = bool(feature.get("through_all", False))
                entry["flip"] = bool(feature.get("flip", False))
            plan.append(entry)
        else:  # linear_pattern
            direction = feature["direction"]
            plan.append(
                {
                    "op": "linear_pattern",
                    "id": feature["id"],
                    "target": feature["target"],
                    "direction": [float(direction[0]), float(direction[1]), float(direction[2])],
                    "spacing_mm": float(feature["spacing_mm"]),
                    "count": int(feature["count"]),
                }
            )
    return plan


def start_sketch_on_anchor(model, spec: dict[str, Any]) -> dict[str, Any]:
    """在 SelectionSpec 命中的面上开草图 (先清空选择集)。"""
    model.ClearSelection2(True)
    _handle, evidence = resolve_selection(model, spec, mark=1)
    if evidence.get("status") not in ("resolved", "resolved_object_only"):
        raise RuntimeError(f"草图锚点选择失败: {evidence}")
    model.SketchManager.InsertSketch(True)
    return evidence


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
    executed: list[dict[str, Any]] = []
    active_sketch: str | None = None
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
            elif op == "sketch_end":
                sketch_ref = end_sketch(model)
                active_sketch = getattr(sketch_ref, "name", None) or current_sketch_name(model)
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
            elif op == "extrude_cut":
                depth = 0.0 if step.get("through_all") else step["depth_mm"] / 1000.0
                feature = extrude_cut(model, active_sketch, depth, flip=step.get("flip", False))
                if feature is None:
                    raise RuntimeError(f"切除特征创建失败: {step['id']}")
                feature_names[step["id"]] = str(get_com_member(feature, "Name"))
            elif op == "linear_pattern":
                target_name = feature_names.get(step["target"])
                if not target_name:
                    raise RuntimeError(f"linear_pattern 目标特征未追踪到: {step['target']}")
                dx, dy, dz = step["direction"]
                created = linear_pattern(
                    model,
                    target_name,
                    dx, dy, dz,
                    step["spacing_mm"] / 1000.0,
                    step["count"],
                )
                feature_names[step["id"]] = str(get_com_member(created, "Name")) if created is not None else ""
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


def collect_ir(model) -> dict[str, Any]:
    """反向门 (尽力而为): 枚举特征生成 IR 骨架, 无法判定的 op 标记 unknown。"""
    try:
        from .sw_inspect import enumerate_features
    except ImportError:
        from sw_inspect import enumerate_features

    features = enumerate_features(model, max_features=200)
    nodes = []
    for feature in features:
        type_name = str(feature.get("type_name") or "")
        op = "extrude_boss" if "Extrusion" in type_name else "unknown"
        nodes.append({"id": feature.get("name"), "op": op, "type_name": type_name})
    return {
        "schemaVersion": FEATURE_GRAPH_SCHEMA_VERSION,
        "name": "collected",
        "features": nodes,
        "limitations": [
            "反向门是尽力而为的骨架, 不承诺往返等价",
            "unknown 节点需要人工判读后手工改写为已知 op",
        ],
    }


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
