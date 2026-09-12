# 命令行：`process-studio`

`process-studio` 是 worker 的第二个客户端，和桌面外壳并列：它走同一个 `dispatch()`，读写同一份 document，保存走同一条路径。所以命令行改过的工作目录桌面能直接打开，反过来也一样；两边看到的步骤状态（ready / stale / not run）是同一套摘要链算出来的。

## 安装

```bash
pip install -e .            # 在仓库根目录；装完就有 process-studio 命令
pip install -e ".[yaml]"    # 流程文件要用 YAML 的话
```

打包好的桌面版也带着它：worker 二进制不带参数时是 RPC 服务，带参数时就是这个命令行。桌面顶栏的 **CLI** 按钮打开一个控制台：把命令（一行一条）或整个流程文件粘进去点 Run，就在桌面打开的工作目录里执行，界面跟着更新；下面还把当前状态对应的常用命令连同程序的完整路径列出来，点 Copy 粘到终端就能跑。

```bash
# macOS
"/Applications/Process Studio Slab.app/Contents/Resources/worker/process-studio-worker" steps list
# Windows
.\resources\worker\process-studio-worker.exe steps list
```

## 工作目录

大多数命令要在一个工作目录里执行。查找顺序：`--root DIR`，环境变量 `PROCESS_STUDIO_ROOT`，然后从当前目录向上找带 `process_studio.sqlite3` 的目录（和 git 找仓库一样）。

```bash
process-studio new ~/devices/dram --kernel slab --name "1T1C"
cd ~/devices/dram
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `new DIR [--name N] [--kernel slab\|levelset]` | 新建工作目录；内核只能在这里选 |
| `kernels` | 这份构建带的内核 |
| `info` | 工程、内核、窗口、精度、步骤数、材料 |
| `status` | 每步的状态 |
| `steps list` / `steps show STEP` | 步骤列表 / 一步的全部设置 |
| `steps add TYPE [--after STEP\|--before STEP] [选项]` | 加一步；TYPE 是 deposit / etch / cmp / no_geometry |
| `steps set STEP [选项]` | 改一步 |
| `steps rm STEP...` / `dup STEP` / `mv STEP POS` | 删除 / 原位复制 / 移到第 POS 位 |
| `steps skip STEP...` / `include STEP...` | 跳过 / 放回运行 |
| `run [--through STEP] [--force]` | 运行；结果还有效的步骤直接复用 |
| `window [--x A B] [--y A B] [--z A B] [--spacing NM] [--spacing-xy NM]` | 看或改工程窗口和精度（改动会丢弃全部结果）；slab 工程 `--spacing` 是 z 步长，`--spacing-xy` 是 XY 弧线弦高，0 表示跟随 z |
| `view section --step STEP [--axis x\|y --at UM \| --line X0 Y0 X1 Y1] -o cut.png` | 截面 PNG |
| `view top --step STEP -o top.png` | 俯视图 PNG |
| `view mesh --step STEP -o step.glb` | 3D 表面，.glb / .gltf / .obj / .stl / .ply |
| `materials list\|add NAME [--category --color --opacity]\|rm NAME` | 材料库 |
| `recipes list\|export FILE.xlsx\|import FILE.xlsx` | Recipe 库，按类型和分组列出 |
| `tools list\|add NAME [--group G] [--notes N]\|rm NAME` | 工具库；分组用斜杠分子组，如 `Etch/Dry` |
| `sketch list\|show ID\|export ID FILE\|import ID FILE` | Quick Sketch |
| `lines list\|add NAME X0 Y0 X1 Y1\|rm NAME` | 保存的 AA–BB 截面线；`view section --named NAME` 沿其中一条切 |
| `flow dump [FILE]` / `flow apply FILE` | 整条流程写成一个文件 / 按文件设置工作目录；`FILE` 写 `-` 从 stdin 读（JSON 以 `{` 开头，否则按 YAML） |
| `log [-n N]` | worker 记录的运行日志 |
| `rpc METHOD [JSON\|@file]` | 直接调 RPC 方法，给脚本用 |

`STEP` 是 `steps list` 里的序号，也可以写步骤名；`view` 里 `0` 表示裸片，不写表示最后一步。

步骤选项（`add` 和 `set` 通用）：`--name`、`--tool`、`--material`（沉积的材料）、`--set KEY=VALUE`（可重复，数字和 true/false 保持类型）、`--unset KEY`、`--mask none|sketch:ID|gds:LAYER/DATATYPE`、`--keep inside|outside`、`--rate MATERIAL=µm/min`、`--stop MATERIAL`、`--no-response MATERIAL`。

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

`flow dump flow.yaml` 把工程写成下面这样；`flow apply flow.yaml` 反过来（`cat flow.json | process-studio flow apply -` 也行）。`apply` 时步骤按位置保留身份：只改了第 4 步参数的文件，重新运行时前 3 步直接复用，和在桌面里改一样。`--root DIR` 指向还不存在的目录时会先按文件里的 `kernel` 建好工作目录。

```yaml
name: 1T1C
kernel: slab
window: {x: [-0.8, 0.8], y: [-0.8, 0.8], z: [-0.8, 0.4]}
resolution_nm: 10          # slab 的 z 步长；level set 内核写 spacing_nm
resolution_xy_nm: 10       # slab 的 XY 弧线弦高，省略则跟随 z
materials:
  - {name: W, category: Metal, color: "#7f8790"}
sketches:
  default:
    shapes:
      - {kind: circle, operation: merge, parameters: {center: [0, 0], radius: 0.22}}
steps:
  - {name: Trench Etch, type: etch, mask: sketch:default, parameters: {target: 0.3, directional_fraction: 1.0}, rates: {Si: 0.12}}
  - {name: Liner, type: deposit, material: TiN, parameters: {target: 0.02, mode: conformal}}
  - {name: Fill, type: deposit, material: W, parameters: {target: 0.2, mode: conformal}}
  - {name: CMP, type: cmp, parameters: {target_z: 0.0}}
```

JSON 和 TOML 也能读；`dump` 写 JSON 或 YAML。
