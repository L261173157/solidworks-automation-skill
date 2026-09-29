"""客观文档对比: compare_documents 用拓扑计数与质量属性给出可复核 verdict。

借鉴 SolidPilot 的 objective diff 思想 (全部全新实现, 未复制其代码):
- ``collect_model_fingerprint``: 只读采集拓扑 (逐实体面/边数)、特征计数、
  质量属性 (体积/表面积/质心) 与装配体组件名清单;
- ``compare_fingerprints``: 纯函数判定, 规则:
  * 拓扑一致且体积/表面积相对差 ≤1%、质心位移 ≤0.1mm → ``verified``;
  * 指标全过但拓扑不同 → ``review_required``;
  * 任一指标超差 → ``mismatch``;
- limitations 明示: 只做计数与标量指标对比, 不做拓扑同构证明、不比对 GD&T。

verdict 是交付证据, 不替代人工审查 (人工复核仍由 sw_review 负责)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .sw_connect import get_com_member, open_document, safe_get_com_member
    from .sw_inspect import bounding_box, enumerate_features
    from .sw_mass_properties import mass_properties
except ImportError:  # 直接以 scripts/ 为工作目录导入
    from sw_connect import get_com_member, open_document, safe_get_com_member
    from sw_inspect import bounding_box, enumerate_features
    from sw_mass_properties import mass_properties

DEFAULT_TOLERANCES = {"volume_rel": 0.01, "area_rel": 0.01, "com_mm": 0.1}


def _safe_relative_delta(a: float, b: float) -> float:
    reference = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / reference


def _topology_fingerprint(model) -> dict[str, Any]:
    # GetBodies2 仅零件/多实体, GetComponents 仅装配体; 用 try 隔离文档类型差异
    # (safe_get_com_member 不吞 pywin32 的 AttributeError)。
    try:
        bodies = tuple(safe_get_com_member(model, "GetBodies2", 0, False) or ())
    except Exception:  # noqa: BLE001
        bodies = ()
    body_prints = []
    for body in bodies:
        body_prints.append(
            {
                "faces": safe_get_com_member(body, "GetFaceCount"),
                "edges": safe_get_com_member(body, "GetEdgeCount"),
            }
        )
    features = enumerate_features(model, max_features=500)
    component_names = []
    try:
        components = safe_get_com_member(model, "GetComponents", True) or ()
    except Exception:  # noqa: BLE001
        components = ()
    for component in components:
        component_names.append(str(get_com_member(component, "Name2") or ""))
    return {
        "bodies": body_prints,
        "feature_count": len(features),
        "component_names": sorted(component_names),
    }


def collect_model_fingerprint(model) -> dict[str, Any]:
    """只读采集模型指纹。质量属性不可用时相应指标为 None (不阻塞拓扑判定)。"""
    mass = mass_properties(model)
    try:
        bbox = bounding_box(model)
    except Exception:  # noqa: BLE001 - 包围盒不可用不阻塞指纹
        bbox = {"status": "failed"}
    fingerprint = {
        "title": safe_get_com_member(model, "GetTitle"),
        "path": safe_get_com_member(model, "GetPathName"),
        "topology": _topology_fingerprint(model),
        "metrics": {
            "volume_mm3": mass.get("volume_mm3"),
            "surface_mm2": mass.get("surface_mm2"),
            "center_of_mass_mm": mass.get("center_of_mass_mm"),
        },
        "bbox": bbox,
        "limitations": [
            "拓扑为计数级对比, 不做同构证明; GD&T/标注不参与判定",
        ],
    }
    return fingerprint


def compare_fingerprints(
    fingerprint_a: dict[str, Any],
    fingerprint_b: dict[str, Any],
    tolerances: dict[str, float] | None = None,
) -> dict[str, Any]:
    """纯函数: 两个指纹的逐指标 delta 与 verdict。"""
    tolerances = {**DEFAULT_TOLERANCES, **(tolerances or {})}
    topology_a = fingerprint_a.get("topology", {})
    topology_b = fingerprint_b.get("topology", {})
    metrics_a = fingerprint_a.get("metrics", {})
    metrics_b = fingerprint_b.get("metrics", {})

    checks: dict[str, Any] = {}

    bodies_equal = topology_a.get("bodies") == topology_b.get("bodies")
    features_equal = topology_a.get("feature_count") == topology_b.get("feature_count")
    components_equal = topology_a.get("component_names") == topology_b.get("component_names")
    checks["topology"] = {
        "bodies_equal": bodies_equal,
        "feature_count_a": topology_a.get("feature_count"),
        "feature_count_b": topology_b.get("feature_count"),
        "components_equal": components_equal,
    }

    metric_results: dict[str, Any] = {}
    volume_a, volume_b = metrics_a.get("volume_mm3"), metrics_b.get("volume_mm3")
    area_a, area_b = metrics_a.get("surface_mm2"), metrics_b.get("surface_mm2")
    com_a, com_b = metrics_a.get("center_of_mass_mm"), metrics_b.get("center_of_mass_mm")

    metrics_available = all(
        value is not None
        for value in (volume_a, volume_b, area_a, area_b, com_a, com_b)
    )
    checks["metrics_available"] = metrics_available

    volume_ok = area_ok = com_ok = True
    if metrics_available:
        volume_delta = _safe_relative_delta(float(volume_a), float(volume_b))
        area_delta = _safe_relative_delta(float(area_a), float(area_b))
        com_delta = max(
            abs(float(com_a[i]) - float(com_b[i])) for i in range(3)
        )
        volume_ok = volume_delta <= tolerances["volume_rel"]
        area_ok = area_delta <= tolerances["area_rel"]
        com_ok = com_delta <= tolerances["com_mm"]
        metric_results = {
            "volume_rel_delta": round(volume_delta, 6),
            "area_rel_delta": round(area_delta, 6),
            "com_delta_mm": round(com_delta, 4),
            "volume_ok": volume_ok,
            "area_ok": area_ok,
            "com_ok": com_ok,
        }
    checks["metrics"] = metric_results

    topology_equal = bodies_equal and features_equal and components_equal
    if not metrics_available:
        verdict = "verified" if topology_equal else "mismatch"
    elif topology_equal and volume_ok and area_ok and com_ok:
        verdict = "verified"
    elif volume_ok and area_ok and com_ok:
        verdict = "review_required"
    else:
        verdict = "mismatch"

    return {
        "schemaVersion": "1.0",
        "verdict": verdict,
        "tolerances": tolerances,
        "checks": checks,
        "limitations": [
            "计数与标量指标对比, 不构成拓扑同构证明",
            "verdict 是交付证据, 不替代 sw_review 人工审查",
        ],
    }


def compare_documents(
    sw,
    path_a: str,
    path_b: str,
    *,
    tolerances: dict[str, float] | None = None,
) -> dict[str, Any]:
    """打开两个文档分别采集指纹后关闭, 返回对比结果。

    文档以 silent 模式打开; 打开失败记入 verdict=mismatch 的 error 字段。
    """
    fingerprints: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, path in (("a", path_a), ("b", path_b)):
        model = open_document(sw, str(path), silent=True, raise_on_error=False)
        if model is None:
            errors[key] = f"无法打开文档: {path}"
            continue
        try:
            fingerprints[key] = collect_model_fingerprint(model)
        finally:
            title = safe_get_com_member(model, "GetTitle")
            if title:
                safe_get_com_member(sw, "CloseDoc", title)

    result: dict[str, Any] = {
        "schemaVersion": "1.0",
        "path_a": str(path_a),
        "path_b": str(path_b),
    }
    if errors:
        result["verdict"] = "mismatch"
        result["error"] = errors
        return result
    result["fingerprint_a"] = fingerprints["a"]
    result["fingerprint_b"] = fingerprints["b"]
    comparison = compare_fingerprints(fingerprints["a"], fingerprints["b"], tolerances)
    result.update(comparison)
    return result
