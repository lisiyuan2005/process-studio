# Nowprocess3D

3D 工艺结构构建与仿真。决策与规格见 `Decisions.md`，架构草案见 `Structure.md`。

## 目录

| 路径 | 内容 |
|---|---|
| `src/nowprocess3d/` | Python 包：`recipe.py` 配方校验，`runner.py` 逐步执行与性能记录，`voxel.py` 体素/截面/GLB，`probe.py` 精度检查 |
| `src/nowprocess3d/backends/` | `viennaps_backend.py`；`openvdb_backend.py` + C++ 扩展 `_vdbcore*.so` |
| `cpp/lsvox/` | ViennaPS 体素化 C++ 扩展：读取 .lvst，按 ToVoxelMesh 规则写稠密材料数组（`jobs/build_lsvox.sge` 编译） |
| `third_party/` | ViennaLS 5.10.0、ViennaHRLE 1.1.2、ViennaCore 2.2.1 头文件（与 ViennaPS 4.7.0 wheel 同版本；不入库，用 `jobs/fetch_third_party.sh` 下载） |
| `cpp/vdbcore/` | OpenVDB C++ 扩展源码（nanobind）：稠密↔VDB、CSG、周期欧氏偏移、表面网格、写 .vdb |
| `examples/nand10/checks.yaml` | B1 精度检查：探测线上界面位置的解析期望值 |
| `tools/compare_runs.py` | 对比多个运行：每步耗时/内存、精度、逐体素一致率 |
| `examples/nand10/recipe.yaml` | 基准 B1：10 条字线 3D NAND 单元格 |
| `tests/tiny_recipe.yaml` | 极小配方，登录节点上几秒跑完，用于检查链路 |
| `jobs/` | qsub 脚本：`setup_env.sge`（.venv，ViennaPS）、`setup_vdb.sge`（.conda-vdb，OpenVDB 14）、`build_vdbcore.sge` 编译 C++ 扩展、`run_b1.sge` 跑 B1 |
| `viewer/` | `build_viewer.py` 把运行目录打包成网页（GLB + 截面 + 性能表） |
| `runs/<name>_<jobid>/` | 运行结果，见下 |

## 从零搭建

```bash
qsub jobs/setup_env.sge            # .venv：ViennaPS 后端
qsub jobs/setup_vdb.sge            # .conda-vdb：OpenVDB 后端
qsub jobs/build_vdbcore.sge        # 编译 OpenVDB C++ 扩展
jobs/fetch_third_party.sh && qsub jobs/build_lsvox.sge   # 编译 ViennaPS 体素化扩展
```

不在版本库中：两个 Python 环境、`third_party/`、编译产物、`runs/`（除对比结果）、`site/`。

## 运行

```bash
# 登录节点：只做校验与小例子
module load python/3.12.15
.venv/bin/process3d validate examples/nand10/recipe.yaml
.venv/bin/process3d run tests/tiny_recipe.yaml --out /tmp/$USER/tiny --grid 1

# 计算节点：完整基准（8 核作业近期排队较久时可用 run_b1_s4.sge，4 核）（两个后端；GRID 为网格间距 nm；GLB=all|final|none）
qsub -v BACKEND=viennaps,GRID=2,GLB=all jobs/run_b1.sge
qsub -v BACKEND=openvdb,GRID=1,GLB=none jobs/run_b1.sge
python tools/compare_runs.py runs/b1_viennaps_g2_<id> runs/b1_openvdb_g2_<id>
.venv/bin/process3d inspect runs/nand10_g2_<jobid>     # 状态 + 每步性能表
.venv/bin/python viewer/build_viewer.py runs/nand10_g2_<jobid> site/<name>
```

退出码：0 成功，2 输入错误（配方或输出目录已存在），3 运行失败（详见 `events.jsonl`）。

## 运行目录

```
manifest.json   软件与后端版本、网格、完整配方
recipe.yaml     本次实际使用的配方
status.json     running / done / failed 及当前步骤
events.jsonl    逐步事件，失败时含 traceback
perf.json       每步：compute / save_state / voxelize / export / checks_perf 各自的 wall、CPU、
                阶段前后 RSS、阶段内峰值（20 ms 采样）、进程峰值；精度检查结果 checks；
                level set 个数与点数、各材料体积、GLB 面数；host 与核数
states/         后端原生状态：ViennaLS .lvst（每层一个文件）或 OpenVDB .vdb（每种材料一个网格）
voxels/         与后端无关的材料体素网格（.npz，格式 nowprocess3d.voxel/0.1）
exports/        每步截面 PNG 与 GLB
```
