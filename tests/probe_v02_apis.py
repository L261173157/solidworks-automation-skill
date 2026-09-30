"""v0.2 真机探针: FeatureLinearPattern3 调用惯例 + fillet/chamfer 选边 + 反向门参数回读。

需要 Windows + SolidWorks + pywin32; 不被 pytest 收集 (无 test_ 前缀)::

    venv/Scripts/python.exe tests/probe_v02_apis.py --output-dir <目录>

查证结论 (2026-09-30, 官方示例 Create Linear Pattern Example (VB.NET) + 本机 sldworks.tlb):
- FeatureLinearPattern3(Num1, Spacing1, Num2, Spacing2, FlipDir1, FlipDir2,
  DName1, DName2, GeometryPattern, VaryInstance) 共 10 参;
- DName1/DName2 = "定义方向 1/2 的尺寸名", 官方示例传字面量 "NULL",
  方向实体走预选: 方向 1 = mark 1, 方向 2 = mark 2, 阵列种子 = mark 4。

本探针回答四个问题:
1. 已消费草图中的构造中心线段能否作为方向实体 (mark 1) 被 FeatureLinearPattern3 接受;
2. FeatureFillet / InsertFeatureChamfer 的目标边选择 mark 惯例 (mark 1 还是 0);
3. 半剖轮廓 + 同草图中心线 + FeatureRevolve2 流程 (part-modeling.md 已有样例, 复核);
4. collect_ir 反向门: model.Parameter("Dn@特征名") 与 IFeature.GetDefinition()
   两条路各能回读哪些参数。
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

from sw_compare import collect_model_fingerprint  # noqa: E402
from sw_connect import connect_solidworks, get_com_member, new_document, save_document  # noqa: E402
from sw_part import (  # noqa: E402
    _select_by_id,
    _select_com_object,
    end_sketch,
    extrude_boss,
    sketch_circle,
    sketch_corner_rectangle,
    start_sketch,
)
from sw_selection import resolve_selection  # noqa: E402


def _volume_mm3(sw, model) -> float:
    fingerprint = collect_model_fingerprint(model)
    volume = (fingerprint.get("metrics") or {}).get("volume_mm3")
    if volume is None:
        raise RuntimeError("体积不可用")
    return float(volume)


def _assert_close(label: str, got: float, expect: float, tol: float = 0.02) -> float:
    delta = abs(got - expect) / expect
    print(f"    {label}: got {got:.2f}, expect {expect:.2f} ({delta:.3%})")
    if delta > tol:
        raise RuntimeError(f"{label} 体积偏差超阈值: {delta:.2%}")
    return got


def _selection_evidence(model) -> str:
    sel = model.SelectionManager
    count = get_com_member(sel, "GetSelectedObjectCount2", -1)
    parts = []
    for index in range(1, int(count) + 1):
        mark = None
        try:
            mark = sel.GetSelectedObjectMark(index)
        except Exception:  # noqa: BLE001
            pass
        parts.append(f"#{index} mark={mark}")
    return f"count={count} [{', '.join(parts)}]"


def _find_centerline(model, sketch_name: str, sketch_ref=None):
    """在 (已消费) 草图里找唯一构造中心线段, 返回 (段对象, 枚举信息)。

    优先用 end_sketch 返回的 SketchSelectionRef.sketch 对象引用
    (晚绑定下 Feature.GetSpecificFeature2 可能 Member not found)。
    """
    sketch = getattr(sketch_ref, "sketch", None) if sketch_ref is not None else None
    if sketch is None:
        feature = model.FeatureByName(sketch_name)
        if feature is None:
            raise RuntimeError(f"找不到草图特征: {sketch_name}")
        for method in ("GetSpecificFeature2", "GetSpecificFeature"):
            try:
                sketch = getattr(feature, method)()
            except Exception:  # noqa: BLE001
                continue
            if sketch is not None:
                break
    if sketch is None:
        raise RuntimeError(f"无法获取草图对象: {sketch_name}")
    segments = get_com_member(sketch, "GetSketchSegments")
    lines = []
    for seg in segments:
        seg_type = int(get_com_member(seg, "GetType"))
        construction = bool(get_com_member(seg, "ConstructionGeometry"))
        info = f"type={seg_type} construction={construction}"
        # GetType 晚绑定下可能恒为 0; IR 自建草图里唯一构造几何段即中心线。
        if construction:
            lines.append(seg)
        print(f"    草图段: {info}")
    if len(lines) != 1:
        raise RuntimeError(f"构造中心线数量 != 1: {len(lines)}")
    return lines[0], {"segment_count": len(segments)}


def probe_linear_pattern(sw, out_dir: Path) -> dict:
    """问题 1: 中心线段作方向实体 + FeatureLinearPattern3 10 参调用。"""
    print("[probe] 线性阵列: 底板(含中心线) + 凸台, 沿 +X 阵列 4 实例")
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    model.SketchManager.CreateCenterLine(-0.04, 0, 0, 0.04, 0, 0)
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    plate_ref = end_sketch(model)
    plate_sketch = plate_ref.name
    if extrude_boss(model, plate_sketch, 0.008) is None:
        raise RuntimeError("底板拉伸失败")

    model.ClearSelection2(True)
    _handle, evidence = resolve_selection(model, {"kind": "coordinate", "point_mm": [0, 0, 8]}, mark=1)
    if evidence.get("status") != "resolved":
        raise RuntimeError(f"顶面锚点失败: {evidence}")
    model.SketchManager.InsertSketch(True)
    # 凸台中心 x=-22.5, 阵列 4 实例间距 15 -> 中心 -22.5/-7.5/7.5/22.5, 全部落在底板上。
    sketch_corner_rectangle(model, -0.0275, -0.005, -0.0175, 0.005)
    boss_sketch = end_sketch(model).name
    # 真机实测: 面上草图默认拉伸方向朝材料内, 需翻转。
    boss = extrude_boss(model, boss_sketch, 0.005, direction=False)
    if boss is None:
        raise RuntimeError("凸台拉伸失败")
    boss_name = str(get_com_member(boss, "Name"))
    print(f"    底板草图={plate_sketch}, 凸台={boss_name}")

    centerline, _info = _find_centerline(model, plate_sketch, sketch_ref=plate_ref)
    entity_name = None
    try:
        entity_name = model.GetEntityName(centerline)
    except Exception:  # noqa: BLE001
        pass
    print(f"    中心线段实体名: {entity_name!r}")

    dname_variants = ["NULL", "None", ""]
    created = None
    used_dname = None
    for dname in dname_variants:
        model.ClearSelection2(True)
        if not _select_by_id(model.Extension, boss_name, "BODYFEATURE", mark=4):
            raise RuntimeError(f"种子特征选择失败: {boss_name}")
        if not _select_com_object(centerline, append=True, mark=1):
            raise RuntimeError("中心线段选择失败")
        print(f"    预选: {_selection_evidence(model)}")
        try:
            created = model.FeatureManager.FeatureLinearPattern3(
                4, 0.015, 1, 0.01, False, False, dname, dname, False, False
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    DName={dname!r} -> COM 异常: {exc}")
            continue
        print(f"    DName={dname!r} -> 返回 {created}")
        if created is not None:
            used_dname = dname
            break
    if created is None:
        raise RuntimeError(f"FeatureLinearPattern3 全部 DName 变体失败: {dname_variants}")

    # 持久化验证: 改名 -> 重建 -> 按名回读。
    pattern_name = "LPattern-probe"
    created.Name = pattern_name
    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    if model.FeatureByName(pattern_name) is None:
        raise RuntimeError("阵列特征未在特征树持久化")

    volume = _volume_mm3(sw, model)
    expect = 60 * 40 * 8 + 4 * 10 * 10 * 5
    _assert_close("线性阵列体积", volume, expect)
    path = out_dir / "probe_lpattern.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("探针件保存失败")
    sw.CloseDoc(model.GetTitle)
    return {"scenario": "linear_pattern_centerline", "status": "pass",
            "dname_literal": used_dname, "entity_name": entity_name,
            "boss_feature": boss_name, "sketch": plate_sketch}


def _vertical_edges(model):
    """枚举竖直边 (z 向直线边), 返回 [(边对象, x_mm, y_mm), ...]。"""
    model.ForceRebuild3(False)
    bodies = model.GetBodies2(0, False)
    edges = []
    seen = set()
    for body in bodies:
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
            dz = abs(end[2] - start[2])
            dx = abs(end[0] - start[0])
            dy = abs(end[1] - start[1])
            if dz > 0.005 and dx < 1e-6 and dy < 1e-6:
                key = (round(start[0], 6), round(start[1], 6))
                if key in seen:
                    continue
                seen.add(key)
                edges.append((edge, start[0] * 1000.0, start[1] * 1000.0))
    return edges


def probe_fillet_chamfer(sw, out_dir: Path) -> dict:
    """问题 2: fillet (x=-30 端两竖边 r5) + chamfer (x=+30 端两竖边 2x45deg) 的选边 mark。"""
    print("[probe] 圆角/倒角: 60x40x8 块, 一端 fillet r5, 另一端 chamfer 2x45deg")
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    block_sketch = end_sketch(model).name
    if extrude_boss(model, block_sketch, 0.008) is None:
        raise RuntimeError("块体拉伸失败")

    edges = _vertical_edges(model)
    print(f"    竖直边: {[(round(x, 2), round(y, 2)) for _e, x, y in edges]}")
    fillet_set = [edge for edge, x, _y in edges if abs(x + 30) < 0.5]
    chamfer_set = [edge for edge, x, _y in edges if abs(x - 30) < 0.5]
    if len(fillet_set) != 2 or len(chamfer_set) != 2:
        raise RuntimeError(f"边过滤数量异常: fillet={len(fillet_set)}, chamfer={len(chamfer_set)}")

    def _try_select_call(edges_, mark, call):
        model.ClearSelection2(True)
        for index, edge in enumerate(edges_):
            if not _select_com_object(edge, append=index > 0, mark=mark):
                return None, f"mark={mark} 选择失败"
        try:
            feature = call()
        except Exception as exc:  # noqa: BLE001
            return None, f"mark={mark} COM 异常: {exc}"
        print(f"    mark={mark} -> 返回 {feature}")
        return feature, f"mark={mark}"

    fillet_feature = None
    fillet_mark = None
    for mark in (1, 0):
        feature, note = _try_select_call(fillet_set, mark, lambda: model.FeatureManager.FeatureFillet(195, 0.005, 0, 0, None, None, None))
        print(f"    fillet {note}")
        if feature is not None:
            fillet_feature, fillet_mark = feature, mark
            break
    if fillet_feature is None:
        raise RuntimeError("FeatureFillet 两种 mark 均失败")

    chamfer_feature = None
    chamfer_mark = None
    for mark in (1, 0):
        feature, note = _try_select_call(chamfer_set, mark, lambda: model.FeatureManager.InsertFeatureChamfer(4, 1, 0.002, math.pi / 4, 0, 0, 0, 0))
        print(f"    chamfer {note}")
        if feature is not None:
            chamfer_feature, chamfer_mark = feature, mark
            break
    if chamfer_feature is None:
        raise RuntimeError("InsertFeatureChamfer 两种 mark 均失败")

    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    if model.FeatureByName(str(get_com_member(fillet_feature, "Name"))) is None:
        raise RuntimeError("fillet 特征未持久化")
    if model.FeatureByName(str(get_com_member(chamfer_feature, "Name"))) is None:
        raise RuntimeError("chamfer 特征未持久化")

    volume = _volume_mm3(sw, model)
    expect = 60 * 40 * 8 - 2 * (25 - 25 * math.pi / 4) * 8 - 2 * (2.0 * 2.0 / 2) * 8
    _assert_close("圆角倒角块体积", volume, expect)
    path = out_dir / "probe_fillet_chamfer.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("探针件保存失败")
    sw.CloseDoc(model.GetTitle)
    return {"scenario": "fillet_chamfer_marks", "status": "pass",
            "fillet_mark": fillet_mark, "chamfer_mark": chamfer_mark}


def probe_revolve(sw, out_dir: Path) -> dict:
    """问题 3: 半剖轮廓 + 同草图中心线 + FeatureRevolve2 (复核 part-modeling.md 流程)。"""
    print("[probe] 旋转: 阶梯轴 半剖轮廓 + 中心线")
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    model.SketchManager.CreateCenterLine(-0.005, 0, 0, 0.12, 0, 0)
    points = [(0, 0), (0, 28), (43, 28), (43, 32.5), (113, 32.5), (113, 0)]
    mm = lambda v: v / 1000.0  # noqa: E731
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        model.SketchManager.CreateLine(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0)
    sketch_name = end_sketch(model).name
    model.ClearSelection2(True)
    if not _select_by_id(model.Extension, sketch_name, "SKETCH"):
        from sw_part import _ensure_sketch_selected

        _ensure_sketch_selected(model, sketch_name)
    feature = model.FeatureManager.FeatureRevolve2(
        True, True, False, False, False, False, 0, 0, 2 * math.pi, 0,
        False, False, 0.0, 0.0, 0, 0, 0, True, True, True,
    )
    if feature is None:
        raise RuntimeError("FeatureRevolve2 返回 None")
    revolve_name = str(get_com_member(feature, "Name"))
    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    if model.FeatureByName(revolve_name) is None:
        raise RuntimeError("旋转特征未持久化")
    volume = _volume_mm3(sw, model)
    expect = math.pi * 28 * 28 * 43 + math.pi * 32.5 * 32.5 * 70
    _assert_close("阶梯轴体积", volume, expect)
    path = out_dir / "probe_revolve.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("探针件保存失败")
    sw.CloseDoc(model.GetTitle)
    return {"scenario": "revolve_centerline", "status": "pass", "feature": revolve_name}


def _probe_definition(model, feature_name: str) -> dict:
    """问题 4b: IFeature.GetDefinition() 常见属性/方法试探回读。"""
    feature = model.FeatureByName(feature_name)
    if feature is None:
        return {"error": f"特征不存在: {feature_name}"}
    try:
        defn = feature.GetDefinition()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"GetDefinition 异常: {exc}"}
    if defn is None:
        return {"error": "GetDefinition 返回 None"}
    result: dict = {}
    for prop in ("D1TotalInstances", "D1Spacing", "D2TotalInstances", "D2Spacing",
                 "D1ReverseDirection", "DefaultRadius", "FilletType"):
        try:
            result[prop] = round(float(getattr(defn, prop)), 6)
        except Exception:  # noqa: BLE001
            pass
    for method, args in (("GetDepth", (True,)), ("GetAngle", (True,)),
                         ("GetDefaultRadius", (0,)), ("GetEdgeChamferDistance", (0,))):
        try:
            result[method] = round(float(getattr(defn, method)(*args)), 6)
        except Exception:  # noqa: BLE001
            pass
    return result or {"error": "常见属性/方法均不可读"}


def probe_reverse_gate(sw, out_dir: Path) -> dict:
    """问题 4: 打开三个探针件, 对每个特征回读 enumerate_features (含 GetDimensions)
    与 IFeature.GetDefinition() 两条路, 确认 collect_ir 反向门可用字段。"""
    print("[probe] 反向门参数回读")
    report = {}
    for part in ("probe_lpattern", "probe_fillet_chamfer", "probe_revolve"):
        path = out_dir / f"{part}.SLDPRT"
        if not path.exists():
            continue
        model = None
        try:
            from sw_connect import open_document
            from sw_inspect import enumerate_features

            model = open_document(sw, str(path), silent=True, raise_on_error=False)
            if model is None:
                report[part] = {"error": "无法打开"}
                continue
            enumeration = enumerate_features(model, max_features=200)
            part_report = {}
            for entry in enumeration.get("features", []):
                name = str(entry.get("name") or "")
                if not name:
                    continue
                dims = {
                    str(dim.get("name")): dim.get("value_mm")
                    for dim in entry.get("dimensions", [])
                }
                part_report[name] = {
                    "type": entry.get("type"),
                    "dimensions": dims,
                    "definition": _probe_definition(model, name),
                }
            report[part] = part_report
            print(f"    {part}: {json.dumps(part_report, ensure_ascii=False)[:600]}")
        except Exception as exc:  # noqa: BLE001
            report[part] = {"error": f"{exc.__class__.__name__}: {exc}"}
        finally:
            if model is not None:
                try:
                    sw.CloseDoc(model.GetTitle)
                except Exception:  # noqa: BLE001
                    pass
    return {"scenario": "reverse_gate_readback", "status": "pass", "parts": report}


PROBES = (
    ("linear_pattern_centerline", probe_linear_pattern),
    ("fillet_chamfer_marks", probe_fillet_chamfer),
    ("revolve_centerline", probe_revolve),
    ("reverse_gate_readback", probe_reverse_gate),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.2 真机探针")
    parser.add_argument("--output-dir", default=str(Path(tempfile.gettempdir()) / "probe_v02_apis"))
    args = parser.parse_args()

    run_dir = Path(args.output_dir) / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    sw, _model = connect_solidworks()
    results = []
    for label, probe in PROBES:
        try:
            detail = probe(sw, run_dir)
            results.append(detail)
            print(f"[PASS] {label}")
        except Exception as exc:  # noqa: BLE001
            results.append({"scenario": label, "status": "fail",
                            "error": str(exc), "traceback": traceback.format_exc(limit=5)})
            print(f"[FAIL] {label}: {exc}")
        finally:
            try:
                model = sw.ActiveDoc
                if model is not None:
                    sw.CloseDoc(model.GetTitle)
            except Exception:  # noqa: BLE001
                pass

    summary = run_dir / "probe_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总: {summary}")
    return 0 if all(item.get("status") == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
