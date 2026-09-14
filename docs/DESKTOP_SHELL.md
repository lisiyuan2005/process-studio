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

worker 内部读线程和执行线程分开。执行分两条道：视图请求（`get_surfaces`、`get_section`、`get_top_view`）走自己的一条，其余请求走主道，各自按到达顺序逐个执行。运行占着主道的时候视图照样有人答，所以一次运行进行中可以查看已经算完的步骤：每步算完就存盘，界面从进度事件里把它标成 Ready，并丢掉之前取过的旧视图。另有两个例外。

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
| `export_mesh` | 把一步的 3D 表面写成 `.glb` / `.gltf` / `.obj` / `.stl` / `.ply`，参数 `destination`，可选 `materials` |
| `save_image` | 把一张 base64 PNG 写到 `destination`，视图右键菜单的「保存图片」走它 |
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

slab 工程有两档**膜模型**（fidelity），顶栏精度按钮旁边的按钮切换，**Detailed**（细节版）和 **Simplified**（简化版）：细节版的膜按球体膨胀算、沿 z 按分辨率采样，角是圆的，planar 的腔口上下沿都会长膜唇（像溅射那样把腔口收死）；简化版的膜全是直角：每个高度上固体轮廓向外推 t，露出的水平面（顶面、保形时还有底面）各移 t 并向外多伸 t 盖住侧壁膜，膜在叠层各平面及其 ±t 之间是常数，每段只算一次多边形偏移，窄于 2t 的缝一样会被封死（pinch-off 保留）；各向同性刻蚀在简化版里前沿也是方的（空腔按盒子而不是球体长大，每个平面一段采样），屏障分步照旧。3D NAND 示例：细节版 56 s，简化版 29 s（板层归一改成只检查改动过的板层之后；之前是 94 s 和 51 s）。两档的结果分开存（简化版的快照和摘要用带 `~simplified` 后缀的步骤键），切换只是换成看另一档已算好的结果，步骤状态随之变化，不会丢掉任何一档；RPC 里是 `project.fidelity`，命令行 `fidelity [detailed|simplified]`，流程文件 `fidelity`。level set 工程没有这个按钮。

slab 内核多一种步骤类型 **Oxidation**：列出会氧化的材料和相对速率，给被消耗的厚度（或时间），露出的表面向内这么厚的一层原地变成指定的氧化物（默认 SiO₂）。前沿和湿法刻蚀完全一样（同样从暴露面出发、同样的屏障分步、简化版同样是方前沿），只是刻掉的部分变成氧化物填回去，体积守恒、外形不变；真实氧化会膨胀 2.2 倍，这里刻意不算。RPC/流程文件的类型名是 `oxidation`，`outputMaterial`/`material` 是产物。level set 内核不提供这一类型。

slab 内核有两种沉积模式：**Conformal** 在每个露出的表面上长同样厚的膜（侧壁、底、顶都是 t，外凸角变圆）；**Planar** 是从正上方落下来的膜、侧壁也长：每个高度上，膜 = 该高度固体轮廓向外推 t（侧壁膜，在墙的上下边缘平着截止）加上天空能看见的水平面各抬高 t（顶面膜向外多伸 t 盖住下面的侧壁膜，凸角是直角），再减掉“上方整列有固体”的阴影。所以孔收窄 t、底升高、深度不变，台阶还是台阶，侧壁里横着挖进去的凹腔（悬垂下面）什么也落不到，腔口上沿（悬垂下沿）和下沿（墙顶）都平着截断，只有膜厚超过腔高时地面才会把腔口顶死。任何地方都不在 z 方向倒圆，膜在叠层各平面及其加 t 之间是常数，每段一个采样就是精确的，所以比保形沉积便宜得多；空片上的 Planar 是一整块平板（衬底就是这样做的）。填平台阶用 Planar 再 CMP：每级台阶的地面都升高 t，抛到目标高度后台阶间就是填充材料。slab 工程有两个分辨率：z 步长（保形沉积和各向同性刻蚀沿高度的采样步长，即圆角肩部台阶的高度）和 XY 弧线弦高（平面内圆角折线逼近的最大偏差，决定每层轮廓的顶点数；留空则跟随 z 步长）。两者独立：z 调细只增加板层数，XY 调细只增加每层顶点数。RPC 的 `plan_grid` / `set_grid` 用 `targetSpacingNm` 和可选的 `targetSpacingXyNm`，命令行是 `window --spacing NM --spacing-xy NM`（0 表示跟随 z），流程文件是 `resolution_nm` 和 `resolution_xy_nm`。

界面上有两处，含义完全不同：

- **Project window**（同一个对话框）：x、y、z 范围，单位 µm。晶圆表面固定在 z = 0，所以 z 范围必须跨过 0；每轴最大 200 µm，超过多半是把 nm 当 µm 填了。改窗口和改网格一样丢弃全部已存结果；slab 工程的衬底厚度就是窗口的深度 |zMin|。`plan_grid` 和 `set_grid` 都接受 `bounds`，level set 工程会在新窗口上重新搜索能整除三个跨度的格子。
- **Simulation grid / Geometry resolution**（顶栏间距按钮）：改的是真实计算精度。level set 工程改的是网格；slab 工程改的是保形沉积的行走步长，对话框相应地不显示节点数和内存，因为该内核没有场。填目标间距（nm）或选预设，worker 用 `grid_for_target_spacing` 搜索能整除三个方向跨度的最近格子，因此三向间距永远相等，不需要手填 nx/ny/nz。对话框实时显示提议的格子形状、节点数、状态体积和运行所需内存，超过 `MAXIMUM_NODES`（2000 万）会拒绝。应用后会丢弃全部已存结果。
- **Colour by**（俯视图底栏）：俯视图按每列最上层材料的颜色画（默认），或按表面高度画：天空能看见的每个平面一种颜色（viridis 顺序色板，低的深蓝、高的黄），底栏图例列出各级高度（µm）。slab 内核的高度级是精确的（每个可见的板面一级）；level set 内核取每列最上层节点的高度，超过 12 级时分成 12 段。RPC `get_top_view` 的 `shading: "material"|"height"`，返回里带 `shading` 和 `levels: [{z, color}]`；命令行 `view top --color-by height`。
- **Sampling**（视口底栏）：只影响显示。它对 level-set 场做线性插值后再提取零等值面或标签，不改变内核算出的结果。俯视图不提供该选项，因为它取的是每列最上层的标签，插值会凭空造出覆盖。

刻蚀步骤的求解器阶数和分块大小都是按需参数，分别写入该 Step 的 `solver_order` 与 `tile_shape`。分块只影响内存占用，不影响结果。

## 流程列表与视图控件

- 右键步骤卡片（或点卡片右上角的 ⋮）打开步骤菜单：**Run to here**、**Duplicate**（在原步骤后插入一份同样设置的副本，副本未运行，它之后的步骤变 `stale`）、**Move up / Move down**、**Skip / Include in the run**、**Delete…**。删除会先确认。这些和 Inspector 里的按钮、拖拽排序是同一套文档操作。
- **循环块**：选中相邻的几步（或一步）后 **Ctrl+G**、选择栏的 **Loop…**、右键菜单或 Edit 菜单的 **Repeat as a loop…**，填名字和总次数，这几步就成为一个循环：选中的那些是第 1 次，后面自动接上 N−1 份同样的副本（未运行）。每一次都是流程里真实的步骤、各有自己的结果，所以点开任何一次的某一步都能看到那一步之后的晶圆；worker 和结果摘要对循环一无所知，只看到摊平的步骤列表。列表里循环显示成一个块：块头可以拖动（整个块一起移）、点一下选中整个循环（Inspector 变成循环设置：改名、改次数、拆开、运行到结尾、删除；视口显示最后一步之后的状态）、折叠；块内第 1 次展开、其余各次折成一行（点开可看）。循环内的编辑会同步到每一次：改参数、改名、跳过、在某一次后面加一步、删一步、原位复制都会在每一次里做同样的事，并从第 1 次那一步开始失效；Alt+↑/↓ 只在本次内移动（各次一起动）；拖拽只对块外的步骤和整个块生效。改次数：变多是在末尾复制第 1 次，变少是丢掉最后几次及其结果。**Take the loop apart** 拆成普通步骤、结果保留。复制整块粘贴得到一个新循环；只复制其中一部分粘贴出来是普通步骤；粘贴到循环内某步之后会落在整个块后面。命令行是 `steps loop STEP... --repeat N --name NAME` 和 `steps unloop STEP`，流程文件里写成一个 `{loop: NAME, repeat: N, steps: [...]}` 条目（见 CLI 文档）。
- 上次打开的工作目录记在这台机器的 `localStorage` 里：刷新或重开窗口会直接回到那个工作目录，只有它打不开（被移走或删掉）时才回首页并说明原因。首页的 **RECENT** 列出最近六个工作目录，点一下打开，× 从列表里去掉。
- 步骤卡片右侧的方框是**是否参与运行**，不是显示开关：勾上表示这一步在运行里，取消勾选表示跳过它。跳过会改变后续几何，因此该步及其之后都会变成 `stale`，需要重新运行。跳过的步骤在副标题里标 `skipped`。
- 3D 视图底栏的材料图例是**显示开关**：点一下隐藏该材料的表面，再点一下显示，便于看内部结构。它只过滤已经取回的三角面，不重新计算，也不改变截面和俯视图（那两张图由 worker 渲染成 PNG）。
- 图例上**右键**（或点色块）打开这种材料在 3D 里的**临时外观**：颜色选择器和 0–100% 的不透明度滑块，立即生效，只作用于 3D 视图，不写进材料库，截面和俯视图仍用库里的颜色；**Library colour** 恢复，改过的图例块显示成虚线边框。切换工程时清空。
- 3D 底栏的 **Clip** 是**剖切平面**：点 X / Y / Z 用垂直于该轴的平面切开模型，滑块把平面从窗口一端移到另一端（读数是 µm），⇄ 换保留哪一侧，再点同一个轴关掉。用的是 three.js 的局部裁剪（`localClippingEnabled`），只在 GPU 里丢掉平面另一侧的像素，不重建网格、不改变结果；切开时改画双面，所以看进去的是内壁而不是空洞（没有封口面）。图上淡蓝的薄片标出平面位置，导出的 PNG 快照是剖开的样子，导出的 mesh 仍是完整几何。
- AA–BB 截面线存在工程里（`project.sectionLines`，每条有 id、名字、A、B 两点，单位 µm），可以保存多条。俯视图上 **Draw AA–BB** 点两下画一条（自动命名 Line 1、Line 2…并切到截面页），**Line by coordinates** 直接输入精确坐标新建，**Edit line** 或截面底栏的 **Edit** 改名字、改坐标或删除；Line 下拉在多条线之间切换。俯视图上所有保存的线都画出来，当前跟随的那条带 A、B 标记，其余淡显并标名字。命令行 `lines list|add|rm` 和 `view section --named NAME` 用同一批线，流程文件里是 `section_lines`。level set 内核沿线双线性采样场，slab 内核直接用 DeviceFlow 的任意直线截面。
- 截面和俯视图都有 **Measure**：点两下量距离，图上标出总长和两个方向的分量；鼠标移动时图的右上角浮着当前坐标（俯视图是 x、y，截面是横轴和 z）；它浮在图上而不放在底栏，是因为底栏内容随鼠标进出变化会让上面的图重新适配而跳动；图的左下角有比例尺，长度自动取整数刻度。这些都只是读数，不改变计算结果。
- 视口上右键：3D 视图可以 **Export mesh**（GLB / OBJ / STL / PLY，走 worker 的 `export_mesh`）和 **Save this view as PNG**（当前相机角度渲染一帧读回来，走 `save_image`）；截面和俯视图可以保存当前图片。文件名默认带工程名和步骤序号。
- 截面和俯视图可以滚轮缩放、拖动平移：滚轮以鼠标位置为中心放大（最多 16 倍），放大后拖动图片平移（测量或画线模式下用中键拖），左下角的「n× · fit」按钮回到整图。缩放只是放大已取回的图片像素，不重新取图；要更细的像素把 Sampling 调高。比例尺按当前放大倍数取整数刻度，坐标读数和测量仍是真实坐标。
- 左右两栏的内边可以拖：拖动改宽度，中间视口相应变窄或变宽，双击恢复默认；宽度记在这台机器的 `localStorage` 里，日志抽屉的左右边界跟着走。视口最窄保留 360 px。
- 选中的步骤变化时视口先清空再取新结果，且只有最新一次请求可以写入视图，避免慢的旧请求把别的步骤的几何盖上来。
- 已经取回的视图按「步骤 + 视图种类 + 参数」缓存在前端（最多 8 份），在同一步的 3D、截面、俯视图之间来回切换不再请求 worker；每次运行开始和改窗口时清空，因为只有这两件事会改变已存的结果。等待期间视口显示「正在构建 / 正在切」的提示，而不是空视图那句「Run the flow」，后者会让人以为结果丢了。
- slab 内核的 3D 视图慢在建网格（几层 conformal 膜要几秒，填满再 CMP 的结构更久），而不是在画。所以网格对每个已存状态只建一次：建好后留在状态对象上（worker 的状态缓存持有它），也写成快照旁边的 `*.mesh.npz`，下次打开工作目录直接读。运行结束后 worker 在后台线程里从最后一步往前预建这些网格，用户看日志或截面的时候网格就在建了；此时点开 3D 会等它建完而不是再建一份。发给前端的是焊接好的带索引几何、不带法线，体积只有原来逐面三份顶点的四分之一；前端把它展开成逐面顶点并算面法线，平面照样平着着色。每个三角形还带一个「贴着哪种材料」的标记：两种材料的交界面在两份网格里各有一份、位置完全重合，画两遍就会在同一批像素上互相争抢，随视角闪烁；薄膜下面几纳米处的衬底顶面在几十微米宽的窗口里同样会和膜顶争抢。所以贴着某种材料的面只在那种材料被隐藏时才画（露出它留下的空腔），那种材料显示着的时候一律不画（本来就在内部看不见）；精确几何是封闭的，还剔除背面；不透明度小于 1 的材料先画一遍只写深度的通道，再画颜色，这样每个像素只显示这种材料最近的那个面（否则沟槽底面会按三角形绘制顺序随机地透过膜顶显出来），后面的其他材料照样透出来；仍然重合的面（level set 的交界面、隐藏材料后显示出来的面）按材料顺序加一点深度偏移，由顺序而不是由光栅化的巧合决定谁在前。3D 画布只在有变化时重绘（`frameloop="demand"`），静止的场景不会一帧帧重画。显示网格跳过了 DeviceFlow 为导出做的顶点拆分，那一步占构建时间的三分之一，对逐面着色的渲染没有意义；验证和导出仍走原来的流形网格。

## Quick Sketch 编辑器

步骤的掩膜来源选 Quick Sketch 后，右侧可以选已有 sketch，也可以点 **Edit** 改它或 **New** 新建，都打开编辑器。工具有矩形（拖对角）、圆（从圆心拖）、多边形和路径（逐点点击，Enter 结束，Escape 放弃），每个新图形带一个布尔操作（merge、subtract、intersect），右侧列表按应用顺序列出图形，可以改数值、改操作、调阵列（个数与间距）、上下移动和删除。坐标默认吸附 5 nm，可改。

画布底下垫着这一步之前一步的俯视图（没跑过就没有），填充是 worker 通过 `preview_mask` 用内核的 CSG 算出来的曝光区域，按步骤的 Keep 设置翻转，所以看到的就是运行时会采样的掩膜，不是前端自己画的近似。保存写入 `sketches/<id>.json`，用到它的步骤随之变成 stale。

## Step 与 Recipe Library

Recipe 有一个 `group` 字段：Recipe Library 先按工艺类型（Deposition、Etch、CMP、No geometry change）分顶层，再按 `group` 路径分组，斜杠表示子组（`ALD/Oxides`）。在 Recipe 的 Group 框里输入名字即新建组，不需要单独管理组；Inspector 的模板下拉按同样的组分段。Excel 导出多一列 Group，导入时没有这列也能读。

顶栏是一条菜单栏，按用途分组：**File**（新建/打开/最近/保存/另存为；导入流程文件、GDSII、材料/配方/工具库；导出流程为 Excel/CSV/流程文件、各库为 xlsx/csv、3D 表面；打开工作目录所在文件夹；关闭工程），**Edit**（撤销/重做、加步骤、复制/粘贴/重复/删除/全选/上下移动/跳过与恢复），**View**（3D/截面/俯视，俯视按材料或高度着色，日志），**Run**（运行、运行到选中步、停止、丢弃结果全部重跑、膜模型 Detailed/Simplified、精度、命令控制台），**Libraries**（材料、配方、工具），**Help**（文档、快捷键、检查更新、关于）。菜单右边是分支、精度、膜模型、内核徽标，最右是保存状态、Log 和 Run。

**步骤多选**：点选一步，Ctrl（Mac 上 Cmd）+点加选/去选，Shift+点选一段，Ctrl+A 全选，Esc 只留当前步。选中两步以上时列表上方出现操作条（Duplicate / ↑ / ↓ / Delete / ×），右键菜单也切换成批量版；Ctrl+C 把选中步骤放进应用内剪贴板（同时以 JSON 写到系统剪贴板），Ctrl+V 粘到当前步之后（新 id，状态 not run），Ctrl+D 原位复制，Delete 删除（会确认），Alt+↑/↓ 整体上下移动（被挡住的不动）。所有编辑都进撤销栈（30 步，Ctrl+Z / Ctrl+Y）。

**导入导出**走 worker 的 `export_flow`（xlsx/csv 一行一步：序号、名字、类型、工具、材料、模式、目标、方向性、掩膜、保留侧、速率、停止层、其他参数 JSON、启用、状态；json/yaml 就是流程文件）、`import_flow`（等于 `flow apply`）、`export_library` / `import_library`（materials/tools 的 xlsx 或 csv，recipes 的 xlsx；导入按名字覆盖同名项）、`copy_workspace`（Save as：复制整个目录并把快照的绝对路径改到新目录）、`reveal_path`（系统文件管理器）。

**自动更新**：首页检查到新版本后点 **Update now**，worker 的 `install_update` 下载本平台本版本的 zip（进度以日志事件回报）、解包到应用旁边的临时目录、写一个更新脚本（Windows 是 .cmd：等应用和 worker 两个进程退出，robocopy 覆盖，重新启动；macOS 是 shell 脚本：ditto 覆盖 .app 再 open），以分离进程启动它，然后壳调用 Tauri 的 `quit_for_update` 退出。源码运行没有可覆盖的安装，会拒绝。**Download only** 仍然只用浏览器下载。

顶栏 **CLI** 现在在 Run 菜单里（Command console…）。它打开一个面板。上半是控制台：把命令粘进去（一行一条，`#` 开头是注释，行首带不带 `process-studio`、worker 路径、`python -m process_studio.cli`、`--root` 都行，会剥掉）或者整个流程文件（JSON 以 `{` 开头，YAML 看 `name:`/`steps:` 这类键），Run 或 Ctrl+Enter 执行。命令在 worker 进程里跑（RPC `run_cli`：`argv`、可选 `stdin`，返回退出码、两路输出和之后的 document），走的是同一个 CLI 代码，`--root` 由界面填成当前工作目录。流程文件走 `flow apply -`（`-` 表示从 stdin 读），勾着「Run the flow after applying」就接着 `run`。**联动**：执行前先保存界面上未存的改动；每条命令返回的 document 直接替换界面上的（步骤、材料、状态立刻更新，视图缓存清空）；`run` 的进度事件走的是这个请求自己的事件流，所以步骤卡的运行/完成标记、日志、Stop 按钮和界面自己点 Run 完全一样。下半把当前状态对应的命令行列出来（`status`、`run`、`run --through N`、当前选中步骤沿当前截面线的 `view section`、`view top`、`view mesh`、`flow dump/apply`），每条带 Copy 按钮，直接粘到终端里跑。程序路径来自 worker 的 `describe`（`cli` 字段）：打包版是 worker 可执行文件本身，源码运行是 worker 所用解释器加 `-m process_studio.cli`；路径带空格会加引号，Windows 路径的程序加 `&` 调用符。命令走的是同一个 worker、同一个工作目录，桌面里改的东西命令行立刻能看到，反过来也一样。

工具是一个库（`tools` 表，document 里的 `tools`）：每个工具有名字、`group` 路径和备注，顶栏 **Tools** 打开编辑器增删改组。Step 和 Recipe 的 Tool 字段是分组下拉，最后一项「Other (type a name)…」可以手填；填的名字不在库里也照样保存为文本，删除库里的工具不改动已引用它的步骤。命令行 `tools list|add NAME --group G|rm NAME`。

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

脚本先在 `work/venv` 建一个虚拟环境（已经激活了别的环境就直接用它；`PROCESS_STUDIO_VENV` 指定别的位置），在里面装依赖和 PyInstaller——Homebrew 或系统的 python3 不允许往自己里面装包（PEP 668 的 `externally-managed-environment`），所以不能直接 `pip install`。然后用 PyInstaller 把 worker 打成独立可执行文件放进 `desktop/src-tauri/resources/worker`，做一次 `describe` 冒烟测试，再执行 `npm run tauri build`。发布版通过 `PROCESS_STUDIO_WORKER` 可以覆盖 worker 路径。

**发布与检查更新**：推一个 `v*` 标签（版本号同时写在 `pyproject.toml`、`src/process_studio/__init__.py`、`desktop/package.json`、`desktop/src-tauri/tauri.conf.json` 和 `Cargo.toml` 里）会构建两个平台的三个版本，并由 `publish` 任务发成一个 GitHub Release，资产命名为 `ProcessStudio-[Slab-|LevelSet-]Windows.zip` 和 `ProcessStudio-[Slab-|LevelSet-]macOS.zip`（外加 dmg）。首页版本号旁边的 **Check for updates** 走 worker 的 `check_update`（读 `releases/latest`，页面本身不联网），比较版本号后给出本平台本版本对应资产的 **Download** 按钮和 **Release notes**，两者都由 worker 的 `open_url` 用系统浏览器打开，且只允许仓库自己的地址。

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
