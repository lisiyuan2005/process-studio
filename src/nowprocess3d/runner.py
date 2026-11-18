"""执行配方：逐步计算，每步保存状态、体素、截面、GLB 与性能参数。"""
from __future__ import annotations

import datetime as dt
import json
import platform
import shutil
import sys
import traceback
from pathlib import Path

import yaml

from . import __version__, perf
from .backends import get_backend
from .recipe import Recipe
from .voxel import export_glb, plot_sections


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Run:
    def __init__(self, recipe: Recipe, out: Path, backend: str, grid: float,
                 glb: str = "all", save_states: bool = True, checks=None):
        self.r, self.out, self.grid = recipe, Path(out), grid
        self.checks = None
        if checks:
            from .probe import load_checks
            self.checks = load_checks(checks)
        self.backend_name, self.glb, self.save_states = backend, glb, save_states
        if self.out.exists() and any(self.out.iterdir()):
            raise FileExistsError(f"输出目录已存在且非空：{self.out}（不覆盖已有结果）")
        for sub in ("states", "voxels", "exports"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.events = open(self.out / "events.jsonl", "a")
        self.perf = {"host": perf.host_info(), "grid_nm": grid, "steps": []}

    def event(self, kind, **kw):
        self.events.write(json.dumps({"t": _now(), "event": kind, **kw}, ensure_ascii=False) + "\n")
        self.events.flush()

    def status(self, state, **kw):
        (self.out / "status.json").write_text(json.dumps(
            {"state": state, "updated": _now(), **kw}, indent=2, ensure_ascii=False))

    def write_perf(self):
        (self.out / "perf.json").write_text(json.dumps(self.perf, indent=2, ensure_ascii=False))

    def snapshot(self, idx: int, sid: str, rec: dict):
        """状态保存 + 体素化 + 导出；各部分分别计时。"""
        name = f"{idx:02d}_{sid}"
        if self.save_states:
            with perf.measure(rec, "save_state"):
                self.be.save_state(self.out / "states" / name)
        with perf.measure(rec, "voxelize"):
            mg = self.be.material_grid()
        rec["volumes_nm3"] = {k: round(v, 1) for k, v in mg.volumes().items()}
        if self.checks and sid in (self.checks.get("expect") or {}):
            from .probe import evaluate
            with perf.measure(rec, "checks_perf"):
                rec["checks"] = evaluate(self.checks, sid, self.be.surface_meshes())
        rec["voxel_shape"] = list(mg.grid.shape)
        rec["voxel_coverage_nm"] = getattr(self.be, "coverage", None)
        with perf.measure(rec, "export"):
            mg.save(self.out / "voxels" / f"{name}.npz")
            d = self.r.domain
            sec = self.r.source.get("outputs", {}).get("sections", {})
            plot_sections(mg, self.r.materials, self.out / "exports" / f"{name}.png",
                          title=f"{idx:02d} {sid}", y_cut=sec.get("y", sum(d["y"]) / 2),
                          z_cuts=tuple(sec.get("z", ())),
                          z_range=(mg.origin[2], mg.origin[2] + mg.grid.shape[2] * mg.spacing))
            last = idx == len(self.r.steps)
            if self.glb == "all" or (self.glb == "final" and last):
                rec["glb_faces"] = export_glb(mg, self.r.materials,
                                              self.out / "exports" / f"{name}.glb")

    def execute(self):
        r = self.r
        manifest = {
            "software": {"nowprocess3d": __version__, "python": sys.version.split()[0],
                         "platform": platform.platform()},
            "backend": self.backend_name, "grid_nm": self.grid,
            "started": _now(), "recipe": r.source,
        }
        Backend = get_backend(self.backend_name)
        manifest["backend_version"] = Backend.version()
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        (self.out / "recipe.yaml").write_text(yaml.safe_dump(r.source, allow_unicode=True,
                                                             sort_keys=False))
        self.status("running", step=0, total=len(r.steps))
        self.event("run_start", backend=self.backend_name, grid=self.grid)

        # 导入绘图/网格库的一次性开销单独记录，不计入第 1 步
        with perf.measure(self.perf, "startup_imports"):
            import matplotlib.pyplot  # noqa: F401
            import skimage.measure  # noqa: F401
            import trimesh  # noqa: F401

        rec = {"index": 0, "id": "s01_substrate", "op": "initial"}
        with perf.measure(rec, "compute"):
            self.be = Backend(r, self.grid)
        rec.update(self.be.stats())
        self.snapshot(1, "s01_substrate", rec)
        self.perf["steps"].append(rec)
        self.write_perf()

        for i, step in enumerate(r.steps, 2):
            sid = step["id"]
            self.status("running", step=i, id=sid, total=len(r.steps) + 1)
            self.event("step_start", step=sid, op=step["op"])
            rec = {"index": i, "id": sid, "op": step["op"]}
            try:
                with perf.measure(rec, "compute"):
                    info = self.be.apply(step)
                rec.update(info or {})
                rec.update(self.be.stats())
                self.snapshot(i, sid, rec)
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                self.perf["steps"].append(rec)
                self.write_perf()
                self.event("step_failed", step=sid, error=rec["error"],
                           traceback=traceback.format_exc())
                self.status("failed", step=i, id=sid, error=rec["error"])
                raise
            self.perf["steps"].append(rec)
            self.write_perf()
            self.event("step_done", step=sid, wall_s=rec["compute"]["wall_s"])
        self.perf["total_wall_s"] = round(sum(
            sum(s[k]["wall_s"] for k in ("compute", "save_state", "voxelize", "export") if k in s)
            for s in self.perf["steps"]), 3)
        self.write_perf()
        self.status("done", total=len(r.steps) + 1)
        self.event("run_done")
        self.events.close()


def perf_table(run_dir: Path) -> str:
    p = json.loads((Path(run_dir) / "perf.json").read_text())
    hdr = (f"{'#':>2} {'step':<18} {'op':<18} {'compute s':>9} {'cpu s':>8} {'voxel s':>7} "
           f"{'export s':>8} {'cmpMB':>6} {'voxMB':>6} {'LS':>3} {'LS pts':>9}")
    out = [f"host={p['host']['hostname']} slots={p['host']['nslots']} grid={p['grid_nm']} nm", hdr]
    for s in p["steps"]:
        g = lambda k, f="wall_s": s.get(k, {}).get(f, float("nan"))
        out.append(f"{s['index']+1 if s['index']==0 else s['index']:>2} {s['id']:<18} {s['op']:<18} "
                   f"{g('compute'):>9.3f} {g('compute','cpu_s'):>8.2f} {g('voxelize'):>7.2f} "
                   f"{g('export'):>8.2f} {g('compute','phase_peak_mb'):>6.0f} "
                   f"{g('voxelize','phase_peak_mb'):>6.0f} "
                   f"{s.get('n_levelsets', 0):>3} {s.get('n_points', 0):>9}"
                   + (f"  ERROR {s['error']}" if "error" in s else ""))
    if "startup_imports" in p:
        out.append(f"startup imports {p['startup_imports']['wall_s']} s")
    if "total_wall_s" in p:
        out.append(f"total wall {p['total_wall_s']} s")
    return "\n".join(out)
