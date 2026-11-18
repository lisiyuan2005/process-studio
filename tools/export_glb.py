"""从运行目录里已保存的体素网格补导每步的 GLB（运行时用了 --glb none 时使用）。

用法：python tools/export_glb.py runs/<run> [--max-faces 60000]
"""
import argparse
import json
import time
from pathlib import Path

import yaml

from nowprocess3d.voxel import MaterialGrid, export_glb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--max-faces", type=int, default=60000)
    a = ap.parse_args()
    run = Path(a.run)
    mats = yaml.safe_load((run / "recipe.yaml").read_text())["materials"]
    log = {}
    for f in sorted((run / "voxels").glob("*.npz")):
        out = run / "exports" / f"{f.stem}.glb"
        t = time.perf_counter()
        faces = export_glb(MaterialGrid.load(f), mats, out, max_faces=a.max_faces)
        log[f.stem] = {"faces": faces, "wall_s": round(time.perf_counter() - t, 2),
                       "bytes": out.stat().st_size}
        print(f.stem, log[f.stem], flush=True)
    (run / "exports" / "glb_export.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
