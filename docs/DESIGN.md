# Process Studio 设计说明

## 产品目标

Process Studio 的核心对象不是一张最终结构图，而是“可复用、可分支、可追溯到每一步快照的工艺流程”。版图定义横向结构，Recipe 定义工艺行为，Process Step 只记录本次调用及覆盖参数。

## 分层

```text
Desktop UI
  ├─ Process Flow / Step Details / Process Log
  ├─ 3D / Top View / arbitrary AA–BB section
  └─ Material and Recipe libraries
                 │
Process Engine ──┼─ resolves recipe + step overrides
                 ├─ resolves GDS / Quick Sketch / full-wafer mask
                 └─ saves timing, log and snapshots
                 │
Geometry Kernel ─┼─ multi-material signed-distance state
                 ├─ deposit / selective etch / CMP
                 └─ measurement and visualization adapters
                 │
Persistence ─────┴─ SQLite metadata + compressed NPZ snapshots
```

UI 不直接修改三维数组；它修改 Step/Recipe/Sketch，再由 Process Engine 调用统一内核。这样未来替换成窄带 Level Set、GPU 或更真实的反应模型时，项目格式和界面不必推倒重来。

## 核心数据对象

- `ProjectDefinition`：名称、三维网格、项目 GDS、当前分支。
- `FlowBranch`：有序步骤和父分支/父步骤。
- `ProcessStep`：Recipe 引用、mask 来源、GDS layer/datatype、keep inside/outside、字段覆盖。
- `Recipe`：类型、设备、输出材料、默认参数、各材料响应/停止层。
- `MaterialDefinition`：名称、类别、颜色、不透明度。
- `MaterialState`：每种材料一个 signed-distance field，并维护覆盖优先级。
- `QuickSketch`：按顺序执行 merge/subtract/intersect 的参数化二维图形列表。

## 快照与差分流程

每个成功步骤产生一个压缩 NPZ 快照，SQLite 只保存元数据和引用。创建分支时，分叉点以前的快照由两个分支共同引用；修改后的步骤才重新计算。删除步骤时会删除该步及其下游引用，只有引用计数归零时才删除物理快照文件。

这是结构级复用，不是逐体素增量编码；它已经解决快速工艺迭代的主要等待时间，同时保持实现可验证。若未来项目变大，可在不改变 Flow API 的前提下加入内容哈希、chunk storage 或 Zarr。

## 扩展路线

建议按价值排序：

1. 基于实测数据的 Recipe 标定和单位/不确定度体系。
2. 晶向相关湿法刻蚀与 facet velocity。
3. 窄带/稀疏 Level Set 和多分辨率网格。
4. CMP pattern-density、dishing 与 erosion。
5. 方向分布、shadowing、loading 和 sidewall passivation。
6. PySide/VTK 渲染与更大的交互数据集。

多人模式、自动报告和工艺演化动画不在当前范围内。
