# 同步分块与二阶界面推进

这一阶段实现的是动态 AMR 的数值基础，不是已经完成的多层/稀疏 AMR。

## 为什么需要这一层

上一个阶段的局部计算依靠足够大的影响范围隔离边界。这可以验证正确性，但工艺流程越长，安全区域可能越大。真正按块推进时，不能让每块独立跑完整个工艺再拼接；相邻块必须使用同一时间层的边界数据。

新增的 transport 内核在同一均匀网格上分块执行。每个时间子步只读取一份不可变的输入场，读取相邻块的 ghost 节点，把结果写入另一份场的互不重叠区域。二阶方案每个子步需要两层 ghost；SSP-RK2 的第二个子步重新读取完整的第一子步状态。改变块大小、遍历顺序，不应改变求解结果。

ghost 的通用实现原则可参阅 [Clawpack 的边界与计算阶数说明](https://www.clawpack.org/dev/setrun.html)。这里采用项目自己的 Hamilton–Jacobi 差分实现，并未引入 Clawpack 运行依赖。

## 新方案

- 一阶：Godunov/upwind 空间差分 + 显式 Euler。
- 二阶：minmod 限制的导数重建 + SSP-RK2 时间推进。
- 外部计算边界：常值 ghost 外推；它不等同于无限空间或任意物理边界。
- 纯垂直、常速刻蚀继续使用原来的精确柱体平移路径。
- 局部细化规划器同步更新数值影响范围：一阶每步最多传播 1 个节点，二阶的两次子步合计最多 4 个节点。

限制器在尖角和不光滑区域会降低局部阶数；“二阶”不表示任意几何的每一点都具有同样精度，也不代表材料距离重建和渲染误差已经消失。

## 如何调用

ETCH Recipe 的 parameters 或当前步骤 overrides 可增加：

```json
{
  "solver_order": 2,
  "tile_shape": [64, 64, 64]
}
```

tile_shape 按 z/y/x 排列，也可使用单个整数；省略它或设为 null 表示一次处理整个网格。旧配方的默认 solver_order 仍为 1，避免无提示地改变已有结果。

直接数值接口：

```python
from process_studio.kernel.transport import evolve_hamilton_jacobi

result, steps = evolve_hamilton_jacobi(
    phi, spacing, normal_speed, velocity, total_time,
    order=2, tile_shape=(64, 64, 64),
)
```

该接口当前只接受空间常数法向速度和常数平移速度。空间变速、粗细层插值、动态重分块、跨层时间插值、稀疏存储尚未接入。

1T1C 对照重算命令（从头执行，不复用旧结果）：

```powershell
python examples/render_1t1c_adaptive.py ../process-studio-1t1c-demo/final-state.npz --adaptive-dir ../process-studio-1t1c-demo/second-order-state --output docs/assets/1t1c-second-order.png --factor 4 --solver-order 2 --tile-size 64
```

## 验证结果

自动化测试包括不整除网格大小的分块、单节点小块、逆序遍历、三维跨块界面、混合刻蚀顶面保护以及局部/全域求解一致性。测试比较原始浮点场，不使用镜像、平滑或填洞。

在一组 23×27×31 网格、27 个分块、每步两次同步的测试中，分块与全域结果的最大差为 **0.0**。这证明该测试中的执行分块没有引入误差，不是对所有物理模型的证明。

独立解析验证使用偏移圆和球面，测量精确界面附近固定 30 nm 带内的 level-set 数值场 RMS 误差：

| 测试 | 网格间距 | 一阶 RMS | 二阶 RMS |
| --- | ---: | ---: | ---: |
| 偏移圆扩张 | 25 nm | 0.814 nm | 0.0546 nm |
| 偏移圆扩张 | 12.5 nm | 0.393 nm | 0.0133 nm |
| 偏移圆扩张 | 6.25 nm | 0.194 nm | 0.00332 nm |
| 偏移球扩张 | 25 nm | 1.293 nm | 0.0750 nm |

以上是解析模型上的数值场误差，不是实际工艺精度，也不是全部界面的 Hausdorff 距离。原始结果在 `docs/assets/transport-validation.json`，由 `examples/benchmark_transport.py` 生成。

## 存储和下一阶段

目前仍保存完整的稠密场；分块减少差分计算的临时数组规模，但没有将总内存降低到仅与表面积成正比。全域距离重建、材料场和可达性计算仍需要全域信息。

下一阶段才是根据误差/界面位置创建和撤销细网格块，并处理粗细层之间的空间及时间插值。在这之前，不能把这一版本称为动态稀疏 AMR，也不能承诺在普通电脑上运行完整 1 nm 三维工程。
