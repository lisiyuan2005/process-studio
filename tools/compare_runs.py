"""对比多个运行：每步耗时/内存、精度检查、体积、与参考运行的逐体素一致率。

用法：python tools/compare_runs.py runs/A runs/B [...] [--ref runs/A] [--json out.json]
"""
import argparse
import json
from pathlib import Path

import numpy as np

from nowprocess3d.voxel import MaterialGrid


def load(run):
    run = Path(run)
    p = json.loads((run / "perf.json").read_text())
    m = json.loads((run / "manifest.json").read_text())
    return {"dir": run, "name": f"{m['backend']}@{p['grid_nm']:g}nm", "perf": p, "manifest": m}


def voxel_agreement(a: MaterialGrid, b: MaterialGrid):
    """在共同覆盖区域（同一网格间距、同一体素中心）比较材料编号。"""
    if abs(a.spacing - b.spacing) > 1e-9 or a.materials != b.materials:
        return None
    g = a.spacing
    lo = [max(a.origin[i], b.origin[i]) for i in range(3)]
    hi = [min(a.origin[i] + a.grid.shape[i] * g, b.origin[i] + b.grid.shape[i] * g) for i in range(3)]
    n = [int(round((hi[i] - lo[i]) / g)) for i in range(3)]
    if min(n) <= 0:
        return None
    sa = tuple(slice(int(round((lo[i] - a.origin[i]) / g)), int(round((lo[i] - a.origin[i]) / g)) + n[i]) for i in range(3))
    sb = tuple(slice(int(round((lo[i] - b.origin[i]) / g)), int(round((lo[i] - b.origin[i]) / g)) + n[i]) for i in range(3))
    A, B = a.grid[sa], b.grid[sb]
    solid = (A > 0) | (B > 0)
    diff = (A != B) & solid
    per = {}
    for k, m in enumerate(a.materials, 1):
        ia, ib = A == k, B == k
        u = (ia | ib).sum()
        per[m] = round(float((ia & ib).sum() / u), 4) if u else None
    return {"disagree_voxels": int(diff.sum()), "disagree_frac_of_solid": round(float(diff.sum() / max(solid.sum(), 1)), 5),
            "iou": per}


def main():
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--ref")
    ap.add_argument("--json")
    ap.add_argument("--checks", default="examples/nand10/checks.yaml")
    a = ap.parse_args()
    excl = set()
    if Path(a.checks).exists():
        excl = {tuple(x) for x in (yaml.safe_load(open(a.checks)).get("exclude_from_stats") or [])}
    runs = [load(r) for r in a.runs]
    ref = load(a.ref) if a.ref else runs[0]
    out = {"runs": [], "ref": ref["name"]}

    print("== 每步：计算 s / 体素化 s / 计算阶段峰值 MB / 体素化阶段峰值 MB")
    ids = [s["id"] for s in ref["perf"]["steps"]]
    hdr = f"{'step':<17}" + "".join(f"{r['name']:>34}" for r in runs)
    print(hdr)
    for sid in ids:
        row = f"{sid:<17}"
        for r in runs:
            s = next((x for x in r["perf"]["steps"] if x["id"] == sid), None)
            if not s:
                row += f"{'-':>34}"
                continue
            c, v = s["compute"], s.get("voxelize", {})
            row += f"{c['wall_s']:>8.2f}{v.get('wall_s', float('nan')):>8.2f}{c.get('phase_peak_mb', float('nan')):>9.0f}{v.get('phase_peak_mb', float('nan')):>9.0f}"
        print(row)

    print("\n== 汇总")
    for r in runs:
        st = r["perf"]["steps"]
        tot = lambda k: sum(s.get(k, {}).get("wall_s", 0) for s in st)
        cpu = sum(s["compute"]["cpu_s"] for s in st)
        pk = lambda k: max(s.get(k, {}).get("phase_peak_mb", 0) for s in st)
        errs = [e for s in st for k, c in (s.get("checks") or {}).items() if (s["id"], k) not in excl
                for e in c["errors"]]
        excl_err = {f"{s['id']}.{k}": c["max_abs_err"] for s in st for k, c in (s.get("checks") or {}).items()
                    if (s["id"], k) in excl}
        miss = sum(len(c["missing"]) for s in st for c in (s.get("checks") or {}).values())
        extra = sum(len(c["extra"]) for s in st for c in (s.get("checks") or {}).values())
        nexp = sum(c["n_expected"] for s in st for c in (s.get("checks") or {}).values())
        summ = {"name": r["name"], "dir": str(r["dir"]),
                "compute_s": round(tot("compute"), 2), "compute_cpu_s": round(cpu, 1),
                "voxelize_s": round(tot("voxelize"), 2), "save_state_s": round(tot("save_state"), 2),
                "export_s": round(tot("export"), 2), "checks_s": round(tot("checks_perf"), 2),
                "peak_compute_mb": pk("compute"), "peak_voxelize_mb": pk("voxelize"),
                "peak_process_mb": max(s["compute"]["peak_rss_mb"] for s in st),
                "checks_n": nexp, "checks_matched": len(errs), "checks_missing": miss, "checks_extra": extra,
                "check_max_abs_err": round(max(map(abs, errs)), 3) if errs else None,
                "check_rms_err": round(float(np.sqrt(np.mean(np.square(errs)))), 3) if errs else None,
                "excluded_checks_max_err": excl_err}
        out["runs"].append(summ)
        print(json.dumps(summ, ensure_ascii=False))

    print("\n== 精度检查（每步每条探测线：最大绝对误差 nm；缺失/多余界面数）")
    for sid in ids:
        row = f"{sid:<17}"
        for r in runs:
            s = next((x for x in r["perf"]["steps"] if x["id"] == sid), {})
            cs = s.get("checks") or {}
            row += "  " + " ".join(
                f"{k}:{v['max_abs_err'] if v['max_abs_err'] is not None else '-'}"
                + (f"/m{len(v['missing'])}" if v["missing"] else "") + (f"/x{len(v['extra'])}" if v["extra"] else "")
                for k, v in cs.items())
        print(row)

    print("\n== 体素一致率（相对参考 %s，同网格才比较）" % ref["name"])
    out["agreement"] = {}
    for r in runs:
        if r is ref or r["dir"] == ref["dir"]:
            continue
        res = {}
        for sid in ids:
            fa = next(ref["dir"].glob(f"voxels/*_{sid}.npz"), None)
            fb = next(r["dir"].glob(f"voxels/*_{sid}.npz"), None)
            if fa and fb:
                res[sid] = voxel_agreement(MaterialGrid.load(fa), MaterialGrid.load(fb))
        out["agreement"][r["name"]] = res
        for sid, v in res.items():
            if v:
                print(f"{r['name']:<16} {sid:<17} 不一致 {v['disagree_frac_of_solid']*100:6.3f}%  IoU {v['iou']}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
