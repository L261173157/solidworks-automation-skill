"""真机探针: linear_pattern 方向 2 (mark 2) 与基准轴方向实体 (v0.3 前置查证)。

需要 Windows + SolidWorks + pywin32; 不被 pytest 收集 (无 test_ 前缀)::

    venv/Scripts/python.exe tests/probe_dir2_axis.py --output-dir <目录>

背景 (2026-09-30 v0.2 已验证): FeatureLinearPattern3 10 参, DName 传 "NULL",
方向 1 = mark 1 预选 (已消费草图 centerline 段, 对象级 Select2), 种子 = mark 4。
方向 2 = mark 2 仅有文档口径 (api_docs_index.py / part-modeling.md), 未真机验证;
基准轴作方向实体同样未验证 (全仓无 InsertAxis 调用)。

本探针回答三个问题 (结论真机 2026-09-30, SW2024 SP5):
1. mark 2 预选 + Num2/Spacing2 透传 => 双向网格阵列成立 (闭式解体积 0.000%);
2. 双向阵列尺寸后缀: D1=数量1, D2=数量2, D3=间距1(米), D4=间距2(米);
3. 两基准面交线 InsertAxis 可建轴 (RefAxis, 属性读取即触发插入); 但轴/模型
   边线作 FeatureLinearPattern3 方向实体被拒绝: 特征可建、GetErrorCode()=51
   (swSketchErrorExtRefFail)、GetD1AxisType() 轴=0/边线=1、实例坍缩为种子。
   唯一可用方向实体 = 已消费草图构造中心线段 (D1AxisType=3, err=0)。
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
    find_centerline_segment,
    sketch_centerline,
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


def _read_dims(model, feature_name: str) -> dict[str, float]:
    """model.Parameter("Dn@特征名") 逐个探测, 返回 {后缀: system_value}。"""
    dims = {}
    for index in range(1, 9):
        full = f"D{index}@{feature_name}"
        try:
            param = model.Parameter(full)
            if param is None:
                continue
            value = param.SystemValue
            if value is not None:
                dims[f"D{index}"] = float(value)
        except Exception:  # noqa: BLE001
            continue
    return dims


def _sketch_on_top_face(model):
    """顶面 (z=8) 开草图, 复用 v0.2 已验证的 coordinate 锚点路径。"""
    model.ClearSelection2(True)
    _handle, evidence = resolve_selection(model, {"kind": "coordinate", "point_mm": [0, 0, 8]}, mark=1)
    if evidence.get("status") != "resolved":
        raise RuntimeError(f"顶面锚点失败: {evidence}")
    model.SketchManager.InsertSketch(True)


def _build_plate_boss(model, *, plate_centerline: bool, boss_centerline: bool):
    """底板 + 顶面凸台, 两个草图各自可选携带构造中心线。

    已验证帧 (v0.2 回归): Front Plane 草图 direction=True 拉伸 -> 板 z∈[0,8],
    顶面 z=8; 面上草图 direction=False 朝板外 (+Z) 拉伸 5 -> 凸台 z∈[8,13]。
    凸台矩形 x∈[-27.5,-17.5] y∈[-20,-10] (中心 -22.5,-15): 4x3 网格间距 15 时
    实例中心 x=-22.5..22.5 / y=-15,0,15 全部落在板内。

    返回 (plate_ref, boss_ref, boss特征名)。
    """
    start_sketch(model, "Front Plane")
    if plate_centerline:
        sketch_centerline(model, -0.04, 0, 0.04, 0)
    sketch_corner_rectangle(model, -0.03, -0.02, 0.03, 0.02)
    plate_ref = end_sketch(model)
    if extrude_boss(model, plate_ref.name, 0.008) is None:
        raise RuntimeError("底板拉伸失败")
    _sketch_on_top_face(model)
    if boss_centerline:
        # 竖直方向中心线 (凸台草图平面 = 全局 XY, 竖直即 +Y 方向)。
        sketch_centerline(model, -0.0225, -0.025, -0.0225, 0.025)
    sketch_corner_rectangle(model, -0.0275, -0.02, -0.0175, -0.01)
    boss_ref = end_sketch(model)
    # 真机实测 (v0.2): 面上草图默认拉伸方向朝材料内, 需翻转。
    boss = extrude_boss(model, boss_ref.name, 0.005, direction=False)
    if boss is None:
        raise RuntimeError("凸台拉伸失败")
    return plate_ref, boss_ref, str(get_com_member(boss, "Name"))


def probe_dir2(sw, out_dir: Path) -> dict:
    """问题 1+2: mark 2 预选 + Num2/Spacing2 -> 双向网格; D1..D8 后缀回读。"""
    print("[probe] 方向 2: 底板(水平中心线=方向1) + 凸台(竖直中心线=方向2), 4x3 网格")
    model = new_document(sw, "part")
    plate_ref, boss_ref, boss_name = _build_plate_boss(
        model, plate_centerline=True, boss_centerline=True
    )
    dir1_segment = find_centerline_segment(model, plate_ref)
    dir2_segment = find_centerline_segment(model, boss_ref)

    model.ClearSelection2(True)
    if not _select_by_id(model.Extension, boss_name, "BODYFEATURE", mark=4):
        raise RuntimeError(f"种子特征选择失败: {boss_name}")
    if not _select_com_object(dir1_segment, append=True, mark=1):
        raise RuntimeError("方向 1 中心线选择失败")
    if not _select_com_object(dir2_segment, append=True, mark=2):
        raise RuntimeError("方向 2 中心线选择失败")
    created = model.FeatureManager.FeatureLinearPattern3(
        4, 0.015, 3, 0.015, False, False, "NULL", "NULL", False, False,
    )
    if created is None:
        raise RuntimeError("FeatureLinearPattern3 (mark 2 方向 2) 返回 None")
    pattern_name = str(get_com_member(created, "Name"))
    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    if model.FeatureByName(pattern_name) is None:
        raise RuntimeError("双向阵列特征未持久化")

    volume = _volume_mm3(sw, model)
    expect = 60 * 40 * 8 + 4 * 3 * 10 * 10 * 5
    _assert_close("双向网格体积", volume, expect)

    dims = _read_dims(model, pattern_name)
    print(f"    D 后缀回读 @{pattern_name}: {json.dumps(dims)}")
    path = out_dir / "probe_lpattern_dir2.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("探针件保存失败")
    sw.CloseDoc(model.GetTitle)
    return {"scenario": "linear_pattern_dir2", "status": "pass",
            "pattern_feature": pattern_name, "dims": dims}


def _feature_names(model) -> list[str]:
    names = []
    feature = get_com_member(model, "FirstFeature")
    while feature is not None:
        try:
            names.append(str(get_com_member(feature, "Name")))
        except Exception:  # noqa: BLE001
            pass
        feature = get_com_member(feature, "GetNextFeature")
    return names


def probe_axis(sw, out_dir: Path) -> dict:
    """问题 3: 两基准面交线 InsertAxis 可建轴 (正向); 轴/模型边线作方向实体被
    FeatureLinearPattern3 拒绝 (负向确认, 真机 2026-09-30 SW2024 SP5)。

    负向证据链: 特征可创建但 GetErrorCode()=51 (swSketchErrorExtRefFail),
    GetDefinition().GetD1AxisType() 轴=0 (未消费) / 边线=1 (识别但不可用),
    实例坍缩为种子 (体积=板+1凸台)。中心线段 (D1AxisType=3, err=0) 是唯一
    可用方向实体 —— 修正 v0.2 文档中"方向实体可以是模型边线、基准轴"的口径。
    """
    print("[probe] 基准轴: 两基准面交线 InsertAxis + 轴/边线方向实体负向确认")
    model = new_document(sw, "part")
    _plate_ref, _boss_ref, boss_name = _build_plate_boss(
        model, plate_centerline=False, boss_centerline=False
    )
    plate_plus_one_boss = 60 * 40 * 8 + 10 * 10 * 5

    names_before = set(_feature_names(model))
    model.ClearSelection2(True)
    if not _select_by_id(model.Extension, "Front Plane", "PLANE", mark=1):
        raise RuntimeError("Front Plane 选择失败")
    if not _select_by_id(model.Extension, "Right Plane", "PLANE", append=True, mark=1):
        raise RuntimeError("Right Plane 选择失败")
    # 真机实测: IModelDoc2.InsertAxis 在晚绑定 dynamic dispatch 下表现为属性
    # (属性读取即触发插入); get_com_member 兼容属性/方法两种形态。
    axis_result = get_com_member(model, "InsertAxis")
    print(f"    InsertAxis 返回: {axis_result!r}")
    if not axis_result:
        raise RuntimeError("两基准面交线 InsertAxis 失败 (正向部分)")
    new_names = [name for name in _feature_names(model) if name not in names_before]
    if not new_names:
        raise RuntimeError("InsertAxis 未产生新特征")
    axis_name = new_names[0]
    axis_feature = model.FeatureByName(axis_name)
    axis_type = str(get_com_member(axis_feature, "GetTypeName2"))
    print(f"    轴特征: name={axis_name!r} type={axis_type!r}")
    if axis_type != "RefAxis":
        raise RuntimeError(f"轴特征类型异常: {axis_type!r}")

    # 负向确认 A: 轴特征作方向 1 (mark 1) -> 特征可建但 err=51 / D1AxisType=0 / 实例坍缩
    model.ClearSelection2(True)
    if not _select_by_id(model.Extension, boss_name, "BODYFEATURE", mark=4):
        raise RuntimeError(f"种子特征选择失败: {boss_name}")
    if not _select_by_id(model.Extension, axis_name, "AXIS", append=True, mark=1):
        raise RuntimeError("基准轴选择失败")
    created = model.FeatureManager.FeatureLinearPattern3(
        3, 0.015, 1, 0.01, False, False, "NULL", "NULL", False, False,
    )
    if created is None:
        raise RuntimeError("轴方向阵列特征未创建 (与既有观测不符)")
    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    axis_err = int(get_com_member(created, "GetErrorCode"))
    axis_d1type = int(get_com_member(get_com_member(created, "GetDefinition"), "GetD1AxisType"))
    axis_vol = _volume_mm3(sw, model)
    print(f"    轴方向: err={axis_err} D1AxisType={axis_d1type} vol={axis_vol:.0f}")
    pattern_name = str(get_com_member(created, "Name"))
    if not (axis_err == 51 and axis_d1type == 0 and abs(axis_vol - plate_plus_one_boss) < 1.0):
        raise RuntimeError(
            f"轴方向实体负向断言不符: err={axis_err} D1AxisType={axis_d1type} vol={axis_vol:.0f}"
        )
    path = out_dir / "probe_axis_direction_negative.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("探针件保存失败")
    sw.CloseDoc(model.GetTitle)

    # 负向确认 B (独立零件): 模型边线作方向 1 (mark 1)
    model = new_document(sw, "part")
    _plate_ref, _boss_ref, boss_name = _build_plate_boss(
        model, plate_centerline=False, boss_centerline=False
    )
    model.ForceRebuild3(False)
    x_edge = None
    for body in tuple(model.GetBodies2(0, False) or ()):
        for edge in body.GetEdges():
            curve = get_com_member(edge, "GetCurve")
            if not bool(get_com_member(curve, "IsLine")):
                continue
            sv = get_com_member(edge, "GetStartVertex")
            ev = get_com_member(edge, "GetEndVertex")
            if not sv or not ev:
                continue
            s = [float(v) for v in get_com_member(sv, "GetPoint")]
            e = [float(v) for v in get_com_member(ev, "GetPoint")]
            if abs(e[0] - s[0]) > 0.05 and abs(e[1] - s[1]) < 1e-6:
                x_edge = edge
                break
        if x_edge is not None:
            break
    if x_edge is None:
        raise RuntimeError("未找到沿 X 的模型边线")
    model.ClearSelection2(True)
    if not _select_by_id(model.Extension, boss_name, "BODYFEATURE", mark=4):
        raise RuntimeError(f"种子特征选择失败: {boss_name}")
    if not _select_com_object(x_edge, append=True, mark=1):
        raise RuntimeError("模型边线选择失败")
    created = model.FeatureManager.FeatureLinearPattern3(
        3, 0.015, 1, 0.01, False, False, "NULL", "NULL", False, False,
    )
    if created is None:
        raise RuntimeError("边线方向阵列特征未创建 (与既有观测不符)")
    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    edge_err = int(get_com_member(created, "GetErrorCode"))
    edge_d1type = int(get_com_member(get_com_member(created, "GetDefinition"), "GetD1AxisType"))
    edge_vol = _volume_mm3(sw, model)
    print(f"    边线方向: err={edge_err} D1AxisType={edge_d1type} vol={edge_vol:.0f}")
    if not (edge_err == 51 and edge_d1type == 1 and abs(edge_vol - plate_plus_one_boss) < 1.0):
        raise RuntimeError(
            f"边线方向实体负向断言不符: err={edge_err} D1AxisType={edge_d1type} vol={edge_vol:.0f}"
        )

    path = out_dir / "probe_edge_direction_negative.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError("探针件保存失败")
    sw.CloseDoc(model.GetTitle)
    return {"scenario": "linear_pattern_axis", "status": "pass",
            "axis_feature": axis_name, "axis_type": axis_type,
            "insert_axis_return": repr(axis_result),
            "axis_direction_rejected": {"error_code": axis_err, "d1_axis_type": axis_d1type},
            "edge_direction_rejected": {"error_code": edge_err, "d1_axis_type": edge_d1type}}


PROBES = (
    ("linear_pattern_dir2", probe_dir2),
    ("linear_pattern_axis", probe_axis),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="方向 2 / 基准轴真机探针")
    parser.add_argument("--output-dir", default=str(Path(tempfile.gettempdir()) / "probe_dir2_axis"))
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
