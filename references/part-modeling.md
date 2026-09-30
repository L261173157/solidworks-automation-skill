# 零件建模 API 参考

## 草图操作

### 基准面选择

```python
# 英文版
model.Extension.SelectByID2("Front Plane", "PLANE", 0, 0, 0, False, 0, None, 0)
# 中文版
model.Extension.SelectByID2("前视基准面", "PLANE", 0, 0, 0, False, 0, None, 0)
```

基准面对应关系：
| 英文 | 中文 | 法线方向 |
|---|---|---|
| Front Plane | 前视基准面 | Z 轴 |
| Top Plane | 上视基准面 | Y 轴 |
| Right Plane | 右视基准面 | X 轴 |

### 草图几何体方法速查

| 方法 | 参数 | 说明 |
|---|---|---|
| `CreateLine(x1,y1,z1, x2,y2,z2)` | 起点终点坐标（米） | 直线 |
| `CreateCircleByRadius(cx,cy,cz, r)` | 圆心+半径 | 圆 |
| `CreateCenterRectangle(cx,cy,cz, rx,ry,rz)` | 中心+角点 | 中心矩形 |
| `CreateCornerRectangle(x1,y1,z1, x2,y2,z2)` | 对角点 | 角点矩形 |
| `CreateArc(cx,cy,cz, x1,y1,z1, x2,y2,z2, dir)` | 圆心+起终点+方向 | 圆弧 |
| `CreatePolygon(cx,cy,cz, sx,sy,sz, sides, inscr)` | 中心+顶点+边数 | 正多边形 |
| `CreateEllipse(cx,cy,cz, mx,my,mz, r)` | 中心+长轴端点+短轴半径 | 椭圆 |
| `CreateSpline2(pointArray, closed)` | 点数组+是否闭合 | 样条曲线 |
| `CreateSketchSlot(type, w, w, ...)` | 类型+宽度+端点 | 槽口 |

### 草图约束

```python
# 选中实体后添加约束
model.SketchAddConstraints("sgHORIZONTAL")
```

常用约束类型：`sgFIXED`, `sgHORIZONTAL`, `sgVERTICAL`, `sgCOLINEAR`, `sgPARALLEL`, `sgPERPENDICULAR`, `sgTANGENT`, `sgCONCENTRIC`, `sgEQUAL`, `sgSYMMETRIC`, `sgMIDPOINT`, `sgCOINCIDENT`

### 草图选择稳定策略

优先使用 `sw_part.sketch()` 上下文或 `end_sketch()` 返回值，不要手写 `SelectByID2("Sketch1", "SKETCH", ...)` 作为唯一选择方式。SolidWorks 2024 中文版反馈过 SKETCH 名称选择持续返回 `False` 的情况。

推荐：

```python
with sketch(model, "Front Plane") as sketch_name:
    sketch_circle(model, 0, 0, mm(25))
feature = extrude_boss(model, sketch_name, mm(50))
```

手动流程：

```python
start_sketch(model, "Front Plane")
sketch_circle(model, 0, 0, mm(25))
sketch_ref = end_sketch(model)
feature = extrude_boss(model, sketch_ref, mm(50))
```

内部规则：`extrude_boss()` / `extrude_cut()` 会按“活动草图引用 -> 创建时缓存引用 -> FeatureByName -> SelectByID2("SKETCH")”顺序选择草图；不再把残留选择集当作草图已选中的证据。

## 特征操作

### FeatureExtrusion3 参数详解

```python
feature_mgr.FeatureExtrusion3(
    Sd,           # bool: 单方向（True）或双方向（False）
    Flip,         # bool: 翻转切割方向
    Dir,          # bool: 翻转挤出方向
    T1,           # int: 终止条件1 (见下表)
    T2,           # int: 终止条件2（双方向时使用）
    D1,           # float: 深度1（米）
    D2,           # float: 深度2
    Dchk1,        # bool: 拔模1
    Dchk2,        # bool: 拔模2
    Ddir1,        # bool: 拔模方向1向外
    Ddir2,        # bool: 拔模方向2向外
    Dang1,        # float: 拔模角度1（弧度）
    Dang2,        # float: 拔模角度2
    OffsetReverse1, # bool: 偏移反转1
    OffsetReverse2, # bool: 偏移反转2
    TranslateSurface, # bool: 平移曲面
    Merge,        # bool: 合并结果
    UseFeatScope, # bool: 自动选择实体范围
    UseAutoSelect, # bool: 自动选择
    T0,           # bool:
    StartOffset   # float: 起始偏移
)
```

终止条件枚举：
| 值 | 名称 | 说明 |
|---|---|---|
| 0 | swEndCondBlind | 给定深度 |
| 1 | swEndCondThroughAll | 完全贯穿 |
| 2 | swEndCondThroughAllBoth | 两侧贯穿 |
| 5 | swEndCondUpToSurface | 成形到一曲面 |
| 6 | swEndCondMidPlane | 两侧对称 |
| 7 | swEndCondUpToBody | 成形到实体 |

### FeatureCut4 参数详解

与 FeatureExtrusion3 类似，但用于切除操作。额外参数：
- `NormalCut`: 法向切除
- `FlipSide`: 翻转切除侧

### FeatureRevolve2 参数

```python
feature_mgr.FeatureRevolve2(
    SingleDir,     # bool: 单方向
    IsSolid,       # bool: 实体旋转（True）
    IsThin,        # bool: 薄壁旋转
    IsCut,         # bool: 切除旋转
    ReverseDir,    # bool: 反转方向
    BothDirectionUpToSameEntity, # bool
    Dir1Type,      # int: 终止条件（0=Blind）
    Dir2Type,      # int
    Dir1Angle,     # float: 旋转角度（弧度）
    Dir2Angle,     # float
    OffsetReverse1, # bool
    OffsetReverse2, # bool
    OffsetDistance1, # float
    OffsetDistance2, # float
    ThinType,      # int: 薄壁类型
    ThinThickness1, # float
    ThinThickness2, # float
    Merge,         # bool
    UseFeatScope,  # bool
    UseAutoSelect  # bool
)
```

### 阶梯轴建模推荐流程

轴类零件优先用“半剖轮廓 + 中心线 + 旋转凸台”一次生成主体，不要逐段拉伸圆柱再合并。这样特征树更短，轴肩位置和总长也更容易由参数表控制。

推荐做法：

1. 用毫米参数表描述轴段：`start_mm`、`end_mm`、`diameter_mm`。
2. 在 `Front Plane` 上画中心线作为旋转轴。
3. 按轴段半径生成一条闭合半剖轮廓。
4. 调用 `FeatureRevolve2(..., 2*pi, ...)` 后立即检查返回特征不是 `None`。
5. 将公差、形位公差、粗糙度作为工程要求记录到属性或交付说明，不要直接扰动名义实体尺寸。

最小模式：

```python
segments = [
    {"start": 0, "end": 43, "diameter": 56},
    {"start": 43, "end": 113, "diameter": 65},
]

with sketch(model, "Front Plane") as sketch_name:
    model.SketchManager.CreateCenterLine(mm(-5), 0, 0, mm(120), 0, 0)
    points = [(0, 0), (0, 28), (43, 28), (43, 32.5), (113, 32.5), (113, 0), (0, 0)]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        model.SketchManager.CreateLine(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0)

model.ClearSelection2(True)
model.Extension.SelectByID2(sketch_name, "SKETCH", 0, 0, 0, False, 0, create_empty_dispatch_variant(), 0)
feature = model.FeatureManager.FeatureRevolve2(
    True, True, False, False, False, False,
    0, 0, 2 * math.pi, 0,
    False, False, 0, 0, 0, 0, 0,
    True, True, True,
)
if feature is None:
    raise RuntimeError("阶梯轴旋转主体创建失败")
```

### 圆柱表面长圆槽

图纸中的键槽、长圆槽、局部沉槽如果位于圆柱外表面，不要直接在中心基准面上切除。中心面切除容易把轴“剖开”，预览看起来像大平面开口，而不是表面凹槽。

稳定做法：

1. 按槽所在圆柱半径创建平行于 `Front Plane` 的偏置参考面，偏置量约等于目标圆柱半径。
2. 在该切向参考面上用 `sketch_slot()` 画长圆槽。
3. 用盲切深度表达槽深；先尝试一个方向，失败再翻转 `flip`。
4. 创建成功后隐藏临时参考面，避免预览图被橙色基准面干扰。

```python
plane = model.FeatureManager.InsertRefPlane(8, mm(radius_mm), 0, 0, 0, 0)
plane.Name = "Plane_Keyway_Tangent"

model.Extension.SelectByID2(plane.Name, "PLANE", 0, 0, 0, False, 0, create_empty_dispatch_variant(), 0)
model.SketchManager.InsertSketch(True)
sketch_name = model.SketchManager.ActiveSketch.Name
sketch_slot(model, mm(x1), 0, mm(x2), 0, mm(slot_width_mm / 2))
model.SketchManager.InsertSketch(True)

feature = extrude_cut(model, sketch_name, mm(depth_mm), direction=True, flip=True)
if feature is None:
    feature = extrude_cut(model, sketch_name, mm(depth_mm), direction=True, flip=False)
if feature is None:
    raise RuntimeError("圆柱表面长圆槽创建失败")
```

注意：`CreateSketchSlot` 参数中 `radius` 是槽宽相关参数，实际表现需用局部测试验证。对制造精确槽宽要求高时，导出后用测量或工程图复核。

### 圆角/倒角

```python
# 圆角 FeatureFillet 参数
feature_mgr.FeatureFillet(
    Options,   # int: 195 = 常用默认值
    R1,        # float: 半径（米）
    R2,        # float: 第二半径
    R3,        # float: 第三半径
    Rarray1,   # variant: 半径数组1
    Rarray2,   # variant: 半径数组2
    Rarray3    # variant: 半径数组3
)

# 倒角 InsertFeatureChamfer
feature_mgr.InsertFeatureChamfer(
    Options,     # int: 4 = 角度距离
    ChamferType, # int: 1 = 等距离
    Width,       # float: 距离（米）
    Angle,       # float: 角度（弧度）
    OtherDist,   # float: 另一距离
    VertexDist1, # float
    VertexDist2, # float
    VertexDist3  # float
)
```

### 圆角/倒角当前可靠边界

圆角和倒角的核心难点不是 API 参数本身，而是**稳定选择目标边线/面**。不同 SolidWorks 版本、界面语言、特征顺序和草图轮廓都会改变自动命名的 `Edge`/`Face`，因此不要把“随机坐标 SelectByID2 选边 + FeatureFillet”当成高可靠流程。

推荐策略：

1. 基准 demo 和无人值守批量脚本不要把圆角/倒角作为成功标准。
2. 需要圆角时，优先在建模阶段用更稳定的草图轮廓近似，例如圆弧、槽口、圆角矩形草图。
3. 必须做特征圆角/倒角时，先通过 body/face/edge 枚举按几何条件过滤目标边，再选择实体对象调用特征，不要依赖临时的 `Edge1` 名称。
4. 圆角失败应降级为保留直边模型，并在审查报告或最终说明中标注“外观圆角未应用”，不要让模型生成流程整体失败。

补充真机实测 (SW2024 SP5, 2026-09-30)：`FeatureFillet` / `InsertFeatureChamfer` 的目标边用**对象级 Select2(append, mark=1)** 选择可行（`_select_com_object` 封装），边过滤按"GetCurve.IsLine + 端点坐标主方向"谓词，闭式解体积校验 0.000% 偏差。

## 阵列特征 (真机查证 2026-09-30, SW2024 SP5)

### FeatureLinearPattern3 (IFeatureManager, 10 参)

```python
feature = model.FeatureManager.FeatureLinearPattern3(
    Num1,     # int: 方向 1 实例数 (含原始)
    Spacing1, # float: 方向 1 间距 (米)
    Num2,     # int: 方向 2 实例数 (含原始), 单方向时传 1
    Spacing2, # float: 方向 2 间距 (米), 单方向传任意正值如 0.01
    FlipDir1, # bool: 反转方向 1
    FlipDir2, # bool: 反转方向 2
    DName1,   # str: 字面量 "NULL" (官方 VB.NET 示例口径)
    DName2,   # str: 字面量 "NULL"
    GeometryPattern,  # bool: 几何阵列
    VaryInstance,     # bool: 可变实例
)
```

**方向不来自参数，来自预选**（与 `FeatureCircularPattern4` 的预选 AXIS 同构）：

```python
model.ClearSelection2(True)
# 1) 种子特征: SelectByID2(name, "BODYFEATURE", 0,0,0, False, 4, callout, 0)  # mark 4
# 2) 方向 1 实体: 对象级 Select2(append=True, mark=1)  # mark 1; 方向 2 用 mark 2
#    方向实体可以是: 模型边线、基准轴、或**已消费草图中的构造中心线段**
#    (晚绑定下用 SketchSelectionRef.sketch.GetSketchSegments 枚举,
#     按 ConstructionGeometry == True 定位; GetType 可能恒返回 0 不可依赖,
#     GetEntityName 对草图段返回空串, 对象级选择即可)
```

真机验证: 底板草图 (矩形 + 构造中心线) 拉伸消费后, 中心线段作方向实体,
`FeatureLinearPattern3(4, 0.015, 1, 0.01, False, False, "NULL", "NULL", False, False)`
创建 4 实例阵列, 改名 + ForceRebuild3 + FeatureByName 回读持久化通过, 闭式解体积校验 0.000% 偏差。

坑: 旧代码按 14 参、方向传 `str(dx)`/`str(dy)`/`str(dz)` 的写法是错的
(实际是 `FeatureLinearPattern4` 的 20 参口径且 DName 是尺寸名而非向量), 会报
`Invalid number of parameters`。

### 特征类型名与尺寸回读 (GetTypeName2 / Parameter)

| type 名 | op | 可回读尺寸 (model.Parameter, SystemValue) |
|---|---|---|
| `"Extrusion"` / `"ICE"` | extrude_boss | `D1@<特征>` = 深度 (米) |
| `"Cut"` | extrude_cut | 深度 |
| `"Revolution"` | revolve_boss | `D1@<特征>` = 角度 (弧度) |
| `"Fillet"` | fillet | `D1@<特征>` = 半径 (米) |
| `"Chamfer"` | chamfer | `D1` = 距离 (米), `D2` = 角度 (弧度) |
| `"LPattern"` | linear_pattern | `D1` = 方向 1 实例数 (无量纲), `D3` = 方向 1 间距 (米) |

注意: `IFeature.GetDimensions` 与 `IModelDoc2.GetFirstFeature` 在 SW2024 类型库不存在
(晚绑定 Member not found); 特征枚举用 `FirstFeature` -> `GetNextFeature` 链。

## SelectByID2 实体类型

| 类型字符串 | 说明 |
|---|---|
| `"PLANE"` | 基准面 |
| `"FACE"` | 面 |
| `"EDGE"` | 边线 |
| `"VERTEX"` | 顶点 |
| `"SKETCH"` | 草图 |
| `"BODYFEATURE"` | 实体特征 |
| `"COMPONENT"` | 组件 |
| `"AXIS"` | 轴线 |
| `"SKETCHSEGMENT"` | 草图线段 |
| `"SKETCHPOINT"` | 草图点 |
| `"EXTSKETCHSEGMENT"` | 外部草图线段 |
| `"REFERENCECURVES"` | 参考曲线 |
