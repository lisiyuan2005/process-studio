# Process Studio 设计说明

## 产品目标

Process Studio 的核心对象不是一张最终结构图，而是“可复用、可分支、可追溯到每一步快照的工艺流程”。版图定义横向结构，step template 定义可复用的工艺设置，Process Step 记录这一次要建什么（simulation）和机器实际设成什么（experiment）。

## 分层

```text
Desktop UI
  ├─ Process Flow / Step Details / Process Log
  ├─ 3D / Top View / arbitrary AA–BB section
  └─ Material, tool and step-template libraries
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
- `ProcessStep`：工艺类型与参数（simulation），可选的 experiment 参数、mask 来源、GDS layer/datatype、keep inside/outside。
- `Recipe`（界面上叫 **step template**）：类型、设备、输出材料、两套参数、各材料响应/停止层。
- `ToolDefinition`：机器的名字、分组、装在上面的 recipe 列表、备注。
- `MaterialDefinition`：名称、类别、颜色、不透明度。
- `QuickSketch`：按顺序执行 merge/subtract/intersect 的参数化二维图形列表。

几何状态本身不是 Process Studio 的数据结构，而是内核的：slab 内核的状态是每种材料一组精确多边形板层，存成 DeviceFlow 自己的 `.dfz`。上层只知道"能存能读、能跑一步、能出三种视图"。

## 快照与差分流程

每个成功步骤产生一个快照文件（`.dfz`），SQLite 只保存元数据和引用。创建分支（**工艺分叉**，界面上的 Branch 菜单）时，分叉点以前的快照由两个分支共同引用，两档 fidelity 的结果都带过去；修改后的步骤才重新计算。删除步骤或删除分支时会删除该步及其下游引用，只有引用计数归零时才删除物理快照文件。

这是结构级复用，不是逐层增量编码；它已经解决快速工艺迭代的主要等待时间，同时保持实现可验证。步骤是否要重算由摘要链决定：改哪一步就只重算那一步之后的部分，改名字不算改。

## 扩展路线

建议按价值排序：

1. 晶向相关湿法刻蚀与 facet velocity。
2. CMP pattern-density、dishing 与 erosion。
3. 方向分布、shadowing、loading 和 sidewall passivation。
4. 基于实测数据的工艺标定和不确定度体系。
5. 键合（把两片的正面对起来），翻片已经有了，缺的是把两个 state 合成一个。

多人模式、自动报告和工艺演化动画不在当前范围内。
