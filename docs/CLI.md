# 命令行：`process-studio`

`process-studio` 是 worker 的第二个客户端，和桌面外壳并列：它走同一个 `dispatch()`，读写同一份 document，保存走同一条路径。所以命令行改过的工作目录桌面能直接打开，反过来也一样；两边看到的步骤状态（ready / stale / not run）是同一套摘要链算出来的。摘要把整数值的浮点数（流程文件里的 `1.0`）和整数（桌面 JSON 里的 `1`）算作同一个值，所以桌面保存一次命令行建的工作目录不会让步骤变 stale。

## 安装

```bash
pip install -e .            # 在仓库根目录；装完就有 process-studio 命令
```

打包好的桌面版也带着它：worker 二进制不带参数时是 RPC 服务，带参数时就是这个命令行。桌面顶栏的 **CLI** 按钮打开一个控制台：把命令（一行一条）或整个流程文件粘进去点 Run，就在桌面打开的工作目录里执行，界面跟着更新；下面还把当前状态对应的常用命令连同程序的完整路径列出来，点 Copy 粘到终端就能跑。

```bash
# macOS
"/Applications/Process Studio.app/Contents/Resources/worker/process-studio-worker" steps list
# Windows
.\resources\worker\process-studio-worker.exe steps list
```

## 工作目录

大多数命令要在一个工作目录里执行。查找顺序：`--root DIR`，环境变量 `PROCESS_STUDIO_ROOT`，然后从当前目录向上找带 `process_studio.sqlite3` 的目录（和 git 找仓库一样）。

```bash
process-studio new ~/devices/dram --name "1T1C"
cd ~/devices/dram
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `new DIR [--name N]` | 新建工作目录 |
| `kernels` | 这份构建带的内核（只有 slab；`0.9.8` 之后 level set 已删除，用它建的工程会被拒绝并说明去哪个版本打开）|
| `cores` | 3D 网格能用几个核；池起不来时说明原因（见桌面文档「网格建在几个核上」）|
| `info` | 工程、内核、窗口、精度、步骤数、材料 |
| `status` | 每步的状态 |
| `steps list` / `steps show STEP` | 步骤列表 / 一步的全部设置 |
| `steps add TYPE [--after STEP\|--before STEP] [选项]` | 加一步；TYPE 是 deposit / etch / cmp / no_geometry / oxidation / flip |
| `steps set STEP [选项]` | 改一步 |
| `steps rm STEP...` / `dup STEP` / `mv STEP POS` | 删除 / 原位复制 / 移到第 POS 位 |
| `steps skip STEP...` / `include STEP...` | 跳过 / 放回运行 |
| `steps loop STEP... [--repeat N] [--name NAME]` / `steps unloop STEP` | 把相邻的几步做成重复 N 次的循环（这几步是第 1 次，后面接上 N−1 份副本）/ 拆开循环，各次留下成为普通步骤 |
| `run [--through STEP] [--force]` | 运行；结果还有效的步骤直接复用 |
| `fidelity [detailed\|simplified]` | 看或切换膜模型：detailed 圆角、按分辨率采样；simplified（默认）直角、每个平面一段，快得多。两档的结果分开保存，切回去不用重算 |
| `window [--x A B] [--y A B] [--z A B] [--spacing NM] [--spacing-xy NM]` | 看或改工程窗口和几何分辨率（改动会丢弃全部结果）：`--spacing` 是 z 步长，`--spacing-xy` 是 XY 弧线弦高（0 表示跟随 z）；衬底厚度就是窗口的深度，`--z -0.2 0.4` 就是 200 nm 的衬底 |
| `view section --step STEP [--axis x\|y --at UM \| --line X0 Y0 X1 Y1] -o cut.png` | 截面 PNG |
| `view top --step STEP [--no-steps] -o top.png` | 俯视图 PNG，按每点最上层材料的颜色画；同一材料自己的高度分界默认画一条压暗的线，`--no-steps` 关掉 |
| `view mesh --step STEP -o step.glb` | 3D 表面，.glb / .gltf / .obj / .stl / .ply |
| `materials list\|add NAME [--category --color --opacity]\|rm NAME` | 材料库 |
| `templates list\|export FILE.xlsx\|import FILE.xlsx` | step template 库（原 `recipes`，老名字仍认），按类型和分组列出 |
| `tools list\|add NAME [--group G] [--notes N] [--recipe NAME]…\|rm NAME` | 工具库；分组用斜杠分子组，如 `Etch/Dry`；`--recipe` 是这台机器上装的配方（可重复，给了就整份替换），步骤用 experiment 的 `tool_recipe` 记下用的哪条 |
| `sketch list\|show ID\|export ID FILE\|import ID FILE` | Quick Sketch |
| `branches list\|add NAME [--after STEP]\|use BRANCH\|rename BRANCH NAME\|rm BRANCH` | 工艺分叉（split）：`add` 把当前分支在某一步之后分出一条新分支，分叉点之前的步骤和**已经算好的结果**一起带过去，之后自动切到新分支；`rm` 只删这条分支独有的结果 |
| `lines list\|add NAME X0 Y0 X1 Y1\|rm NAME` | 保存的 AA–BB 截面线；`view section --named NAME` 沿其中一条切 |
| `flow dump [FILE]` / `flow apply FILE` | 整条流程写成一个文件 / 按文件设置工作目录；`FILE` 写 `-` 从 stdin 读（JSON 以 `{` 开头，否则按 YAML） |
| `log [-n N]` | worker 记录的运行日志 |
| `rpc METHOD [JSON\|@file]` | 直接调 RPC 方法，给脚本用 |

`STEP` 是 `steps list` 里的序号，也可以写步骤名；`view` 里 `0` 表示裸片，不写表示最后一步。

步骤选项（`add` 和 `set` 通用）：`--name`、`--tool`、`--material`（沉积的材料）、`--set KEY=VALUE`（可重复，数字和 true/false 保持类型）、`--unset KEY`、`--mask none|sketch:ID|gds:LAYER/DATATYPE`、`--keep inside|outside`、`--rate MATERIAL=µm/min`、`--stop MATERIAL`、`--no-response MATERIAL`。

**两套参数**：`--set` 改的是 simulation 那套（内核读的）。机器实际设成什么用 `--set-experiment KEY=VALUE`（可重复）、`--unset-experiment KEY`、`--same-experiment`（放弃自己那套，回到"和 simulation 一样"）。第一次 `--set-experiment` 会先复制一份 simulation 的值，所以只要写不一样的地方。内核不读这套，步骤摘要也不含它，**补记机台设置不会让已算好的结果过期**。`steps show` 两套都列。

`--tool` 里带 ALD/ALE/MLD 时，新加的沉积步骤默认参数是 `cycles` 和 `rate_per_cycle`，不是 `target`。

全局选项：`--root DIR`、`--json`（结果输出为 JSON，进度和提示仍在 stderr）、`-q`。

退出码：0 成功；2 参数或引用错误；3 工作目录打不开；4 运行失败（内核报错）；130 被中断。

## 例子

```bash
process-studio steps add deposit --name "TiN liner" --material TiN --set target=0.02 --set mode=conformal
process-studio steps set "Trench Etch" --set target=0.3 --rate Si=0.12
process-studio run
process-studio view section --line -0.6 -0.6 0.6 0.6 -o diag.png
process-studio --json status | jq '.stepStatuses'
```

参数扫描：

```bash
for t in 0.02 0.04 0.06; do
  process-studio -q steps set "TiN liner" --set target=$t
  process-studio -q run
  process-studio -q view section -o liner-$t.png
done
```

## 流程文件

`flow dump flow.yaml` 把工程写成下面这样；`flow apply flow.yaml` 反过来（`cat flow.json | process-studio flow apply -` 也行）。`apply` 时步骤按位置保留身份：只改了第 4 步参数的文件，重新运行时前 3 步直接复用，和在桌面里改一样。`--root DIR` 指向还不存在的目录时会先建好工作目录。

```yaml
name: 1T1C
kernel: slab
window: {x: [-0.8, 0.8], y: [-0.8, 0.8], z: [-0.8, 0.4]}
resolution_nm: 10          # z 步长（保形沉积和各向同性刻蚀沿高度的采样）
resolution_xy_nm: 10       # XY 弧线弦高，省略则跟随 z
fidelity: detailed         # 膜模型：detailed 或 simplified（默认）
materials:
  - {name: W, category: Metal, color: "#7f8790"}
sketches:
  default:
    shapes:
      - {kind: circle, operation: merge, parameters: {center: [0, 0], radius: 0.22}}
steps:
  - {name: Trench Etch, type: etch, mask: sketch:default, parameters: {target: 0.3, directional_fraction: 1.0}, rates: {Si: 0.12}}
  - {name: Liner, type: deposit, material: TiN, parameters: {target: 0.02, mode: conformal}}
  - {name: HZO, type: deposit, material: HZO, tool: ALD, parameters: {cycles: 240, rate_per_cycle: 0.0009, mode: conformal},
     experiment: {cycles: 240, rate_per_cycle: 0.0009, tool_recipe: Siva_HZO_300C, time_min: 42}}   # 机器实际设成什么；省略表示和上面一样
  - {name: Fill, type: deposit, material: W, parameters: {target: 0.2, mode: conformal}}
  - {name: CMP, type: cmp, parameters: {target_z: 0.0}}
  - {name: Gate oxide, type: oxidation, material: SiO2, parameters: {target: 0.005}, rates: {Si: 1}}
  - loop: ON pair            # 一个循环：里面的步骤按顺序重复 repeat 次
    repeat: 4
    steps:
      - {name: Oxide, type: deposit, material: SiO2, parameters: {target: 0.02, mode: conformal}}
      - {name: Nitride, type: deposit, material: SiN, parameters: {target: 0.03, mode: conformal}}
```

循环（`loop` 条目）在工作目录里展开成真实的步骤：每一次都是列表里的一步，带着 `loop: {id, name, repeat, iteration}` 标记，`steps list` 的 Loop 列显示「ON pair 2/4」。`flow dump` 把同一个循环折回一个条目（只写第 1 次）；文件里循环可以嵌套，展开时内层并入外层。

沉积的厚度有三种写法，内核按这个顺序读：`target`（直接给厚度）、`cycles × rate_per_cycle`（ALD 的写法）、`time_min × rate`。刻蚀和氧化同样可以给 `target` 或时间×速率。

`flip`：把整片翻过来，之后的步骤做在原来的背面。没有参数，只有可选的 `axis`（`y` 默认，绕 y 轴翻，横向镜像 x；`x` 则镜像 y）。体积不变，翻两次回到原样。

`oxidation`：`rates` 里列出会被氧化的材料，`parameters.target` 是被消耗的厚度（按各材料的 rate 比例），`material` 是生成的氧化物（默认 SiO2）。露出的表面向内 `target` 那一层原地变成氧化物，不模拟体积膨胀。

JSON 和 TOML 也能读；`dump` 写 JSON 或 YAML。
