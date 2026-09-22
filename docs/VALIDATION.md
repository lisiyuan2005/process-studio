# 验证清单

## 自动化测试

```bash
python -m pytest -q          # 207 项
cd desktop && npm run test   # 77 项
```

Python 侧按文件覆盖：

- **slab 内核**（`tests/test_slab_kernel.py`，27 项）：裸片厚度与高度换算、保形膜在沟槽内外都等厚、各向同性刻蚀的 undercut、CMP 平面按工程高度、状态存档往返、单步不修改输入状态、带掩膜的沉积（lift-off 语义，存档后仍然成立）、z 与 XY 两个分辨率各自的作用、俯视图看穿某种材料、氧化只吃该吃的材料，以及混合刻蚀剖面和图形化沉积**被拒绝而不是被近似**。
- **各向同性刻蚀**（`tests/test_isotropic_etch_speed.py`，18 项）：湿法前沿不穿过封闭的横向屏障、密封空洞不是刻蚀源、开壳之后才刻到目标；以及那轮提速本身的不变量——同一半径的膨胀只算一次、共用 reach 的相邻采样只施加一次、区域"几乎相同"的容差、深度对采样区间的约束、方形前沿与切片密度无关。
- **3D 显示网格**（`tests/test_mesh_builder.py`、`tests/test_mesh_triangulation.py`、`tests/test_mesh_pool.py`，30 项）：每个面记住自己贴着哪种材料、看不见的面不进视图、自由表面与完整表面分开缓存、两种三角化都覆盖整张面、ear clipping 掉顶点时回退而不是失败、多进程池起不来时只是变慢。
- **撤回**（`tests/test_cancellation.py`，4 项）：没装检查时没人能停下一步、沉积中途放弃、检查只在这一次调用里生效、被停的步骤报告 `Cancelled`。
- **工具自己的 recipe**（`tests/test_cli.py`、`tests/test_worker_protocol.py`）：装在机器上的配方列表随 Excel 往返，改别的字段不动它，步骤用 experiment 的 `tool_recipe` 记录用的哪条。
- **两套参数**（`tests/test_worker_protocol.py`、`tests/test_cli.py`）：experiment 的值随文档、流程文件和 Excel 往返，**写它不会让已经 clean 的步骤变 stale**，导出表格能按 simulation 或 experiment 出两份。
- **版图与库**（`tests/test_layout_inputs.py`、`tests/test_libraries.py`）：Quick Sketch 四种图形、布尔运算、阵列、JSON round-trip、GDS layer/datatype 栅格化、简化 Excel Recipe 往返。
- **持久化**（`tests/test_storage.py`、`tests/test_shared_library.py`，15 项）：工程/分支/共享快照、级联失效、无引用文件删除、工作目录搬走之后结果仍然找得到；共享库（材料/工具/Recipe 在用户目录里一份）的身份戳、删除不复活、多窗口不互相覆盖。
- **更新**（`tests/test_update.py`，13 项）：平台对应的资产名、版本号数值比较、把最新 release 和当前构建对照（版本号可以往下走，见下）、解压到应用旁边、失败留下的目录下次清掉、只信系统信任库并在取不到时回退到打包的 CA。
- **打包**（`tests/test_packaging.py`）：把 `scipy` / `skimage` / `skfmm` 的 import 变成失败（它们是 level set 的求解与建面，已经随它一起不装了），再从零重新 import worker 启动时会加载的每个模块并发一次 `describe`。谁不小心在模块顶层 import 了它们，这里就会红，而不是等到打包版在真机上炸。

**版本号重置**（`0.9.8` → `0.1.0`）：更新检查因此不能只比"更大"。`describe_release` 同时给出 `available`（最新 release 与当前构建**不同**）和 `newer`（严格更大），界面用前者，所以从 `0.9.8` 装上来的用户仍然能看到 `0.1.0`。

## 桌面前端与 worker

`tests/test_worker_protocol.py`（76 项）在 RPC 层覆盖：能力上报、建立/打开工作目录、document 往返、退役内核被拒绝并说明去哪里打开、整条流程与三种视图、任意 AA–BB 线的截面、改分辨率与改工程窗口（都丢弃结果）、按摘要缓存（改一步只重算其后、改名不失效、删步同时清快照和摘要、GDS 步骤把版图指纹算进摘要）、运行到指定步、工艺分叉（在某一步之后分出分支并带上已算好的结果、改名、删除只删独有结果）、GDS 导入、Excel 往返、逐行服务与错误码，以及视图请求的并发与 `Superseded`。

前端的纯函数在 vitest 下覆盖（9 个文件）：override 解析与回退、编辑/重排的失效范围、分叉关系（`project.test.ts`）、共享库的同步与删除（`library.test.ts`）、视口的相机与缩放记忆（`Viewport.test.ts`）、CLI 面板、数值输入框、剪贴板、版图坐标、标签页、样式。

这些测试验证的是协议和状态一致性，不是工艺精度。

## 打包冒烟测试

`scripts/build_desktop.sh` 和 `build_desktop.ps1` 在打包 worker 之后立即向它发一次 `describe` 并检查响应。它用于捕捉 PyInstaller 漏收依赖、入口导入错误这类只在打包版暴露的问题。

## 端到端示例

`examples/3d-nand/` 是一条 34 步的替代栅 3D NAND 流程，命令行可以直接跑：

```bash
process-studio --root nand flow apply examples/3d-nand/flow.json
process-studio --root nand run
```

`tests/test_cli.py` 会把这个流程文件套进一个临时工作目录，验证它仍然能被应用（步骤数、材料、循环折叠）。

## 物理解释边界

测试验证的是离散几何行为、数据一致性和流程复用，不代表工艺结果已经对某一 fab/tool 标定。若用于实际工艺决策，应使用截面 SEM/TEM、膜厚、CD 和刻蚀速率数据标定 Recipe。
