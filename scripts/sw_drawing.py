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
    from .sw_connect import create_empty_dispatch_variant, get_com_member
except ImportError:
    from sw_preflight import import_com_dependencies
    from sw_connect import create_empty_dispatch_variant, get_com_member

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

# ---- fork 定制: GB 公差 / 坐标扫描标注 ----
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


def _scan_dim_entry(axis, display_dimension, nominal_mm, apply_tolerance):
    """@brief 单个扫描尺寸的结果条目（内部用）。"""
    entry = {
        "axis": axis,
        "display_dimension_set": bool(display_dimension),
        "nominal_mm": float(nominal_mm),
        "tolerance_report": None,
    }
    if display_dimension and apply_tolerance:
        entry["tolerance_report"] = apply_gb1804m(display_dimension, nominal_mm)
    elif display_dimension:
        entry["tolerance_report"] = {"status": "skipped", "reason": "apply_tolerance=False"}
    return entry


def scan_view_dimensions(drawing_model, front_view, top_view, *,
                         nominal_width_mm, nominal_height_mm, nominal_depth_mm,
                         gap=0.030, apply_tolerance=True):
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
            _scan_dim_entry("W", dd_w, nominal_width_mm, apply_tolerance),
            _scan_dim_entry("H", dd_h, nominal_height_mm, apply_tolerance),
            _scan_dim_entry("D", dd_d, nominal_depth_mm, apply_tolerance),
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
