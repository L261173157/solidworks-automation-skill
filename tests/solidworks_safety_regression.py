"""真机回归: 运行时自适应与破坏性保护 (SW2024/SW2026)。

需要 Windows + SolidWorks + pywin32; 不被 pytest 收集 (无 test_ 前缀)::

    python tests/solidworks_safety_regression.py --output-dir <目录>

场景契约与 tests/test_sw_api_compat.py (伪对象单测) 对应。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "scripts"))

from sw_api_compat import default_cache_path  # noqa: E402
from sw_connect import connect_solidworks, new_document, save_document  # noqa: E402
from sw_part import (  # noqa: E402
    current_sketch_name,
    end_sketch,
    extrude_boss,
    sketch_corner_rectangle,
    start_sketch,
)


def _build_box(sw, out_dir: Path, name: str, size=0.01, content: bytes | None = None) -> Path:
    """构建 10mm 盒并保存; content 指定时直接写文件内容 (模拟既有文档)。"""
    path = out_dir / f"{name}.SLDPRT"
    if content is not None:
        path.write_bytes(content)
        return path
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    half = size / 2
    sketch_corner_rectangle(model, -half, -half, half, half)
    end_sketch(model)
    boss = extrude_boss(model, current_sketch_name(model), size)
    if boss is None:
        raise RuntimeError("基体拉伸特征创建失败")
    if not save_document(model, str(path)):
        raise RuntimeError(f"保存失败: {path}")
    return path


def scenario_overwrite_gate(sw, out_dir: Path) -> dict:
    target = out_dir / "gate.SLDPRT"
    target.write_bytes(b"ORIGINAL-CONTENT")
    model = new_document(sw, "part")
    try:
        save_document(model, str(target))
    except FileExistsError as exc:
        if "SW_SAVE_TARGET_EXISTS" not in str(exc):
            raise
        if target.read_bytes() != b"ORIGINAL-CONTENT":
            raise RuntimeError("拒绝覆盖后原文件被改动")
        return {"rejected": True}
    raise RuntimeError("未拒绝覆盖已有文件")


def scenario_overwrite_with_backup(sw, out_dir: Path) -> dict:
    target = out_dir / "backup.SLDPRT"
    old_content = b"OLD-DOCUMENT-BYTES-0x5A"
    target.write_bytes(old_content)
    model = new_document(sw, "part")
    if not save_document(model, str(target), overwrite=True, backup=True):
        raise RuntimeError("overwrite=True 保存失败")
    backup_dir = target.parent / ".cadstudio_backups"
    backups = list(backup_dir.glob("backup.*.SLDPRT"))
    if len(backups) != 1:
        raise RuntimeError(f"备份数量异常: {backups}")
    if backups[0].read_bytes() != old_content:
        raise RuntimeError("备份内容与原文件不一致")
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest[-1]["sha256"] != hashlib.sha256(old_content).hexdigest():
        raise RuntimeError("清单 SHA-256 与原文件不符")
    return {"backup": backups[0].name, "sha256": manifest[-1]["sha256"][:12]}


def scenario_extrude_compat_chain(sw, out_dir: Path) -> dict:
    """拉伸走 FeatureExtrusion3->2 兼容链, 胜出变体按版本落缓存。"""
    cache_path = default_cache_path()
    model = new_document(sw, "part")
    start_sketch(model, "Front Plane")
    sketch_corner_rectangle(model, -0.005, -0.005, 0.005, 0.005)
    end_sketch(model)
    boss = extrude_boss(model, current_sketch_name(model), 0.01)
    if boss is None:
        raise RuntimeError("兼容链拉伸失败")
    if not cache_path.is_file():
        raise RuntimeError("api_compat.json 缓存未生成")
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    chosen = (cache.get("unknown") or {}).get("part.extrude_boss") or (cache.get(str(sw.RevisionNumber)) or {}).get(
        "part.extrude_boss"
    )
    if chosen not in ("FeatureExtrusion3", "FeatureExtrusion2"):
        raise RuntimeError(f"缓存中的拉伸变体异常: {chosen}")
    return {"chosen_variant": chosen, "revision": sw.RevisionNumber}


SCENARIOS = (
    scenario_overwrite_gate,
    scenario_overwrite_with_backup,
    scenario_extrude_compat_chain,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行时自适应与破坏性保护真机回归")
    parser.add_argument("--output-dir", default=str(Path(tempfile.gettempdir()) / "solidworks_safety_regression"))
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

    summary = run_dir / "safety_regression_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总: {summary}")
    return 0 if all(item["status"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
