"""
示例: 制造零件图一键生成(计划 → 执行 → 审查三段管线)

对任意零件(含无模型驱动尺寸的导入件/占位件)生成 GB 加工工程图:
图框/标题栏 + 第一角三视图 + W/H/D 坐标扫描标注 + GB/T 1804-m 对称公差 +
技术要求 + PDF 导出 + 三重证据审查。

管线组成:
  ① sw_drawing_plan.plan_manufacturing_drawing  纯计划(图幅/比例/公差/布局)
  ② sw_drawing.generate_manufacturing_drawing   COM 执行(顺序硬约束见该函数注释)
  ③ sw_review.review_manufacturing_drawing      审查门禁(结构/布局/PDF 文字)

修改下方 PART_PATH / 名义尺寸 / 图框配置后即可复用。名义尺寸用于公差分档,
可用 sw_inspect.overall_dimensions 现场测量获得。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from sw_connect import connect_solidworks
from sw_drawing_plan import plan_manufacturing_drawing
from sw_drawing import generate_manufacturing_drawing
from sw_review import review_manufacturing_drawing

# ===== 配置(按你的零件改) =====
PART_PATH = r"C:\parts\mypart.SLDPRT"
OUT_DIR = r"C:\exports"
# 名义尺寸(mm):W=X 向宽、H=Y 向高、D=Z 向深;仅用于 GB/T 1804-m 公差分档
NOMINAL = {"width_mm": 80.0, "height_mm": 60.0, "depth_mm": 40.0}
# 可选:GB 图框(.slddrt)与工程图文档模板(.drwdot);留空则不套图框(告警)
FRAME_SPEC = {
    "paper_size": "A3",              # 放不下可点选比例(≥1:2)时自动升图幅
    "projection": "first_angle",     # GB 第一角
    # "template_path": r"D:\Program Files\solidworks\SOLIDWORKS\data\templates\gb.drwdot",
    # "sheet_format_path": r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\lang\Chinese-Simplified\sheetformat\a3 - gb.slddrt",
    "title_block": {
        "名称": "支架",
        "材料": "Q235-A",
        "数量": 2,
    },
    "technical_requirements": ["调质 28~32HRC。"],
}


def main():
    if not os.path.exists(PART_PATH):
        print("零件不存在: %s(改 PART_PATH 后重试)" % PART_PATH)
        return

    # ① 计划:图幅/比例/视图布局/公差分配/标题栏/技术要求(无 COM,可先看再执行)
    evidence = {"part_path": PART_PATH, "nominal": NOMINAL}
    plan = plan_manufacturing_drawing(evidence, FRAME_SPEC)
    print("计划: status=%s 图幅=%s 比例=%s 策略=%s" % (
        plan["status"], plan["frame"]["paper_size"],
        plan.get("scale_ratio_text"), plan["dimensioning"]["strategy"]))
    for check in plan["checks"]:
        print("  [%s] %s: %s" % (check["status"], check["id"], check["message"]))
    if plan["status"] == "blocked":
        print("计划被阻断: %s" % plan["error_code"])
        return

    sw, _ = connect_solidworks()

    # ② 执行:按计划落图并导出 PDF
    execution = generate_manufacturing_drawing(sw, plan, OUT_DIR)
    print("执行: status=%s" % execution["status"])
    for name, stage in execution["stages"].items():
        print("  [%s] %s%s" % (
            stage.get("status", "?"), name,
            " (%s)" % stage["error_code"] if stage.get("error_code") else ""))
    artifacts = execution.get("artifacts") or {}
    if not artifacts.get("pdf"):
        print("PDF 未导出,终止审查: %s" % execution.get("error_code"))
        return

    # ③ 审查:结构 + 布局碰撞 + PDF 矢量文字重叠 + PNG 目视证据
    report = review_manufacturing_drawing(
        pdf_path=artifacts["pdf"],
        preview_png_path=os.path.splitext(artifacts["pdf"])[0] + "_preview.png",
        report_path=os.path.join(OUT_DIR, "review_report.json"),
    )
    print("审查: status=%s%s" % (report["status"],
          " (%s)" % report["error_code"] if report.get("error_code") else ""))
    print(json.dumps({name: section.get("status") for name, section in report["sections"].items()},
                     ensure_ascii=False))
    print("产物: %s / %s / review_report.json" % (
        artifacts["slddrw"], artifacts["pdf"]))
    print("完成;尺寸位置/重叠/尺寸链请按 review 报告目视复核。")


if __name__ == "__main__":
    main()
