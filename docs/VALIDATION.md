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

## UI 冒烟测试

`python -m process_studio --smoke-test --workspace work\ui-smoke` 会完整创建窗口、布局、数据库、默认工程和三种视图，然后正常退出。它用于捕捉打包、导入和 UI 初始化错误。

## 端到端示例

`examples/build_1t1c_demo.py` 从 Si 初始衬底开始运行 9 步流程，并检查最终输出可以保存为 NPZ、SQLite 和三视图图片。示例工程可再次由桌面 UI 打开，证明计算结果与交互层使用同一数据格式。

## 物理解释边界

测试验证的是离散几何行为、数据一致性和流程复用，不代表工艺结果已经对某一 fab/tool 标定。若用于实际工艺决策，应使用截面 SEM/TEM、膜厚、CD 和刻蚀速率数据标定 Recipe，并把网格收敛性作为 acceptance criterion。
