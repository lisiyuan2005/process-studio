# 1T1C 九步结构结果

这组图片从平坦 Si 衬底开始，按同一条流程连续计算一次，并在每一步结束后保存真实材料状态。全部结果使用 6.25 nm 三维均匀网格、二阶界面推进；AA 截面固定在 y=0.225 µm，视角和坐标范围保持不变，便于逐步比较。

> 这是用于工艺推导和软件验证的参数化 2×2 1T1C 几何模型，不是经过晶圆厂数据标定的 TCAD 模型。图片不做轮廓镜像、补洞或几何平滑，因而会保留当前内核的离散误差和异常空隙。

![九步 AA 截面对比](assets/1t1c-steps/all-steps-aa.png)

## 01 电容沟槽刻蚀

对 Si 使用圆形 Quick Sketch 掩膜，目标深度 0.42 µm，方向性比例 0.92。该步骤建立后续保形沉积的沟槽母结构。

![01 Etch capacitor trenches](assets/1t1c-steps/step-01.png)

## 02 沉积电容介质

在所有与外部空气连通的 Si 表面进行 25 nm Al2O3 等厚几何沉积，包括沟槽侧壁和底部。

![02 Deposit capacitor dielectric](assets/1t1c-steps/step-02.png)

## 03 沉积下电极

在当前暴露表面继续沉积 25 nm TiN。旧材料界面保持不动，新材料按优先级覆盖显示。

![03 Deposit lower electrode](assets/1t1c-steps/step-03.png)

## 04 填充电容金属

以 85 nm W 保形沉积近似金属填充。当前模型允许沉积过程中发生 pinch-off，但尚未模拟同一步内封口后输运截止，因此图中保留的内部空隙需要被视作模型结果而不是自动修补对象。

![04 Fill capacitor metal](assets/1t1c-steps/step-04.png)

## 05 电容 CMP

把 Al2O3、TiN 和 W 在 z=0 µm 以上的部分移除，以 Si 作为停止层，得到平坦化后的电容顶部。

![05 Planarize capacitor](assets/1t1c-steps/step-05.png)

## 06 沉积层间介质

在平坦化结构上沉积 100 nm SiO2，形成后续垂直沟道和栅结构的层间介质。

![06 Deposit interlayer dielectric](assets/1t1c-steps/step-06.png)

## 07 建立垂直沟道

使用 channel Quick Sketch 在指定区域放置 220 nm 高的 Si 方向性柱体。当前步骤是参数化几何构造，不包含真实外延生长动力学。

![07 Grow vertical channels](assets/1t1c-steps/step-07.png)

## 08 图形化栅介质

使用环形 Quick Sketch 放置 130 nm 高的 Al2O3 栅介质柱体，基准高度为 z=0.045 µm。当前步骤同样是几何构造，不是 ALD 输运模型。

![08 Pattern gate dielectric](assets/1t1c-steps/step-08.png)

## 09 图形化 TiN 字线

使用 gate Quick Sketch 放置 75 nm 高的 TiN 结构，基准高度为 z=0.075 µm，完成当前 2×2 1T1C 示范结构。

![09 Pattern TiN wordlines](assets/1t1c-steps/step-09.png)

## 数值设置

- 求解范围：x/y/z 均为 −0.7…0.7 µm
- 网格：225 × 225 × 225，共 11,390,625 个节点
- 实际空间间距：6.25 nm
- 混合刻蚀：二阶受限导数重建与 SSP-RK2
- 执行分块：24 × 24 × 24；分块只控制内存与执行方式，不改变物理网格
- 截面显示：从连续材料场采样；3D 使用原始零等值面；Top View 使用求解器原生标签

每一步的压缩数值状态和解析后的参数保存在本地输出目录中，不放入 Git 仓库，避免把大型二进制计算数据写入源代码历史。
