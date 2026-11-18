"""命令行入口：process3d validate / run / inspect。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .recipe import RecipeError, load

EXIT_OK, EXIT_INPUT, EXIT_RUN = 0, 2, 3


def main(argv=None):
    ap = argparse.ArgumentParser(prog="process3d", description="Nowprocess3D 3D 工艺结构构建")
    ap.add_argument("--version", action="version", version=f"process3d {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="校验配方，不做几何计算")
    v.add_argument("recipe")

    r = sub.add_parser("run", help="执行配方（重计算，请在计算节点运行）")
    r.add_argument("recipe")
    r.add_argument("--out", required=True)
    r.add_argument("--backend", default="viennaps")
    r.add_argument("--grid", type=float, default=2.0, help="网格间距 nm")
    r.add_argument("--glb", choices=["all", "final", "none"], default="all")
    r.add_argument("--no-states", action="store_true", help="不保存后端原生状态")
    r.add_argument("--checks", help="精度检查文件（探测线界面的解析期望值）")

    i = sub.add_parser("inspect", help="查看运行目录的状态与每步性能")
    i.add_argument("run_dir")

    a = ap.parse_args(argv)
    if a.cmd in ("validate", "run"):
        try:
            rec = load(a.recipe)
        except RecipeError as e:
            print("配方校验失败：", file=sys.stderr)
            for d in e.diagnostics:
                print("  - " + d, file=sys.stderr)
            return EXIT_INPUT
        if a.cmd == "validate":
            print(rec.summary())
            print("OK")
            return EXIT_OK
        from .runner import Run, perf_table
        try:
            Run(rec, Path(a.out), a.backend, a.grid, a.glb, not a.no_states, a.checks).execute()
        except FileExistsError as e:
            print(e, file=sys.stderr)
            return EXIT_INPUT
        except Exception as e:
            print(f"运行失败：{e}（详见 {a.out}/events.jsonl）", file=sys.stderr)
            return EXIT_RUN
        print(perf_table(Path(a.out)))
        return EXIT_OK
    if a.cmd == "inspect":
        from .runner import perf_table
        d = Path(a.run_dir)
        print(json.loads((d / "status.json").read_text()))
        print(perf_table(d))
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
