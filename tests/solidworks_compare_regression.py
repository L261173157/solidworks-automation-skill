"""真机回归: 客观对比 compare_documents (SW2024/SW2026)。

需要 Windows + SolidWorks + pywin32; 不被 pytest 收集 (无 test_ 前缀)::

    python tests/solidworks_compare_regression.py --output-dir <目录>

场景契约与 tests/test_sw_compare.py (纯函数单测) 对应。
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

from sw_assembly import add_component  # noqa: E402
from sw_compare import compare_documents  # noqa: E402
from sw_connect import connect_solidworks, new_document, save_document  # noqa: E402
from sw_part import (  # noqa: E402
    current_sketch_name,
    end_sketch,
    extrude_boss,
    sketch_corner_rectangle,
    start_sketch,
)


def _build_box(model, size):
    half = size / 2
    start_sketch(model, "Front Plane")
    sketch_corner_rectangle(model, -half, -half, half, half)
    end_sketch(model)
    boss = extrude_boss(model, current_sketch_name(model), size)
    if boss is None:
        raise RuntimeError("基体拉伸特征创建失败")
    return boss


def _make_box_file(sw, out_dir: Path, name: str, size: float) -> Path:
    model = new_document(sw, "part")
    _build_box(model, size)
    path = out_dir / f"{name}.SLDPRT"
    if not save_document(model, str(path)):
        raise RuntimeError(f"保存失败: {path}")
    return path


def _make_assembly_file(sw, out_dir: Path, name: str, part_path: Path, copies: int) -> Path:
    asm = new_document(sw, "assembly")
    for index in range(copies):
        add_component(asm, str(part_path), x=0.05 * index, y=0.0, z=0.0)
    path = out_dir / f"{name}.SLDASM"
    if not save_document(asm, str(path)):
        raise RuntimeError(f"保存失败: {path}")
    return path


def scenario_identical_verified(sw, out_dir: Path) -> dict:
    path_a = _make_box_file(sw, out_dir, "cmp_a", 0.01)
    result = compare_documents(sw, str(path_a), str(path_a))
    if result["verdict"] != "verified":
        raise RuntimeError(f"同文件对比应为 verified: {result.get('verdict')} {result.get('error')}")
    return {"verdict": result["verdict"]}


def scenario_resized_mismatch(sw, out_dir: Path) -> dict:
    path_a = _make_box_file(sw, out_dir, "cmp_b10", 0.01)
    path_b = _make_box_file(sw, out_dir, "cmp_b12", 0.012)
    result = compare_documents(sw, str(path_a), str(path_b))
    if result["verdict"] != "mismatch":
        raise RuntimeError(f"尺寸差异应为 mismatch: {result.get('verdict')}")
    delta = result["checks"]["metrics"]["volume_rel_delta"]
    # 10^3 -> 12^3: 728/1728 ≈ 0.4213
    if not (0.40 < delta < 0.45):
        raise RuntimeError(f"体积相对差不符合闭式解: {delta}")
    return {"volume_rel_delta": delta}


def scenario_assembly_component_diff(sw, out_dir: Path) -> dict:
    part_path = _make_box_file(sw, out_dir, "cmp_part", 0.01)
    asm_a = _make_assembly_file(sw, out_dir, "cmp_asm1", part_path, 1)
    asm_b = _make_assembly_file(sw, out_dir, "cmp_asm2", part_path, 2)
    result = compare_documents(sw, str(asm_a), str(asm_b))
    if result["verdict"] != "mismatch":
        raise RuntimeError(f"组件数差异应为 mismatch: {result.get('verdict')}")
    if result["checks"]["topology"]["components_equal"]:
        raise RuntimeError("组件清单应当不同")
    return {"components_a": 1, "components_b": 2}


def scenario_roundtrip_verified(sw, out_dir: Path) -> dict:
    path_a = _make_box_file(sw, out_dir, "cmp_c", 0.01)
    time.sleep(1)
    result = compare_documents(sw, str(path_a), str(path_a))
    if result["verdict"] != "verified":
        raise RuntimeError(f"落盘重读后应 verified: {result.get('verdict')}")
    return {"verdict": result["verdict"]}


SCENARIOS = (
    scenario_identical_verified,
    scenario_resized_mismatch,
    scenario_assembly_component_diff,
    scenario_roundtrip_verified,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="客观对比真机回归")
    parser.add_argument("--output-dir", default=str(Path(tempfile.gettempdir()) / "solidworks_compare_regression"))
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

    summary = run_dir / "compare_regression_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总: {summary}")
    return 0 if all(item["status"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
