"""真机回归: Feature Graph IR 试点 — 3 个参数化件双路构建对比。

需要 Windows + SolidWorks + pywin32; 不被 pytest 收集 (无 test_ 前缀)::

    python tests/solidworks_feature_graph_regression.py --output-dir <目录>

每个部件: 直调库函数构建参照件 -> build_from_ir 构建试点件 ->
compare_documents 必须 verified -> 闭式解体积独立校验 (防止双路同错)。
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
from sw_connect import connect_solidworks, new_document, open_document, save_document  # noqa: E402
import feature_graph as feature_graph_module  # noqa: E402
from sw_part import (  # noqa: E402
    end_sketch,
    extrude_boss,
    extrude_cut,
    linear_pattern,
    sketch_circle,
    sketch_corner_rectangle,
    start_sketch,
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
    "schemaVersion": "1.0",
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
    "schemaVersion": "1.0",
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
    "schemaVersion": "1.0",
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


SCENARIOS = (
    scenario_box_pattern,
    scenario_flange,
    scenario_bracket,
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
