"""Speed and memory of the slab and OpenVDB kernels on the same flows.

    python benchmarks/compare_kernels.py            # every case, both kernels
    python benchmarks/compare_kernels.py --case nand10 --kernel openvdb ...

Each (case, kernel, fidelity, resolution) runs in a fresh process, so its
peak memory is its own: the flow's steps, then one section, the top view
and the 3D mesh of the last step, the way the desktop opens it. Reported:
flow time, view time, peak resident memory of the process (and of any mesh
workers it started), and the size of the last step's stored result.

Cases:
  nand10   the 10-word-line 3D NAND cell of benchmarks/nand10 (layers,
           vertical walls, one hole: what the slab model is made for)
  holes    a 3 x 3 array of round holes through a stack, then four rounds
           of a conformal film and an isotropic etch back (curved fronts
           that pile up vertices in polygons and cost a lattice nothing
           extra)
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks" / "nand10"))

NM = 1e-3


def _holes():
    from process_studio.layout.quick_sketch import QuickSketch, SketchShape
    from process_studio.models import ProcessType
    from run import step

    shapes = [
        SketchShape("circle", parameters={"center": (x * 0.2, y * 0.2), "radius": 0.06})
        for x in (-1, 0, 1)
        for y in (-1, 0, 1)
    ]
    sketches = {"holes": QuickSketch("holes", shapes)}
    rates = {"Si": 1.0, "SiO2": 1.0, "SiN": 1.0}
    flow = [
        step("ox", ProcessType.DEPOSIT, "SiO2", target=0.05, mode="planar"),
        step("sin", ProcessType.DEPOSIT, "SiN", target=0.05, mode="planar"),
        step("holes", ProcessType.ETCH, rates=rates, sketch="holes", target=0.25, directional_fraction=1.0),
    ]
    for cycle in range(4):
        flow.append(step(f"film{cycle}", ProcessType.DEPOSIT, "Al2O3", target=0.012, mode="conformal"))
        flow.append(step(f"wet{cycle}", ProcessType.ETCH, rates={"Al2O3": 1.0, "SiN": 0.5}, target=0.008, directional_fraction=0.0))
    window = (-0.3, 0.3, -0.3, 0.3, -0.25, 0.25)
    return flow, sketches, window


def _nand10():
    import run

    return run.flow(), run.SKETCHES, (0.0, 0.32, 0.0, 0.15, -0.05, 0.8)


CASES = {"nand10": _nand10, "holes": _holes}


def measure(case: str, kernel_id: str, fidelity: str, resolution_nm: float) -> dict:
    from process_studio.defaults import default_materials
    from process_studio.grid import window_grid
    from process_studio.kernels import get_kernel
    from process_studio.models import ProjectDefinition
    from process_studio.worker.serialize import grid_dict

    flow, sketches, window = CASES[case]()
    kernel = get_kernel(kernel_id)
    project = ProjectDefinition(
        case, grid_dict(window_grid(*window)), kernel=kernel_id,
        resolution_um=resolution_nm * NM, voxel_um=resolution_nm * NM,
    )
    project.fidelity = fidelity
    materials = default_materials()
    start = time.perf_counter()
    state = kernel.initial_state(project, materials=materials)
    for item in flow:
        state = kernel.run_step(
            state, item, project=project, recipes={}, sketches=sketches,
            logger=lambda _m: None, materials=materials,
        )
    flow_s = time.perf_counter() - start
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / f"last{kernel.info.snapshot_suffix}"
        state.save(path)
        stored = path.stat().st_size
        # the slab kernel keeps its mesh beside the stored file; give it one
        if hasattr(state, "path"):
            state = kernel.load_state(path)
        colors = {name: "#808080" for name in kernel.state_materials(state)}
        start = time.perf_counter()
        kernel.section(state, colors, project=project, axis="y")
        kernel.top_view(state, colors, project=project)
        surfaces = kernel.surfaces(state, project=project)
        views_s = time.perf_counter() - start
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024  # bytes on macOS, KiB on Linux
    return {
        "case": case, "kernel": kernel_id, "fidelity": fidelity, "resolution_nm": resolution_nm,
        "flow_s": round(flow_s, 2), "views_s": round(views_s, 2),
        "peak_mb": round(own * scale / 2**20), "mesh_workers_peak_mb": round(children * scale / 2**20),
        "stored_mb": round(stored / 2**20, 2),
        "triangles": sum(item["triangleCount"] for item in surfaces["surfaces"]),
        "materials": len(kernel.state_materials(state)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case")
    parser.add_argument("--kernel")
    parser.add_argument("--fidelity")
    parser.add_argument("--resolution-nm", type=float)
    parser.add_argument("--timeout", type=float, default=1200.0, help="seconds per run")
    parser.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.one:
        print(json.dumps(measure(args.case, args.kernel, args.fidelity, args.resolution_nm)))
        return 0
    plan = [
        (case, kernel, fidelity, nm)
        for case, sizes in (("nand10", (4.0, 2.0)), ("holes", (5.0, 2.5)))
        for fidelity in ("simplified", "detailed")
        for nm in sizes
        for kernel in ("slab", "openvdb")
    ]
    if args.case:
        plan = [p for p in plan if p[0] == args.case]
    if args.kernel:
        plan = [p for p in plan if p[1] == args.kernel]
    if args.fidelity:
        plan = [p for p in plan if p[2] == args.fidelity]
    if args.resolution_nm:
        plan = [p for p in plan if p[3] == args.resolution_nm]
    for case, kernel, fidelity, nm in plan:
        command = [sys.executable, __file__, "--one", "--case", case, "--kernel", kernel,
                   "--fidelity", fidelity, "--resolution-nm", str(nm)]
        try:
            done = subprocess.run(command, capture_output=True, text=True, env={**os.environ}, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(json.dumps({"case": case, "kernel": kernel, "fidelity": fidelity,
                              "resolution_nm": nm, "error": f"over {args.timeout:g} s"}), flush=True)
            continue
        if done.returncode:
            print(json.dumps({"case": case, "kernel": kernel, "fidelity": fidelity,
                              "resolution_nm": nm, "error": done.stderr.strip().splitlines()[-1:]}), flush=True)
        else:
            print(done.stdout.strip().splitlines()[-1], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
