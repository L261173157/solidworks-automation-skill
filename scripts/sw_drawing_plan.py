"""
制造零件图计划层(纯 Python,无 COM)。

输入三维证据(名义尺寸、孔候选、质量属性)与图框规格,输出可审计的制造零件图
计划(drawing_plan);执行层 scripts/sw_drawing.generate_manufacturing_drawing
按计划落图,审查层 scripts/sw_review.review_manufacturing_drawing 复核产物。

设计约束:
  * 计划层不接触 SolidWorks,可完全单测(与 vibecad plan_from_brief 同模式);
  * 视图比例必须 ≥1:2 才允许坐标扫描标注(低于阈值 SW 不暴露可点选边,
    见 scripts/sw_drawing.PICKABILITY_THRESHOLD_SCALE),放不下自动升图幅;
  * 公差默认 GB/T 1804-m 对称 ±,按外部已知名义尺寸分档,不读回读值;
  * 计划只描述意图与证据来源,是否真正渲染以导出 PDF/BMP 目视复核为准。
"""
from fractions import Fraction

try:
    from .sw_drawing import (
        PAPER_SIZES,
        STANDARD_DRAWING_SCALES,
        PICKABILITY_THRESHOLD_SCALE,
        gb1804m_band,
    )
except ImportError:
    from sw_drawing import (
        PAPER_SIZES,
        STANDARD_DRAWING_SCALES,
        PICKABILITY_THRESHOLD_SCALE,
        gb1804m_band,
    )

PLAN_SCHEMA_VERSION = "manufacturing_drawing_plan/1.0"

PROJECTIONS = ("first_angle", "third_angle")

# 图幅升档顺序(GB 加工图常用 A4-A0)。
PAPER_UPSIZE_ORDER = ("A4", "A3", "A2", "A1", "A0")

# 视图间距按图幅取值:>=1:2 比例下视图轮廓外扩仅约 6mm,间距需覆盖两倍余量
# 并给尺寸文字留位(SW2024 电机项目 gen_drawing.py 实证值)。
DEFAULT_GAP_M_BY_PAPER = {
    "A4": 0.040,
    "A3": 0.050,
    "A2": 0.030,
    "A1": 0.030,
    "A0": 0.030,
}

MARGIN_M = 0.012
TITLE_BLOCK_WIDTH_M = 0.180
TITLE_BLOCK_HEIGHT_M = 0.055

DEFAULT_TECHNICAL_REQUIREMENTS = (
    "未注公差按 GB/T 1804-{grade}。",
    "锐边去毛刺。",
)


def _scale_ratio(scale):
    """@brief 将浮点比例转换为 SolidWorks 可接受的整数比。"""
    ratio = Fraction(float(scale)).limit_denominator(1000)
    return ratio.numerator, ratio.denominator


def scale_ratio_text(scale):
    """@brief 比例的人类可读文本(如 2:1 / 1:2);SolidWorks ScaleRatio 即 num:den。"""
    numerator, denominator = _scale_ratio(scale)
    return "%d:%d" % (numerator, denominator)


def _positive_nominal(nominal):
    """@brief 校验并归一化 W/H/D 名义尺寸(mm),非法时返回 None。"""
    try:
        width, height, depth = (float(nominal["width_mm"]), float(nominal["height_mm"]), float(nominal["depth_mm"]))
    except (KeyError, TypeError, ValueError):
        return None
    if min(width, height, depth) <= 0:
        return None
    return {"width_mm": width, "height_mm": height, "depth_mm": depth}


def _layout_for_paper(nominal, paper_size, *, projection, gap_m=None, margin_m=MARGIN_M,
                      title_block_width_m=TITLE_BLOCK_WIDTH_M,
                      title_block_height_m=TITLE_BLOCK_HEIGHT_M):
    """@brief 在指定图幅内计算自适应三视图布局,返回含比例的布局或 blocked 结果。

    布局沿用 plan_standard_view_layout 的工作区扣除法(页边距 + 右下标题栏带),
    区别在于:第一角投影俯视图在主视图下方(GB 默认),第三角在上方;
    视图间距按图幅查表。纯计算,不接触 COM。
    """
    spec = PAPER_SIZES[paper_size]
    width_m = nominal["width_mm"] / 1000.0
    height_m = nominal["height_mm"] / 1000.0
    depth_m = nominal["depth_mm"] / 1000.0
    gap = DEFAULT_GAP_M_BY_PAPER.get(paper_size, 0.030) if gap_m is None else float(gap_m)

    sheet_width = spec["width_m"]
    sheet_height = spec["height_m"]
    working_left = margin_m
    working_bottom = margin_m + title_block_height_m + gap
    working_right = sheet_width - margin_m
    working_top = sheet_height - margin_m
    available_width = working_right - working_left
    available_height = working_top - working_bottom
    raw_scale = min(
        (available_width - gap) / (width_m + depth_m),
        (available_height - gap) / (height_m + depth_m),
    )
    if raw_scale <= 0:
        return {"status": "blocked", "paper_size": paper_size, "error_code": "DRAWING_PLAN_WORKING_AREA_INVALID"}

    scale = next((item for item in STANDARD_DRAWING_SCALES if item <= raw_scale + 1e-12), raw_scale)
    front_w, front_h = width_m * scale, height_m * scale
    top_w, top_h = width_m * scale, depth_m * scale
    right_w, right_h = depth_m * scale, height_m * scale
    layout_w = front_w + gap + right_w
    layout_h = front_h + gap + top_h
    origin_x = working_left + max(0.0, (available_width - layout_w) / 2.0)
    layout_bottom = working_bottom + max(0.0, (available_height - layout_h) / 2.0)

    def view_record(name, left, bottom, view_width, view_height):
        """@brief 单个视图的中心点与边界框记录。"""
        return {
            "name": name,
            "center": [left + view_width / 2.0, bottom + view_height / 2.0],
            "box": {"left": left, "bottom": bottom, "right": left + view_width, "top": bottom + view_height},
        }

    if projection == "first_angle":
        top_record = view_record("*Top", origin_x, layout_bottom, top_w, top_h)
        front_record = view_record("*Front", origin_x, layout_bottom + top_h + gap, front_w, front_h)
    else:
        front_record = view_record("*Front", origin_x, layout_bottom, front_w, front_h)
        top_record = view_record("*Top", origin_x, layout_bottom + front_h + gap, top_w, top_h)
    right_record = view_record("*Right", origin_x + front_w + gap, front_record["box"]["bottom"], right_w, right_h)

    return {
        "status": "pass",
        "paper_size": paper_size,
        "sheet": {"width_m": sheet_width, "height_m": sheet_height},
        "working_area": {"left": working_left, "bottom": working_bottom, "right": working_right, "top": working_top},
        "title_block_box": {
            "left": sheet_width - margin_m - title_block_width_m,
            "bottom": margin_m,
            "right": sheet_width - margin_m,
            "top": margin_m + title_block_height_m,
        },
        "scale": scale,
        "scale_ratio": list(_scale_ratio(scale)),
        "views": [front_record, top_record, right_record],
        "gap_m": gap,
    }


def select_paper_and_scale(nominal, *, preferred_paper="A3", allow_upsize=True,
                           max_paper="A0", min_scale=PICKABILITY_THRESHOLD_SCALE,
                           projection="first_angle"):
    """@brief 图幅/比例联合求解:优先用户指定图幅,比例低于可点选阈值时逐级升图幅。

    返回 {status∈pass|review_required, layout, scale_ok, papers_tried}。
    升到 max_paper 仍低于阈值时返回 review_required,由调用方把标注策略降级为
    仅模型尺寸路线(扫描标注在 <1:2 时静默失败,必须提前规避)。
    """
    order = PAPER_UPSIZE_ORDER
    start = order.index(preferred_paper) if preferred_paper in order else 0
    end = order.index(max_paper) if max_paper in order else len(order) - 1
    papers_tried = []
    chosen = None
    for paper in order[start:end + 1]:
        layout = _layout_for_paper(nominal, paper, projection=projection)
        papers_tried.append({"paper_size": paper, "scale": layout.get("scale"), "status": layout["status"]})
        if layout["status"] == "blocked":
            continue
        chosen = layout
        if layout["scale"] >= min_scale or not allow_upsize:
            break
    if chosen is None:
        return {
            "status": "review_required",
            "layout": None,
            "scale_ok": False,
            "papers_tried": papers_tried,
            "error_code": "DRAWING_PLAN_NO_FITTABLE_PAPER",
        }
    scale_ok = chosen["scale"] >= min_scale
    return {
        "status": "pass" if scale_ok else "review_required",
        "layout": chosen,
        "scale_ok": scale_ok,
        "papers_tried": papers_tried,
        "error_code": None if scale_ok else "DRAWING_PLAN_SCALE_BELOW_PICKABILITY",
    }


def assign_overall_tolerances(nominal, *, grade="m"):
    """@brief 为 W/H/D 总体尺寸分配 GB/T 1804 对称 ± 公差条目。

    带宽按外部已知名义尺寸查表(不读尺寸回读值,单位不一致会错档)。
    当前仅内置 m 级;f/c/v 等级属公差智能化阶段。
    """
    if grade != "m":
        raise ValueError("当前仅支持 GB/T 1804-m;f/c/v 等级尚未实现。")
    entries = []
    for dim_id, key in (("W", "width_mm"), ("H", "height_mm"), ("D", "depth_mm")):
        value = nominal[key]
        band = gb1804m_band(value)
        entries.append({
            "id": dim_id,
            "nominal_mm": value,
            "tolerance": {
                "type": "symmetric",
                "plus_mm": band,
                "minus_mm": band,
                "source": "gb1804-m",
                "grade": grade,
            },
        })
    return entries


def build_technical_requirements(extra_lines=None, *, grade="m"):
    """@brief 组装技术要求文本行;默认含未注公差声明与去毛刺条目。"""
    lines = [template.format(grade=grade) for template in DEFAULT_TECHNICAL_REQUIREMENTS]
    for line in extra_lines or []:
        text = str(line).strip()
        if text:
            lines.append(text)
    return lines


def _title_block_fields(frame_spec, layout, evidence):
    """@brief 合成标题栏字段:显式字段优先,比例/重量按证据自动补齐。"""
    explicit = dict(frame_spec.get("title_block") or {})
    auto = {}
    if layout is not None:
        auto["比例"] = scale_ratio_text(layout["scale"])
    mass = (evidence or {}).get("mass") or {}
    weight = mass.get("mass_kg")
    if weight is not None and mass.get("mass_meaningful"):
        auto["重量"] = round(float(weight), 4)
    fields = dict(auto)
    fields.update({str(k): v for k, v in explicit.items() if v is not None})
    return fields, sorted(auto)


def plan_manufacturing_drawing(evidence, frame_spec=None, options=None):
    """@brief 由三维证据与图框规格生成制造零件图计划(纯函数,无 COM)。

    evidence: {part_path, nominal:{width_mm,height_mm,depth_mm},
               mass?:{mass_kg,mass_meaningful}, holes?:[...]}
    frame_spec: {paper_size?, template_path?(.drwdot), sheet_format_path?(.slddrt),
                 projection?, title_block?{字段:值}, technical_requirements?[行]}
    options: {grade?:"m", min_scan_scale?, allow_paper_upsize?, max_paper?, gap_m?}

    返回计划字典(status∈pass|review_required|blocked),结构见
    schemas/manufacturing_drawing_plan.schema.json。
    """
    frame_spec = dict(frame_spec or {})
    options = dict(options or {})
    evidence = dict(evidence or {})
    grade = str(options.get("grade", "m"))
    projection = str(frame_spec.get("projection", "first_angle"))
    preferred_paper = str(frame_spec.get("paper_size", "A3")).upper()

    checks = []

    def check(check_id, ok, message_pass, message_fail):
        checks.append({"id": check_id, "status": "pass" if ok else "fail", "message": message_pass if ok else message_fail})
        return ok

    part_path = str(evidence.get("part_path") or "").strip()
    part_ok = check("plan-part-path", bool(part_path), "已提供零件路径", "缺少零件路径,执行层无法打开模型")

    nominal = _positive_nominal(evidence.get("nominal") or {})
    nominal_ok = check(
        "plan-nominal-sizes",
        nominal is not None,
        "W/H/D 名义尺寸为有限正数",
        "名义尺寸缺失或非法(width/height/depth_mm 必须为正)",
    )

    projection_ok = check(
        "plan-projection",
        projection in PROJECTIONS,
        "投影方式受支持",
        f"未知投影方式: {projection}",
    )
    paper_ok = check(
        "plan-paper-size",
        preferred_paper in PAPER_SIZES,
        "图幅受支持",
        f"未知图幅: {preferred_paper}",
    )
    if not (part_ok and nominal_ok and projection_ok and paper_ok):
        return {
            "schema": PLAN_SCHEMA_VERSION,
            "status": "blocked",
            "stage": "plan",
            "checks": checks,
            "error_code": "DRAWING_PLAN_INPUT_INVALID",
            "manual_review_required": True,
            "retryable": False,
        }

    selection = select_paper_and_scale(
        nominal,
        preferred_paper=preferred_paper,
        allow_upsize=bool(options.get("allow_paper_upsize", True)),
        max_paper=str(options.get("max_paper", "A0")).upper(),
        min_scale=float(options.get("min_scan_scale", PICKABILITY_THRESHOLD_SCALE)),
        projection=projection,
    )
    layout = selection["layout"]
    if layout is None:
        return {
            "schema": PLAN_SCHEMA_VERSION,
            "status": "blocked",
            "stage": "plan",
            "checks": checks,
            "papers_tried": selection["papers_tried"],
            "error_code": selection.get("error_code") or "DRAWING_PLAN_NO_FITTABLE_PAPER",
            "manual_review_required": True,
            "retryable": False,
        }

    check(
        "plan-scale-pickability",
        selection["scale_ok"],
        "视图比例 %.3g ≥ 1:2,允许坐标扫描标注" % layout["scale"],
        "升档至 %s 后比例仍低于 1:2,标注策略降级为仅模型尺寸" % layout["paper_size"],
    )

    strategy = "scan" if selection["scale_ok"] else "model_only"
    overall = assign_overall_tolerances(nominal, grade=grade)
    title_fields, auto_fields = _title_block_fields(frame_spec, layout, evidence)
    technical_requirements = build_technical_requirements(
        frame_spec.get("technical_requirements"),
        grade=grade,
    )

    plan = {
        "schema": PLAN_SCHEMA_VERSION,
        "status": selection["status"],
        "stage": "plan",
        "part": {"path": part_path, "nominal": nominal},
        "frame": {
            "paper_size": layout["paper_size"],
            "projection": projection,
            "template_path": frame_spec.get("template_path"),
            "sheet_format_path": frame_spec.get("sheet_format_path"),
            "sheet": layout["sheet"],
            "title_block_box": layout["title_block_box"],
        },
        "views": layout["views"],
        "gap_m": layout["gap_m"],
        "scale": layout["scale"],
        "scale_ratio": layout["scale_ratio"],
        "scale_ratio_text": scale_ratio_text(layout["scale"]),
        "scale_source": "auto_from_nominal",
        "papers_tried": selection["papers_tried"],
        "dimensioning": {
            "strategy": strategy,
            "overall_dimensions": overall,
        },
        "mating_dimension_candidates": [
            {
                "diameter_mm": item.get("diameter_mm"),
                "source": "brep_cylindrical_face",
                "suggestion": None,
                "review_required": True,
            }
            for item in (evidence.get("holes") or [])
            if item.get("diameter_mm") is not None
        ],
        "title_block": {"fields": title_fields, "auto_fields": auto_fields},
        "technical_requirements": {"lines": technical_requirements},
        "tolerance_grade": grade,
        "checks": checks,
        "error_code": selection["error_code"],
        "manual_review_required": True,
        "retryable": selection["status"] == "review_required",
        "limitations": [
            "计划仅描述意图;尺寸位置、重叠和尺寸链完整性须以导出 PDF/BMP 目视复核",
            "W/H/D 为包络总体尺寸,内部特征定位尺寸须由模型尺寸路线或人工补齐",
            "孔配合建议(如 H7)为二期公差智能化能力,当前一律 review_required",
        ],
    }
    if strategy == "model_only":
        plan["dimensioning"]["fallback_reason"] = (
            "视图比例低于 1:2,坐标扫描标注不可用;仅插入模型尺寸,总体尺寸公差须人工标注"
        )
    return plan
