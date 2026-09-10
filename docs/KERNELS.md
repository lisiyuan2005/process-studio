# 两个仿真内核

工程在**新建时**选择内核，之后不能更改。两个内核的几何表示不同，快照互不可读，换内核等于把之前每一步的含义都改掉，因此这不是一个设置项，而是工程本身的一部分。界面在新建工作区时给出选择，顶栏显示当前工程用的内核，`save_document` 会拒绝任何试图改动它的文档。

| | Level set | Slab (DeviceFlow) |
| --- | --- | --- |
| 来源 | Process Studio 自带内核 | ProcessFlow-Emulator 使用的 DeviceFlow 0.2.0 内核，原样并入 |
| 几何表示 | 均匀网格上的符号距离场 | 精确多边形板层（slab）堆叠 |
| 精度参数 | 网格间距，决定全部结果 | 保形沉积的行走步长；其他操作是精确的 |
| 沉积 | 保形、方向性棱柱、蒸发、填充 | 保形、平面 |
| 刻蚀 | 方向性、各向同性及两者混合（`directional_fraction` 任意值） | 垂直（1）或各向同性（0），不支持中间值 |
| CMP | 可指定材料与停止层 | 平面截断，对所有材料一视同仁 |
| 掩膜 | 无掩膜、Quick Sketch、GDS | 无掩膜、Quick Sketch、GDS |
| 3D 视图 | Marching cubes 等值面 | 直接输出几何三角面，无重采样 |
| 快照 | `.npz`，每材料一个场 | `.dfz`，DeviceFlow 的 ZIP 状态存档 |
| 节点上限 | 2000 万 | 不适用 |

## 怎么选

- 需要混合刻蚀剖面、图形化沉积、有选择性的 CMP，或者要看数值收敛，用 **Level set**。
- 需要膜厚就是给定值、掩膜边缘就在掩膜位置、不想为了分辨薄膜而堆网格，用 **Slab**。它没有网格可收敛，代价是模型能表达的工艺更少。

## 高度约定

两个内核在界面上的高度是一致的：晶圆表面在 z = 0，衬底向下延伸到工程窗口的 `zMin`，沉积向上生长。

DeviceFlow 自己的坐标是从 z = 0 的地板向上生长的，所以 slab 内核把衬底建成 `|zMin|` 厚的一层，并在所有对外的高度上加回 `zMin`。CMP 的 `target_z`、截面的纵轴、3D 的包围盒都按工程高度给出，不需要用户换算。

## 不能做的事会明确报错

slab 内核不会把做不到的工艺近似成别的东西。以下情况直接失败并说明原因：

- `directional_fraction` 不是 0 或 1；
- 沉积模式是 `directional`、`evaporation` 或 `fill`；
- 沉积步骤带掩膜（该内核只能整片沉积，图形化要靠随后的刻蚀）；
- 刻蚀既没有目标深度也没有时间。

CMP 步骤如果带了材料列表或停止层，会在日志里说明该内核的 CMP 不区分材料，然后按平面截断执行。

## 代码位置

```
src/deviceflow/                     并入的 DeviceFlow 0.2.0 源码，未做修改（见 VENDOR.md）
src/process_studio/kernels/base.py  内核接口：初始状态、单步执行、读写状态、三种视图
src/process_studio/kernels/levelset.py  原有 level set 内核的包装
src/process_studio/kernels/slab.py      DeviceFlow 适配：步骤翻译、掩膜、状态存档、视图光栅化
```

`runner.py`、`protocol.py` 只通过这个接口工作，不判断内核 id。新增内核只需实现接口并在 `kernels/__init__.py` 注册。

## 依赖

slab 内核需要 `shapely` 和 `trimesh`，两者已在 `pyproject.toml` 的依赖里，打包脚本也会一起收进 worker。缺少它们时注册表只提供 level set 内核，`describe` 的 `kernels` 列表随之变短，不会假装可用。
