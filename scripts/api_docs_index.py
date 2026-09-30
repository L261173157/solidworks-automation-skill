"""SolidWorks API 签名索引: 从本机类型库提取接口/成员事实, 供 Agent 现查现用。

借鉴 andrewbartels1/SolidworksMCP-python 的 API 文档查询工具思想 (全部全新实现):
- 数据源是本机已安装的 SolidWorks 类型库 (sldworks.tlb 等), 只提取
  名称/调用类别/参数个数这类事实, 不复制帮助文档文本, 无许可证问题;
- ``references/api-lookup.md`` 的人工查证记录以 CURATED_NOTES 合并进结果;
- 索引离线可用: 生成一次 (references/data/api_index.json), MCP 工具零网络查询。

用法::

    python scripts/api_docs_index.py --output            # 生成/刷新索引文件
    python scripts/api_docs_index.py --lookup AddMate5   # 命令行查询
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

try:
    from .sw_capability_probe import _find_typelib
    from .sw_preflight import import_com_dependencies
except ImportError:  # 直接以 scripts/ 为工作目录导入
    from sw_capability_probe import _find_typelib
    from sw_preflight import import_com_dependencies

TYPELIB_PATTERNS = [
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldworks.tlb",
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\**\sldworks.tlb",
]

# 常用接口白名单 (自动化最常触达的表面; 枚举与冷门接口不入索引)
WHITELIST_INTERFACES = {
    "ISldWorks",
    "IModelDoc2",
    "IPartDoc",
    "IAssemblyDoc",
    "IDrawingDoc",
    "IFeature",
    "IFeatureManager",
    "ISketchManager",
    "ISketch",
    "IModelDocExtension",
    "ISelectionManager",
    "IComponent2",
    "IBody2",
    "IFace2",
    "IEdge",
    "IVertex",
    "ISurface",
    "IConfiguration",
    "IConfigurationManager",
    "IDimension",
    "IDisplayDimension",
    "IModelView",
    "IModelViewManager",
    "IPackAndGo",
    "IMathTransform",
    "IMathVector",
    "IMate2",
    "IMateFeatureData",
    "IInterferenceDetectionMgr",
    "ISheetMetalFeatureData",
    "IMotionStudyManager",
    "IMotionStudy",
    "IMotionStudyProperties",
    "ITableAnnotation",
    "IBomTableAnnotation",
    "IView",
    "ISheet",
    "INote",
    "IAnnotation",
    "ILayer",
    "IEnumBodies",
    "IEnumFaces2",
    "IEnumEdges",
    "ITransform",
}

# references/api-lookup.md 的人工真机查证记录 (仅事实, 不含帮助文本)
CURATED_NOTES: dict[str, str] = {
    "ISldWorks.AddMate5": "真机手记: 15 参数签名, 最后一个 ErrorStatus 为 by-ref 整型; swAddMateError_NoError=1, by-ref 返回 1 不算失败",
    "IComponent2.GetCorresponding": "真机手记: 把零件文档对象映射到装配体上下文; 同一零件多实例时必须由目标组件实例调用",
    "ISurface.IsCylinder": "真机手记: 识别圆柱面 (轴/孔/齿轮); CylinderParams[6] 是半径, 单位米",
    "IComponent2.SetTransformAndSolve2": "真机手记: 驱动组件位置并让装配求解器更新; 不稳定时优先复用组件现有 Transform2 改 ArrayData",
    "IModelDocExtension.GetMotionStudyManager": "真机手记: Motion Study 强类型接口在独立类型库 swmotionstudy.tlb",
    "ISldWorks.LoadFile4": "真机手记: 导入外来 CAD 文件用, 不支持 OpenDoc6 的 silent 选项; 需动态代理传递 by-ref VARIANT",
    "IFeatureManager.FeatureLinearPattern3": "真机手记 (SW2024 SP5): 10 参 (Num1, Spacing1, Num2, Spacing2, FlipDir1, FlipDir2, DName1, DName2, GeometryPattern, VaryInstance); DName1/DName2 传字面量 \"NULL\", 方向实体走预选: 方向1=mark 1 / 方向2=mark 2, 种子特征=mark 4 (BODYFEATURE); 已消费草图中的构造中心线段 (对象级 Select2) 可作方向实体。2026-09-30 补充: 方向 2 (mark 2) 双向网格真机验证成立 (4x3 闭式解 0.000%); 尺寸后缀 D1=数量1, D2=数量2, D3=间距1(米), D4=间距2(米)。方向实体仅中心线段可用: 基准轴 (SelectByID2 \"AXIS\"/对象级/mark 128/RefAxis 底层对象/DName 传轴名) 与模型边线 (对象级/按坐标 SelectByID2 \"EDGE\", 含官方示例口径) 均被拒绝 — 特征可建但 GetErrorCode()=51 (swFeatureErrorExtRefFail), GetDefinition().GetD1AxisType() 轴=0/边线=1/中心线段=3, 实例坍缩为种子",
    "IModelDoc2.InsertAxis": "真机手记 (SW2024 SP5): 预选两个基准面 (SelectByID2 PLANE) 后, 交线处创建基准轴特征 (名字 Axis1, GetTypeName2=\"RefAxis\"); 晚绑定 dynamic dispatch 下 InsertAxis 表现为属性 — 属性读取即触发插入并返回 bool, get_com_member 兼容属性/方法两种形态; 该轴可被 FeatureCircularPattern4 消费 (mark 1), 但 FeatureLinearPattern3 拒绝其作方向实体 (见上条)",
    "IModelDoc2.FirstFeature": "真机手记 (SW2024 SP5): 特征枚举成员名是 FirstFeature (0 参), GetFirstFeature 不存在; 链式 GetNextFeature; 特征尺寸回读用 IModelDoc2.Parameter(\"Dn@特征名\").SystemValue (长度米/角度弧度), IFeature.GetDimensions 在 SW2024 类型库不存在",
}

INVOKE_KINDS = {1: "method", 2: "propget", 4: "propput", 8: "propputref"}


def _find_sldworks_typelib() -> Path | None:
    return _find_typelib(TYPELIB_PATTERNS)


def _enumerate_typelib(pythoncom, typelib_path: Path) -> dict[str, Any]:
    """提取白名单接口的成员事实: {接口: {成员: {"kind", "params"}}}。"""
    library = pythoncom.LoadTypeLib(str(typelib_path))
    interfaces: dict[str, Any] = {}
    for index in range(library.GetTypeInfoCount()):
        try:
            name = str(library.GetDocumentation(index)[0] or "")
        except Exception:  # noqa: BLE001
            continue
        if name not in WHITELIST_INTERFACES:
            continue
        typeinfo = library.GetTypeInfo(index)
        try:
            attr = typeinfo.GetTypeAttr()
            # pywin32 的 TYPEATTR 是位置元组, 实测布局 (SW2024 SP5):
            # [0]=IID [4]=cbSizeInstance [5]=typekind [6]=cFuncs [7]=cVars [8]=cImplTypes
            func_count = int(attr[6])
        except Exception:  # noqa: BLE001
            continue
        members: dict[str, Any] = {}
        for func_index in range(func_count):
            try:
                # pywin32 的 FUNCDESC 位置元组 (SW2024 SP5 实测):
                # [0]=memid [2]=参数 TYPEDESC 元组 (长度即参数个数)
                # [4]=invkind (1=method 2=propget 4=propput)
                func_desc = typeinfo.GetFuncDesc(func_index)
                memid = int(func_desc[0])
                invkind = int(func_desc[4])
                param_descs = tuple(func_desc[2] or ())
                c_params = len(param_descs)
                names = typeinfo.GetNames(memid)
                member_name = str(names[0]) if names else ""
            except Exception:  # noqa: BLE001
                continue
            if not member_name:
                continue
            kind = INVOKE_KINDS.get(invkind, f"invkind{invkind}")
            entry = {"kind": kind, "params": c_params}
            existing = members.get(member_name)
            if existing is None:
                members[member_name] = entry
            else:
                # 同名重载/属性对: 保留参数最多的形态
                if c_params > existing.get("params", 0):
                    members[member_name] = entry
        if members:
            interfaces[name] = members
    return interfaces


def build_index(typelib_path: Path | None = None, sw_revision: str | None = None) -> dict[str, Any]:
    """生成完整索引结构 (不落盘)。"""
    pythoncom, _win32com, _variant = import_com_dependencies()
    resolved = typelib_path or _find_sldworks_typelib()
    if resolved is None:
        raise RuntimeError(
            "未找到 sldworks.tlb; 请在装有 SolidWorks 的机器上生成索引 (可与安装探测无关地手动传 typelib_path)"
        )
    interfaces = _enumerate_typelib(pythoncom, resolved)
    return {
        "schemaVersion": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "typelib": str(resolved),
        "sw_revision": sw_revision,
        "interface_count": len(interfaces),
        "member_count": sum(len(members) for members in interfaces.values()),
        "interfaces": interfaces,
        "curated_notes": CURATED_NOTES,
        "limitations": [
            "仅含类型库签名事实 (名称/调用类别/参数个数), 不含帮助文档文本",
            "参数个数含 by-ref 出参; 调用时以类型库为准并结合 api-lookup.md 查证",
        ],
    }


def default_index_path() -> Path:
    return ROOT / "references" / "data" / "api_index.json"


def generate_index_file(output_path: Path | None = None, sw_revision: str | None = None) -> Path:
    """生成并落盘索引文件, 返回路径。"""
    index = build_index(sw_revision=sw_revision)
    target = output_path or default_index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def load_index(index_path: Path | None = None) -> dict[str, Any]:
    """加载已生成的索引; 文件缺失时返回空索引 (不阻塞调用方)。"""
    path = index_path or default_index_path()
    if not path.is_file():
        return {"schemaVersion": "1.0", "interfaces": {}, "curated_notes": CURATED_NOTES}
    return json.loads(path.read_text(encoding="utf-8"))


def lookup(query: str, *, index: dict[str, Any] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """按成员/接口名做大小写不敏感的包含匹配, 返回命中的签名事实。

    结果项: {"interface", "member", "kind", "params", "note" (可人工查证记录)}。
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    source = index if index is not None else load_index()
    interfaces: dict[str, Any] = source.get("interfaces", {})
    curated: dict[str, str] = source.get("curated_notes", {})
    hits: list[dict[str, Any]] = []
    for interface_name in sorted(interfaces):
        members: dict[str, Any] = interfaces.get(interface_name, {})
        interface_match = needle in interface_name.lower()
        for member_name in sorted(members):
            if needle in member_name.lower() or interface_match:
                entry = members[member_name]
                hits.append(
                    {
                        "interface": interface_name,
                        "member": member_name,
                        "kind": entry.get("kind"),
                        "params": entry.get("params"),
                        "note": curated.get(f"{interface_name}.{member_name}"),
                    }
                )
                if len(hits) >= limit:
                    return hits
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="SolidWorks API 签名索引")
    parser.add_argument("--output", default=None, help="索引输出路径 (默认 references/data/api_index.json)")
    parser.add_argument("--typelib", default=None, help="显式指定 sldworks.tlb 路径")
    parser.add_argument("--revision", default=None, help="记录的 SolidWorks 版本号 (如 32.5.0)")
    parser.add_argument("--lookup", default=None, help="查询成员/接口名后退出")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if args.lookup:
        hits = lookup(args.lookup, limit=args.limit)
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0
    output = generate_index_file(Path(args.output) if args.output else None, sw_revision=args.revision)
    index = load_index(output)
    print(
        json.dumps(
            {
                "status": "pass",
                "output": str(output),
                "interface_count": index.get("interface_count"),
                "member_count": index.get("member_count"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
