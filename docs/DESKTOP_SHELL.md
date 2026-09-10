# 桌面前端：Tauri + React 外壳

桌面前端与 [ProcessFlow-Emulator](https://github.com/lisiyuan2005/ProcessFlow-Emulator) 使用同一套架构和视觉语言：Tauri 外壳、React 前端、Python worker，三者通过 JSON-line RPC 通信。这是唯一的界面；原先的 Tkinter 界面已删除。

## 分层

```
desktop/src            React 前端（TypeScript，不含任何工艺计算）
desktop/src-tauri      Rust 外壳：窗口、文件对话框、worker 进程管理
src/process_studio/worker   Python worker：RPC 分发、执行、几何导出
src/process_studio/kernel   Level Set 内核，未改动
```

前端不做任何数值计算，也不保存自己的工程格式。工程仍然是 `process_studio.sqlite3` 加快照目录，格式与旧版本一致。

## 工作目录

一个工作目录包含：

| 路径 | 内容 |
| --- | --- |
| `process_studio.sqlite3` | 工程、分支、步骤、Recipe、材料、快照索引 |
| `process_studio_snapshots/` | 每步一个 `.npz` 材料状态 |
| `sketches/<id>.json` | Quick Sketch；旧版放在根目录的 `default-sketch.json` 仍可读取 |
| `layouts/` | 导入的 GDSII 文件 |

## RPC 协议

一行一个 JSON 请求写入 stdin，一行一个响应写回 stdout。执行过程中的进度以 `{"kind":"event"}` 行实时输出，Rust 侧转成 `process-studio-worker` 窗口事件。

| 方法 | 作用 |
| --- | --- |
| `ping` / `describe` | 版本与能力（求解器阶数、可用插值上限、是否支持表面提取） |
| `create_workspace` / `open_workspace` | 建立或打开工作目录，返回完整 document |
| `save_document` | 写回工程、分支、步骤、Recipe、材料 |
| `plan_grid` | 给出目标间距对应的格子形状、节点数与内存估计 |
| `set_grid` | 按目标间距或显式格子更换网格，同时作废全部已存结果 |
| `save_sketch` | 写入 Quick Sketch |
| `run_flow` | 执行分支，可指定 `throughStepId` 或 `force` |
| `get_surfaces` | 每种材料的 marching cubes 三角面，base64 传输 |
| `get_section` / `get_top_view` | 截面与俯视图 PNG |
| `import_gds` / `gds_layers` | 导入布局并列出 layer/datatype |
| `export_recipes_xlsx` / `import_recipes_xlsx` | Recipe 库 Excel 往返 |

Rust 侧只放行上表中的方法名，并把 `root` 规范化成绝对路径后才交给 worker。

## 增量执行

worker 为每一步计算一个链式摘要，内容包括该步自带的工艺定义、Quick Sketch、网格，以及前一步的摘要。摘要与快照一起存在 `worker_step_digests` 表里。

- 摘要一致且快照存在 → 直接加载快照，前端显示 Cached。
- 任何一步改变 → 该步及其之后全部标记 dirty，运行时从第一个 dirty 步重算。
- 只改步骤名称不会作废结果，因为名称不参与摘要。
- 换网格、改 Step 参数或改 Sketch 都会作废受影响的步骤；修改 Recipe Library 不会影响已经存在的 Step。

执行永远从初始衬底重放，不会把粗网格的旧结果插值当成细网格的初始条件。

## 精度控制

界面上有两处，含义完全不同：

- **Simulation grid**（顶栏间距按钮）：改的是真实计算精度。填目标间距（nm）或选预设，worker 用 `grid_for_target_spacing` 搜索能整除三个方向跨度的最近格子，因此三向间距永远相等，不需要手填 nx/ny/nz。对话框实时显示提议的格子形状、节点数、状态体积和运行所需内存，超过 `MAXIMUM_NODES`（2000 万）会拒绝。应用后会丢弃全部已存结果。
- **Sampling**（视口底栏）：只影响显示。它对 level-set 场做线性插值后再提取零等值面或标签，不改变内核算出的结果。俯视图不提供该选项，因为它取的是每列最上层的标签，插值会凭空造出覆盖。

刻蚀步骤的求解器阶数和分块大小都是按需参数，分别写入该 Step 的 `solver_order` 与 `tile_shape`。分块只影响内存占用，不影响结果。

## Step 与 Recipe Library

流程中的 Step 是独立工艺实例：名称可自由编辑，只要求选择 deposition、etch、CMP 或 no geometry change 类型。右侧可从 Recipe Library 加载模板，也可从空白 Step 逐项添加参数并另存为 Recipe。加载和保存都是复制，不保留引用关系，因此修改或删除库里的 Recipe 不会改变已有流程。

## 开发

```bash
cd desktop
npm install
npm run tauri dev      # 需要本机能 import process_studio
```

开发模式下 Rust 直接调用 `python3 -m process_studio.worker`，并把仓库的 `src` 放进 `PYTHONPATH`。用 `PROCESS_STUDIO_PYTHON` 可以指定解释器。

`npm run dev` 只启动浏览器预览：它有完整界面但没有内核，运行和视图都会明确报错，不会伪造结果。

## 打包

```powershell
./scripts/build_desktop.ps1     # Windows
./scripts/build_desktop.sh      # macOS / Linux
```

脚本先用 PyInstaller 把 worker 打成独立可执行文件放进 `desktop/src-tauri/resources/worker`，做一次 `describe` 冒烟测试，再执行 `npm run tauri build`。发布版通过 `PROCESS_STUDIO_WORKER` 可以覆盖 worker 路径。

`Desktop builds` 工作流跑的就是这两个脚本，构建前先执行 `python -m pytest -q`，前端测试由脚本里的 `npm run test` 负责。构建只产出应用本身：Windows 用 `--no-bundle`，交付 `ProcessStudio.exe` 加同级的 `resources/`；macOS 用 `--bundles app`，交付 `Process Studio.app`。不生成 NSIS、MSI 或 DMG。

worker 的查找顺序是先平台资源目录（`.app` 里的 `Contents/Resources`、Linux 包的 `/usr/lib/<产品名>`），再退回可执行文件所在目录。免安装布局靠的是第二条，`PROCESS_STUDIO_WORKER` 仍可覆盖两者。


## 尚未实现

- 前端还没有图形化的 Quick Sketch 编辑器，只能选择已有 sketch；`save_sketch` 接口已经就绪。
- 细化执行（`run_refined_branch`）还没有接到界面，仍需从 API 或示例脚本调用。
- 分支只能切换，创建、重命名和删除还没有界面入口；`storage.create_branch` 已经就绪。
- 前端不显示 CMP dishing、晶向湿蚀等未实现的物理，因为内核本身没有实现它们。
