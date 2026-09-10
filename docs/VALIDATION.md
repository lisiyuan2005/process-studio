# 验证清单

## 自动化测试

运行 `python -m pytest -q`。测试覆盖：

- Level Set：Godunov 梯度、平面平移、圆形扩张/收缩、重初始化。
- 3D Etch：mask 保护、目标深度、干法方向性、混合刻蚀、湿法 undercut。
- Deposition：顶面/底面/侧壁等厚、CD 缩小、reentrant pinch-off 与封闭空洞。
- Multi-material：覆盖优先级、选择性响应、stop material、方向性图形沉积、CMP。
- Layout：Quick Sketch 四种图形、布尔运算、阵列、JSON round-trip、GDS layer/datatype 栅格化。
- Library：简化 Excel Recipe 导入导出。
- Persistence：项目、分支、共享快照、级联失效和无引用文件删除。
- Engine：Recipe + Step override、mask 解析、多步运行和快照记录。

## 通用引擎回归

`tests/test_general_engine.py` 另行覆盖 4 种偏移图形 × 3 种刻蚀模式的局部/全域一致性、跨区域多步流程、区域递归合并、GDS 实例旋转与单位、布尔孔洞及反向掩膜、旧材料界面不变性、封闭孔洞不被后续沉积填充、二阶距离重建的网格收敛，以及超出资源预算时拒绝计算。

局部/全域测试比较 core 内的原始浮点场，不比较经过平滑的图片。更多执行约束见 [通用引擎说明](GENERAL_ENGINE.md)。

### 1T1C 原始场复测

固定 x 中心 −0.315 µm、AA 的 y=0.225 µm，比较 W 外轮廓左右半宽差；两次真实网格间距都是 6.25 nm。下表不经过镜像或平滑：

| z (µm) | 旧独立单元块 (nm) | 通用引擎全域重算 (nm) |
| --- | ---: | ---: |
| −0.025 | 1.250 | 1.529 |
| −0.050 | 14.462 | 2.106 |
| −0.100 | 1.225 | 0.000738 |
| −0.200 | 1.161 | 0.000738 |
| −0.300 | 1.162 | 0.000342 |

旧的大幅局部不对称显著减小，但上部拐角仍有约 1.5–2.1 nm 的离散误差，不能宣称每处都更精确或已经零误差。这里测量的是对称性，不是绝对工艺尺寸准确度。原始数字在 `assets/symmetry-before.json`、`assets/symmetry-after.json`，测量脚本为 `examples/measure_section_symmetry.py`。

## 同步分块与二阶推进回归

`tests/test_synchronized_transport.py` 验证一阶/二阶方案在二维和三维中的分块/全域逐点一致性，含尺寸不整除、很小的块、逆序遍历和非法参数；还验证偏移圆扩张/收缩、偏移球扩张、非圆图形斜向移动的解析误差及网格收敛。

Engine 测试同时检查二阶选项能从步骤传入刻蚀内核，且局部细化规划器会扩大影响范围以覆盖全部 RK 子步；还增加了非光滑柱体初始场的混合运动解析对照。完整测试共 **93 项**。详见 [方案和原始数值](SYNCHRONIZED_SOLVER.md)。

## UI 冒烟测试

`python -m process_studio --smoke-test --workspace work\ui-smoke` 会完整创建窗口、布局、数据库、默认工程和三种视图，然后正常退出。它用于捕捉打包、导入和 UI 初始化错误。

## 端到端示例

`examples/build_1t1c_demo.py` 从 Si 初始衬底开始运行 9 步流程，并检查最终输出可以保存为 NPZ、SQLite 和三视图图片。示例工程可再次由桌面 UI 打开，证明计算结果与交互层使用同一数据格式。

## 物理解释边界

测试验证的是离散几何行为、数据一致性和流程复用，不代表工艺结果已经对某一 fab/tool 标定。若用于实际工艺决策，应使用截面 SEM/TEM、膜厚、CD 和刻蚀速率数据标定 Recipe，并把网格收敛性作为 acceptance criterion。
