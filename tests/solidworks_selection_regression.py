"""真机回归: 声明式选择引擎 (SelectionSpec)。

需要 Windows + SolidWorks + pywin32。文件名不带 test_ 前缀, 不会被 pytest 收集;
运行方式::

    python tests/solidworks_selection_regression.py --output-dir <目录>

场景契约与 tests/test_sw_selection.py (伪 COM 单测) 一一对应。
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "scripts"))

from sw_assembly import add_component, add_mate_coincident  # noqa: E402
from sw_connect import connect_solidworks, new_document, save_document  # noqa: E402
from sw_part import (  # noqa: E402
    current_sketch_name,
    end_sketch,
    extrude_boss,
    sketch_corner_rectangle,
    start_sketch,
)
from sw_selection import resolve_selection  # noqa: E402


def _build_box(model, size=0.01):
    """前视基准面 size×size 矩形拉伸 size (米)。"""
    start_sketch(model, "Front Plane")
    half = size / 2
    sketch_corner_rectangle(model, -half, -half, half, half)
    end_sketch(model)
    boss = extrude_boss(model, current_sketch_name(model), size)
    if boss is None:
        raise RuntimeError("基体拉伸特征创建失败")
    return boss


def scenario_plane_alias(sw, out_dir: Path) -> dict:
    asm = new_document(sw, "assembly")
    handle, evidence = resolve_selection(asm, {"kind": "plane", "name": "Front Plane"}, mark=1)
    if evidence.get("status") != "resolved":
        raise RuntimeError(f"英文基准面名解析失败: {evidence}")
    # 中文别名失败时必须回退到英文别名 (真机实测中文版 SW 不认中文名)。
    handle, evidence = resolve_selection(asm, {"kind": "plane", "name": "前视基准面"}, mark=1)
    if evidence.get("status") != "resolved" or evidence.get("matched") != "Front Plane":
        raise RuntimeError(f"中文别名回退失败: {evidence}")
    selection_manager = asm.SelectionManager
    count = selection_manager.GetSelectedObjectCount2(-1)
    if count != 1:
        raise RuntimeError(f"选择计数异常: {count}")
    return {"matched": evidence["matched"], "entity_type": evidence["entity_type"]}


def scenario_coordinate_face(sw, out_dir: Path) -> dict:
    part_path = out_dir / "sel_box.SLDPRT"
    part = new_document(sw, "part")
    _build_box(part, 0.01)
    save_document(part, str(part_path))

    asm = new_document(sw, "assembly")
    add_component(asm, str(part_path), x=0.0, y=0.0, z=0.0)

    # 10mm 盒的顶面中心位于 z=10mm; 以毫米声明, 引擎内部转米。
    # 新文档首次坐标选择可能静默失败, 引擎必须自动刷新视图重试。
    handle, evidence = resolve_selection(
        asm, {"kind": "coordinate", "point_mm": [0, 0, 10]}, mark=1
    )
    if evidence.get("status") != "resolved":
        raise RuntimeError(f"坐标命中顶面失败: {evidence}")
    selection_manager = asm.SelectionManager
    count = selection_manager.GetSelectedObjectCount2(-1)
    if count != 1:
        raise RuntimeError(f"坐标命中后选择计数异常: {count}")
    return {"point_m": evidence["point_m"], "selection_count": count}


def scenario_component_spec(sw, out_dir: Path) -> dict:
    part_path = out_dir / "comp_box.SLDPRT"
    part = new_document(sw, "part")
    _build_box(part, 0.01)
    save_document(part, str(part_path))

    asm = new_document(sw, "assembly")
    add_component(asm, str(part_path), x=0.0, y=0.0, z=0.0)
    add_component(asm, str(part_path), x=0.05, y=0.0, z=0.0)

    # SolidWorks 组件实例名为 comp_box-1 / comp_box-2; 精确名唯一命中时
    # 返回组件对象本身 (resolved_object_only, 不写选择集), 而公共关键字
    # "comp_box" 必须报 ambiguous (歧义不猜测是引擎契约)。
    handle, evidence = resolve_selection(asm, {"kind": "component", "name": "comp_box-2"})
    if evidence.get("status") != "resolved_object_only" or handle is None:
        raise RuntimeError(f"组件精确名匹配失败: {evidence}")
    handle, evidence = resolve_selection(asm, {"kind": "component", "name": "comp_box"})
    if evidence.get("status") != "ambiguous" or len(evidence.get("candidates", [])) != 2:
        raise RuntimeError(f"公共关键字应报告歧义: {evidence}")
    return {"resolved": "comp_box-2", "ambiguous_candidates": 2}


def scenario_mate_via_coordinate_specs(sw, out_dir: Path) -> dict:
    """两个 10mm 盒相邻放置, 以坐标 spec 命中贴合面做重合配合。"""
    part_path = out_dir / "mate_box.SLDPRT"
    part = new_document(sw, "part")
    _build_box(part, 0.01)
    save_document(part, str(part_path))

    asm = new_document(sw, "assembly")
    add_component(asm, str(part_path), x=0.0, y=0.0, z=0.0)
    add_component(asm, str(part_path), x=0.02, y=0.0, z=0.0)

    # 10mm 盒: 组件 1 固定于原点 (x: -5..5), 组件 2 在 x+20mm (x: 15..25)。
    # 面 1: 盒 1 右侧面中心 (5,0,5); 面 2: 盒 2 左侧面中心 (15,0,5)。
    # 两个面不贴合 (留 10mm 间隙), 重合配合把盒 2 平移到贴上盒 1。
    mate = add_mate_coincident(
        asm,
        {"kind": "coordinate", "point_mm": [5, 0, 5]},
        "FACE",
        {"kind": "coordinate", "point_mm": [15, 0, 5]},
        "FACE",
    )
    if mate is None:
        raise RuntimeError("坐标 spec 配合创建失败")
    return {"mate_created": True}


SCENARIOS = (
    scenario_plane_alias,
    scenario_coordinate_face,
    scenario_component_spec,
    scenario_mate_via_coordinate_specs,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="声明式选择引擎真机回归")
    parser.add_argument("--output-dir", default=str(Path(tempfile.gettempdir()) / "solidworks_selection_regression"))
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

    summary = run_dir / "selection_regression_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总: {summary}")
    return 0 if all(item["status"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
