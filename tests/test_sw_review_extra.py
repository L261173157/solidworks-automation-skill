"""@brief sw_review 覆盖率补充测试: 布局碰撞、BMP/PDF 证据、报告链路与 CLI 入口。

所有用例离线且确定性:
- COM/图形边界全部使用假模型与 monkeypatch, 不触发真实 SolidWorks;
- PDF 解析通过注入假 fitz 模块控制 span, 不依赖真实 PyMuPDF;
- __main__ 入口用 runpy.run_path 执行源文件, 同时覆盖顶层导入回退分支。
"""
from __future__ import annotations

import json
import runpy
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.sw_review as sw_review
from scripts import sw_connect, sw_preflight
from scripts.sw_connect import SolidWorksDocumentOpenError
from scripts.sw_review import (
    _drawing_boxes_overlap,
    _import_pdf_parser,
    _parse_args,
    _read_bmp_size,
    _review_worst_status,
    activate_model_for_preview,
    build_review_report,
    clear_selection_for_preview,
    collect_geometry_measurements,
    collect_model_summary,
    evaluate_review_report,
    group_coaxial_hole_segments,
    inspect_bmp_preview,
    inspect_pdf_text_layout,
    main,
    render_pdf_preview_png,
    review_drawing_layout,
    review_manufacturing_drawing,
    run_review,
    save_preview,
    save_review_previews,
    set_standard_view,
    validate_hole_positions,
    write_markdown_summary,
    write_review_report,
    zoom_to_fit,
)


def _box(left, bottom, right, top, **extra):
    box = {"left": left, "bottom": bottom, "right": right, "top": top}
    box.update(extra)
    return box


def _codes(evaluation):
    return [issue["code"] for issue in evaluation["issues"]]


def _install_top_level_import_stubs(monkeypatch) -> dict:
    """@brief 注入顶层同名桩模块, 使 runpy 直接执行源文件时命中 ImportError 回退。"""
    stubs = {
        name: types.ModuleType(name)
        for name in ("sw_connect", "sw_drawing", "sw_preflight", "sw_assembly")
    }
    stubs["sw_connect"].connect_solidworks = lambda version=None: (None, None)
    stubs["sw_connect"].get_com_member = sw_connect.get_com_member
    stubs["sw_connect"].open_document = sw_connect.open_document
    stubs["sw_connect"].classify_sw_file_load_errors = sw_connect.classify_sw_file_load_errors
    stubs["sw_connect"].SolidWorksDocumentOpenError = SolidWorksDocumentOpenError
    stubs["sw_preflight"].import_com_dependencies = sw_preflight.import_com_dependencies
    stubs["sw_drawing"].inspect_drawing_structure = lambda *args, **kwargs: {}
    stubs["sw_assembly"].get_interference_detection = lambda model: {}
    for name, stub in stubs.items():
        monkeypatch.setitem(sys.modules, name, stub)
    return stubs


# ---------------------------------------------------------------------------
# __main__ 入口 + 顶层导入回退
# ---------------------------------------------------------------------------

def test_script_entry_uses_fallback_import_and_exits_2(monkeypatch, tmp_path, capsys):
    """@brief runpy 以 __main__ 执行源文件: 导入回退生效且无文档时退出码为 2。"""
    _install_top_level_import_stubs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["sw_review.py", "--output-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(Path(sw_review.__file__).resolve()), run_name="__main__")
    assert excinfo.value.code == 2
    assert "没有可审查" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _import_pdf_parser 分支
# ---------------------------------------------------------------------------

def test_import_pdf_parser_falls_back_to_fitz(monkeypatch):
    """@brief pymupdf 不可导入时回退 fitz。"""
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    stub_fitz = types.ModuleType("fitz")
    monkeypatch.setitem(sys.modules, "fitz", stub_fitz)
    assert _import_pdf_parser() is stub_fitz


def test_import_pdf_parser_raises_without_candidates(monkeypatch):
    """@brief 两个解析器都缺失且未配置目录时抛出原始 ImportError。"""
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.delenv("CADSTUDIO_PYMUPDF_PATH", raising=False)
    with pytest.raises(ImportError):
        _import_pdf_parser()


def test_import_pdf_parser_configured_dir_not_found(monkeypatch, tmp_path):
    """@brief 配置目录不存在时跳过 sys.path 注入并抛出原始错误。"""
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.setenv("CADSTUDIO_PYMUPDF_PATH", str(tmp_path / "missing-dir"))
    with pytest.raises(ImportError):
        _import_pdf_parser()


def test_import_pdf_parser_skips_duplicate_sys_path_entry(monkeypatch, tmp_path):
    """@brief 配置目录已在 sys.path 中时不得重复插入。"""
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.setenv("CADSTUDIO_PYMUPDF_PATH", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ImportError):
        _import_pdf_parser()
    assert sys.path.count(str(tmp_path)) == 1


def test_import_pdf_parser_prepends_configured_dir(monkeypatch, tmp_path):
    """@brief fitz 最初导入失败时, 配置目录被插入 sys.path 并从中成功导入。"""
    monkeypatch.setitem(sys.modules, "pymupdf", None)   # 真 fitz 会转 import pymupdf, 一并失败
    candidate_dir = tmp_path / "pdfsdk"
    candidate_dir.mkdir()
    (candidate_dir / "fitz.py").write_text('MARKER = "configured-fitz"\n', encoding="utf-8")
    monkeypatch.setenv("CADSTUDIO_PYMUPDF_PATH", str(candidate_dir))
    try:
        module = _import_pdf_parser()
        assert module.MARKER == "configured-fitz"
        assert str(candidate_dir) in sys.path
    finally:
        if str(candidate_dir) in sys.path:
            sys.path.remove(str(candidate_dir))
        sys.modules.pop("fitz", None)


# ---------------------------------------------------------------------------
# _drawing_boxes_overlap 边界
# ---------------------------------------------------------------------------

def test_drawing_boxes_overlap_rejects_invalid_boxes():
    """@brief 缺失或字段不全的包围盒一律视为无碰撞。"""
    full = _box(0.0, 0.0, 1.0, 1.0)
    assert _drawing_boxes_overlap(None, full) is False
    assert _drawing_boxes_overlap(full, None) is False
    assert _drawing_boxes_overlap({"left": 0.0, "bottom": 0.0, "right": 1.0}, full) is False
    assert _drawing_boxes_overlap(_box(0.0, 0.0, 1.0, 1.0), _box(2.0, 2.0, 3.0, 3.0)) is False
    assert _drawing_boxes_overlap(_box(0.0, 0.0, 1.0, 1.0), _box(0.5, 0.5, 2.0, 2.0)) is True


# ---------------------------------------------------------------------------
# review_drawing_layout
# ---------------------------------------------------------------------------

def test_review_drawing_layout_detects_view_and_title_collisions():
    """@brief 视图互撞与视图侵入标题栏必须产出确认级碰撞。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.10, 0.10, 0.30, 0.30)},
            {"name": "Top", "box": _box(0.20, 0.20, 0.40, 0.40)},
            {"name": "NoBox"},
            {"name": "Partial", "box": {"left": 0.0, "bottom": 0.0, "right": 0.1}},
        ],
        "dimensions": [],
        "notes": [],
        "title_block": {"box": _box(0.25, 0.15, 0.45, 0.25)},
    }
    result = review_drawing_layout(structure)
    codes = {finding["code"] for finding in result["findings"]}
    assert "DRAWING_VIEW_OVERLAP" in codes
    assert "DRAWING_VIEW_TITLE_BLOCK_INTRUSION" in codes
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_LAYOUT_COLLISION_DETECTED"
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["drawing-view-boxes"]["status"] == "warning"


def test_review_drawing_layout_dimension_collisions():
    """@brief 尺寸互撞/侵入标题栏/侵入其它视图按证据来源定级。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [
            {"name": "D1", "box": _box(0.10, 0.35, 0.14, 0.40), "view": "Front"},
            {"name": "D2", "box": _box(0.12, 0.37, 0.16, 0.42), "view": "Front",
             "box_source": "estimated", "box_confidence": "low"},
            {"name": "D3", "box": _box(0.11, 0.36, 0.15, 0.41), "view": "Front"},
            {"name": "D4", "box": _box(0.10, 0.10, 0.14, 0.15), "view": "Right"},
            {"name": "D5", "box": _box(0.06, 0.06, 0.09, 0.09), "view": "Top"},
            {"name": "D6", "box": _box(0.75, 0.05, 0.80, 0.10),
             "box_source": "estimated", "box_confidence": "unavailable"},
            {"name": "D7", "box": _box(0.72, 0.06, 0.78, 0.12)},
        ],
        "notes": [],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }
    result = review_drawing_layout(structure)
    findings = result["findings"]
    codes = {finding["code"] for finding in findings}
    assert {"DRAWING_DIMENSION_TEXT_OVERLAP",
            "DRAWING_DIMENSION_TITLE_BLOCK_INTRUSION",
            "DRAWING_DIMENSION_OTHER_VIEW_INTRUSION"} <= codes
    native_pairs = [f for f in findings
                    if f["code"] == "DRAWING_DIMENSION_TEXT_OVERLAP"
                    and f["evidence_source"] == "native"]
    assert native_pairs and native_pairs[0]["severity"] == "fail"
    estimated_pairs = [f for f in findings
                       if f["code"] == "DRAWING_DIMENSION_TEXT_OVERLAP"
                       and f["evidence_source"] == "estimated"]
    assert estimated_pairs and {f["confidence"] for f in estimated_pairs} == {"low", "unavailable"}
    title_findings = [f for f in findings
                      if f["code"] == "DRAWING_DIMENSION_TITLE_BLOCK_INTRUSION"]
    assert {f["severity"] for f in title_findings} == {"warning", "fail"}
    assert result["error_code"] == "DRAWING_LAYOUT_COLLISION_DETECTED"


def test_review_drawing_layout_pass_with_complete_evidence():
    """@brief 证据完整且无碰撞时通过, 并给出像素预览可用信息。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [
            {"name": "D1", "box": _box(0.08, 0.35, 0.12, 0.40), "view": "Front"},
        ],
        "notes": [],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }
    result = review_drawing_layout(
        structure, preview_evidence=[{"exists": True, "likely_blank": False}]
    )
    assert result["status"] == "pass"
    assert result["error_code"] is None
    assert result["manual_review_required"] is False
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["drawing-dimension-boxes"]["message"] == "尺寸文字边界完整（SolidWorks 原生或最终 PDF 矢量文字）"
    assert checks["drawing-dimension-estimate-provenance"]["message"] == "没有使用估算尺寸边界"
    assert checks["drawing-layout-collisions"]["message"] == "未发现结构化布局碰撞风险"
    assert checks["drawing-pixel-preview"]["status"] == "pass"
    summary = result["evidence_summary"]
    assert summary["dimension_count"] == 1
    assert summary["native_dimension_box_count"] == 1
    assert summary["pixel_preview_available"] is True


def test_review_drawing_layout_without_dimensions_reports_message():
    """@brief 未读取到尺寸实体时给出对应检查消息。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [],
        "notes": [],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }
    result = review_drawing_layout(structure)
    assert result["status"] == "pass"
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["drawing-dimension-boxes"]["message"] == "没有读取到尺寸实体"


def test_review_drawing_layout_estimated_evidence_requires_visual_review():
    """@brief 仅存在估算尺寸边界(无碰撞)时返回 review_required。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [
            {"name": "E1", "box": _box(0.06, 0.32, 0.10, 0.36), "view": "Front",
             "box_source": "estimated", "box_confidence": "low"},
            {"name": "E2", "box": _box(0.40, 0.32, 0.44, 0.36), "view": "Right",
             "box_source": "estimated"},
        ],
        "notes": [],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }
    result = review_drawing_layout(structure)
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_LAYOUT_ESTIMATED_EVIDENCE_REQUIRES_VISUAL_REVIEW"
    assert result["retryable"] is True
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["drawing-dimension-estimate-provenance"]["status"] == "warning"
    assert checks["drawing-dimension-boxes"]["status"] == "warning"
    assert "保守估算 2/2" in checks["drawing-dimension-boxes"]["message"]


def test_review_drawing_layout_estimated_collision_risk_only():
    """@brief 只有估算证据碰撞(无原生确认)时返回估算风险错误码。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [],
        "notes": [
            {"text": "估算注释", "box": _box(0.08, 0.34, 0.13, 0.39), "box_source": "estimated"},
        ],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }
    result = review_drawing_layout(structure)
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_LAYOUT_ESTIMATED_COLLISION_RISK"
    assert result["findings"]
    assert all(not finding["confirmed_collision"] for finding in result["findings"])


def test_review_drawing_layout_incomplete_evidence_blocks_confidence():
    """@brief 注释缺边界或标题栏缺失时返回证据不完整。"""
    structure = {
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [
            {"name": "D1", "box": _box(0.08, 0.35, 0.12, 0.40), "view": "Front"},
        ],
        "notes": [{"text": "缺边界注释"}],
    }
    result = review_drawing_layout(structure)
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_LAYOUT_EVIDENCE_INCOMPLETE"
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["drawing-title-block-box"]["status"] == "warning"
    assert checks["drawing-note-boxes"]["status"] == "warning"
    assert checks["drawing-pixel-preview"]["status"] == "warning"


def test_review_drawing_layout_note_collisions():
    """@brief 注释四类碰撞、视图标签豁免与同页约束全部生效。"""
    structure = {
        "views": [
            {"name": "Front", "type": 1, "sheet": "S1", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Section", "type": 2, "box": _box(0.35, 0.30, 0.55, 0.50)},
            {"name": "Detail", "type": 3, "box": _box(0.35, 0.05, 0.55, 0.25)},
        ],
        "dimensions": [
            {"name": "DIM1", "box": _box(0.06, 0.32, 0.10, 0.36), "view": "Front"},
        ],
        "notes": [
            {"text": "技术要求", "box": _box(0.60, 0.30, 0.66, 0.34)},
            {"text": "检验", "box": _box(0.62, 0.31, 0.68, 0.35)},
            {"text": "表面粗糙度", "box": _box(0.07, 0.33, 0.12, 0.38)},
            {"text": "毛坯", "box": _box(0.08, 0.34, 0.13, 0.39), "box_source": "estimated"},
            {"text": "剖A", "note_kind": "view_label", "owner_view": "Section",
             "box": _box(0.46, 0.40, 0.50, 0.44)},
            {"text": "剖B", "note_kind": "view_label", "owner_view": "",
             "box": _box(0.36, 0.06, 0.41, 0.11)},
            {"text": "", "box": _box(0.36, 0.31, 0.41, 0.36)},
            {"text": "", "box": _box(0.36, 0.24, 0.41, 0.31)},
            {"text": "", "box": _box(0.06, 0.42, 0.11, 0.47)},
            {"text": "异页注释", "sheet": "S9", "box": _box(0.13, 0.43, 0.18, 0.48)},
            {"text": "标题栏注记", "box": _box(0.72, 0.05, 0.78, 0.10), "box_source": "estimated"},
            {"text": "右下注记", "box": _box(0.73, 0.07, 0.79, 0.12)},
            {"text": "PDF 注释", "box": _box(0.15, 0.36, 0.20, 0.41), "box_source": "pdf_vector_text"},
            {"text": "PDF 尺寸注记", "box": _box(0.065, 0.325, 0.115, 0.375),
             "box_source": "pdf_vector_text"},
        ],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }
    result = review_drawing_layout(structure)
    findings = result["findings"]
    codes = {finding["code"] for finding in findings}
    assert {"DRAWING_NOTE_TITLE_BLOCK_INTRUSION",
            "DRAWING_NOTE_VIEW_INTRUSION",
            "DRAWING_NOTE_DIMENSION_OVERLAP",
            "DRAWING_NOTE_TEXT_OVERLAP"} <= codes
    # 视图标签归属于 Section, 不得与所属视图判成侵入
    assert not any(finding["first"]["name"] == "剖A" for finding in findings)
    # 空文本注释与两个特殊视图同时相交时不豁免; 单一特殊视图相交时也不豁免
    section_hits = [f for f in findings
                    if f["code"] == "DRAWING_NOTE_VIEW_INTRUSION"
                    and f["second"]["name"] == "Section" and f["first"]["name"] == ""]
    assert len(section_hits) == 1
    assert any(f["second"]["name"] == "Detail" and f["first"]["name"] == "" for f in findings)
    assert any(f["second"]["name"] == "Front" and f["first"]["name"] == "" for f in findings)
    # 异页注释与 Front 视图不相交判定
    assert not any(f["first"]["name"] == "异页注释" for f in findings)
    # 证据来源与置信度
    assert any(f["code"] == "DRAWING_NOTE_VIEW_INTRUSION"
               and f["evidence_source"] == "pdf_vector_text"
               and f["severity"] == "warning" for f in findings)
    assert any(f["code"] == "DRAWING_NOTE_TITLE_BLOCK_INTRUSION"
               and f["evidence_source"] == "estimated"
               and f["severity"] == "warning" for f in findings)
    assert any(f["code"] == "DRAWING_NOTE_TITLE_BLOCK_INTRUSION"
               and f["evidence_source"] == "native"
               and f["severity"] == "fail" for f in findings)
    assert result["error_code"] == "DRAWING_LAYOUT_COLLISION_DETECTED"


# ---------------------------------------------------------------------------
# _read_bmp_size / inspect_bmp_preview
# ---------------------------------------------------------------------------

def _bmp_bytes(width, height, bpp=24, top_down=False, dark_rows=()):
    """@brief 构造最小 BI_RGB BMP: 白底 + 指定行置黑, 支持正负高度。"""
    channels = bpp // 8
    stride = ((width * bpp + 31) // 32) * 4
    rows = []
    for row in range(height):
        fill = b"\x00" if row in dark_rows else b"\xff"
        rows.append(fill * (width * channels) + b"\x00" * (stride - width * channels))
    if top_down:
        pixel_rows = rows
        height_field = -height
    else:
        pixel_rows = list(reversed(rows))
        height_field = height
    pixels = b"".join(pixel_rows)
    header = bytearray()
    header.extend(b"BM")
    header.extend((54 + len(pixels)).to_bytes(4, "little"))
    header.extend(b"\x00\x00\x00\x00")
    header.extend((54).to_bytes(4, "little"))
    header.extend((40).to_bytes(4, "little"))
    header.extend(width.to_bytes(4, "little", signed=True))
    header.extend(height_field.to_bytes(4, "little", signed=True))
    header.extend((1).to_bytes(2, "little"))
    header.extend(bpp.to_bytes(2, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend(len(pixels).to_bytes(4, "little"))
    header.extend(b"\x00" * 16)
    return bytes(header) + pixels


def test_read_bmp_size_rejects_bad_headers(tmp_path):
    """@brief 头部过短或非 BM 文件返回 (None, None)。"""
    short = tmp_path / "short.bmp"
    short.write_bytes(b"BMxxxxx")
    assert _read_bmp_size(short) == (None, None)
    assert _read_bmp_size(tmp_path / "ghost.bmp") == (None, None)


def test_inspect_bmp_preview_reports_missing_and_empty_files(tmp_path):
    """@brief 文件不存在或为空时直接返回未通过信息。"""
    missing = inspect_bmp_preview(tmp_path / "ghost.bmp")
    assert missing["exists"] is False
    assert missing["size_bytes"] == 0
    assert missing["likely_blank"] is True

    empty = tmp_path / "empty.bmp"
    empty.write_bytes(b"")
    report = inspect_bmp_preview(empty)
    assert report["exists"] is True
    assert report["size_bytes"] == 0
    assert report["likely_blank"] is True


def test_inspect_bmp_preview_garbage_bytes_use_sample_fallback(tmp_path):
    """@brief 非 BMP 内容无法建立像素网格时回退到字节采样判空。"""
    garbage = tmp_path / "garbage.bmp"
    garbage.write_bytes(b"M" * 1000)
    report = inspect_bmp_preview(garbage)
    assert report["width"] is None
    assert report["height"] is None
    assert report["unique_sample_values"] == 1
    assert report["likely_blank"] is True


def test_inspect_bmp_preview_tiny_file_falls_back_to_whole_data(tmp_path):
    """@brief 不足 14 字节的文件走默认偏移并采样全量数据。"""
    tiny = tmp_path / "tiny.bmp"
    tiny.write_bytes(b"BMxxxxxx")
    report = inspect_bmp_preview(tiny)
    assert report["width"] is None
    assert report["unique_sample_values"] == len(set(b"BMxxxxxx"))
    assert report["likely_blank"] is True


def test_inspect_bmp_preview_top_down_pixels(tmp_path):
    """@brief 负高度(自顶向下)BMP 的暗像素仍应被识别。"""
    target = tmp_path / "topdown.bmp"
    target.write_bytes(_bmp_bytes(2, 2, top_down=True, dark_rows={1}))
    report = inspect_bmp_preview(target)
    assert report["likely_blank"] is False
    assert report["dark_pixel_ratio"] > 0
    assert report["width"] == 2 and report["height"] == 2


def test_inspect_bmp_preview_skips_pixels_beyond_truncated_data(tmp_path):
    """@brief 声明尺寸大于实际像素数据时逐像素跳过而不是崩溃。"""
    target = tmp_path / "truncated.bmp"
    target.write_bytes(_bmp_bytes(32, 32)[:54 + 100])
    report = inspect_bmp_preview(target)
    assert report["width"] == 32 and report["height"] == 32
    assert report["likely_blank"] is True   # 只采到首行白像素


def test_inspect_bmp_preview_reports_read_errors(tmp_path, monkeypatch):
    """@brief 读取阶段抛错时把错误信息写入报告而不是崩溃。"""
    monkeypatch.setattr(
        sw_review, "_file_info",
        lambda path: {"path": str(path), "exists": True, "size_bytes": 123},
    )
    report = inspect_bmp_preview(tmp_path / "ghost.bmp")
    assert report["error"]
    assert report["width"] is None and report["height"] is None


# ---------------------------------------------------------------------------
# 预览导出与视图控制
# ---------------------------------------------------------------------------

class _SelectionManager:
    def __init__(self, count=0, raises=False):
        self._count = count
        self._raises = raises

    def GetSelectedObjectCount2(self, mark):
        assert mark == -1
        if self._raises:
            raise RuntimeError("count boom")
        return self._count


class _PreviewModel:
    """@brief 支持 zoom/clear/view/SaveBMP 的假 IModelDoc2。"""

    def __init__(self, selection_manager=None, save_ok=True, save_writes=True):
        self.SelectionManager = selection_manager
        self._save_ok = save_ok
        self._save_writes = save_writes
        self.cleared = []
        self.named_views = []
        self.save_calls = []
        self.zoomed = False
        self.redrawn = False

    def GetTitle(self):
        return "preview.SLDPRT"

    def ViewZoomtofit2(self):
        self.zoomed = True

    def GraphicsRedraw2(self):
        self.redrawn = True

    def ClearSelection2(self, flag):
        self.cleared.append(flag)

    def ShowNamedView2(self, name, view_id):
        self.named_views.append((name, view_id))

    def SaveBMP(self, path, width, height):
        self.save_calls.append((path, width, height))
        if self._save_writes:
            Path(path).write_bytes(_bmp_bytes(2, 2, dark_rows={1}))
        return self._save_ok


def _raise_no_app(name):
    raise RuntimeError("no active app")


def test_zoom_to_fit_calls_members():
    model = _PreviewModel()
    zoom_to_fit(model)
    assert model.zoomed and model.redrawn


def test_clear_selection_without_manager_skips_count():
    model = _PreviewModel(selection_manager=None)
    clear_selection_for_preview(model)
    assert model.cleared == [True]
    assert model.redrawn


def test_clear_selection_zero_count_keeps_single_clear():
    model = _PreviewModel(selection_manager=_SelectionManager(0))
    clear_selection_for_preview(model)
    assert model.cleared == [True]
    assert model.redrawn


def test_clear_selection_with_selection_clears_again():
    model = _PreviewModel(selection_manager=_SelectionManager(3))
    clear_selection_for_preview(model)
    assert model.cleared == [True, True]
    assert model.redrawn


def test_clear_selection_count_error_is_swallowed():
    model = _PreviewModel(selection_manager=_SelectionManager(raises=True))
    clear_selection_for_preview(model)
    assert model.cleared == [True]
    assert model.redrawn


def test_set_standard_view_resolves_named_ids():
    model = _PreviewModel()
    set_standard_view(model, "front")
    set_standard_view(model, "Isometric")
    assert model.named_views == [("", 1), ("", 7)]
    assert model.zoomed


def test_set_standard_view_falls_back_to_view_name():
    model = _PreviewModel()
    set_standard_view(model, "My Custom View")
    assert model.named_views == [("My Custom View", -1)]


def test_activate_model_handles_missing_inputs(monkeypatch):
    """@brief 无模型/空标题/会话不可得时一律返回 False。"""
    assert activate_model_for_preview(None) is False

    class _EmptyTitle:
        def GetTitle(self):
            return ""

    assert activate_model_for_preview(_EmptyTitle()) is False

    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))

    class _Titled:
        def GetTitle(self):
            return "doc.SLDPRT"

    assert activate_model_for_preview(_Titled()) is False


class _ActivateApp:
    def __init__(self, doc3=None, doc2=None, active=None,
                 raise_doc3=False, raise_doc2=False):
        self._doc3 = doc3
        self._doc2 = doc2
        self._active = active
        self._raise_doc3 = raise_doc3
        self._raise_doc2 = raise_doc2

    def ActivateDoc3(self, title, silent, option, errors):
        if self._raise_doc3:
            raise RuntimeError("doc3 boom")
        return self._doc3

    def ActivateDoc2(self, title, silent, errors):
        if self._raise_doc2:
            raise RuntimeError("doc2 boom")
        return self._doc2

    @property
    def ActiveDoc(self):
        return self._active


def _titled_model():
    return SimpleNamespace(GetTitle=lambda: "doc.SLDPRT")


def test_activate_model_success_via_activate_doc3(monkeypatch):
    model = _titled_model()
    app = _ActivateApp(doc3=model)
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=lambda name: app))
    assert activate_model_for_preview(model) is True


def test_activate_model_falls_back_to_activate_doc2(monkeypatch):
    model = _titled_model()
    app = _ActivateApp(raise_doc3=True, doc2=None, active=model)
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=lambda name: app))
    assert activate_model_for_preview(model) is True


def test_activate_model_returns_false_when_both_fail(monkeypatch):
    model = _titled_model()
    app = _ActivateApp(raise_doc3=True, raise_doc2=True)
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=lambda name: app))
    assert activate_model_for_preview(model) is False


def test_save_preview_writes_bmp_and_converts_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    model = _PreviewModel()
    target = save_preview(model, tmp_path / "shot.png", view_name="top", width=800, height=600)
    expected = tmp_path / "shot.bmp"
    assert target == str(expected.resolve())
    assert expected.exists() and expected.stat().st_size > 0
    assert model.save_calls == [(str(expected.resolve()), 800, 600)]
    assert model.named_views == [("", 5)]


def test_save_preview_raises_when_savebmp_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    model = _PreviewModel(save_ok=False)
    with pytest.raises(RuntimeError, match="预览图导出失败"):
        save_preview(model, tmp_path / "shot.bmp")


def test_save_preview_raises_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    model = _PreviewModel(save_ok=True, save_writes=False)
    with pytest.raises(RuntimeError, match="预览图导出失败"):
        save_preview(model, tmp_path / "shot.bmp")


def test_save_review_previews_exports_default_views(tmp_path, monkeypatch):
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    model = _PreviewModel()
    paths = save_review_previews(model, tmp_path / "previews", basename="w1")
    assert [Path(path).name for path in paths] == [
        "w1_isometric.bmp", "w1_front.bmp", "w1_top.bmp", "w1_right.bmp"
    ]
    assert all(Path(path).exists() for path in paths)


# ---------------------------------------------------------------------------
# collect_model_summary / collect_geometry_measurements
# ---------------------------------------------------------------------------

def test_collect_model_summary_tolerates_error_code_failures():
    """@brief GetErrorCode 抛错的系统特征按无错误证据处理。"""

    class _SystemFeature:
        Name = "Annotations"

        def GetTypeName2(self):
            return "AnnotationFolder"

        def GetErrorCode(self):
            raise RuntimeError("member not supported")

        def GetNextFeature(self):
            return None

    class _Model:
        def GetTitle(self):
            return "t"

        def GetPathName(self):
            return "p"

        def GetType(self):
            return 1

        def FirstFeature(self):
            return _SystemFeature()

    summary = collect_model_summary(_Model())
    assert summary["features"][0]["error_code"] is None
    assert summary["faulty_features"] == []
    assert "feature_error" not in summary


def test_collect_model_summary_reports_chain_failure():
    """@brief 特征链遍历失败时记录 feature_error。"""

    class _BrokenFeature:
        Name = "F1"

        def GetTypeName2(self):
            return "T"

        def GetErrorCode(self):
            return 0

        def GetNextFeature(self):
            raise RuntimeError("chain boom")

    class _Model:
        def GetTitle(self):
            return "t"

        def GetPathName(self):
            return "p"

        def GetType(self):
            return 1

        def FirstFeature(self):
            return _BrokenFeature()

    summary = collect_model_summary(_Model())
    assert summary["feature_error"] == "chain boom"
    assert summary["feature_count"] == 1


def test_collect_model_summary_reports_first_feature_failure():
    """@brief FirstFeature 本身不可读时同样记录 feature_error。"""

    class _Model:
        def GetTitle(self):
            return "t"

        def GetPathName(self):
            return "p"

        def GetType(self):
            return 1

        def FirstFeature(self):
            raise RuntimeError("no feature access")

    summary = collect_model_summary(_Model())
    assert summary["feature_count"] == 0
    assert "no feature access" in summary["feature_error"]


def test_geometry_measurement_reports_short_part_box():
    """@brief GetPartBox 返回坐标不足 6 个时记录错误证据。"""

    class _Model:
        def GetType(self):
            return 1

        def GetPartBox(self, flag):
            return [0.0, 0.0, 0.0, 0.1]

        def GetBodies2(self, kind, visible):
            return []

    measurements = collect_geometry_measurements(_Model())
    assert measurements["envelope_mm"] is None
    assert any("未返回 6 个坐标值" in error for error in measurements["errors"])
    assert measurements["hole_count"] == 0


def test_geometry_measurement_reports_box_read_failure():
    """@brief GetPartBox 抛错时记录包围盒读取失败。"""

    class _Model:
        def GetType(self):
            return 1

        def GetPartBox(self, flag):
            raise RuntimeError("box boom")

        def GetBodies2(self, kind, visible):
            return []

    measurements = collect_geometry_measurements(_Model())
    assert any("包围盒读取失败" in error for error in measurements["errors"])


def test_geometry_measurement_skips_unusable_faces():
    """@brief 非圆柱面、参数不足与抛错面都被跳过并记录。"""
    flat_surface = SimpleNamespace(IsCylinder=False)
    short_surface = SimpleNamespace(
        IsCylinder=True, CylinderParams=(0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    )

    class _FaceNoSurface:
        def GetSurface(self):
            return None

    class _FaceFlat:
        def GetSurface(self):
            return flat_surface

    class _FaceShortParams:
        def GetSurface(self):
            return short_surface

    class _FaceRaising:
        def GetSurface(self):
            raise RuntimeError("surface boom")

    class _Body:
        def GetFaces(self):
            return [_FaceNoSurface(), _FaceFlat(), _FaceShortParams(), _FaceRaising()]

    class _Model:
        def GetType(self):
            return 1

        def GetPartBox(self, flag):
            return (-0.01, -0.01, -0.01, 0.01, 0.01, 0.01)

        def GetBodies2(self, kind, visible):
            return [_Body()]

    measurements = collect_geometry_measurements(_Model())
    assert measurements["cylindrical_faces"] == []
    assert measurements["holes"] == []
    assert any("圆柱面读取失败" in error for error in measurements["errors"])


def test_geometry_measurement_reports_topology_failure():
    """@brief GetBodies2 抛错时记录实体拓扑读取失败。"""

    class _Model:
        def GetType(self):
            raise RuntimeError("GetType boom")

        def GetPartBox(self, flag):
            return (-0.01, -0.01, -0.01, 0.01, 0.01, 0.01)

        def GetBodies2(self, kind, visible):
            raise RuntimeError("topology boom")

    measurements = collect_geometry_measurements(_Model())
    assert any("实体拓扑读取失败" in error for error in measurements["errors"])
    assert measurements["hole_count"] == 0
    assert "unsupported_doc_type" not in measurements


# ---------------------------------------------------------------------------
# 孔分组与孔位验收
# ---------------------------------------------------------------------------

def test_group_coaxial_skips_degenerate_segments():
    """@brief 零向量轴与原点不足三维的孔段不参与分组。"""
    segments = [
        {"diameter_mm": 10.0, "position_mm": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 0.0]},
        {"diameter_mm": 8.0, "position_mm": [0.0, 0.0]},
        {"diameter_mm": 6.0, "position_mm": [0.0, 0.0, 5.0], "axis": [0.0, 0.0, 2.0]},
    ]
    groups = group_coaxial_hole_segments(segments)
    assert len(groups) == 1
    assert groups[0]["segment_count"] == 1
    assert groups[0]["feature_kind"] == "simple"
    assert groups[0]["diameters_mm"] == [6.0]


def test_group_coaxial_merges_segments_on_shared_axis():
    """@brief 同轴不同径的孔段合并为复合孔证据。"""
    segments = [
        {"diameter_mm": 10.0, "position_mm": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0]},
        {"diameter_mm": 14.0, "position_mm": [0.0, 0.0, 3.0], "axis": [0.0, 0.0, -1.0]},
        {"diameter_mm": 10.0, "position_mm": [20.0, 0.0, 0.0], "axis": [1.0, 0.0, 0.0]},
    ]
    groups = group_coaxial_hole_segments(segments, position_tolerance_mm=0.05)
    assert len(groups) == 2
    compound = next(group for group in groups if group["segment_count"] == 2)
    assert compound["diameters_mm"] == [10.0, 14.0]
    assert compound["feature_kind"] == "compound"


def test_validate_hole_positions_skips_used_and_short_positions():
    """@brief 已占用孔段跳过、不足三维的期望位置判为未匹配。"""
    measurements = {"holes": [
        {"diameter_mm": 8.0, "position_mm": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0]},
        {"diameter_mm": 10.0, "position_mm": [100.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0]},
    ]}
    expected = [
        {"id": "H1", "diameter_mm": 8.0, "position_mm": [0.0, 0.0, 0.0]},
        {"id": "H2", "diameter_mm": 8.0, "position_mm": [1.0, 0.0, 0.0]},
        {"id": "H3", "diameter_mm": 5.0, "position_mm": [1.0, 2.0]},
    ]
    result = validate_hole_positions(measurements, expected)
    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is True
    assert result["checks"][1]["passed"] is False
    assert result["checks"][1]["actual_diameter_mm"] == 10.0
    assert result["checks"][2]["passed"] is False
    assert result["checks"][2]["actual_diameter_mm"] is None
    assert result["checks"][2]["diameter_error_mm"] is None
    assert result["unmatched_actual_count"] == 1


def test_validate_hole_positions_zero_axis_candidates_fail():
    """@brief 候选孔轴为零向量时轴线距离无穷大, 判定失败。"""
    measurements = {"holes": [
        {"diameter_mm": 8.0, "position_mm": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 0.0]},
    ]}
    result = validate_hole_positions(
        measurements, [{"id": "H1", "diameter_mm": 8.0, "position_mm": [0.0, 0.0, 0.0]}]
    )
    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False
    assert result["checks"][0]["position_error_mm"] == float("inf")


# ---------------------------------------------------------------------------
# inspect_pdf_text_layout (假 fitz)
# ---------------------------------------------------------------------------

class _FakeTextPage:
    def __init__(self, payload, width=595.0, height=842.0):
        self._payload = payload
        self.rect = SimpleNamespace(width=width, height=height)

    def get_text(self, kind):
        assert kind == "dict"
        return self._payload


class _FakeTextDocument:
    def __init__(self, pages, open_raises=False):
        self._pages = pages
        self._open_raises = open_raises

    def __iter__(self):
        if self._open_raises:
            raise RuntimeError("bad pdf")
        return iter(self._pages)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def _fake_fitz(monkeypatch, document):
    monkeypatch.setattr(
        sw_review, "_import_pdf_parser", lambda: SimpleNamespace(open=lambda path: document)
    )


def _payload(*spans):
    return {"blocks": [{"lines": [{"spans": list(spans)}]}]}


def _span(text, bbox, font="Arial", size=10.0):
    return {"text": text, "bbox": bbox, "font": font, "size": size}


def test_inspect_pdf_text_layout_rejects_invalid_files(tmp_path):
    """@brief 非 PDF 后缀或空文件直接判无效。"""
    not_pdf = tmp_path / "drawing.txt"
    not_pdf.write_bytes(b"hello")
    result = inspect_pdf_text_layout(not_pdf)
    assert result["status"] == "blocked"
    assert result["error_code"] == "DRAWING_PDF_INVALID"

    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    result = inspect_pdf_text_layout(empty_pdf)
    assert result["error_code"] == "DRAWING_PDF_INVALID"
    assert result["manual_review_required"] is True


def test_inspect_pdf_text_layout_reports_missing_parser(tmp_path, monkeypatch):
    """@brief 缺少 PyMuPDF 时返回可指导安装的错误码。"""
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        sw_review, "_import_pdf_parser",
        lambda: (_ for _ in ()).throw(ImportError("no pymupdf")),
    )
    result = inspect_pdf_text_layout(pdf)
    assert result["error_code"] == "DRAWING_PDF_TEXT_PARSER_MISSING"
    assert "requirements-pdf.txt" in result["message"]


def test_inspect_pdf_text_layout_filters_invalid_spans(tmp_path, monkeypatch):
    """@brief 空文本/边界不全/非有限坐标的 span 全部被过滤。"""
    document = _FakeTextDocument([
        _FakeTextPage(_payload(
            _span("R10 mm", [10.0, 10.0, 60.0, 30.0]),
            _span("", [10.0, 10.0, 60.0, 30.0]),
            _span("残缺", [1.0, 2.0, 3.0]),
            _span("NaN", [float("nan"), 0.0, 1.0, 1.0]),
            _span("无边界", None),
        ))
    ])
    _fake_fitz(monkeypatch, document)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = inspect_pdf_text_layout(pdf)
    assert result["text_span_count"] == 1
    assert result["numeric_text_span_count"] == 1
    assert result["pages"][0]["widthPt"] == 595.0
    assert result["pages"][0]["textSpans"][0]["dimensionCandidate"] is True
    assert result["pages"][0]["textSpans"][0]["font"] == "Arial"


def test_inspect_pdf_text_layout_enforces_span_limit(tmp_path, monkeypatch):
    """@brief span 超过安全上限时判定解析失败且可重试。"""
    document = _FakeTextDocument([
        _FakeTextPage(_payload(
            _span("R10 mm", [10.0, 10.0, 60.0, 30.0]),
            _span("R12 mm", [70.0, 10.0, 120.0, 30.0]),
        ))
    ])
    _fake_fitz(monkeypatch, document)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = inspect_pdf_text_layout(pdf, maximum_spans=1)
    assert result["error_code"] == "DRAWING_PDF_TEXT_PARSE_FAILED"
    assert result["retryable"] is True
    assert "安全上限" in result["message"]


def test_inspect_pdf_text_layout_reports_parse_exception(tmp_path, monkeypatch):
    """@brief fitz 打开/迭代抛错时进入解析失败分支。"""
    document = _FakeTextDocument([], open_raises=True)
    _fake_fitz(monkeypatch, document)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = inspect_pdf_text_layout(pdf)
    assert result["error_code"] == "DRAWING_PDF_TEXT_PARSE_FAILED"
    assert result["message"] == "bad pdf"


def test_inspect_pdf_text_layout_without_vector_text(tmp_path, monkeypatch):
    """@brief 无可提取文字时返回 VECTOR_TEXT_MISSING。"""
    document = _FakeTextDocument([_FakeTextPage({"blocks": []})])
    _fake_fitz(monkeypatch, document)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = inspect_pdf_text_layout(pdf)
    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_PDF_VECTOR_TEXT_MISSING"
    assert result["message"] == "PDF 中未提取到矢量文字。"
    assert result["overlaps"] == []


def test_inspect_pdf_text_layout_ignores_sub_threshold_overlap(tmp_path, monkeypatch):
    """@brief 交叠面积低于阈值时不计为重叠风险。"""
    document = _FakeTextDocument([
        _FakeTextPage(_payload(
            _span("R10 mm", [0.0, 0.0, 10.0, 10.0]),
            _span("R12 mm", [9.96, 0.0, 20.0, 10.0]),   # 0.04 x 10 = 0.4 pt^2
        ))
    ])
    _fake_fitz(monkeypatch, document)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = inspect_pdf_text_layout(pdf)
    assert result["status"] == "review_required"
    assert result["overlaps"] == []
    assert result["error_code"] is None
    assert result["message"] == "PDF 矢量文字边界已读取。"


# ---------------------------------------------------------------------------
# build_review_report
# ---------------------------------------------------------------------------

class _ReviewFeature:
    def __init__(self, name, ftype, error_code=0, nxt=None):
        self.Name = name
        self._ftype = ftype
        self._code = error_code
        self._nxt = nxt

    def GetTypeName2(self):
        return self._ftype

    def GetErrorCode(self):
        return self._code

    def GetNextFeature(self):
        return self._nxt


class _ReviewModel:
    """@brief 支持 build_review_report 全链路的假文档。"""

    def __init__(self, doc_type=1, rebuild=True, rebuild_raises=False):
        self._doc_type = doc_type
        self._rebuild = rebuild
        self._rebuild_raises = rebuild_raises
        self.features = [
            _ReviewFeature("Sketch1", "ProfileFeature"),
            _ReviewFeature("Boss-Extrude1", "Extrusion"),
        ]
        self.SelectionManager = None

    def GetType(self):
        return self._doc_type

    def GetTitle(self):
        return "review_model.SLDPRT"

    def GetPathName(self):
        return r"D:\fake\review_model.SLDPRT"

    def FirstFeature(self):
        return self.features[0]

    def ForceRebuild3(self, flag):
        if self._rebuild_raises:
            raise RuntimeError("rebuild boom")
        return self._rebuild

    def ViewZoomtofit2(self):
        pass

    def GraphicsRedraw2(self):
        pass

    def ClearSelection2(self, flag):
        pass

    def ShowNamedView2(self, name, view_id):
        pass

    def SaveBMP(self, path, width, height):
        Path(path).write_bytes(_bmp_bytes(2, 2, dark_rows={1}))
        return True

    def GetPartBox(self, flag):
        return (-0.05, -0.05, -0.05, 0.05, 0.05, 0.05)

    def GetBodies2(self, kind, visible):
        return []


def test_build_review_report_for_part(tmp_path, monkeypatch):
    """@brief 零件文档产出预览/几何/期望输出检查与评估结果。"""
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    step = tmp_path / "part.step"
    step.write_bytes(b"x" * 2048)
    model = _ReviewModel(doc_type=1)

    report = build_review_report(
        model, tmp_path / "out", basename="part", expected_outputs=[str(step)]
    )
    checks = report["checks"]
    assert checks["model_available"] is True
    assert checks["previews_created"] is True
    assert checks["previews_not_blank"] is True
    assert checks["expected_outputs_exist"] is True
    assert checks["rebuild_ok"] is True
    assert checks["feature_summary_available"] is True
    assert checks["geometry_measurements_available"] is True
    assert report["cad_spec"]["envelope_mm"]["length"] == 100.0
    assert "interference" not in report
    assert "drawing_structure" not in report
    assert report["evaluation"]["status"] in {"pass", "warn"}
    assert report["review_notes"]


def test_build_review_report_for_assembly(tmp_path, monkeypatch):
    """@brief 装配体自动执行干涉检查; 重建失败记录为 None。"""
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    monkeypatch.setattr(sw_review, "get_interference_detection", lambda model: {
        "status": "warn", "api": "modern",
        "interference_count": 1, "interfering_components": ["plate-1"],
    })
    model = _ReviewModel(doc_type=2, rebuild_raises=True)

    report = build_review_report(model, tmp_path / "out2", basename="asm")
    assert report["checks"]["rebuild_ok"] is None
    assert report["checks"]["interference_free"] is False
    assert report["interference"]["interference_count"] == 1
    assert report["checks"]["geometry_measurements_available"] is False


def test_build_review_report_for_drawing(tmp_path, monkeypatch):
    """@brief 工程图自动执行结构 + 布局审查, 空结构导致布局受阻。"""
    monkeypatch.setattr(sw_review, "_win32com", SimpleNamespace(GetActiveObject=_raise_no_app))
    monkeypatch.setattr(sw_review, "inspect_drawing_structure", lambda model: {
        "status": "pass", "error_code": None, "views": [], "dimensions": [], "notes": [],
    })
    model = _ReviewModel(doc_type=3)

    report = build_review_report(model, tmp_path / "out3", basename="drw")
    assert report["checks"]["drawing_views_present"] is True
    assert report["drawing_layout"]["status"] == "blocked"
    assert report["drawing_layout"]["error_code"] == "DRAWING_STANDARD_VIEWS_MISSING"
    assert "drawing_layout_blocked" in _codes(report["evaluation"])


# ---------------------------------------------------------------------------
# evaluate_review_report 补充分支
# ---------------------------------------------------------------------------

def _eval_report(**overrides):
    report = {
        "model": {"type": 1, "faulty_features": []},
        "cad_spec": {"envelope_mm": {"length": 10.0, "width": 10.0, "height": 10.0}},
        "previews": [],
        "expected_outputs": [],
        "checks": {
            "model_available": True,
            "previews_created": True,
            "previews_not_blank": True,
            "expected_outputs_exist": True,
            "feature_summary_available": True,
            "geometry_measurements_available": True,
            "geometry_measurements_error_free": True,
            "rebuild_ok": True,
            "feature_errors_absent": True,
        },
    }
    report.update(overrides)
    return report


def _good_preview(path="a.bmp"):
    return {"path": path, "exists": True, "size_bytes": 20000, "width": 1600, "height": 1000}


def test_evaluate_review_report_full_pass_with_two_previews():
    """@brief 全部检查通过且预览充足时评分为 pass/100。"""
    report = _eval_report(previews=[_good_preview("a.bmp"), _good_preview("b.bmp")])
    evaluation = evaluate_review_report(report)
    assert evaluation["status"] == "pass"
    assert evaluation["score"] == 100
    assert evaluation["issues"] == []
    assert evaluation["manual_review_required"] is True


def test_evaluate_review_report_flags_small_and_lowres_previews():
    """@brief 过小与低分辨率预览分别告警; 缺失文件不告警。"""
    report = _eval_report(previews=[
        {"path": "tiny.bmp", "exists": True, "size_bytes": 500, "width": 320, "height": 240},
        {"path": "gone.bmp", "exists": False, "size_bytes": 0, "width": None, "height": None},
    ])
    codes = _codes(evaluate_review_report(report))
    assert "preview_file_too_small" in codes
    assert "preview_resolution_low" in codes
    assert "too_few_previews" not in codes


def test_evaluate_review_report_flags_small_output_files():
    """@brief 过小的交付文件告警; 不存在的文件跳过体积检查。"""
    report = _eval_report(expected_outputs=[
        {"path": "part.step", "exists": True, "size_bytes": 100},
        {"path": "gone.step", "exists": False, "size_bytes": 0},
    ])
    codes = _codes(evaluate_review_report(report))
    assert "output_file_too_small" in codes


def test_evaluate_review_report_missing_model_and_previews():
    """@brief 模型缺失与预览失败都是硬失败。"""
    report = _eval_report()
    report["checks"]["model_available"] = False
    report["checks"]["previews_created"] = False
    codes = _codes(evaluate_review_report(report))
    assert "model_missing" in codes
    assert "previews_missing" in codes


def test_evaluate_review_report_flags_blank_previews():
    """@brief 预览生成但疑似空白时硬失败。"""
    report = _eval_report()
    report["checks"]["previews_not_blank"] = False
    codes = _codes(evaluate_review_report(report))
    assert "previews_blank" in codes


def test_evaluate_review_report_expected_outputs_missing():
    """@brief 期望输出缺失判失败, 且不再触发未声明告警。"""
    report = _eval_report()
    report["checks"]["expected_outputs_exist"] = False
    codes = _codes(evaluate_review_report(report))
    assert "expected_outputs_missing" in codes
    assert "expected_outputs_not_declared" not in codes


def test_evaluate_review_report_feature_summary_unavailable():
    """@brief 特征树摘要不可读时告警。"""
    report = _eval_report()
    report["checks"]["feature_summary_available"] = False
    codes = _codes(evaluate_review_report(report))
    assert "feature_summary_unavailable" in codes


def test_evaluate_review_report_tolerates_non_numeric_envelope():
    """@brief 包络字段非数值时忽略退化检查而不是崩溃。"""
    report = _eval_report()
    report["cad_spec"]["envelope_mm"] = {"length": "abc", "width": 1.0, "height": 1.0}
    codes = _codes(evaluate_review_report(report))
    assert "degenerate_envelope" not in codes


def test_evaluate_review_report_zero_interference_is_ignored():
    """@brief 干涉计数为 0 时不产生告警。"""
    report = _eval_report()
    report["model"]["type"] = 2
    report["interference"] = {"status": "warn", "interference_count": 0}
    codes = _codes(evaluate_review_report(report))
    assert "interference_detected" not in codes


def test_evaluate_review_report_drawing_layout_blocked_and_pass():
    """@brief 布局受阻告警; 布局通过不产生告警。"""
    blocked = _eval_report()
    blocked["drawing_layout"] = {"status": "blocked", "error_code": "DRAWING_STANDARD_VIEWS_MISSING"}
    assert "drawing_layout_blocked" in _codes(evaluate_review_report(blocked))

    passed = _eval_report()
    passed["drawing_layout"] = {"status": "pass"}
    assert "drawing_layout_blocked" not in _codes(evaluate_review_report(passed))
    assert "drawing_layout_review_required" not in _codes(evaluate_review_report(passed))


# ---------------------------------------------------------------------------
# 报告落盘链路
# ---------------------------------------------------------------------------

def test_write_review_report_roundtrip(tmp_path):
    """@brief 报告 JSON 落盘并返回路径。"""
    report = {"checks": {"a": True}, "evaluation": {"status": "pass"}}
    path = write_review_report(report, tmp_path / "nested" / "report.json")
    assert Path(path).exists()
    assert json.loads(Path(path).read_text(encoding="utf-8"))["checks"] == {"a": True}


def test_write_review_report_rejects_parent_escape(tmp_path, monkeypatch):
    """@brief 含 '..' 的报告路径必须被拒绝。

    注: 正常路径经 _expand_path 的 resolve() 会消解 '..', 因此这里注入
    不做 resolve 的展开函数以模拟穿越输入。
    """
    monkeypatch.setattr(sw_review, "_expand_path", lambda path: Path(str(path)))
    with pytest.raises(ValueError, match=r"\.\."):
        write_review_report({}, tmp_path / ".." / "escape.json")


def test_write_markdown_summary_renders_sections(tmp_path):
    """@brief 摘要包含状态/检查/问题/建议/预览且建议去重。"""
    report = {
        "evaluation": {
            "status": "warn", "score": 88, "manual_review_required": True,
            "issues": [{"severity": "warn", "code": "x", "message": "注意"}],
            "recommendations": ["先修 A", "先修 A", "再修 B"],
        },
        "checks": {"previews_created": True, "model_available": False},
        "previews": [{"path": "p1.bmp"}, {"path": "p2.bmp"}],
    }
    path = write_markdown_summary(report, tmp_path / "summary.md")
    text = Path(path).read_text(encoding="utf-8")
    assert "`warn`" in text and "`88`" in text
    assert "`previews_created`: `True`" in text
    assert "`warn` `x`: 注意" in text
    assert text.count("先修 A") == 2
    assert "- `p1.bmp`" in text


def test_write_markdown_summary_empty_sections(tmp_path):
    """@brief 无问题/无建议时输出占位文案。"""
    report = {"evaluation": {}, "checks": {}, "previews": []}
    path = write_markdown_summary(report, tmp_path / "empty.md")
    text = Path(path).read_text(encoding="utf-8")
    assert "`unknown`" in text
    assert "No rule-based issues." in text
    assert "Inspect generated previews" in text


def test_write_markdown_summary_rejects_parent_escape(tmp_path, monkeypatch):
    """@brief 含 '..' 的摘要路径必须被拒绝。"""
    monkeypatch.setattr(sw_review, "_expand_path", lambda path: Path(str(path)))
    with pytest.raises(ValueError, match=r"\.\."):
        write_markdown_summary({}, tmp_path / ".." / "escape.md")


def test_run_review_writes_report_and_summary(tmp_path, monkeypatch):
    """@brief 一站式审查净化 basename 并写回 summary_path。"""
    captured = {}

    def fake_build(model, output_dir, basename="review", views=None, expected_outputs=None):
        captured["output_dir"] = output_dir
        captured["basename"] = basename
        captured["views"] = views
        return {
            "checks": {}, "previews": [], "expected_outputs": [],
            "evaluation": {"status": "pass", "score": 100, "issues": [], "recommendations": []},
        }

    monkeypatch.setattr(sw_review, "build_review_report", fake_build)
    report, report_path = run_review(
        object(), tmp_path, basename="../evil/../part", views=("isometric",)
    )
    assert captured["basename"] == "part"
    assert captured["output_dir"] == tmp_path.resolve()
    assert captured["views"] == ("isometric",)
    assert Path(report_path).name == "part_review_report.json"
    assert Path(report_path).exists()
    assert Path(report["summary_path"]).name == "part_review_summary.md"
    assert Path(report["summary_path"]).exists()


# ---------------------------------------------------------------------------
# CLI: _parse_args / main
# ---------------------------------------------------------------------------

def test_parse_args_defaults_and_options(monkeypatch):
    """@brief 命令行参数的默认值与覆盖值。"""
    monkeypatch.setattr(sys, "argv", [
        "sw_review.py", "--output-dir", "out", "--file", "p.SLDPRT",
        "--basename", "w1", "--views", " isometric , front , ",
        "--expected", "a.step", "--expected", "b.step",
        "--version", "2024", "--silent-open", "--fail-on-warn",
    ])
    args = _parse_args()
    assert args.output_dir == "out"
    assert args.file == "p.SLDPRT"
    assert args.views == " isometric , front , "
    assert args.expected == ["a.step", "b.step"]
    assert args.version == 2024
    assert args.silent_open is True
    assert args.fail_on_warn is True

    monkeypatch.setattr(sys, "argv", ["sw_review.py", "--output-dir", "out"])
    args = _parse_args()
    assert args.basename == "review"
    assert args.views == "isometric,front,top,right"
    assert args.expected == []
    assert args.version is None
    assert args.silent_open is False
    assert args.fail_on_warn is False


def test_main_returns_2_when_no_active_document(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_review.py", "--output-dir", "unused"])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), None))
    assert main() == 2
    assert "没有可审查" in capsys.readouterr().out


def test_main_reports_structured_open_failure(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "sw_review.py", "--output-dir", "unused", "--file", "ghost.SLDPRT",
    ])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), object()))

    def boom(*args, **kwargs):
        raise SolidWorksDocumentOpenError("打开失败", error_code=512)

    monkeypatch.setattr(sw_review, "open_document", boom)
    assert main() == 2
    out = capsys.readouterr().out
    assert "审查失败" in out
    assert "error_code: 512" in out


def test_main_file_not_found_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "sw_review.py", "--output-dir", "unused", "--file", "ghost.SLDPRT",
    ])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), object()))

    def boom(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(sw_review, "open_document", boom)
    assert main() == 2
    assert "error_code" not in capsys.readouterr().out


def _fake_run_review(status, issues, captured=None):
    def fake(model, output_dir, basename="review", views=None, expected_outputs=None):
        if captured is not None:
            captured["views"] = views
            captured["expected_outputs"] = expected_outputs
        report = {
            "evaluation": {"status": status, "score": 90, "issues": issues},
            "summary_path": "summary.md",
        }
        return report, "report.json"

    return fake


def test_main_success_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_review.py", "--output-dir", "unused"])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), object()))
    monkeypatch.setattr(
        sw_review, "run_review", _fake_run_review("pass", [])
    )
    assert main() == 0
    out = capsys.readouterr().out
    assert "报告: report.json" in out
    assert "摘要: summary.md" in out
    assert "状态: pass / 分数: 90" in out


def test_main_parses_views_and_expected_outputs(monkeypatch):
    captured = {}
    monkeypatch.setattr(sys, "argv", [
        "sw_review.py", "--output-dir", "unused",
        "--views", " isometric , front ", "--expected", "a.step",
    ])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), object()))
    monkeypatch.setattr(
        sw_review, "run_review", _fake_run_review("pass", [], captured)
    )
    assert main() == 0
    assert captured["views"] == ["isometric", "front"]
    assert captured["expected_outputs"] == ["a.step"]


def test_main_fail_status_returns_two_with_issue_lines(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_review.py", "--output-dir", "unused"])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), object()))
    monkeypatch.setattr(sw_review, "run_review", _fake_run_review(
        "fail", [{"severity": "fail", "code": "rebuild_failed", "message": "重建失败"}]
    ))
    assert main() == 2
    out = capsys.readouterr().out
    assert "问题:" in out
    assert "- [fail] rebuild_failed: 重建失败" in out


def test_main_fail_on_warn_returns_one(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "sw_review.py", "--output-dir", "unused", "--fail-on-warn",
    ])
    monkeypatch.setattr(sw_review, "connect_solidworks", lambda version=None: (object(), object()))
    monkeypatch.setattr(sw_review, "run_review", _fake_run_review("warn", []))
    assert main() == 1


# ---------------------------------------------------------------------------
# 制造零件图三重证据审查
# ---------------------------------------------------------------------------

def test_review_worst_status_ordering():
    """@brief 状态严重度取最严重者; 未知状态按 failed 处理。"""
    assert _review_worst_status([]) == "pass"
    assert _review_worst_status(["pass"]) == "pass"
    assert _review_worst_status(["pass", "review_required"]) == "review_required"
    assert _review_worst_status(["review_required", "blocked"]) == "blocked"
    assert _review_worst_status(["blocked", "failed"]) == "failed"


class _FakePixmap:
    def __init__(self, write_file=True):
        self._write = write_file

    def save(self, target):
        if self._write:
            Path(target).write_bytes(b"png-bytes")


class _FakePage:
    def __init__(self, pixmap, raise_on_render=False):
        self._pixmap = pixmap
        self._raise = raise_on_render

    def get_pixmap(self, dpi=150):
        if self._raise:
            raise RuntimeError("render boom")
        return self._pixmap


class _FakeDocument:
    def __init__(self, page):
        self._page = page

    def __getitem__(self, index):
        if index != 0:
            raise IndexError(index)
        return self._page

    def close(self):
        pass


def test_render_pdf_preview_png_missing_source(tmp_path):
    result = render_pdf_preview_png(tmp_path / "no.pdf", tmp_path / "out.png")
    assert result["status"] == "blocked"
    assert result["error_code"] == "REVIEW_PDF_PREVIEW_SOURCE_MISSING"
    assert result["rendered"] is False


def test_render_pdf_preview_png_missing_parser(tmp_path, monkeypatch):
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        sw_review, "_import_pdf_parser",
        lambda: (_ for _ in ()).throw(ImportError("no pymupdf")),
    )
    result = render_pdf_preview_png(pdf, tmp_path / "out.png")
    assert result["status"] == "blocked"
    assert result["error_code"] == "REVIEW_PDF_PREVIEW_PARSER_MISSING"


def test_render_pdf_preview_png_success(tmp_path, monkeypatch):
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    target = tmp_path / "preview" / "drawing.png"
    document = _FakeDocument(_FakePage(_FakePixmap(write_file=True)))
    monkeypatch.setattr(
        sw_review, "_import_pdf_parser", lambda: SimpleNamespace(open=lambda path: document)
    )
    result = render_pdf_preview_png(pdf, target, dpi=300)
    assert result["status"] == "pass"
    assert result["rendered"] is True
    assert result["error_code"] is None
    assert target.exists()


def test_render_pdf_preview_png_empty_output_is_failed(tmp_path, monkeypatch):
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    target = tmp_path / "out.png"
    document = _FakeDocument(_FakePage(_FakePixmap(write_file=False)))
    monkeypatch.setattr(
        sw_review, "_import_pdf_parser", lambda: SimpleNamespace(open=lambda path: document)
    )
    result = render_pdf_preview_png(pdf, target)
    assert result["status"] == "failed"
    assert result["rendered"] is False
    assert result["error_code"] == "REVIEW_PDF_PREVIEW_RENDER_FAILED"


def test_render_pdf_preview_png_render_exception(tmp_path, monkeypatch):
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    document = _FakeDocument(_FakePage(_FakePixmap(), raise_on_render=True))
    monkeypatch.setattr(
        sw_review, "_import_pdf_parser", lambda: SimpleNamespace(open=lambda path: document)
    )
    result = render_pdf_preview_png(pdf, tmp_path / "out.png")
    assert result["status"] == "failed"
    assert result["error_code"] == "REVIEW_PDF_PREVIEW_RENDER_FAILED"
    assert result["error"] == "render boom"


def _layout_pass_structure():
    return {
        "status": "pass",
        "error_code": None,
        "views": [
            {"name": "Front", "box": _box(0.05, 0.30, 0.25, 0.50)},
            {"name": "Top", "box": _box(0.05, 0.05, 0.25, 0.25)},
            {"name": "Right", "box": _box(0.35, 0.30, 0.55, 0.50)},
        ],
        "dimensions": [],
        "notes": [],
        "title_block": {"box": _box(0.70, 0.02, 0.95, 0.20)},
    }


def test_review_manufacturing_drawing_without_evidence():
    """@brief 无任何证据时整体 blocked 且三项检查 skipped。"""
    report = review_manufacturing_drawing()
    assert report["status"] == "blocked"
    assert report["error_code"] == "REVIEW_MANUFACTURING_DRAWING_NO_EVIDENCE"
    assert report["retryable"] is True
    assert report["manual_review_required"] is True
    checks = {check["id"]: (check["status"], check["message"]) for check in report["checks"]}
    assert checks["review-structure"][0] == "skipped"
    assert checks["review-pdf-text"][0] == "skipped"
    assert checks["review-pdf-preview"][0] == "skipped"


def test_review_manufacturing_drawing_pass_downgraded_to_review_required(tmp_path, monkeypatch):
    """@brief 存在估算证据门禁哲学: 即使全部 pass 也降级为 review_required。"""
    monkeypatch.setattr(
        sw_review, "inspect_drawing_structure", lambda model, **kwargs: _layout_pass_structure()
    )
    monkeypatch.setattr(sw_review, "inspect_pdf_text_layout", lambda path: {"status": "pass"})
    monkeypatch.setattr(
        sw_review, "render_pdf_preview_png",
        lambda pdf, png, dpi=150: {"status": "pass"},
    )
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = review_manufacturing_drawing(object(), str(pdf))
    assert report["status"] == "review_required"
    assert report["error_code"] is None
    assert report["retryable"] is False
    checks = {check["id"]: check["status"] for check in report["checks"]}
    assert checks == {"review-structure": "pass", "review-pdf-text": "pass",
                      "review-pdf-preview": "pass"}


def test_review_manufacturing_drawing_collects_worst_status_and_writes(tmp_path, monkeypatch):
    """@brief 结构缺 status 判 failed; 布局 blocked; 汇总写盘且使用默认 PNG 路径。"""
    monkeypatch.setattr(
        sw_review, "inspect_drawing_structure", lambda model, **kwargs: {"error_code": None}
    )
    monkeypatch.setattr(sw_review, "inspect_pdf_text_layout", lambda path: {
        "status": "review_required", "error_code": "DRAWING_PDF_TEXT_OVERLAP_RISK",
    })
    rendered = {}

    def fake_render(pdf, png, dpi=150):
        rendered["png"] = Path(png)
        rendered["dpi"] = dpi
        return {"status": "pass"}

    monkeypatch.setattr(sw_review, "render_pdf_preview_png", fake_render)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = review_manufacturing_drawing(
        object(), str(pdf), dpi=300, report_path=tmp_path / "deep" / "review.json"
    )
    assert report["status"] == "failed"
    assert report["error_code"] == "DRAWING_STANDARD_VIEWS_MISSING"
    assert report["retryable"] is True
    assert report["report_path"] == str(tmp_path / "deep" / "review.json")
    saved = json.loads((tmp_path / "deep" / "review.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "manufacturing_drawing_review/1.0"
    assert rendered["dpi"] == 300
    assert rendered["png"] == pdf.with_suffix(".png")
    checks = {check["id"]: check["status"] for check in report["checks"]}
    assert checks["review-structure"] == "skipped"   # structure 无 status 字段


def test_review_manufacturing_drawing_custom_png_target(tmp_path, monkeypatch):
    """@brief 显式 preview_png_path 与仅工程图复核两种组合。"""
    monkeypatch.setattr(
        sw_review, "inspect_drawing_structure", lambda model, **kwargs: _layout_pass_structure()
    )
    monkeypatch.setattr(sw_review, "inspect_pdf_text_layout", lambda path: {"status": "pass"})
    rendered = {}

    def fake_render(pdf, png, dpi=150):
        rendered["png"] = Path(png)
        return {"status": "pass"}

    monkeypatch.setattr(sw_review, "render_pdf_preview_png", fake_render)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = review_manufacturing_drawing(
        object(), str(pdf), preview_png_path=tmp_path / "custom.png"
    )
    assert report["status"] == "review_required"   # pass 降级
    assert rendered["png"] == tmp_path / "custom.png"

    drawing_only = review_manufacturing_drawing(object())
    assert drawing_only["status"] == "review_required"
    checks = {check["id"]: check["status"] for check in drawing_only["checks"]}
    assert checks["review-pdf-text"] == "skipped"
    assert checks["review-pdf-preview"] == "skipped"
