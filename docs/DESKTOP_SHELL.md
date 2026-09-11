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
| `process_studio_snapshots/` | 每步一个材料状态：level set 是 `.npz`，slab 是 `.dfz`；slab 快照旁边的 `*.mesh.npz` 是它的 3D 显示网格，第一次构建后留下来，删掉只会让下次打开 3D 视图重新构建 |
| `sketches/<id>.json` | Quick Sketch；旧版放在根目录的 `default-sketch.json` 仍可读取 |
| `layouts/` | 导入的 GDSII 文件 |

## RPC 协议

worker 是一个常驻进程，整个会话只启动一次，进程退出时（崩溃或被杀）Rust 侧让所有等待中的请求失败并附上 stderr 尾部，下一次请求再起一个新的。启动一次打包版 worker 约 3 秒（解压、解释器、numpy/scipy/shapely/trimesh 的 import），常驻之后一次视图请求只要几十毫秒，所以不要回到每次调用起一个进程的做法。

一行一个 JSON 请求写入 stdin，一行一个响应写回 stdout，请求和响应靠 `id` 配对，多个请求可以同时在途。执行过程中的进度以 `{"kind":"event","id":…}` 行实时输出，带着所属请求的 id，Rust 侧转成 `process-studio-worker` 窗口事件，事件里的 `requestId` 就是它。

worker 内部读线程和执行线程分开：请求按到达顺序逐个执行，但有两个例外。

- `{"kind":"cancel","id":…}` 撤回一个请求：还在排队的立刻以 `Cancelled` 回复；正在执行的运行在下一步之前停下，已经算完的步骤连快照带摘要都保留，下次运行从那里续。
- 视图类请求（`get_surfaces`、`get_section`、`get_top_view`、`plan_grid`）同一工作目录只保留最新一个待执行的，被替代的立刻以 `Superseded` 回复，不再计算。连点五个步骤只算最后一个。

最近用过的状态留在 worker 内存里（`state_cache.py`），按快照文件路径为键，预算默认 1 GB，可用环境变量 `PROCESS_STUDIO_STATE_CACHE_MB` 改。细网格工程一个状态几百 MB，读一次压缩快照要几秒，缓存命中后视图不再付这个代价。

| 方法 | 作用 |
| --- | --- |
| `ping` / `describe` | 版本与能力（可用内核、求解器阶数、可用插值上限、是否支持表面提取） |
| `create_workspace` / `open_workspace` | 建立或打开工作目录，返回完整 document；建立时用 `kernel` 选内核 |
| `save_document` | 写回工程、分支、步骤、Recipe、材料 |
| `plan_grid` | 给出目标间距对应的格子形状、节点数与内存估计 |
| `set_grid` | 按目标间距或显式格子更换网格；无网格的内核改的是几何分辨率。两种都会删除全部已存结果 |
| `save_sketch` | 写入 Quick Sketch |
| `preview_mask` | 未保存的 sketch 在工程窗口上的曝光区域 PNG，由内核自己的符号距离函数算出，编辑器拿它当填充 |
| `run_flow` | 执行分支，可指定 `throughStepId` 或 `force` |
| `get_surfaces` | 每种材料的 marching cubes 三角面，base64 传输 |
| `get_section` / `get_top_view` | 截面与俯视图 PNG。截面可按 `axis`+`position` 沿 x 或 y 切，也可给 `line: {start:[x,y], end:[x,y]}` 沿任意 AA–BB 线切，此时横轴是沿线距离 |
| `import_gds` / `gds_layers` | 导入布局并列出 layer/datatype |
| `export_recipes_xlsx` / `import_recipes_xlsx` | Recipe 库 Excel 往返 |

Rust 侧只放行上表中的方法名，并把 `root` 规范化成绝对路径后才交给 worker。前端可以自带 `requestId`，运行流程时就是这样做的，这样顶栏的 **Stop** 才知道要撤回哪一个。

`desktop/src-tauri/src/lib.rs` 里的 `Worker` 有 `cargo test` 覆盖：它真的起一个 Python worker，验证多个请求各自拿到自己的答案、进度事件带对 id、取消后进程仍然可用、进程被杀后等待者收到失败。

## 增量执行

worker 为每一步计算一个链式摘要，内容包括该步自带的工艺定义、Quick Sketch、网格，以及前一步的摘要。摘要与快照一起存在 `worker_step_digests` 表里。

- 摘要一致且快照存在 → 直接加载快照，前端显示 Cached。
- 任何一步改变 → 该步及其之后全部作废，运行时从第一个作废的步骤重算。
- 只改步骤名称不会作废结果，因为名称不参与摘要。
- 改 Step 参数或改 Sketch 会作废受影响的步骤；修改 Recipe Library 不会影响已经存在的 Step。
- 换网格会直接删除全部快照：那些场是在旧网格上的，不是这个工程的结果。

每个步骤有三种状态，`open_workspace`、`save_document` 和 `run_flow` 都会返回：

| 状态 | 含义 | 视图 |
| --- | --- | --- |
| `clean` | 快照存在且摘要匹配 | 直接显示 |
| `stale` | 快照存在但摘要已变，或上游改过 | 仍然显示上次运行存下的结果，并在视口上标注「已过期」 |
| `dirty` | 从未运行过，没有任何结果 | 提示先运行，不去请求 worker |

过期不等于没有结果。改了参数还想看上一次算出来的结构是常态，因此 `stale` 照常出图，只是明确标注它不是当前参数的结果。

执行永远从初始衬底重放，不会把粗网格的旧结果插值当成细网格的初始条件。

## 仿真内核

工程在新建时选内核，之后不能改：`describe` 给出可用内核及各自能力，`create_workspace` 接受 `kernel`，`save_document` 拒绝改动它。视图、执行和快照格式都由内核自己提供，`runner.py` 和 `protocol.py` 不判断内核 id。两个内核的能力对照见[内核说明](KERNELS.md)。

## 精度控制

界面上有两处，含义完全不同：

- **Project window**（同一个对话框）：x、y、z 范围，单位 µm。晶圆表面固定在 z = 0，所以 z 范围必须跨过 0；每轴最大 200 µm，超过多半是把 nm 当 µm 填了。改窗口和改网格一样丢弃全部已存结果；slab 工程的衬底厚度就是窗口的深度 |zMin|。`plan_grid` 和 `set_grid` 都接受 `bounds`，level set 工程会在新窗口上重新搜索能整除三个跨度的格子。
- **Simulation grid / Geometry resolution**（顶栏间距按钮）：改的是真实计算精度。level set 工程改的是网格；slab 工程改的是保形沉积的行走步长，对话框相应地不显示节点数和内存，因为该内核没有场。填目标间距（nm）或选预设，worker 用 `grid_for_target_spacing` 搜索能整除三个方向跨度的最近格子，因此三向间距永远相等，不需要手填 nx/ny/nz。对话框实时显示提议的格子形状、节点数、状态体积和运行所需内存，超过 `MAXIMUM_NODES`（2000 万）会拒绝。应用后会丢弃全部已存结果。
- **Sampling**（视口底栏）：只影响显示。它对 level-set 场做线性插值后再提取零等值面或标签，不改变内核算出的结果。俯视图不提供该选项，因为它取的是每列最上层的标签，插值会凭空造出覆盖。

刻蚀步骤的求解器阶数和分块大小都是按需参数，分别写入该 Step 的 `solver_order` 与 `tile_shape`。分块只影响内存占用，不影响结果。

## 流程列表与视图控件

- 右键步骤卡片（或点卡片右上角的 ⋮）打开步骤菜单：**Run to here**、**Duplicate**（在原步骤后插入一份同样设置的副本，副本未运行，它之后的步骤变 `stale`）、**Move up / Move down**、**Skip / Include in the run**、**Delete…**。删除会先确认。这些和 Inspector 里的按钮、拖拽排序是同一套文档操作。
- 上次打开的工作目录记在这台机器的 `localStorage` 里：刷新或重开窗口会直接回到那个工作目录，只有它打不开（被移走或删掉）时才回首页并说明原因。首页的 **RECENT** 列出最近六个工作目录，点一下打开，× 从列表里去掉。
- 步骤卡片右侧的方框是**是否参与运行**，不是显示开关：勾上表示这一步在运行里，取消勾选表示跳过它。跳过会改变后续几何，因此该步及其之后都会变成 `stale`，需要重新运行。跳过的步骤在副标题里标 `skipped`。
- 3D 视图底栏的材料图例是**显示开关**：点一下隐藏该材料的表面，再点一下显示，便于看内部结构。它只过滤已经取回的三角面，不重新计算，也不改变截面和俯视图（那两张图由 worker 渲染成 PNG）。
- 俯视图上的 **Draw AA–BB** 点两下（A、B）画一条截面线，画完自动切到截面页沿这条线出图；截面页的 Cut 选择器里可以在沿 x、沿 y 和这条线之间切换，线一直画在俯视图上，**Clear line** 清掉。level set 内核沿线双线性采样场，slab 内核直接用 DeviceFlow 的任意直线截面。
- 截面和俯视图都有 **Measure**：点两下量距离，图上标出总长和两个方向的分量；鼠标移动时图的右上角浮着当前坐标（俯视图是 x、y，截面是横轴和 z）；它浮在图上而不放在底栏，是因为底栏内容随鼠标进出变化会让上面的图重新适配而跳动；图的左下角有比例尺，长度自动取整数刻度。这些都只是读数，不改变计算结果。
- 选中的步骤变化时视口先清空再取新结果，且只有最新一次请求可以写入视图，避免慢的旧请求把别的步骤的几何盖上来。
- 已经取回的视图按「步骤 + 视图种类 + 参数」缓存在前端（最多 8 份），在同一步的 3D、截面、俯视图之间来回切换不再请求 worker；每次运行开始和改窗口时清空，因为只有这两件事会改变已存的结果。等待期间视口显示「正在构建 / 正在切」的提示，而不是空视图那句「Run the flow」，后者会让人以为结果丢了。
- slab 内核的 3D 视图慢在建网格（几层 conformal 膜要几秒，填满再 CMP 的结构更久），而不是在画。所以网格对每个已存状态只建一次：建好后留在状态对象上（worker 的状态缓存持有它），也写成快照旁边的 `*.mesh.npz`，下次打开工作目录直接读。运行结束后 worker 在后台线程里从最后一步往前预建这些网格，用户看日志或截面的时候网格就在建了；此时点开 3D 会等它建完而不是再建一份。发给前端的是焊接好的带索引几何、不带法线，体积只有原来逐面三份顶点的四分之一；前端把它展开成逐面顶点并算面法线，平面照样平着着色。每个三角形还带一个「贴着别的材料」的标记：两种材料的交界面在两份网格里各有一份、位置完全重合，画两遍就会在同一批像素上互相争抢，随视角闪烁。所以所有材料都显示时这些面一份都不画（本来就在内部看不见），隐藏了某个材料才画出来，露出它留下的空腔；仍然重合的面（level set 的交界面、隐藏材料后显示出来的面）按材料顺序加一点深度偏移，由顺序而不是由光栅化的巧合决定谁在前。3D 画布只在有变化时重绘（`frameloop="demand"`），静止的场景不会一帧帧重画。显示网格跳过了 DeviceFlow 为导出做的顶点拆分，那一步占构建时间的三分之一，对逐面着色的渲染没有意义；验证和导出仍走原来的流形网格。

## Quick Sketch 编辑器

步骤的掩膜来源选 Quick Sketch 后，右侧可以选已有 sketch，也可以点 **Edit** 改它或 **New** 新建，都打开编辑器。工具有矩形（拖对角）、圆（从圆心拖）、多边形和路径（逐点点击，Enter 结束，Escape 放弃），每个新图形带一个布尔操作（merge、subtract、intersect），右侧列表按应用顺序列出图形，可以改数值、改操作、调阵列（个数与间距）、上下移动和删除。坐标默认吸附 5 nm，可改。

画布底下垫着这一步之前一步的俯视图（没跑过就没有），填充是 worker 通过 `preview_mask` 用内核的 CSG 算出来的曝光区域，按步骤的 Keep 设置翻转，所以看到的就是运行时会采样的掩膜，不是前端自己画的近似。保存写入 `sketches/<id>.json`，用到它的步骤随之变成 stale。

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

worker 二进制不带参数时是 RPC 服务，带参数时是命令行工具 `process-studio`（见 [CLI](CLI.md)），所以打包产物里不需要第二个可执行文件。

脚本先用 PyInstaller 把 worker 打成独立可执行文件放进 `desktop/src-tauri/resources/worker`，做一次 `describe` 冒烟测试，再执行 `npm run tauri build`。发布版通过 `PROCESS_STUDIO_WORKER` 可以覆盖 worker 路径。

`PROCESS_STUDIO_KERNELS` 决定这一份打包带哪些内核：不设是两个都带；设成 `slab` 或 `levelset` 就只带一个。选择被写进 worker 里的 `process_studio/kernels/enabled.txt`，另一个内核的包不进 bundle（slab 版不带 scikit-image，level set 版不带 deviceflow、shapely、trimesh）。单内核版的产品名和标识符不同（`Process Studio Slab`、`Process Studio Level Set`），可以和完整版装在同一台机器上。单内核版新建工作区不再有内核选择，打开另一个内核建的工程会明确拒绝并说明该去哪个版本打开，不会用错的内核去跑它。冒烟测试按 `PROCESS_STUDIO_KERNELS` 检查 worker 报告的内核。

`Desktop builds` 工作流跑的就是这两个脚本，打 tag 时每个平台跑三份（完整、只有 slab、只有 level set）；手动触发时可以在 GitHub 的 Run workflow 对话框里选平台和版本，只跑一个作业，比如本机杀毒软件（CrowdStrike 一类）会删掉 PyInstaller 产物时，就用它打 Windows 包。构建前先执行 `python -m pytest -q`，前端测试由脚本里的 `npm run test` 负责。构建只产出应用本身：Windows 用 `--no-bundle`，交付 `ProcessStudio.exe` 加同级的 `resources/`；macOS 用 `--bundles app`，交付 `Process Studio.app`。不生成 NSIS、MSI 或 DMG。

macOS 构建固定在 `macos-14` 运行器上，`tauri.conf.json` 里 `signingIdentity` 设为 `-`，由 Tauri 在打包时用 ad-hoc 身份签名整个 bundle，DMG 因此是从已签名的 app 生成的。

只靠链接器留下的签名是不够的：那只覆盖可执行文件，bundle 没有 `_CodeSignature/CodeResources`，Info.plist 也未绑定，`spctl` 会报 `code has no resources but signature indicates they must be present`，内核在启动时直接 SIGKILL。CI 因此在打包前检查该文件存在并跑 `codesign --verify --strict`，不通过就让构建失败。

首次打开会提示「Apple 无法验证此 App」，这是公证检查，不是签名失败。macOS 15 起右键打开不再绕过它，需到系统设置的隐私与安全性里点「仍要打开」，或执行 `xattr -dr com.apple.quarantine "/Applications/Process Studio.app"` 清掉隔离标记。

worker 的查找顺序是先平台资源目录（`.app` 里的 `Contents/Resources`、Linux 包的 `/usr/lib/<产品名>`），再退回可执行文件所在目录。免安装布局靠的是第二条，`PROCESS_STUDIO_WORKER` 仍可覆盖两者。


## 尚未实现

- 细化执行（`run_refined_branch`）还没有接到界面，仍需从 API 或示例脚本调用。
- 分支只能切换，创建、重命名和删除还没有界面入口；`storage.create_branch` 已经就绪。
- 前端不显示 CMP dishing、晶向湿蚀等未实现的物理，因为内核本身没有实现它们。
