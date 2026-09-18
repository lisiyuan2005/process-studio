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

- `{"kind":"cancel","id":…}` 撤回一个请求：还在排队的立刻以 `Cancelled` 回复；正在执行的运行**在当前这一步内部就会停**，不用等这一步跑完——内核把撤回标志交给几何层（`deviceflow.cancellation`），保形/直角沉积和各向同性刻蚀的 z 采样循环、以及 `harmonize` 重建排布的地方都会隔一小段检查一次，所以按下 Stop 通常几十毫秒内就停住，而不是拖到这一步结束（一步在细网格上可能要几十秒）。已经算完的步骤连快照带摘要都保留，下次运行从那里续；停在半路的那一步什么也不存，下次重算。界面上按下 Stop 后按钮变成 **Stopping…** 并禁用，表示请求已经送到。
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

**改了版图却还是 up to date**：步骤摘要里只有 `maskSource`、`layer`、`datatype`，**版图文件本身完全没参与**。而导入 GDS 是把文件复制进 `layouts/` 并起一个带时间戳的新名字，所以换一张版图、或者就地改一张，摘要都纹丝不动——整条流程继续报告"全部最新"，拒绝重跑。`v0.9.6` 起 `mask_source == "gds"` 的步骤把版图的指纹（路径 + 大小 + 修改时间）算进摘要；用 stat 而不是读内容哈希，是因为真实版图动辄几百 MB，而每次刷新流程状态都要算一次。不读版图的步骤不受影响。

**Run → Run the selected step again**（`Ctrl+F5`）：强制重跑选中的那一步，**即使它显示 up to date**。它之前的步骤照常复用缓存，它和它之后的全部重算（每一步都从上一步留下的状态开始）。用在摘要看不见的东西变了的时候——或者你就是想重新算一遍。和 **Discard results and run everything again** 不同，后者是丢掉全部结果从头来。

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
**3D 视图为什么要等**：等的几乎全是 worker 构建显示网格，与传输和浏览器无关。一个 5×5 孔的 ONON 堆栈（约 9 万三角面）实测：构建 1.4 s，序列化 0.01 s，2.4 MB 载荷到了前端解码加建几何一共 38 ms。构建只按状态付一次——结果存在状态对象上，也存在快照旁边的 `*.mesh.npz` 里，同一步再看是 0 s，重开工程也不用重算；跑完一条流程后 worker 还会起一个后台线程从最后一步往前预热。所以真正会等的是打开一个还没预热、也没有 sidecar 的步骤。构建里最贵的是把每层的**盖面**三角化：一个带孔阵列的层是一个上千顶点的多边形，GEOS 的约束 Delaunay 要 40 ms，占整个构建的一半。`v0.9.5` 起改用 `mapbox_earcut` 耳切法，同一批多边形快约 30 倍（1.5 s → 0.05 s），三角形数量完全相同（不加点不丢点的三角化必然是 `V + 2H - 2` 个），面积和体积逐位一致，只是盖面内部的对角线走向不同——共面且平坦着色，看不出区别。它是可选加速器（约 0.17 MB 的 wheel），装不上就退回 GEOS，两条路都跑同样的校验。

**Quick Sketch 的圆是 32 边形**（`SKETCH_QUAD_SEGS`，每象限 8 段）。圆周上每一段边要花**八个**显示三角形——它同时是两个筒壁的一格和四个平面盖的一个楔子——所以一个方块挖一个圆形凹槽再留根柱子，三角形数就是 `8N + 12`。这个值原来是 64（整圆 256 段，同一个结构 2060 个三角形），买到的是 26 皮米的弦高误差，比原子间距还小两个数量级，而内核工作的分辨率是 10 nm 量级。改成 32 段后同一个结构是 268 个三角形（少 7.7 倍），弦高误差是半径的 0.48%，在 340 nm 半径上是 1.6 nm，掩模面积和体积变化约 0.1%（多边形内接于圆，所以圆形掩模略小）。唯一要注意的是**很大的圆**：32 段在 50 µm 半径上要差 240 nm，这种形状建议直接画多边形。

**工作目录里不存绝对路径**：数据库里两处路径——版图（`projects.gds_path`）和每一步的计算结果（`snapshots.path`）——存的都是**相对工作目录的路径**（导入版图会把文件复制进工作目录的 `layouts/`，所以存的就是它在工作目录里的位置）。以前存的是绝对路径，工程一旦换机器、换用户目录，或者打包成 zip 发给别人再解开，就再也找不到版图，所有从版图取掩膜的步骤都跑不了——而界面上只显示 up to date，不显示"版图不见了"。旧工程里那种绝对路径（连 Windows 的 `\\?\C:\Users\...` 一起）读的时候会被认领：按文件名去这个工作目录里该类文件的固定位置找（版图在 `layouts/`，快照在 `*_snapshots/`），找到就用；找不到就原样留着，好让报错里出现的还是用户当初选的那个路径。

快照那一处更要紧：**每一步算好的结果都是按绝对路径记的**，工作目录一换位置，31 步的结果全部指向一个不存在的目录，而界面上什么都不说——步骤看着还是 ready，真去要的时候才重算或者报错。版图的路径同时也是步骤摘要（digest）的一部分，所以以前**光是复制一份工作目录就会让所有从版图取掩膜的步骤变成 stale**；摘要里现在放的是存储用的相对路径。（`mtime` 仍然在摘要里，而 zip 解压会丢掉亚秒精度，所以经过一次压缩包往返，从版图取掩膜的那几步可能白重算一次——方向是对的那一边：宁可多算，不要把改过的版图当没改。）

拿真实工程验过：把那个 31 步的 3D DRAM 工作目录（老版本写的、满是绝对路径）整个复制到另一个位置，31 步全部仍然是 ready，俯视图也照样渲染得出来。

## 多个工程同时打开（标签页）

一个窗口里每个标签页是**一整个工程**：自己的文档、选择、视图、撤销历史。切标签不是重新加载——不在屏幕上的标签**仍然挂载着**，回去的时候相机、选中的步骤、取回来的网格都还在。但它**不向 worker 要任何东西**：视图是 worker 最贵的那一半工作，而所有标签共用一个 worker。

几条规则（在 `domain/tabs.ts` 里，单独测）：

- **一个工程只在一个标签里**。两个标签自动保存同一个工程会互相覆盖对方的编辑。所以标签在打开一个工程之前先问外壳；已经有别的标签拿着它，那个标签被切到前面，这个标签什么也不做。
- **窗口永远有一个标签**：关掉最后一个留下一张主页（就是"新标签页"）。
- **关掉左边的标签不会换掉屏幕上的工程**；关掉当前这个显示它的邻居。
- 标签页**第一次被看到时才打开它的工程**，不是窗口启动时。恢复五个标签否则就是五个工程排队过一个 worker，第一个都还没能用。
- 标签上的小圆点：蓝色＝这个工程正在跑，灰色＝有没存的改动。只有一个标签时整条标签栏不显示，单工程看起来和以前一模一样。

快捷键：**Ctrl+T** 新标签，**Ctrl+W** 关标签，**Ctrl+Tab / Ctrl+Shift+Tab** 换标签，标签上**中键**也关。File 菜单里的 Recent 现在**在新标签里打开**。

因为所有标签都挂载着，有两件事必须挑出来：**键盘只发给屏幕上那个标签**（否则 Ctrl+C 会让隐藏的标签也复制它自己的选择，最后一个应答的赢），**worker 事件只发给发起它的标签**（事件带 `requestId`；没有 requestId 的一般日志给屏幕上那个标签）。否则后台标签会因为别人的运行而把自己的步骤标成 running。

代价是内存：每个标签有自己的视图缓存，而一份 3D 载荷可以有几十 MB。开十个大工程不是免费的。

**窗口不宽的时候右栏不见了**：把窗口缩到半屏，右边的 inspector 显示不全甚至完全看不见，而且**没有滚动条提示**。量出来的实情是：1120px 宽时它被切掉 52px，960px（窗口能缩到的最窄）时被切掉 **212px**——278px 宽的右栏只剩 66px。

三件事同时成立才会这样，而每一件单看都不像问题：

1. `.app-shell` 是个只写了行、没写列的 grid，于是它有一个隐式的 `auto` 列，宽度取**最宽那一行的内容**——而最宽的那行是顶栏，它的 min-content 宽度约 **1170px**。下面的 `.workspace-grid` 就被拉到 1170px，跟窗口多宽没关系。
2. 顶栏确实要 1170px，因为它 `min-width: auto`，里面的菜单栏、分支控件、Run 按钮都不能收缩。
3. `.workspace-grid` 中间那列写的是 `minmax(340px, 1fr)`——**有下限**。三列装不下时 grid 就整体溢出，而溢出去的永远是**最末那一列，也就是右栏**。

然后页面上的 `overflow: hidden` 把证据也藏了：既不出滚动条，也看不出东西被切掉。

改法对应这三条：shell 明确写 `grid-template-columns: minmax(0, 1fr)`；顶栏 `min-width: 0`，并按**指定顺序**让内容退场（先工程路径，再 wordmark，再保存状态的文字——菜单、日志和 Run 永远不动）；中间那列改成 `minmax(0, 1fr)`，**让视口先让步，而不是让侧栏掉到屏幕外**。

**三栏按比例分窗口宽度，不给 px 宽度**（`--steps-width: 21%`、`--inspector-width: 22%`）。px 宽度正是"三栏装不进某些窗口"的来源，于是要靠断点一级一级往下调，而**任何断点没预见到的宽度就会让一栏掉出屏幕**；按比例是构造上就装得下，什么都不用预见。拖动分隔条存的也是**比例**（`domain/layout.ts`），所以换台显示器、换个窗口大小，你调的布局还是你调的那个样子。约束只剩一条：中间视口至少占 26%，两侧各自 13%–45%——两边都拖到最宽时，先从 inspector 那边还回去。老版本存的是 px，读到大于 1 的数就按当前窗口宽度折成比例。

实测各宽度下 inspector 的宽度：1920px 窗口 → 422px，1440 → 317，1024 → 225，960 → 211，720 → 158。再没有断点，也没有"某个宽度突然跳一下"。

（顺带查了栏内部：720px 窗口下 inspector 只有 158px，Quick Sketch 的两个按钮会戳出去 4px——被 `overflow: hidden` 悄悄吃掉，和外面那个 bug 同一类。现在它们会换行，配方按钮的 px 下限也去掉了。）

页面的 `min-width` 原来写 960 而内容要 1170，现在是 **720**；窗口最小尺寸（`tauri.conf.json`）从 960×640 调到 **720×600**，1440 宽的屏幕上可以真正半屏并排了。

用 Chromium 从 1920px 一路量到 720px 逐个确认：右栏完整可见（≥236px）、视口有真实宽度（≥268px）、Run 按钮不被切、顶栏和页面都不溢出。`styles.test.ts` 把这几条不变量钉住了（包括"任何断点里都不许给中间列写 px 下限"），因为出错的地方正是样式表，而这种错读布局代码时完全看不出来。

## 跨工程复制粘贴

Ctrl+C / Ctrl+V 现在**跨标签、跨窗口**都能用，因为放上系统剪贴板的是完整的 JSON 载荷，任何东西都能读它——另一个标签、另一个窗口、一个脚本、一段发给同事的消息。

关键在于**一个步骤不是自足的**：它点名一个设备（tool）、一个产出材料、每个响应对应一种材料，有时还有一张 Quick Sketch。粘回原来的工程这些都在；粘到另一个工程可能都不在，而一个点名了目标工程从没听说过的材料的步骤**根本跑不了**。所以剪贴板上放的是**步骤 + 它们点名的定义**，粘贴时把接收方缺的补上。（配方不在其中：配方是个模板，加载进步骤之后步骤自己带着结果。）

两条不让人意外的规则：

- **接收方已有的定义赢**。粘贴不会悄悄改掉目标工程里氧化物的颜色，或者某台设备的产能。
- **Sketch 按 id 和内容一起匹配**。同一个 id 装着不同的图是不同的 sketch：粘进来的那张会拿到自己的新 id，于是那个步骤画的还是它原来画的东西。Sketch 是工作目录里独立的文件（不在文档里），所以粘进来的那张会立刻写盘，否则运行时读不到。

粘贴之后日志里会写清楚：从哪个工程来的、顺带加了哪些材料/设备/sketch。老版本写上剪贴板的是一个裸的步骤数组，现在读不了那种——它本来也没带依赖，粘过去也是跑不了的。

**顶栏的 Display 菜单**（只有 slab 工程有）放的是「影响网格怎么建」的开关，不是「显示什么」：三角化方式（Ear clipping / Delaunay），以及是否构建**材料之间的界面面**。

界面面这件事值得说清楚：两个材料之间那个面，在两边的网格里各有一份，视图在两个材料都显示时**两份都不画**（会抢同一批像素）。所以它们在你隐藏某个材料、要看它让出的空腔之前一文不值——而在层叠结构里它们几乎就是整个网格。5×5 孔的块实测：不建界面面 **6,572 个三角形、0.77 s、载荷 0.18 MB**；建了是 **90,644 个、1.95 s、2.41 MB**。所以默认不建，**一旦隐藏任何一个材料就自动重新取**（缓存按这个标志分开，切回去是 0 s），菜单里那个开关是强制一直建。导出 mesh 走的永远是建了界面面的那一份——文件是实体，不是视图。完全被包住、没有任何自由表面的材料会拿到一个空网格，而不是报错。sidecar 按「哪个三角化器 + 建没建界面面」分开存（`*.mesh-ears-free.npz`、`*.mesh-ears-full.npz` 等），文件里记着这两个选择，对不上就重建。

**网格什么时候建**：跑完一条流程，后台线程为**每一个执行过的步骤**建 3D 视图打开时要的那份网格（自由表面），从最后一步往前——任何一步都可能是你要点开的那步，而这份是便宜的那份。**材料之间的界面面**（隐藏材料看内部时才用到的那份，贵好几倍）不在这里建：**打开某一步的 3D 视图时，后台立刻为那一步建它**——这是"它会被用到"的第一个可靠证据，因为隐藏材料通常就是下一个动作。给一条二十来步的流程的每一步都建，是半分钟的后台 CPU，而其中绝大部分步骤根本没人打开。Display 菜单里的 **Prepare them for every step after a run**（默认关）可以要求提前全建，适合要把整条流程走一遍、边走边隐藏材料的人。

**网格建在几个核上**：一个材料的盖面和侧壁从不看别的材料的网格，所以一个层叠结构的各个材料就是天然的并行工作。线程拿不到这份收益——shapely 确实放开了解释器锁，但建网格有一半是 Python 层的顶点焊接，实测 4 线程比 1 线程还慢（0.82×）。所以用的是**进程池**：5×5 孔的块，完整网格 1.29 s → 0.42 s（**3.0×**），自由表面 0.46 s → 0.27 s（1.7×，受最慢的那个材料限制）。真实的 31 步 3D DRAM 工程（253 个 slab、7 种材料）两条路也逐位相同；它的数字见下面那一段，因为在它身上，多核只是后来才排上的第二笔账。

把状态交给子进程走的是一个临时文件，不是每个任务塞一份：每个材料一个任务才能让核都忙起来（材料之间的量差得很远，真实工程里 19.4 s 对 0.4 s），但任务的参数是要复制给子进程的，一个 22.7 MB 的状态和 7 个材料就是 159 MB 过一遍管道。写一次文件、各自从页缓存读，实测一样快（25.3 s 对 25.9 s），而且没有大小上限——这点比听起来重要：最早那版设了 8 MB 上限，结果在第一个真实工程上池根本没被用上（22.7 MB），白测了一轮"没有加速"。

几件值得知道的事：子进程一律用 `spawn` 启动，不用 `fork`——worker 里全是线程（RPC 的两条道、预热线程），fork 一个多线程进程会把别的线程正持有的锁一起带过去。代价是每个子进程要重新 import numpy/shapely/trimesh，一个 4 进程的池大约 0.9 s（打包版约 2 s），所以：池**建一次就留着**（热的时候一轮开销约 1 ms）；**没有哪次建网格会等它**——第一次建网格自己顺序建，同时在后台把池拉起来，第二次开始才走并行；4 个闲着的子进程占约 420 MB，所以**三分钟没人用就还给机器**。池在 worker 退出时被强制结束——一个还活着的子进程和 worker 本身一样会占住安装目录，那正是更新程序不能忍受的事。

这一整套是**加速器，不是依赖**：起不了进程、子进程死了、子进程读不到交接文件，任何一种情况都退回本进程顺序建，并且从此不再问。`PROCESS_STUDIO_MESH_WORKERS=1` 可以直接关掉它，命令行 `process-studio cores` 会告诉你这台机器上池能不能起来、用几个核——打包脚本就是拿这条命令做冒烟测试的，因为冻结的可执行文件要靠启动**它自己**来开子进程，这是唯一一件在源码里对、在打包后可能丢的事。`cores` 还报一个 `warmed`：只带 level set 内核的包按设计根本不装几何库（没有 3D 显示网格这回事），子进程里没东西可预热——这不是故障，池照样起得来；带 slab 内核的包如果 `warmed` 是 false，那才是打包丢了东西，脚本会让构建失败。

**网格自己也快了一大截（和核数无关）**：建一个材料的网格，开头要把**所有材料**的所有环 node 成一张公共线网、polygonize 成原子面、再对每个 slab 逐个材料做成员判定——这一整块**跟"现在建哪个材料"毫无关系**，却为每个材料各算了一遍。接着判断侧壁的那一遍是"每条边 × 每个 slab"的纯 Python 循环，同样与产出多少三角形无关。真实工程（253 个 slab、7 种材料）上这两件事是：公共图形 6.6 s × 7 = 46 s，侧壁循环 95,586 × 253 = **2400 万次** Python 步 × 7——一个只有 68 个三角形的材料和一个 277,000 个的材料花一样的时间。

现在公共图形算一次给所有材料用（`Arrangement`，一个面属于哪个材料用一个 int16 表示，靠的是同一 slab 内各材料互不重叠——`validate` 保证这点，真有重叠就直接报错而不是把两个材料网格叠在一起），侧壁那一遍交给 numpy 按边分块做。同一个工程，单核：自由表面 **88.2 s → 19.7 s**，完整网格 **118.1 s → 48.4 s**，网格逐字节相同（blake2b 指纹一致）。

走完整 RPC、含序列化，这个工程现在是：

| | 今天之前 | 单核 | 4 核 |
| --- | --- | --- | --- |
| 自由表面（打开 3D 视图那份） | 94.9 s | 21.5 s | **17.2 s** |
| 完整网格（隐藏材料那份） | 116.7 s | 42.9 s | **27.0 s** |

顺带说明为什么**自由表面这条路仍然留着**：重构之后两者的差从 1.2× 变回 2.0×（公共图形原先占了两边同样多的时间，把它摊薄之后，界面面的真实代价才显出来），而且三角形数是 343,477 对 1,111,500——3.2 倍的数据要过 RPC、进浏览器、占显存。时间差不多不等于代价差不多。

- 3D 视图底栏的 **Mesh** 下拉切换三角化方式：**Ear clipping**（默认）和 **Delaunay**。两者描述的是同一个实体——顶点数、三角形数、表面积、体积逐位一致，只有共面盖面内部的对角线走向不同，平坦着色看不出区别——所以这是个对照开关，不改变仿真结果。换一个要重建网格（`get_surfaces` 的 `triangulation` 参数），两种各自缓存，快照旁边也各存一个 sidecar（默认那个是 `*.mesh.npz`，另一个是 `*.mesh-delaunay.npz`，文件里记着是哪一种，对不上就重建）。
- 3D 视图底栏的材料图例是**显示开关**：点一下隐藏该材料的表面，再点一下显示，便于看内部结构。它只过滤已经取回的三角面，不重新计算，也不改变截面和俯视图（那两张图由 worker 渲染成 PNG）。
- 图例上**右键**（或点色块）打开这种材料在 3D 里的**临时外观**：颜色选择器和 0–100% 的不透明度滑块，立即生效，只作用于 3D 视图，不写进材料库，截面和俯视图仍用库里的颜色；**Library colour** 恢复，改过的图例块显示成虚线边框。切换工程时清空。
**把单元胞按阵列平铺显示**：3D 视图底栏的 ⊞ 按钮打开，填 X/Y 的**份数**和**周期**（µm），视图里就按这个网格画出许多份。周期默认取工程窗口的大小——窗口本来就是你把它裁成一个胞时的周期，所以默认值通常就是对的；改过之后不再跟着窗口走。

**只有画面在重复，计算没有**。这正是它划算的地方：同样的阵列如果做成几何，每一步 process、每次 harmonize、每次建网格都要多付 N 倍的多边形（实测过：在 2D 多边形这一层"算一个再平移"比整体算还慢 4–8 倍，因为 GEOS 每次调用有约 13 µs 固定开销，而每顶点的边际成本随规模下降）。而复制一份**已经建好的网格**只多一次 draw call，计算上一分钱不花——所以图里可以有阵列，尽管仿真从来没算过它。

因此：截面、俯视图、导出的 mesh **仍然是那一个胞**。这是一张图，不是一个结果。

两个细节：份数是奇数时，正中间那一份就落在被仿真的那个胞的位置上，所以打开平铺不会让屏幕上已有的东西跳位置；超过 400 份就只画一份（并提示），免得把视图变成幻灯片。打开或改动平铺时相机会跟着推远/拉近——相机参数只在画布挂载时读一次（这正是"用户转过的视角不会被每次渲染重置"的原因），所以这里单独按比例缩放相机到原点的距离，方向不动。

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

更新脚本本身有几条要紧的约束（`v0.9.2` 之前都踩过）。它是分离进程、没有控制台启动的，所以 `timeout` 用不了——没有控制台它会立刻以 “input redirection is not supported” 失败，等待就等于没等，应用还开着就开始复制，文件全被占用，结果是旧版原封不动、解包出来的新版留在旁边变成一个 `process-studio-update-XXXX` 文件夹。现在用 `ping -n 2 127.0.0.1` 睡觉，而且每段等待都有上限（两分钟），超时会在日志里留一行再继续。robocopy 的退出码现在会看：小于 8 是成功，8 及以上是真失败，走 `:failed` 分支——日志里写清楚写不进哪个目录、新版解包在哪，并把那个目录用资源管理器打开，而不是删掉新版、把旧版当成更新完了重新启动。日志开头会记下应用目录和解包目录。macOS 一侧原来是先 `rm -rf` 掉 .app 再 `ditto`，ditto 失败就什么都不剩了；现在先把旧 bundle 改名放一边，新的就位才删掉，失败就换回去。每次开始更新还会先清掉之前失败留下的 `process-studio-update-*` 目录，那里面一个就是一整份解包的程序。日志在应用目录旁边的 `process-studio-update.log`。

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

worker 二进制（或解释器加 `-m process_studio.worker`）不带参数时是 RPC 服务，带参数时是命令行工具 `process-studio`（见 [CLI](CLI.md)），所以打包产物里不需要第二个可执行文件。

两个平台现在打包方式不一样：

- **macOS / Linux**（`build_desktop.sh`）：先在 `work/venv` 建一个虚拟环境（已经激活了别的环境就直接用它；`PROCESS_STUDIO_VENV` 指定别的位置），在里面装依赖和 PyInstaller——Homebrew 或系统的 python3 不允许往自己里面装包（PEP 668 的 `externally-managed-environment`），所以不能直接 `pip install`。然后用 PyInstaller 把 worker 打成独立可执行文件放进 `desktop/src-tauri/resources/worker`。
- **Windows**（`build_desktop.ps1`）：不用 PyInstaller。脚本自己下载官方的 embeddable Python（`PROCESS_STUDIO_EMBED_PYTHON` 指定版本，默认 3.12.7）解到 `desktop/src-tauri/resources/python`，改它的 `._pth` 打开 `import site` 并加上 `Lib\site-packages`，用 `get-pip.py` 装上 pip，再装包——全是 PyPI 上的普通 wheel，不需要系统装 Python。外壳找不到 `resources/worker` 下的可执行文件时，会去找 `resources/python/python.exe` 并以 `-m process_studio.worker` 启动它（见下面「杀毒软件」一节的原因）。两条路径做完都会做一次 `describe` 冒烟测试，再执行 `npm run tauri build`。发布版通过 `PROCESS_STUDIO_WORKER` 可以覆盖 worker 路径（两个平台都适用）。

  **只带 slab 的 Windows 包装的是最小依赖集**，不是 `.[render]`：`numpy scipy pillow gdstk openpyxl pyyaml shapely trimesh`，再用 `--no-deps` 装 `process_studio` 本身，跳过 `scikit-fmm`、`scikit-image`（连带 `networkx`、`imageio`、`tifffile`、`lazy-loader`）——这些都是 level set 内核专用的（前者是快速行进求解器，后者是 marching cubes 建 3D 面），slab 从不导入到它们。`scipy` 留着：trimesh 导出 mesh 时的顶点上色内部会调这个模块，跟哪个内核无关。`matplotlib` 整体已经从 `pyproject.toml` 的核心依赖里删掉，两个内核都用不到（deviceflow 自己的 PNG 导出本来就是惰性 import 加优雅降级）。装完之后脚本还会删掉 `pip`/`setuptools`/`wheel`（构建期工具，运行时从不 import）、每个依赖包自带的 `tests`/`test` 目录和 `__pycache__`。这一套让只带 slab 的 Windows 包从 `v0.9.0` 的 139 MB（解压 354 MB）降到 `v0.9.1` 的约 65-70 MB。`full` 和 `levelset` 两个变体、以及 macOS/Linux 的 PyInstaller 打包都没有改动。

**发布与检查更新**：推一个 `v*` 标签（版本号同时写在 `pyproject.toml`、`src/process_studio/__init__.py`、`desktop/package.json`、`desktop/src-tauri/tauri.conf.json` 和 `Cargo.toml` 里）会构建两个平台的三个版本，并由 `publish` 任务发成一个 GitHub Release，资产命名为 `ProcessStudio-[Slab-|LevelSet-]Windows.zip` 和 `ProcessStudio-[Slab-|LevelSet-]macOS.zip`（外加 dmg）。首页版本号旁边的 **Check for updates** 走 worker 的 `check_update`（读 `releases/latest`，页面本身不联网），比较版本号后给出本平台本版本对应资产的 **Download** 按钮和 **Release notes**，两者都由 worker 的 `open_url` 用系统浏览器打开，且只允许仓库自己的地址。

`PROCESS_STUDIO_KERNELS` 决定这一份打包带哪些内核：不设是两个都带；设成 `slab` 或 `levelset` 就只带一个。选择被写进 worker 里的 `process_studio/kernels/enabled.txt`，另一个内核的包不进 bundle（slab 版不带 scikit-image，level set 版不带 deviceflow、shapely、trimesh）。单内核版的产品名和标识符不同（`Process Studio Slab`、`Process Studio Level Set`），可以和完整版装在同一台机器上。单内核版新建工作区不再有内核选择，打开另一个内核建的工程会明确拒绝并说明该去哪个版本打开，不会用错的内核去跑它。冒烟测试按 `PROCESS_STUDIO_KERNELS` 检查 worker 报告的内核。

`Desktop builds` 工作流跑的就是这两个脚本，打 tag 时每个平台跑三份（完整、只有 slab、只有 level set）；手动触发时可以在 GitHub 的 Run workflow 对话框里选平台和版本，只跑一个作业。构建前先执行 `python -m pytest -q`，前端测试由脚本里的 `npm run test` 负责。构建只产出应用本身：Windows 用 `--no-bundle`，交付 `ProcessStudio.exe` 加同级的 `resources/`；macOS 用 `--bundles app`，交付 `Process Studio.app`。不生成 NSIS、MSI 或 DMG。

macOS 构建固定在 `macos-14` 运行器上，`tauri.conf.json` 里 `signingIdentity` 设为 `-`，由 Tauri 在打包时用 ad-hoc 身份签名整个 bundle，DMG 因此是从已签名的 app 生成的。

只靠链接器留下的签名是不够的：那只覆盖可执行文件，bundle 没有 `_CodeSignature/CodeResources`，Info.plist 也未绑定，`spctl` 会报 `code has no resources but signature indicates they must be present`，内核在启动时直接 SIGKILL。CI 因此在打包前检查该文件存在并跑 `codesign --verify --strict`，不通过就让构建失败。

**删不掉安装文件夹、提示「正在被占用」**：是 worker 进程还活着。它住在 `resources/python/`（Windows）或 `.app` 里（macOS），一个还在运行的解释器会锁住自己所在的目录，所以整个文件夹删不掉——而且因为过去没有任何代码去结束它，一个 worker 可以熬过好几次应用会话。`v0.9.6` 起外壳在 `RunEvent::Exit` 时先关掉 worker 的 stdin（让它把手里的东西写完再走），三秒内不走就直接结束它；worker 自己那边也不再无限期 `join()` 执行线程——stdin 关闭后只给两秒宽限，之后不管它们干到哪都退出（都是 daemon 线程）。这两件事都必要：光关 stdin 不够，因为 worker 只在两个请求之间才会注意到，而构建显示网格这类活**根本没有取消检查**；光杀也不行，因为杀在写快照中间会留下半个文件。如果你现在手上还有删不掉的目录：Windows 用任务管理器结束 `python.exe`（命令行里带 `process_studio.worker` 那个），macOS 用 `pkill -f process-studio-worker`。

**杀毒软件把 Windows 的 worker 删了（`v0.9.0` 之前的版本）**：`v0.8.x` 及更早的 Windows 包用 PyInstaller，杀毒或终端防护软件（Windows Defender、CrowdStrike 之类）经常把这类单文件可执行程序当可疑文件删掉，报 `The packaged process worker was not found`。`v0.9.0` 起 Windows 包不再用 PyInstaller，改成上面说的 embeddable Python + 普通 wheel，`python.exe` 是官方签名的解释器，安装的包都是 PyPI 正式发行的 wheel，不再是这类工具典型的误报目标。仍然遇到同样报错时：到 Windows 安全中心 → 病毒和威胁防护 → 保护历史记录里恢复并允许，或者把解压目录加入排除项，再重新解压一次；公司电脑可能要找 IT 放行。另一条路是不依赖打包的 worker：`pip install "process-studio[render] @ git+https://github.com/lisiyuan2005/process-studio@v0.9.5"`，发布版的外壳在找不到打包 worker 时会依次找 `PROCESS_STUDIO_WORKER`、`resources/python/python.exe`、PATH 和 pip 的 Scripts 目录（Windows 的 `%APPDATA%\Python\Python3xx\Scripts`、`%LOCALAPPDATA%\Programs\Python\Python3xx\Scripts`；macOS/Linux 的 `~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin`）里的 `process-studio-worker`。

**「Could not check: certificate verify failed / unable to get local issuer certificate」**：这跟网络和 GitHub 都没关系，是打包版的 Python 不知道该信任谁。它自带的 OpenSSL 是在构建机上编译的，里面写死的 CA bundle 路径在用户机器上指向一个不存在的文件，于是任何 https 请求都验不过证书。`v0.9.5` 起更新检查显式构造验证上下文（`update.verifier()`）：优先用 **truststore** 读**操作系统自己的证书库**——那是这台机器的浏览器信任的同一套，因此公司 IT 为 https 检查代理装进去的根证书也在里面，这是任何我们自带的 bundle 都不可能知道的；读不到就退回自带的 **certifi**，再不行才用解释器自己的默认值。两个包都进了依赖和 Windows slab 的显式安装清单（`--no-deps` 装的那份原来两个都没有）。证书验证失败的报错也不再直接把 OpenSSL 那行原样抛给用户，而是说明这是信任库的问题、公司网络该怎么办、以及去发布页用浏览器下载总是可行。

首次打开会提示「Apple 无法验证此 App」，这是公证检查，不是签名失败。macOS 15 起右键打开不再绕过它，需到系统设置的隐私与安全性里点「仍要打开」，或执行 `xattr -dr com.apple.quarantine "/Applications/Process Studio.app"` 清掉隔离标记。

worker 的查找顺序：`PROCESS_STUDIO_WORKER` 指定的路径 → 平台资源目录（`.app` 里的 `Contents/Resources`、Linux 包的 `/usr/lib/<产品名>`）或可执行文件所在目录下的 `resources/worker/<worker>`（两个位置都试，免安装布局靠后一个）→ Windows 上同样两个位置下的 `resources/python/python.exe`（找到就以 `-m process_studio.worker` 启动，并把应用根目录通过 `PROCESS_STUDIO_APP_ROOT` 传给它，供自动更新定位）→ PATH 和 pip 的 per-user 脚本目录里 pip 装出来的 `process-studio-worker`。开发模式（`npm run tauri dev`）不走这条链，直接调本机 `python3 -m process_studio.worker`。


## 尚未实现

- 细化执行（`run_refined_branch`）还没有接到界面，仍需从 API 或示例脚本调用。
- 分支只能切换，创建、重命名和删除还没有界面入口；`storage.create_branch` 已经就绪。
- 前端不显示 CMP dishing、晶向湿蚀等未实现的物理，因为内核本身没有实现它们。
