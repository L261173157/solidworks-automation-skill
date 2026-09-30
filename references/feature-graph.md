# Feature Graph IR 词表速览 (v1.2 / v0.3)

Feature Graph IR 是 AI 意图与 COM 调用之间的确定性中间层: AI 只产出符合
`apps/desktop/cad_workbench/schemas/feature_graph.schema.json` 的 IR 文档,
`scripts/feature_graph.py` 负责 `validate_ir` (结构校验) → `lower_to_calls`
(确定性降级, 无 LLM 参与) → `build_from_ir` (按计划建模 + 特征名追踪)。
入口是 MCP 工具 `solidworks_submit_feature_graph` (仅 reviewed 模式)。

**单位**: 长度一律毫米 (`_mm` 后缀), 角度一律度 (`_deg` 后缀)。
**引用**: 特征以 `id` 先向后引用 (禁止前向); 顶层 `schemaVersion` 必须是 `"1.2"`。

## 特征词表 (features[].op)

| op | 必填字段 | 可选字段 | 语义要点 |
|---|---|---|---|
| `extrude_boss` | `id, sketch, depth_mm` | `midplane, flip` | 面上草图默认拉伸方向可能朝材料内, 用 `flip: true` 显式翻转 |
| `extrude_cut` | `id, sketch` | `depth_mm, through_all, flip` | `through_all: true` 时无需 `depth_mm` |
| `revolve_boss` | `id, sketch` | `angle_deg` (默认 360) | 旋转轴 = 草图中**恰 1 条** `centerline`; 半剖轮廓一次成轴类件 |
| `fillet` | `id, radius_mm, edges` | — | `edges` 是非空 edge SelectionSpec 数组, 歧义/未命中即停, 绝不猜测 |
| `chamfer` | `id, distance_mm, edges` | `angle_deg` (默认 45) | 同上 |
| `linear_pattern` | `id, target, direction, spacing_mm, count` | `direction2, spacing2_mm, count2` | `target` 引用先前特征 id; `direction: {sketch: <先前特征id>}`, 该草图恰含 1 条 `centerline` 作方向 1 实体 (mark 1)。`direction2` 提供时 `spacing2_mm/count2` 必填, 且必须引用**不同于** direction 的草图 (mark 2), 双向网格 |

## 草图 shape 词表 (sketch.shapes[])

| shape | 字段 | 说明 |
|---|---|---|
| `rectangle` | `x1, y1, x2, y2` | 对角矩形, 草图局部坐标 |
| `centerline` | `x1, y1, x2, y2` | 构造中心线: 不参与轮廓; revolve 的轴 / linear_pattern 方向 1 与方向 2 的实体 |
| `circle` | `cx, cy, r` | |
| `circle_array` | `cx, cy, orbit_mm, r_hole_mm, count` (+`start_angle_deg`) | 圆周阵列在草图层确定性展开为逐个圆, 不做特征级阵列 |

## 锚点与边引用 (SelectionSpec)

`sketch.plane` (基准面名) 与 `sketch.face_anchor` (SelectionSpec) 二选一。
SelectionSpec `kind`: `coordinate` (需 `point_mm`) / `plane` / `face` / `named` /
`edge` (fillet/chamfer 边引用; 可带 `geometry_signature`/`nth`/`feature` 限定)。

## 反向门 (collect_ir)

`collect_ir(model)` 枚举特征树生成带回读参数的 IR 骨架: 往返等价仅对回读字段
承诺 (深度/半径/距离/角度/间距/数量, 含方向 2 的数量/间距, 单位已换算);
草图轮廓与特征引用关系不回读, `unknown` 节点需人工判读。
linear_pattern 尺寸后缀 (真机实测): D1=数量1, D2=数量2, D3=间距1, D4=间距2;
单方向阵列无 D2/D4, 回读为 null。

## 已知边界 (与 capabilities.yaml 同步)

- 引用锚点 (face_anchor/边线) 不保证上游编辑后存活: 重建语义, 不是编辑语义。
- 特征级 linear_pattern 底层是 `FeatureLinearPattern3` 10 参真机口径
  (DName 传字面量 `"NULL"`, 方向 1/2 实体对象级预选 mark 1/mark 2), 见
  `references/part-modeling.md` 阵列特征一节。
- **方向实体仅支持已消费草图构造中心线段**: 基准轴/模型边线虽见诸 API 文档
  口径, 但 `FeatureLinearPattern3` 晚绑定路径下被真机否定
  (GetErrorCode=51 swSketchErrorExtRefFail, 实例坍缩; D1AxisType 中心线段=3/
  边线=1/基准轴=0), 词表因此只接受 `{sketch: id}` 形态, 见
  `tests/probe_dir2_axis.py` 与 `references/part-modeling.md`。
- 真机验证门: 直调参照件 + IR 试点件双路对比 verified + 闭式解体积校验
  (偏差 ≤2%), 见 `tests/solidworks_feature_graph_regression.py`。
