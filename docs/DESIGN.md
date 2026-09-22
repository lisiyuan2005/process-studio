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
Worker (runner) ─┼─ resolves recipe + step overrides
                 ├─ resolves GDS / Quick Sketch / full-wafer mask
                 └─ saves timing, log and snapshots
                 │
Geometry Kernel ─┼─ exact multi-material polygon slabs (DeviceFlow)
                 ├─ deposit / selective etch / oxidation / CMP
                 └─ its own sections, top views and 3D triangles
                 │
Persistence ─────┴─ SQLite metadata + DeviceFlow .dfz snapshots
```

UI 不自己算几何；它修改 Step/Recipe/Sketch，再由 worker 调用内核。视图也是内核给的——截面和俯视图是 worker 画出来的图片，3D 是内核给的三角面。这样换内核或者换更真实的工艺模型时，项目格式和界面不必推倒重来：内核接口在 `kernels/base.py`，工程记住自己用的是哪个。

## 核心数据对象

- `ProjectDefinition`：名称、工程窗口（x/y/z 范围）、几何分辨率、fidelity、项目 GDS、当前分支、保存的截面线。
- `FlowBranch`：有序步骤和父分支/父步骤。
- `ProcessStep`：Recipe 引用、mask 来源、GDS layer/datatype、keep inside/outside、字段覆盖。
- `Recipe`：类型、设备、输出材料、默认参数、各材料响应/停止层。
- `MaterialDefinition`：名称、类别、颜色、不透明度。
- `QuickSketch`：按顺序执行 merge/subtract/intersect 的参数化二维图形列表。

几何状态本身不是 Process Studio 的数据结构，而是内核的：slab 内核的状态是每种材料一组精确多边形板层，存成 DeviceFlow 自己的 `.dfz`。上层只知道"能存能读、能跑一步、能出三种视图"。

## 快照与差分流程

每个成功步骤产生一个快照文件（`.dfz`），SQLite 只保存元数据和引用。创建分支（**工艺分叉**，界面上的 Branch 菜单）时，分叉点以前的快照由两个分支共同引用，两档 fidelity 的结果都带过去；修改后的步骤才重新计算。删除步骤或删除分支时会删除该步及其下游引用，只有引用计数归零时才删除物理快照文件。

这是结构级复用，不是逐层增量编码；它已经解决快速工艺迭代的主要等待时间，同时保持实现可验证。步骤是否要重算由摘要链决定：改哪一步就只重算那一步之后的部分，改名字不算改。

## 扩展路线

建议按价值排序：

1. 每个 tool 两套参数（simulation / experiment）和单位体系，导出时可选。
2. Tool 自己的 recipe 库（如 ALD 的 `Siva_HZO_300C`），与流程模板分开。
3. 背面工艺（翻片）与更自由的衬底定义。
4. 晶向相关湿法刻蚀与 facet velocity。
5. CMP pattern-density、dishing 与 erosion。
6. 方向分布、shadowing、loading 和 sidewall passivation。

多人模式、自动报告和工艺演化动画不在当前范围内。
