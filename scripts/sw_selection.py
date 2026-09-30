"""声明式选择引擎: 用 SelectionSpec 取代裸 SelectByID2 名称串。

背景: SolidWorks 的 Extension.SelectByID2 依赖精确的实体名称字符串与隐式
全局选择集 marks，跨语言界面/版本脆弱。本模块提供结构化选择描述:

- ``named``: 兼容旧的名称串 (缺省类型由调用方 hint 提供);
- ``plane``: 基准面, 自动尝试中英文别名;
- ``coordinate``: 屏幕坐标命中 (point_mm, 引擎内部转米);
- ``component``: 组件按名称/关键字;
- ``face`` / ``edge`` / ``vertex`` / ``sketch_segment``: 枚举拓扑候选,
  结合名称与几何签名评分; face/edge/vertex 还支持 point_mm 作为
  "中点提示"评分 (候选中点距离 <= 1mm 得唯一高分, 多命中即 ambiguous);
  歧义时返回候选清单, 绝不猜测 (pilot)。

选择成功一律通过对象级 Select2/Select4 落到选择集, 并返回证据字典
(status/kind/matched/score/candidates) 供上层审查。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

try:
    from .sw_connect import (
        create_empty_dispatch_variant,
        get_com_member,
        mm,
        safe_get_com_member,
    )
    from .sw_entity_reference import geometry_signature, resolve_semantic_reference
except ImportError:  # 直接以 scripts/ 为工作目录导入
    from sw_connect import (
        create_empty_dispatch_variant,
        get_com_member,
        mm,
        safe_get_com_member,
    )
    from sw_entity_reference import geometry_signature, resolve_semantic_reference

VALID_KINDS = {
    "named",
    "plane",
    "face",
    "edge",
    "vertex",
    "sketch_segment",
    "component",
    "coordinate",
}

# face/edge/vertex 的 point_mm 中点提示容差 (米): 候选中点落在该距离内得分。
_POINT_MIDPOINT_TOLERANCE_M = 0.001

SPEC_FIELDS = {
    "kind",
    "name",
    "feature",
    "geometry_signature",
    "point_mm",
    "nth",
    "entity_type",
}

DEFAULT_PLANE_CANDIDATES = (
    "Front Plane",
    "前视基准面",
    "Top Plane",
    "上视基准面",
    "Right Plane",
    "右视基准面",
)

_MAX_AMBIGUOUS_CANDIDATES = 5


@dataclass(frozen=True)
class SelectionSpec:
    """结构化选择描述。

    kind: named | plane | face | edge | vertex | sketch_segment | component | coordinate
    name: 实体/特征/组件名称或别名 (named/plane/component/sketch_segment)
    feature: 限定候选所属特征名
    geometry_signature: 期望的几何签名 (几何匹配时校验)
    point_mm: coordinate 形态的命中点 (毫米, 内部转米);
        face/edge/vertex 形态下作为"中点提示"评分 (候选中点距离 <= 1mm 得唯一高分)
    nth: 同类候选序号 (0 起)
    entity_type: 覆盖 SolidWorks 选择类型串 (默认按 kind 推断)
    """

    kind: str
    name: str | None = None
    feature: str | None = None
    geometry_signature: str | None = None
    point_mm: tuple[float, float, float] | None = None
    nth: int = 0
    entity_type: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(f"未知选择形态: {self.kind!r} (允许: {sorted(VALID_KINDS)})")
        if self.nth < 0:
            raise ValueError("nth 必须 >= 0")
        if self.point_mm is not None and len(self.point_mm) != 3:
            raise ValueError("point_mm 必须是 [x, y, z] 三元组")

    @classmethod
    def coerce(cls, value: Any, *, default_kind: str = "named") -> "SelectionSpec":
        """字符串/字典/SelectionSpec 统一归一化; 非法输入抛 ValueError。"""
        if isinstance(value, SelectionSpec):
            return value
        if isinstance(value, str):
            return cls(kind=default_kind, name=value)
        if isinstance(value, dict):
            unknown = set(value) - SPEC_FIELDS
            if unknown:
                raise ValueError(f"SelectionSpec 含未知字段: {sorted(unknown)} (允许: {sorted(SPEC_FIELDS)})")
            merged = {"kind": default_kind, **value}
            if merged["kind"] not in VALID_KINDS:
                raise ValueError(f"未知选择形态: {merged['kind']!r} (允许: {sorted(VALID_KINDS)})")
            point = merged.get("point_mm")
            if point is not None and not isinstance(point, (tuple, list)):
                raise ValueError("point_mm 必须是 [x, y, z] 三元组")
            return cls(
                kind=merged["kind"],
                name=merged.get("name"),
                feature=merged.get("feature"),
                geometry_signature=merged.get("geometry_signature"),
                point_mm=tuple(point) if point is not None else None,
                nth=int(merged.get("nth", 0)),
                entity_type=merged.get("entity_type"),
            )
        raise ValueError(f"无法解释选择描述: {value!r}")


def selection_evidence(status: str, spec: SelectionSpec, **extra: Any) -> dict[str, Any]:
    """构造统一形状的选择证据。"""
    evidence: dict[str, Any] = {
        "status": status,
        "kind": spec.kind,
        "name": spec.name,
        "nth": spec.nth,
    }
    evidence.update(extra)
    return evidence


def _entity_type_for(spec: SelectionSpec, hint: str | None) -> str:
    if spec.entity_type:
        return spec.entity_type
    by_kind = {
        "named": hint or "EDGE",
        "plane": "PLANE",
        "face": "FACE",
        "edge": "EDGE",
        "vertex": "VERTEX",
        "sketch_segment": "SKETCHSEGMENT",
        "component": "COMPONENT",
    }
    return by_kind.get(spec.kind, hint or "EDGE")


def _select_by_name(extension, name: str, entity_type: str, append: bool, mark: int) -> bool:
    return bool(
        extension.SelectByID2(
            name,
            entity_type,
            0.0,
            0.0,
            0.0,
            append,
            mark,
            create_empty_dispatch_variant(),
            0,
        )
    )


def _candidate_metadata(candidate: Any) -> dict[str, Any]:
    """提取拓扑候选的轻量元数据 (面积/中点等), 成员缺失时字段留空。

    face 有 GetArea/GetCenterPoint; edge 无这些成员 (SW2024 类型库实测,
    AttributeError), 中点退化为起止顶点平均 (直线边精确, 曲线边为弦中点近似)。
    """
    metadata: dict[str, Any] = {}
    try:
        area = safe_get_com_member(candidate, "GetArea")
        if area is not None:
            metadata["area"] = round(float(area), 6)
    except Exception:
        pass
    point = None
    for getter in ("GetPointAtPoint", "GetCenterPoint"):
        try:
            point = safe_get_com_member(candidate, getter)
        except Exception:
            point = None
        if point is not None:
            break
    if point is None:
        try:
            start_vertex = safe_get_com_member(candidate, "GetStartVertex")
            end_vertex = safe_get_com_member(candidate, "GetEndVertex")
            if start_vertex and end_vertex:
                start = tuple(float(v) for v in safe_get_com_member(start_vertex, "GetPoint"))
                end = tuple(float(v) for v in safe_get_com_member(end_vertex, "GetPoint"))
                point = tuple((start[i] + end[i]) / 2.0 for i in range(3))
        except Exception:
            point = None
    if point is not None:
        try:
            metadata["midpoint"] = [round(float(value), 6) for value in tuple(point)]
        except TypeError:
            pass
    return metadata


def _enumerate_topology(model, kind: str) -> list[Any]:
    """枚举 B-Rep 拓扑候选 (pilot)。"""
    bodies = safe_get_com_member(model, "GetBodies2", 0, False) or ()
    if isinstance(bodies, tuple) and bodies and not hasattr(bodies[0], "GetFaces"):
        bodies = tuple(bodies)
    candidates: list[Any] = []
    getter = {"face": "GetFaces", "edge": "GetEdges", "vertex": "GetVertices"}.get(kind)
    if getter is None:
        return candidates
    for body in bodies:
        found = safe_get_com_member(body, getter)
        if found:
            candidates.extend(tuple(found))
    return candidates


def _score_candidates(model, spec: SelectionSpec, candidates: list[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """按名称/签名/中点提示/序号给候选打分, 返回 (排序后候选, 候选证据)。

    match_score 是纯匹配分 (签名 +5 / 中点命中 +5), 用于资格判定;
    排序键再减枚举序号打破平手。二者不可混用, 否则高序号的精确匹配
    会被误判 not_found。
    """
    target_m = None
    if spec.point_mm is not None:
        target_m = [float(value) / 1000.0 for value in spec.point_mm]
    ranked: list[tuple[int, int, Any, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        metadata = _candidate_metadata(candidate)
        match_score = 0
        if spec.geometry_signature:
            expected = spec.geometry_signature
            actual = geometry_signature([metadata.get("area"), metadata.get("midpoint")])
            if expected and actual == expected:
                match_score += 5
        if target_m is not None and metadata.get("midpoint") is not None:
            distance = math.dist(metadata["midpoint"], target_m)
            metadata["midpoint_distance_m"] = round(distance, 6)
            if distance <= _POINT_MIDPOINT_TOLERANCE_M:
                match_score += 5
        metadata["match_score"] = match_score
        ranked.append((match_score, index, candidate, metadata))
    # 匹配分优先; 同分按枚举序 (确定性)。真机教训 (SW2024 SP5): 若排序键
    # 用 "匹配分-枚举序", fillet 改变边枚举顺序后, 晚枚举的精确匹配会输给
    # 早枚举的不匹配, 选中错误边。
    ranked.sort(key=lambda item: (-item[0], item[1]))
    ordered = [candidate for _, _, candidate, _ in ranked]
    candidate_infos = [
        {
            "index": position,
            "score": metadata["match_score"] - position,
            **metadata,
        }
        for position, (_match_score, _enum_index, _candidate, metadata) in enumerate(ranked)
    ]
    return ordered, candidate_infos


def resolve_selection(
    model,
    spec: Any,
    *,
    entity_type_hint: str | None = None,
    append: bool = False,
    mark: int = 1,
) -> tuple[Any | None, dict[str, Any]]:
    """解析并执行一次选择, 返回 (选中对象或 None, 证据字典)。

    语义: 命中对象时通过对象级 Select2/Select4 或名称串 SelectByID2 写入
    选择集; 不能唯一确定时 (ambiguous) 返回候选清单且不改动选择。
    """
    spec = SelectionSpec.coerce(spec, default_kind="named")
    extension = safe_get_com_member(model, "Extension")
    if extension is None:
        return None, selection_evidence("failed", spec, message="模型无 Extension 对象")

    if spec.kind == "named":
        name = spec.name or ""
        entity_type = _entity_type_for(spec, entity_type_hint)
        if _select_by_name(extension, name, entity_type, append, mark):
            return None, selection_evidence("resolved", spec, entity_type=entity_type, matched=name)
        return None, selection_evidence("not_found", spec, entity_type=entity_type, matched=name)

    if spec.kind == "plane":
        entity_type = _entity_type_for(spec, entity_type_hint)
        # 显式名优先; 失败后回退全部别名 (真机实测部分中文版 SolidWorks
        # 仅接受英文基准面名, 反之亦然)。
        candidates: list[str | None] = []
        if spec.name:
            candidates.append(spec.name)
        for alias in DEFAULT_PLANE_CANDIDATES:
            if alias not in candidates:
                candidates.append(alias)
        for candidate in candidates:
            if candidate and _select_by_name(extension, candidate, entity_type, append, mark):
                return None, selection_evidence("resolved", spec, entity_type=entity_type, matched=candidate)
        return None, selection_evidence("not_found", spec, entity_type=entity_type, candidates=candidates)

    if spec.kind == "coordinate":
        if spec.point_mm is None:
            return None, selection_evidence("failed", spec, message="coordinate 形态必须提供 point_mm")
        x, y, z = (mm(float(value)) for value in spec.point_mm)
        callout = create_empty_dispatch_variant()
        selected = bool(
            extension.SelectByID2("", "", x, y, z, append, mark, callout, 0)
        )
        if not selected:
            # 真机实测: 新建文档首次坐标选择会因显示细分未就绪静默失败,
            # 刷新视图后重试一次即可命中。
            safe_get_com_member(model, "ViewZoomtofit2")
            safe_get_com_member(model, "GraphicsRedraw2")
            selected = bool(
                extension.SelectByID2("", "", x, y, z, append, mark, callout, 0)
            )
        status = "resolved" if selected else "not_found"
        return None, selection_evidence(status, spec, point_m=[x, y, z])

    if spec.kind == "component":
        components = safe_get_com_member(model, "GetComponents", True) or ()
        matches = []
        for component in components:
            name = str(get_com_member(component, "Name2") or get_com_member(component, "Name") or "")
            if spec.name and (name == spec.name or spec.name in name):
                matches.append((name, component))
        if len(matches) == 1:
            name, component = matches[0]
            # 真机实测 (SW2024 SP5): 名称串 SelectByID2("x","COMPONENT") 与
            # Select4(append, None, True) 均无法可靠写入选择集; 唯一匹配时
            # 返回组件对象本身 (object_only), 不写选择集。需要配合选择的
            # 场景应改用 face/coordinate spec。
            return component, selection_evidence("resolved_object_only", spec, matched=name)
        if len(matches) > 1:
            return None, selection_evidence(
                "ambiguous",
                spec,
                candidates=[{"name": name} for name, _ in matches[:_MAX_AMBIGUOUS_CANDIDATES]],
            )
        return None, selection_evidence("not_found", spec, candidates=[])

    if spec.kind == "sketch_segment":
        sketch_manager = safe_get_com_member(model, "SketchManager")
        segments = []
        for sketch in [safe_get_com_member(sketch_manager, "ActiveSketch")] if sketch_manager else []:
            segments.extend(tuple(safe_get_com_member(sketch, "GetSketchSegments") or ()))
        if not segments:
            return None, selection_evidence("not_found", spec, message="无活动草图段")
        ordered, candidate_infos = _score_candidates(model, spec, list(segments))
        index = spec.nth
        if index >= len(ordered):
            return None, selection_evidence("not_found", spec, candidates=candidate_infos)
        segment = ordered[index]
        selected = bool(safe_get_com_member(segment, "Select2", append, mark))
        return (segment if selected else None), selection_evidence(
            "resolved" if selected else "failed", spec, candidates=candidate_infos
        )

    # face / edge / vertex: 拓扑枚举 + 评分 (pilot)
    candidates = _enumerate_topology(model, spec.kind)
    if not candidates:
        return None, selection_evidence("not_found", spec, message=f"未枚举到 {spec.kind} 候选")
    ordered, candidate_infos = _score_candidates(model, spec, candidates)
    requires_unique = bool(spec.geometry_signature) or spec.point_mm is not None
    best_match = max((info.get("match_score", 0) for info in candidate_infos), default=0)
    ties = sum(1 for info in candidate_infos if info.get("match_score", 0) == best_match and best_match > 0)
    if requires_unique and (best_match < 5 or ties != 1):
        return None, selection_evidence(
            "ambiguous" if ties > 1 else "not_found",
            spec,
            candidates=candidate_infos[:_MAX_AMBIGUOUS_CANDIDATES],
        )
    index = spec.nth
    if index >= len(ordered):
        return None, selection_evidence("not_found", spec, candidates=candidate_infos[:_MAX_AMBIGUOUS_CANDIDATES])
    candidate = ordered[index]
    selected = bool(safe_get_com_member(candidate, "Select2", append, mark))
    return (candidate if selected else None), selection_evidence(
        "resolved" if selected else "failed",
        spec,
        candidates=candidate_infos[:_MAX_AMBIGUOUS_CANDIDATES],
    )


def resolve_pair_for_mate(
    model,
    spec1: Any,
    spec2: Any,
    *,
    mark: int = 1,
    entity_type_hints: tuple[str | None, str | None] = (None, None),
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """为配合准备两次选择: 清空选择集 -> 依次解析 -> 校验选择计数。

    返回 (证据1, 证据2, 选择计数)。任一证据 status != resolved 或计数 != 2
    时, 上层不应继续 AddMate5。
    """
    model.ClearSelection2(True)
    handle1, evidence1 = resolve_selection(
        model, spec1, entity_type_hint=entity_type_hints[0], append=False, mark=mark
    )
    handle2, evidence2 = resolve_selection(
        model, spec2, entity_type_hint=entity_type_hints[1], append=True, mark=mark
    )
    selection_manager = safe_get_com_member(model, "SelectionManager")
    count = int(safe_get_com_member(selection_manager, "GetSelectedObjectCount2", -1) or 0)
    if evidence1.get("status") != "resolved":
        evidence1["selection_count"] = count
        return evidence1, evidence2, count
    if evidence2.get("status") != "resolved":
        evidence2["selection_count"] = count
        return evidence1, evidence2, count
    evidence1["selection_count"] = count
    evidence2["selection_count"] = count
    return evidence1, evidence2, count
