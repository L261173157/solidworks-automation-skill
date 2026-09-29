"""SolidWorks 工程图兼容入口。

工程图实现已归档到 ``subskills/solidworks-engineering-drawing``。该桥接文件保留
历史 ``scripts.sw_drawing`` 导入路径，避免已有 Skill、测试和第三方调用失效。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


# ---- fork 定制: COM 导入与共享常量/辅助（原 scripts.sw_drawing 保留部分）----
try:
    from .sw_preflight import import_com_dependencies
    from .sw_connect import create_empty_dispatch_variant, get_com_member, new_document, open_document
except ImportError:
    from sw_preflight import import_com_dependencies
    from sw_connect import create_empty_dispatch_variant, get_com_member, new_document, open_document

pythoncom, _win32com, VARIANT = import_com_dependencies()

PAPER_SIZES = {
    "A4": {"code": 5, "width_m": 0.297, "height_m": 0.210},
    "A3": {"code": 6, "width_m": 0.420, "height_m": 0.297},
    "A2": {"code": 7, "width_m": 0.594, "height_m": 0.420},
    "A1": {"code": 8, "width_m": 0.841, "height_m": 0.594},
    "A0": {"code": 9, "width_m": 1.189, "height_m": 0.841},
}

def _safe_member(obj, name, *args, default=None):
    """@brief 读取工程图 COM 成员，失败时返回默认值。"""
    if obj is None:
        return default
    try:
        value = get_com_member(obj, name, *args)
        return default if value is None else value
    except Exception:
        return default

def _as_sequence(value):
    """@brief 统一 COM 数组、元组和单对象返回值。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]

def _finite_positive(value, default):
    """@brief 将 COM 数值转成有限正数，否则使用保守默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default), False
    if not math.isfinite(number) or number <= 0:
        return float(default), False
    return number, True

def _scale_ratio(scale):
    """@brief 将浮点比例转换为 SolidWorks 可接受的整数比。"""
    ratio = Fraction(float(scale)).limit_denominator(1000)
    return ratio.numerator, ratio.denominator

# ---- 上游桥接: 工程图实现已迁移至 subskills/solidworks-engineering-drawing ----
_IMPLEMENTATION = (
    Path(__file__).resolve().parents[1]
    / "subskills"
    / "solidworks-engineering-drawing"
    / "scripts"
    / "drawing_workflow.py"
)
_SPEC = importlib.util.spec_from_file_location("solidworks_drawing_workflow", _IMPLEMENTATION)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"无法加载工程图子技能实现: {_IMPLEMENTATION}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

__all__ = list(getattr(_MODULE, "__all__", ()))
if not __all__:
    __all__ = [name for name in vars(_MODULE) if not name.startswith("_")]
for _name in __all__:
    globals()[_name] = getattr(_MODULE, _name)

# ---- fork 定制: GB 公差 / 坐标扫描标注 / 制造零件图管线 ----
# =============================================================================
# 尺寸公差（dimension_tolerances）
# 来源：SW2024 电机项目 gen_drawing.py set_tol。仅覆盖线性尺寸公差；GD&T 几何公差框
# 仍为 reference_only。返回工程图模块统一证据字典（status∈pass|failed|review_required）。
# =============================================================================

# swTolType_e（SolidWorks.Interop.swconst）。技能约定：硬编码整数 + 注释枚举来源，
# 不加载 swconst.tlb。
SW_TOL_NONE = 0       # swTolNONE
SW_TOL_MIN = 1        # swTolMIN
SW_TOL_MAX = 2        # swTolMAX
SW_TOL_BASIC = 3      # swTolBASIC
SW_TOL_SYMMETRIC = 4  # swTolSYMMETRIC —— SW2024 已验证渲染
SW_TOL_BILATERAL = 5  # swTolBILATERAL（上下不同）
SW_TOL_LIMIT = 6      # swTolLIMIT

# GB/T 1804 一般公差·中等级 m：(上界 mm, 对称 ± mm)，线性查表。
_GB1804M_BANDS = (
    (6.0, 0.1),
    (30.0, 0.2),
    (120.0, 0.3),
    (400.0, 0.5),
    (float("inf"), 0.8),
)


def gb1804m_band(nominal_mm):
    """@brief GB/T 1804-m（一般公差·中等级）按名义尺寸线性分档，返回对称 ±（mm）。

    纯查表函数，不接触 COM，可单测。
    """
    n = abs(float(nominal_mm))
    for upper, band in _GB1804M_BANDS:
        if n <= upper:
            return band
    return 0.8  # 防御性兜底（>400）


def set_dimension_tolerance(display_dimension, nominal_mm, *, tol_type=SW_TOL_SYMMETRIC,
                            plus_mm=None, minus_mm=None):
    """@brief 为 DisplayDimension 设置尺寸公差。

    默认对称（tol_type=4）。plus_mm/minus_mm 任一为 None 时，按 GB/T 1804-m 以名义尺寸
    自动分档填充。公差值内部换算为米调用 IDimension.SetToleranceValues。

    三个必踩坑（详见 references/tolerances.md）：
      1. 必须用 SetToleranceType 方法启用 ± 显示（ITolerance.Type 属性赋值不渲染）；
      2. 带宽按已知名义尺寸查表，不读尺寸回读值（GetValue2/SystemValue 单位会错档）；
      3. SetToleranceValues 取米（±0.2mm → 0.0002）。

    返回证据字典：status=pass 仅代表 COM 调用成功，是否真正渲染须以导出 PDF/BMP 目视复核。
    """
    result = {
        "status": "failed",
        "stage": "tolerance",
        "tolerance_type": int(tol_type),
        "nominal_mm": float(nominal_mm),
        "plus_mm": None,
        "minus_mm": None,
        "band_source": None,
        "rendered_via": "IDimension.SetToleranceType + SetToleranceValues",
        "error_code": None,
        "retryable": False,
        "manual_review_required": True,
    }
    try:
        auto = (plus_mm is None) or (minus_mm is None)
        band = gb1804m_band(nominal_mm)
        if plus_mm is None:
            plus_mm = band
        if minus_mm is None:
            minus_mm = band
        plus_mm = float(plus_mm)
        minus_mm = float(minus_mm)
        result["plus_mm"] = plus_mm
        result["minus_mm"] = minus_mm
        result["band_source"] = "gb1804m_m" if auto else "explicit"

        dimension = get_com_member(display_dimension, "GetDimension")
        if dimension is None:
            result["error_code"] = "DRAWING_TOLERANCE_NO_DIMENSION"
            return result

        # 1) 启用显示（必须用方法，不是 ITolerance.Type= 属性赋值）
        get_com_member(dimension, "SetToleranceType", int(tol_type))
        # 2) 公差值，单位米：上差=+tol，下差=-tol（对称时两者同值）
        get_com_member(dimension, "SetToleranceValues", plus_mm / 1000.0, -minus_mm / 1000.0)

        result["status"] = "pass"
    except Exception as exc:
        result["error_code"] = "DRAWING_TOLERANCE_SET_FAILED"
        result["retryable"] = True
        result["error"] = str(exc)
    return result


def apply_gb1804m(display_dimension, nominal_mm):
    """@brief 便捷封装：按 GB/T 1804-m 套对称 ± 公差。"""
    return set_dimension_tolerance(display_dimension, nominal_mm, tol_type=SW_TOL_SYMMETRIC)


# =============================================================================
# 视图比例与坐标扫描式标注（drawing_edge_scan_dimensioning）
# 来源：SW2024 电机项目 gen_drawing.py find_edge / find_solid_x / picktype / adddim。
# 设计：find_edge / find_solid_x 为纯函数（注入 picktype 可单测）；make_picktype 把真实
# 工程图接成 picktype；scan_view_dimensions 端到端编排（前视图 W/H + 俯视图 D）。
# =============================================================================

# 视图比例低于此阈值时，SolidWorks 不暴露可点选的边/面（点选恒返回 type=12 视图对象，
# probe_scale 实证：0.50 可选 / 0.45 不可选）。
PICKABILITY_THRESHOLD_SCALE = 0.5


def pickability_ok(view):
    """@brief 视图比例门禁自检：读 ScaleRatio，判断是否 ≥1:2。

    返回 {status∈pass|blocked, scale, threshold, reason}。status=blocked 表示坐标扫描会
    静默失败，调用方应先 force_view_scale。
    """
    ratio = _safe_member(view, "ScaleRatio", default=None)
    scale = None
    if isinstance(ratio, (tuple, list)) and len(ratio) == 2 and ratio[1]:
        try:
            scale = float(ratio[0]) / float(ratio[1])
        except (TypeError, ValueError, ZeroDivisionError):
            scale = None
    result = {
        "status": "blocked",
        "stage": "pickability",
        "scale": scale,
        "threshold": PICKABILITY_THRESHOLD_SCALE,
        "reason": None,
    }
    if scale is None:
        result["reason"] = "无法读取视图 ScaleRatio"
    elif scale < PICKABILITY_THRESHOLD_SCALE:
        result["reason"] = (
            "视图比例 %.3f 低于 %.2f（1:2），SolidWorks 不暴露可点选边/面；"
            "先调 force_view_scale" % (scale, PICKABILITY_THRESHOLD_SCALE)
        )
    else:
        result["status"] = "pass"
    return result


def force_view_scale(view, scale, *, drawing_model=None):
    """@brief 强制视图真实比例，绕开 UseSheetScale 默认 True。

    CreateDrawViewFromModelView3 的 Scale 参数在 UseSheetScale 默认 True 时被忽略，大件会
    静默落到图纸比例（常 <1:2，低于可选择性阈值）。本函数：UseSheetScale=False +
    UseParentScale=False + ScaleDecimal=scale，可选重建后回读 ScaleRatio 验证。

    返回 {status∈pass|failed|review_required, scale, scale_ratio, verified}。
    """
    s = float(scale)
    result = {
        "status": "failed",
        "stage": "scale",
        "scale": s,
        "scale_ratio": None,
        "verified": False,
        "error_code": None,
        "retryable": False,
        "manual_review_required": False,
    }
    try:
        if hasattr(view, "UseSheetScale"):
            view.UseSheetScale = False
        if hasattr(view, "UseParentScale"):
            view.UseParentScale = False
        try:
            view.ScaleDecimal = s
        except Exception:
            num, den = _scale_ratio(s)
            view.ScaleRatio = (int(num), int(den))
        if drawing_model is not None:
            try:
                get_com_member(drawing_model, "EditRebuild3")
            except Exception:
                pass
        ratio = _safe_member(view, "ScaleRatio", default=None)
        if isinstance(ratio, (tuple, list)) and len(ratio) == 2 and ratio[1]:
            result["scale_ratio"] = [int(ratio[0]), int(ratio[1])]
            actual = float(ratio[0]) / float(ratio[1])
            result["verified"] = abs(actual - s) <= max(1e-6, abs(s) * 1e-3)
            result["status"] = "pass"
        else:
            result["status"] = "review_required"
            result["manual_review_required"] = True
            result["error_code"] = "DRAWING_SCALE_READBACK_FAILED"
    except Exception as exc:
        result["error_code"] = "DRAWING_SCALE_FORCE_FAILED"
        result["retryable"] = True
        result["error"] = str(exc)
    return result


def find_edge(view_outline, axis, side, fixed, picktype, *,
              coarse_steps=24, binary_iters=16, micro_range=0.0009):
    """@brief 在视图轮廓 `axis` 方向 `side` 侧找到一条可点选边（seltype==1）的坐标。

    纯函数（picktype 由调用方注入，可单测）。
      view_outline: [xmin, ymin, xmax, ymax]（米）。
      axis: 0=沿 X 扫描（变 X，固定 Y），1=沿 Y 扫描（变 Y，固定 X）。
      side: "min"（取 lo 侧边）或 "max"（取 hi 侧边）。
      fixed: 另一轴的固定坐标（米）。
      picktype(c, axis, fixed)->int: 0=空/仅视图对象，1=边，2=面。每次调用会自行清选择。

    算法：粗扫(24步)定位 空→实体 过渡 → 二分(16次)精确定位边界 → 以边界为中心
    ±micro_range(0.9mm)/0.1mm 微扫落在 seltype==1 的可选边上。对薄边与旋转/缩放鲁棒。
    返回边所在坐标 c（米），或 None（该侧无可点选几何）；微扫未命中边时回退返回边界中心。
    """
    lo = float(view_outline[axis])
    hi = float(view_outline[axis + 2])
    if hi <= lo:
        return None
    c_empty = None
    c_geom = None
    for k in range(coarse_steps + 1):
        if side == "min":
            c = lo + (hi - lo) * k / coarse_steps
        else:
            c = hi - (hi - lo) * k / coarse_steps
        st = picktype(c, axis, fixed)
        if st == 0:
            c_empty = c
        else:
            c_geom = c
            if c_empty is not None:
                break
    if c_geom is None:
        return None
    if c_empty is None:
        c_empty = lo if side == "min" else hi
    a, b = c_empty, c_geom
    for _ in range(binary_iters):
        m = (a + b) / 2.0
        if picktype(m, axis, fixed) == 0:
            a = m
        else:
            b = m
    center = (a + b) / 2.0
    for k in range(-9, 10):
        c = center + micro_range * k / 9.0
        if picktype(c, axis, fixed) == 1:
            return c
    return center


def find_solid_x(view_outline, ymid, picktype):
    """@brief 在视图 X 跨度内找一个落在实体面（seltype==2）上的 X，远离竖边/内部空隙。

    作为竖向（H/D）边扫描的固定 X，使点选不咬到竖边（夹爪等含中央空隙的零件）。
    找不到时回退到 X 中点。
    """
    lo = float(view_outline[0])
    hi = float(view_outline[2])
    if hi <= lo:
        return (lo + hi) / 2.0
    for frac in (0.5, 0.25, 0.75, 0.35, 0.65, 0.15, 0.85):
        x = lo + (hi - lo) * frac
        if picktype(x, 0, ymid) == 2:
            return x
    return (lo + hi) / 2.0


def make_picktype(drawing_model):
    """@brief 把活动工程图文档接成 find_edge 的 picktype(c, axis, fixed)->seltype 闭包。

    内部用 SelectionManager.GetSelectedObjectCount2 + GetSelectedObjectType6（回退 5/4/3）
    读首个几何对象类型（跳过 type=12 的视图对象）。每次点选前 ClearSelection。
    SelectByID2 作用于**活动文档**——调用方须确保该工程图已激活。
    任何点选异常都降级返回 0（视为空），使扫描优雅地得到部分结果而非抛错。
    """
    extension = _safe_member(drawing_model, "Extension", default=None)
    sel_mgr = _safe_member(drawing_model, "SelectionManager", default=None)
    empty = create_empty_dispatch_variant()
    cached_type_method = []

    def _gettype(idx):
        if cached_type_method:
            try:
                return get_com_member(sel_mgr, cached_type_method[0], idx, -1)
            except Exception:
                cached_type_method.clear()
        for mname in ("GetSelectedObjectType6", "GetSelectedObjectType5",
                      "GetSelectedObjectType4", "GetSelectedObjectType3"):
            try:
                value = get_com_member(sel_mgr, mname, idx, -1)
                cached_type_method.append(mname)
                return value
            except Exception:
                continue
        return 0

    def seltype():
        count = _safe_member(sel_mgr, "GetSelectedObjectCount2", -1, default=0)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        for idx in range(1, count + 1):
            if _gettype(idx) in (1, 2):
                return _gettype(idx)
        return 0

    def picktype(c, axis, fixed):
        xc = c if axis == 0 else fixed
        yc = fixed if axis == 0 else c
        try:
            get_com_member(drawing_model, "ClearSelection")
        except Exception:
            pass
        try:
            get_com_member(extension, "SelectByID2", "", "", xc, yc, 0.0, False, 0, empty, 0)
        except Exception:
            return 0
        return seltype()

    return picktype


def add_dimension_between(drawing_model, point_a, point_b, dim_x, dim_y, *, retries=4):
    """@brief 在活动工程图上点选两点并 AddDimension2 标注，失败重试 ≤retries 次。

    point_a/point_b: (x, y) 米；dim_x/dim_y: 尺寸文字放置坐标（米）。
    边线点选可能在刚创建视图几何未稳态时瞬时失败，故重试。返回 DisplayDimension 或 None。
    """
    extension = _safe_member(drawing_model, "Extension", default=None)
    ax, ay = float(point_a[0]), float(point_a[1])
    bx, by = float(point_b[0]), float(point_b[1])
    empty = create_empty_dispatch_variant()
    for _ in range(max(1, int(retries))):
        try:
            get_com_member(drawing_model, "ClearSelection")
        except Exception:
            pass
        get_com_member(extension, "SelectByID2", "", "", ax, ay, 0.0, False, 0, empty, 0)
        get_com_member(extension, "SelectByID2", "", "", bx, by, 0.0, True, 0, empty, 0)
        dd = get_com_member(drawing_model, "AddDimension2", dim_x, dim_y, 0.0)
        if dd:
            try:
                get_com_member(drawing_model, "ClearSelection")
            except Exception:
                pass
            return dd
    return None


def _scan_dim_entry(axis, display_dimension, nominal_mm, apply_tolerance, tolerance_resolver=None):
    """@brief 单个扫描尺寸的结果条目(内部用)。

    tolerance_resolver(dim_id, nominal_mm)->dict|None:计划层公差解析器;返回
    {plus_mm, minus_mm, tol_type} 时按显式公差写入,返回 None 走默认 GB/T 1804-m。
    """
    entry = {
        "axis": axis,
        "display_dimension_set": bool(display_dimension),
        "nominal_mm": float(nominal_mm),
        "tolerance_report": None,
    }
    if display_dimension and not apply_tolerance:
        entry["tolerance_report"] = {"status": "skipped", "reason": "apply_tolerance=False"}
        return entry
    if display_dimension and tolerance_resolver is not None:
        resolved = tolerance_resolver(axis, nominal_mm)
        if resolved is not None:
            entry["tolerance_report"] = set_dimension_tolerance(
                display_dimension,
                nominal_mm,
                tol_type=resolved.get("tol_type", SW_TOL_SYMMETRIC),
                plus_mm=resolved.get("plus_mm"),
                minus_mm=resolved.get("minus_mm"),
            )
            return entry
    if display_dimension:
        entry["tolerance_report"] = apply_gb1804m(display_dimension, nominal_mm)
    return entry


def scan_view_dimensions(drawing_model, front_view, top_view, *,
                         nominal_width_mm, nominal_height_mm, nominal_depth_mm,
                         gap=0.030, apply_tolerance=True, tolerance_resolver=None):
    """@brief 坐标扫描式标注前视图 W/H + 俯视图 D，可选套 GB/T 1804-m 对称公差。

    端到端：取两视图轮廓 → find_edge 定位 W/H/D 的左/右、上/下边 → add_dimension_between
    标注 →（可选）apply_gb1804m。调用前须确保：
      ① 工程图已激活（SelectByID2 作用于活动文档）；
      ② 视图已存盘（新建视图仅暴露部分剪影边，存盘后全部边线可选）；
      ③ 视图比例≥1:2（用 force_view_scale）。

    返回 {status∈pass|review_required|failed, dimensions:[…], edges, outline_front,
    outline_top, manual_review_required}。三个尺寸全成功标 pass；任一缺失标
    review_required；全程 pass 仍要求目视复核尺寸位置/重叠/尺寸链。
    """
    report = {
        "status": "review_required",
        "stage": "edge_scan",
        "dimensions": [],
        "edges": {},
        "outline_front": None,
        "outline_top": None,
        "apply_tolerance": bool(apply_tolerance),
        "nominal": {
            "width_mm": float(nominal_width_mm),
            "height_mm": float(nominal_height_mm),
            "depth_mm": float(nominal_depth_mm),
        },
        "manual_review_required": True,
        "error_code": None,
    }
    try:
        fo = _as_sequence(_safe_member(front_view, "GetOutline", default=[]))
        to = _as_sequence(_safe_member(top_view, "GetOutline", default=[]))
        if len(fo) < 4 or len(to) < 4:
            report["error_code"] = "DRAWING_SCAN_NO_OUTLINE"
            return report
        fo = [float(v) for v in fo]
        to = [float(v) for v in to]
        report["outline_front"] = fo
        report["outline_top"] = to

        fmx = (fo[0] + fo[2]) / 2.0
        fmy = (fo[1] + fo[3]) / 2.0
        tmy = (to[1] + to[3]) / 2.0

        picktype = make_picktype(drawing_model)

        # W：前视图 X 跨度（固定 Y=中）
        lw = find_edge(fo, 0, "min", fmy, picktype)
        rw = find_edge(fo, 0, "max", fmy, picktype)
        # H：前视图 Y 跨度（固定 X=实体面，避开中央空隙与竖边）
        hx = find_solid_x(fo, fmy, picktype)
        bh = find_edge(fo, 1, "min", hx, picktype)
        th = find_edge(fo, 1, "max", hx, picktype)
        # D：俯视图 Y 跨度（俯视图深度轴=竖向）
        dxv = find_solid_x(to, tmy, picktype)
        bd = find_edge(to, 1, "min", dxv, picktype)
        td = find_edge(to, 1, "max", dxv, picktype)
        report["edges"] = {"W": [lw, rw], "H": [bh, th], "D": [bd, td]}

        try:
            get_com_member(drawing_model, "ClearSelection")
        except Exception:
            pass

        dd_w = add_dimension_between(drawing_model, (lw, fmy), (rw, fmy), fmx, fo[1] - gap * 0.6) \
            if (lw and rw) else None
        dd_h = add_dimension_between(drawing_model, (hx, bh), (hx, th), fo[0] - gap * 0.6, fmy) \
            if (bh and th) else None
        dd_d = add_dimension_between(drawing_model, (dxv, bd), (dxv, td), to[0] - gap * 0.6, tmy) \
            if (bd and td) else None

        report["dimensions"] = [
            _scan_dim_entry("W", dd_w, nominal_width_mm, apply_tolerance, tolerance_resolver),
            _scan_dim_entry("H", dd_h, nominal_height_mm, apply_tolerance, tolerance_resolver),
            _scan_dim_entry("D", dd_d, nominal_depth_mm, apply_tolerance, tolerance_resolver),
        ]

        all_set = all(r["display_dimension_set"] for r in report["dimensions"])
        report["status"] = "pass" if all_set else "review_required"
        report["error_code"] = None if all_set else "DRAWING_SCAN_PARTIAL"
    except Exception as exc:
        report["status"] = "failed"
        report["error_code"] = "DRAWING_SCAN_FAILED"
        report["retryable"] = True
        report["error"] = str(exc)
    return report


# =============================================================================
# 制造零件图执行层(manufacturing_drawing_generation)
# 来源:SW2024 电机项目 gen_drawing.py 定稿流程 + sw_drawing_plan 计划契约。
# 编排顺序的硬约束(真机实证,违反会静默失败):
#   ① 文档级偏好(MMGS/第一角/小数位)必须在建任何视图/尺寸之前——尺寸显示
#      单位在创建时锁定,事后改偏好不刷新旧尺寸;
#   ② 套图框用 SetupSheet5 且 TemplateIn 必须 12(Custom),传 0..11 会忽略
#      .slddrt 路径;
#   ③ 新建视图先 SaveAs3 存盘再做坐标扫描(存盘提交几何后全部边线才可选);
#   ④ SelectByID2 作用于活动文档,扫描前强制 ActivateDoc3;
#   ⑤ 图框与注释共存上限为"恰好 1 条单行注释"——第 2 条注释(含 InsertNote)
#      或多行文本(\r\n)会使图框内容渲染丢失(2026-08 真机矩阵复现;尺寸标注
#      不受影响)。信息+技术要求必须合并为唯一一条单行注释。
# =============================================================================


def set_drawing_document_preferences(drawing_model, *, projection="first_angle", decimals=None):
    """@brief 设置工程图文档级偏好:单位制 MMGS(毫米)、投影角度、可选线性小数位。

    必须在创建任何视图/尺寸之前调用(drw_unit_fix.py 实证:尺寸显示在创建时
    锁定,事后设置不回溯)。应用级 SetUserPreferenceIntegerValue 对已有文档
    无效,必须走 model 级通道。第一角=(79,1) 已实证;第三角不写该键(多数
    区域安装默认即第三角)。

    decimals 默认 None=不设置:SW2024 实证 (49,0) 会把公差值显示四舍五入到
    整数(±0.3 渲染成 ±0),模板默认小数位可同时让名义尺寸保持整数观感、
    公差保留小数,与 gen_drawing.py 定稿行为一致。
    """
    result = {
        "status": "failed",
        "stage": "preferences",
        "units": {"key": 263, "value": 5, "meaning": "swUnitSystem_MMGS"},
        "projection": {"key": 79, "value": 1 if projection == "first_angle" else None,
                       "meaning": "swDrawingViewProjection"},
        "decimals": {"key": 49, "value": decimals, "meaning": "线性尺寸小数位(None=不设置)"},
        "applied": [],
        "manual_review_required": False,
        "retryable": True,
        "error_code": None,
    }
    try:
        if get_com_member(drawing_model, "SetUserPreferenceIntegerValue", 263, 5):
            result["applied"].append("units_mmgs")
        if projection == "first_angle":
            if get_com_member(drawing_model, "SetUserPreferenceIntegerValue", 79, 1):
                result["applied"].append("first_angle")
        if decimals is not None:
            if get_com_member(drawing_model, "SetUserPreferenceIntegerValue", 49, int(decimals)):
                result["applied"].append(f"decimals_{int(decimals)}")
        result["status"] = "pass" if "units_mmgs" in result["applied"] else "review_required"
        if result["status"] == "review_required":
            result["error_code"] = "DRAWING_PREF_UNITS_NOT_APPLIED"
    except Exception as exc:
        result["error_code"] = "DRAWING_PREF_SET_FAILED"
        result["error"] = str(exc)
    return result


def setup_sheet_format(drawing_model, paper_size, format_path):
    """@brief 为当前图纸套用自定义图框(.slddrt)。

    SetupSheet5 的 TemplateIn 必须 12(swDwgTemplateCustom),否则 format_path 被
    忽略(SW2024 真机实证);套用后图纸尺寸由图框格式自身决定。返回证据字典。
    """
    paper_size = str(paper_size).upper()
    spec = PAPER_SIZES.get(paper_size)
    if spec is None:
        return {
            "status": "blocked",
            "stage": "sheet_format",
            "configured": False,
            "retryable": False,
            "error_code": "DRAWING_FORMAT_UNKNOWN_PAPER",
        }
    path = Path(format_path)
    if not path.is_file():
        return {
            "status": "blocked",
            "stage": "sheet_format",
            "configured": False,
            "retryable": True,
            "paper_size": paper_size,
            "format_path": str(path),
            "error_code": "DRAWING_FORMAT_FILE_MISSING",
        }
    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    sheet_name = str(_safe_member(sheet, "GetName", default="") or "")
    result = {
        "status": "failed",
        "stage": "sheet_format",
        "paper_size": paper_size,
        "paper_code": spec["code"],
        "format_path": str(path),
        "sheet_name": sheet_name,
        "configured": False,
        "manual_review_required": True,
        "retryable": True,
        "error_code": None,
    }
    try:
        configured = bool(get_com_member(
            drawing_model,
            "SetupSheet5",
            sheet_name,
            spec["code"],
            12,  # swDwgTemplateCustom:必须传 12 才认 .slddrt 路径
            1.0,
            1.0,
            False,
            str(path),
            0.0,
            0.0,
            "",
            False,
        ))
        result["configured"] = configured
        result["status"] = "pass" if configured else "failed"
        if not configured:
            result["error_code"] = "DRAWING_FORMAT_SETUP_FAILED"
    except Exception as exc:
        result["error_code"] = "DRAWING_FORMAT_SETUP_FAILED"
        result["error"] = str(exc)
    return result


def add_text_note(drawing_model, text, x, y, height=0.005):
    """@brief 用 CreateText2 在指定位置放置单行文字注释并回读验证。

    替代 x/y 参数失效的 add_note(InsertNote 不接收位置)。返回证据字典。
    """
    result = {
        "status": "failed",
        "stage": "note",
        "text": str(text),
        "position_m": [float(x), float(y)],
        "created": False,
        "readback": None,
        "retryable": True,
        "error_code": None,
    }
    try:
        note = get_com_member(drawing_model, "CreateText2", str(text), float(x), float(y), 0.0, float(height), 0.0)
        result["created"] = note is not None
        if note is not None:
            result["readback"] = {
                "text": str(_safe_member(note, "GetText", default="") or ""),
                "x": _safe_member(note, "GetX", default=None),
                "y": _safe_member(note, "GetY", default=None),
            }
            result["status"] = "pass"
        else:
            result["error_code"] = "DRAWING_NOTE_CREATE_FAILED"
    except Exception as exc:
        result["error_code"] = "DRAWING_NOTE_CREATE_FAILED"
        result["error"] = str(exc)
    return result


def _iter_drawing_notes(drawing_model):
    """@brief 遍历图纸全部注释(含图框模板注释与视图注释),返回 (view_name, note)。"""
    pairs = []
    current = _safe_member(drawing_model, "GetFirstView")
    guard = 0
    while current is not None and guard < 2000:
        view_name = str(_safe_member(current, "Name", default="") or "")
        for note in _as_sequence(_safe_member(current, "GetNotes", default=[])):
            pairs.append((view_name, note))
        nxt = _safe_member(current, "GetNextView")
        if nxt is None or nxt is current:
            break
        current = nxt
        guard += 1
    return pairs


def probe_title_block_links(drawing_model):
    """@brief 探测图框标题栏注释是否与自定义属性联动(含 $PRP 占位符)。

    官方 GB 模板标题栏单元格若为 "$PRP:xxx" 形式,写文档自定义属性即可自动
    填充;若为空白文本则只能用注释克位兜底。只读,不修改文档。
    """
    fields = []
    texts = []
    for _view_name, note in _iter_drawing_notes(drawing_model):
        text = str(_safe_member(note, "Text", default="") or _safe_member(note, "GetText", default="") or "")
        if not text:
            continue
        texts.append(text)
        for match in re.findall(r"\$PRP(?:SHEET)?\s*:?\s*\"?([^\$\"]+)\"?", text):
            name = match.strip()
            if name and name not in fields:
                fields.append(name)
    return {
        "status": "pass",
        "stage": "title_block_probe",
        "linked": bool(fields),
        "linked_fields": fields,
        "note_count": len(texts),
        "evidence_texts": texts[:40],
        "manual_review_required": True,
    }


def fill_title_block(drawing_model, fields, *, tech_lines=None,
                     note_x=0.045, note_y_from_top=0.035):
    """@brief 填充标题栏字段:优先属性联动,空白标题栏退回唯一一条单行注释。

    属性联动路线:文档级 CustomPropertyManager 写入,图框 "$PRP" 注释自动刷新
    (需先 probe_title_block_links 确认模板支持)。

    注释克位路线(SW2024 硬约束,见下方"图框与注释共存约束"):信息字段与技术
    要求合并为**唯一一条单行注释**,置于图纸左上角。GB 官方模板标题栏单元格为
    空,这是实证兜底方案。
    """
    fields = {str(key): value for key, value in dict(fields or {}).items() if value is not None}
    tech_lines = [str(line) for line in (tech_lines or []) if str(line).strip()]
    result = {
        "status": "review_required",
        "stage": "title_block",
        "method_used": None,
        "fields_written": [],
        "tech_lines": tech_lines,
        "note_evidence": None,
        "probe": None,
        "manual_review_required": True,
        "retryable": False,
        "error_code": None,
    }
    if not fields and not tech_lines:
        result["error_code"] = "DRAWING_TITLE_BLOCK_NO_FIELDS"
        return result
    probe = probe_title_block_links(drawing_model)
    result["probe"] = probe
    if fields and probe["linked"]:
        extension = _safe_member(drawing_model, "Extension", default=None)
        manager = _safe_member(extension, "CustomPropertyManager", "", default=None) if extension is not None else None
        if manager is not None:
            written = []
            try:
                for name, value in fields.items():
                    added = _safe_member(manager, "Add3", name, 30, str(value), 1, default=False)
                    if not added:
                        added = _safe_member(manager, "Set2", name, str(value), default=False)
                    if added:
                        written.append(name)
                result["method_used"] = "custom_properties"
                result["fields_written"] = written
                if written and not tech_lines:
                    result["status"] = "pass"
                    return result
                # 属性已写但仍有技术要求行 → 技术要求走唯一一条单行注释。
                if written:
                    note_text = "技术要求:" + ";".join(tech_lines)
                    note = add_text_note(drawing_model, note_text, note_x, _sheet_height_m(drawing_model) - note_y_from_top)
                    result["note_evidence"] = note
                    result["status"] = note["status"]
                    return result
                result["error_code"] = "DRAWING_TITLE_BLOCK_PROPERTY_WRITE_FAILED"
                # 属性写入失败继续走注释兜底,不直接返回。
            except Exception as exc:
                result["error_code"] = "DRAWING_TITLE_BLOCK_PROPERTY_WRITE_FAILED"
                result["error"] = str(exc)
                # 属性写入失败继续走注释兜底,不直接返回。
    # 注释克位兜底:信息字段+技术要求合并为唯一一条单行注释(SW2024 硬约束)。
    parts = [f"{key}:{value}" for key, value in fields.items()]
    if tech_lines:
        parts.append("技术要求:" + ";".join(tech_lines))
    note_text = "   ".join(parts)
    note = add_text_note(drawing_model, note_text, note_x, _sheet_height_m(drawing_model) - note_y_from_top)
    result["method_used"] = "text_note_fallback"
    result["fields_written"] = list(fields)
    result["note_evidence"] = note
    result["status"] = note["status"]
    return result


def _sheet_height_m(drawing_model):
    """@brief 读取当前图纸高度(米),失败回退 A3 高度。"""
    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    return _finite_positive(_safe_member(sheet, "Height"), 0.297)[0]


def _plan_tolerance_resolver(plan):
    """@brief 把计划中的总体尺寸公差条目接成 scan_view_dimensions 的解析器。"""
    entries = {
        str(item.get("id")): item
        for item in ((plan.get("dimensioning") or {}).get("overall_dimensions") or [])
    }

    def resolver(dim_id, nominal_mm):
        """@brief 按 W/H/D 返回计划公差;计划缺失时返回 None 走默认分档。"""
        entry = entries.get(str(dim_id))
        if not entry:
            return None
        tolerance = entry.get("tolerance") or {}
        if tolerance.get("source") == "explicit" or "plus_mm" in tolerance:
            return {
                "tol_type": SW_TOL_SYMMETRIC if tolerance.get("type", "symmetric") == "symmetric" else SW_TOL_BILATERAL,
                "plus_mm": tolerance.get("plus_mm"),
                "minus_mm": tolerance.get("minus_mm"),
            }
        return None

    return resolver


_STATUS_SEVERITY = {"pass": 0, "review_required": 1, "blocked": 2, "failed": 3}


def _worst_status(stages):
    """@brief 汇总各阶段状态,取最严重者。"""
    worst = "pass"
    for status in stages:
        if _STATUS_SEVERITY.get(status, 3) > _STATUS_SEVERITY.get(worst, 0):
            worst = status
    return worst


def generate_manufacturing_drawing(sw, plan, out_dir, *, sheet_format_candidates=None):
    """@brief 按制造零件图计划端到端生成 GB 工程图并导出 PDF(总编排)。

    流程(顺序硬约束见本节头部注释):
      校验计划 → 开零件 → 新建工程图(可选 .drwdot 模板)→ 套图框(.slddrt)→
      文档级偏好(先于视图!)→ 按计划建三视图并强制真实比例 → 先存盘再激活 →
      尺寸标注(scan/model_only 按计划策略)→ 标题栏填充 → 技术要求注释 →
      重建存盘 → 导出 PDF。

    返回 {status, stage, stages, artifacts, manual_review_required};产物路径在
    artifacts.slddrw / artifacts.pdf,供审查层(review_manufacturing_drawing)
    做结构/布局/PDF 文字三重证据复核。执行层永不自动 pass 交付。
    """
    report = {
        "status": "failed",
        "stage": "execute",
        "schema": "manufacturing_drawing_execution/1.0",
        "stages": {},
        "artifacts": {},
        "manual_review_required": True,
        "retryable": True,
        "error_code": None,
    }
    try:
        part_path = str(((plan or {}).get("part") or {}).get("path") or "").strip()
        nominal = ((plan or {}).get("part") or {}).get("nominal") or {}
        views_spec = (plan or {}).get("views") or []
        frame = (plan or {}).get("frame") or {}
        if not part_path or len(views_spec) < 3 or not nominal:
            report["error_code"] = "DRAWING_EXEC_PLAN_INVALID"
            return report
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(part_path).stem
        slddrw_path = out_dir / f"{stem}.SLDDRW"
        pdf_path = out_dir / f"{stem}.pdf"

        # 1) 零件进入会话(CreateDrawViewFromModelView3 需要模型已打开)。
        part_model = open_document(sw, part_path, silent=True)
        report["stages"]["open_part"] = {
            "status": "pass" if part_model is not None else "review_required",
            "part_path": part_path,
        }

        # 2) 新建工程图:优先 .drwdot 文档模板,否则默认空图。
        paper_size = str(frame.get("paper_size") or "A3").upper()
        paper_spec = PAPER_SIZES.get(paper_size, PAPER_SIZES["A3"])
        template_path = frame.get("template_path")
        if template_path and Path(template_path).is_file():
            drawing_model = get_com_member(sw, "NewDocument", str(template_path), paper_spec["code"], 0.0, 0.0)
        else:
            drawing_model = new_document(sw, "drawing")
        if drawing_model is None:
            report["error_code"] = "DRAWING_EXEC_NEW_DOCUMENT_FAILED"
            report["stages"]["new_document"] = {"status": "failed", "template_path": template_path}
            return report
        report["stages"]["new_document"] = {"status": "pass", "template_path": template_path}

        # 3) 套图框(.slddrt):显式路径 > 本机候选 > 跳过并告警。
        format_path = frame.get("sheet_format_path")
        if not format_path and sheet_format_candidates:
            selection = select_drawing_template(sheet_format_candidates, paper_size=paper_size)
            format_path = selection.get("selected")
            report["stages"]["select_format"] = selection
        if format_path:
            report["stages"]["sheet_format"] = setup_sheet_format(drawing_model, paper_size, format_path)
        else:
            report["stages"]["sheet_format"] = {
                "status": "review_required",
                "configured": False,
                "error_code": "DRAWING_FORMAT_NOT_SPECIFIED",
                "message": "未提供图框且未找到本机候选,图框需人工确认",
            }

        # 4) 文档级偏好:必须先于视图/尺寸(创建即锁定)。
        report["stages"]["preferences"] = set_drawing_document_preferences(drawing_model)

        # 5) 按计划建三视图 + 强制真实比例(UseSheetScale 默认会忽略 Scale 参数)。
        scale = float((plan or {}).get("scale") or 1.0)
        created_views = {}
        for item in views_spec:
            center = item["center"]
            view = get_com_member(
                drawing_model,
                "CreateDrawViewFromModelView3",
                part_path,
                item["name"],
                float(center[0]),
                float(center[1]),
                scale,
            )
            if view is None:
                report["error_code"] = "DRAWING_EXEC_VIEW_CREATE_FAILED"
                report["stages"]["create_views"] = {"status": "failed", "failed_view": item["name"]}
                return report
            created_views[item["name"]] = view
        scale_reports = [force_view_scale(view, scale, drawing_model=drawing_model) for view in created_views.values()]
        report["stages"]["create_views"] = {
            "status": _worst_status(item["status"] for item in scale_reports),
            "views": list(created_views),
            "scale_reports": scale_reports,
        }
        get_com_member(drawing_model, "EditRebuild3")

        # 6) 先存盘提交视图几何(未存盘视图只暴露部分剪影边),再强制激活
        #    (SelectByID2 作用于活动文档)。SaveAs3 返回值在动态派发下同样
        #    不可靠,以落盘证据为准。
        get_com_member(drawing_model, "SaveAs3", str(slddrw_path), 0, 0)
        errors_variant = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        title = str(_safe_member(drawing_model, "GetTitle", default="") or "")
        try:
            get_com_member(sw, "ActivateDoc3", title, False, 0, errors_variant)
        except Exception:
            pass
        saved_on_disk = slddrw_path.is_file() and slddrw_path.stat().st_size > 0
        report["stages"]["commit"] = {
            "status": "pass" if saved_on_disk else "review_required",
            "slddrw": str(slddrw_path),
            "saved": bool(saved_on_disk),
            "evidence": "file_on_disk" if saved_on_disk else "file_missing",
        }

        # 7) 尺寸标注:scan=坐标扫描 W/H/D+计划公差;model_only=仅模型尺寸。
        strategy = str(((plan or {}).get("dimensioning") or {}).get("strategy") or "scan")
        pickability = {name: pickability_ok(view) for name, view in created_views.items()}
        if strategy == "scan" and all(item["status"] == "pass" for item in pickability.values()):
            scan_report = scan_view_dimensions(
                drawing_model,
                created_views.get("*Front"),
                created_views.get("*Top"),
                nominal_width_mm=float(nominal.get("width_mm", 0.0)),
                nominal_height_mm=float(nominal.get("height_mm", 0.0)),
                nominal_depth_mm=float(nominal.get("depth_mm", 0.0)),
                tolerance_resolver=_plan_tolerance_resolver(plan),
            )
            report["stages"]["dimensioning"] = {
                "status": scan_report["status"],
                "strategy": "scan",
                "report": scan_report,
            }
        else:
            inserted = insert_dimensions(drawing_model)
            inserted_count = len(_as_sequence(inserted)) if inserted else 0
            report["stages"]["dimensioning"] = {
                "status": "pass" if inserted_count else "review_required",
                "strategy": "model_only",
                "inserted_count": inserted_count,
                "pickability": pickability,
                "error_code": None if inserted_count else "DRAWING_MODEL_DIMENSIONS_EMPTY",
            }

        # 8) 标题栏填充 + 技术要求:合并处理(SW2024 硬约束,见下方约束说明)。
        #    SetupSheet5 套用的图框与注释共存上限为"恰好 1 条单行注释"——第 2 条
        #    注释(含 InsertNote)或多行文本(\r\n)都会使图框内容渲染丢失
        #    (2026-08 真机复现:1 条单行幸存/2 条必死/多行必死;尺寸标注不受影响)。
        #    因此信息字段与技术要求由 fill_title_block 合并为唯一一条单行注释。
        tech_lines = (((plan or {}).get("technical_requirements") or {}).get("lines")) or []
        report["stages"]["title_block"] = fill_title_block(
            drawing_model,
            ((plan or {}).get("title_block") or {}).get("fields") or {},
            tech_lines=tech_lines,
        )
        report["stages"]["technical_requirements"] = {
            "status": report["stages"]["title_block"]["status"],
            "lines": tech_lines,
            "merged_into_single_note": True,
            "reason": "SW2024 图框与注释共存上限为 1 条单行,技术要求已并入信息注释",
        }

        # 9) 重建、存盘、导出 PDF。
        get_com_member(drawing_model, "EditRebuild3")
        get_com_member(drawing_model, "SaveAs3", str(slddrw_path), 0, 0)
        pdf_ok = export_sheet_to_pdf(drawing_model, str(pdf_path), sw_app=sw)
        report["stages"]["export_pdf"] = {
            "status": "pass" if pdf_ok else "failed",
            "pdf": str(pdf_path),
            "error_code": None if pdf_ok else "DRAWING_EXEC_PDF_EXPORT_FAILED",
        }

        report["artifacts"] = {
            "slddrw": str(slddrw_path),
            "pdf": str(pdf_path) if pdf_ok else None,
        }
        stage_status = {
            name: item.get("status", "failed") if isinstance(item, dict) else "failed"
            for name, item in report["stages"].items()
        }
        report["status"] = _worst_status(stage_status.values())
        report["error_code"] = next(
            (item.get("error_code") for item in report["stages"].values()
             if isinstance(item, dict) and item.get("status") in {"failed", "blocked"} and item.get("error_code")),
            None,
        )
        if report["status"] == "pass":
            # 尺寸位置/重叠/尺寸链仍需目视复核,执行层永不自动 pass 交付。
            report["status"] = "review_required"
        return report
    except Exception as exc:
        report["error_code"] = "DRAWING_EXEC_FAILED"
        report["error"] = str(exc)
        return report
