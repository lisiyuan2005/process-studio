"""The NAND10 benchmark: a 10-word-line 3D NAND cell, built step by step,
with the interfaces along three probe lines checked against their analytic
heights (``checks.yaml``, from the Nowprocess3D work this kernel came from).

    python benchmarks/nand10/run.py [--kernel openvdb|slab] [--fidelity detailed]
                                    [--resolution-nm 2]

The window is closed rather than periodic: the layout is symmetric about
its x edges (half a slit on each side), where a closed window and a
periodic one agree. Prints each step's time and, per step and probe, the
worst interface error in nm.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from process_studio.defaults import default_materials  # noqa: E402
from process_studio.grid import window_grid  # noqa: E402
from process_studio.kernels import get_kernel  # noqa: E402
from process_studio.layout.quick_sketch import QuickSketch, SketchShape  # noqa: E402
from process_studio.models import MaterialResponse, ProcessStep, ProcessType, ProjectDefinition  # noqa: E402
from process_studio.worker.serialize import grid_dict  # noqa: E402

NM = 1e-3
MATERIALS = ("Si", "SiO2", "SiN", "poly-Si", "W")


def step(name, kind, material=None, rates=None, sketch=None, **parameters):
    responses = {m: MaterialResponse(m, rate_um_per_min=r) for m, r in (rates or {}).items()}
    if sketch:
        parameters["sketch_id"] = sketch
    return ProcessStep(
        name, process_type=kind, parameters=parameters, material_responses=responses,
        output_material=material, mask_source="quick_sketch" if sketch else "none",
    )


def flow() -> list[ProcessStep]:
    planar = dict(mode="planar")
    steps = [("s01_substrate", None)]
    stack = []
    for index in range(10):
        stack.append(step(f"s02_ox{index}", ProcessType.DEPOSIT, "SiO2", target=25 * NM, **planar))
        stack.append(step(f"s02_sin{index}", ProcessType.DEPOSIT, "SiN", target=30 * NM, **planar))
    stack.append(step("s02_on_stack", ProcessType.DEPOSIT, "SiO2", target=25 * NM, **planar))
    all_rates = {m: 1.0 for m in MATERIALS}
    steps = stack + [
        step("s03_cap", ProcessType.DEPOSIT, "SiO2", target=50 * NM, **planar),
        step("s04_hole_etch", ProcessType.ETCH, rates=all_rates, sketch="hole", target=645 * NM, directional_fraction=1.0),
        step("s05a_block_ox", ProcessType.DEPOSIT, "SiO2", target=6 * NM, mode="conformal"),
        step("s05b_ctl", ProcessType.DEPOSIT, "SiN", target=6 * NM, mode="conformal"),
        step("s05c_tunnel_ox", ProcessType.DEPOSIT, "SiO2", target=5 * NM, mode="conformal"),
        step("s06_punch", ProcessType.ETCH, rates=all_rates, target=22 * NM, directional_fraction=1.0),
        step("s07_channel", ProcessType.DEPOSIT, "poly-Si", target=8 * NM, mode="conformal"),
        step("s08_core", ProcessType.DEPOSIT, "SiO2", target=27 * NM, mode="conformal"),
        step("s09_cmp", ProcessType.CMP, target_z=615 * NM),
        step("s09b_cap2", ProcessType.DEPOSIT, "SiO2", target=30 * NM, **planar),
        step("s10_slit_etch", ProcessType.ETCH, rates=all_rates, sketch="slit", target=645 * NM, directional_fraction=1.0),
        step("s11_sin_removal", ProcessType.ETCH, rates={"SiN": 1.0}, target=130 * NM, directional_fraction=0.0),
        step("s12_w_fill", ProcessType.DEPOSIT, "W", target=18 * NM, mode="conformal"),
        step("s13_w_recess", ProcessType.ETCH, rates={"W": 1.0}, target=25 * NM, directional_fraction=0.0),
        step("s14a_slit_fill", ProcessType.DEPOSIT, "SiO2", target=46 * NM, mode="conformal"),
        step("s14b_cmp", ProcessType.CMP, target_z=645 * NM),
    ]
    return steps


SKETCHES = {
    "hole": QuickSketch("hole", [SketchShape("circle", parameters={"center": (160 * NM, 75 * NM), "radius": 50 * NM})]),
    "slit": QuickSketch("slit", [
        SketchShape("rectangle", parameters={"center": (20 * NM, 75 * NM), "size": (40 * NM, 150 * NM)}),
        SketchShape("rectangle", parameters={"center": (300 * NM, 75 * NM), "size": (40 * NM, 150 * NM)}),
    ]),
}


def probe_openvdb(state, line):
    """Material along a probe line, every 0.05 nm: (positions nm, labels)."""
    from scipy import ndimage

    lat = state.lattice
    dense = state.dense()
    names = list(dense)
    solid = state.solid()
    k0 = state._dense_k0
    axis, at, (lo, hi) = line["axis"], line["at"], line["range"]
    s = np.arange(lo, hi, 0.05)
    xyz = {"x": None, "y": None, "z": None}
    for key, value in at.items():
        xyz[key] = np.full_like(s, value)
    xyz[axis] = s
    coords = [
        (xyz["x"] * NM - lat.origin[0]) / lat.g,
        (xyz["y"] * NM - lat.origin[1]) / lat.g,
        (xyz["z"] * NM - lat.origin[2]) / lat.g - k0,
    ]
    fields = np.stack([ndimage.map_coordinates(dense[n], coords, order=1, mode="nearest") for n in names])
    inside = ndimage.map_coordinates(solid, coords, order=1, mode="nearest") < 0
    labels = np.where(inside, fields.argmin(axis=0), -1)
    return s, labels, names


def probe_slab(state, line):
    from shapely.geometry import Point

    device = state.device
    axis, at, (lo, hi) = line["axis"], line["at"], line["range"]
    s = np.arange(lo, hi, 0.05)
    names = list(state.priority)
    labels = np.full(s.shape, -1)
    if axis == "z":
        x, y = at["x"] * NM, at["y"] * NM
        point = Point(x, y)
        for slab in device._state.slabs:
            for material, region in slab.regions.items():
                if region.covers(point):
                    z0 = slab.z0 * 1000 + state.z_offset * 1000
                    z1 = slab.z1 * 1000 + state.z_offset * 1000
                    labels[(s >= z0) & (s < z1)] = names.index(material.name) if material.name in names else -1
    else:
        z = at["z"] * NM - state.z_offset
        y = at["y"] * NM
        for slab in device._state.slabs:
            if not (slab.z0 <= z < slab.z1):
                continue
            for material, region in slab.regions.items():
                xs = s * NM
                hit = np.array([region.covers(Point(x, y)) for x in xs])
                labels[hit] = names.index(material.name) if material.name in names else -1
    return s, labels, names


def _flat(values):
    """The expected heights, with the YAML aliases' nested lists spread out."""
    out = []
    for value in values:
        out.extend(_flat(value) if isinstance(value, list) else [float(value)])
    return out


def interfaces(s, labels):
    change = np.nonzero(labels[1:] != labels[:-1])[0]
    return [float((s[i] + s[i + 1]) / 2) for i in change]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", default="openvdb")
    parser.add_argument("--fidelity", default="detailed")
    parser.add_argument("--resolution-nm", type=float, default=2.0)
    parser.add_argument("--checks", action="store_true", help="probe interfaces after every step")
    args = parser.parse_args()
    checks = yaml.safe_load((Path(__file__).with_name("checks.yaml")).read_text())
    kernel = get_kernel(args.kernel)
    project = ProjectDefinition(
        "nand10", grid_dict(window_grid(0.0, 0.32, 0.0, 0.15, -0.05, 0.8)),
        kernel=args.kernel, resolution_um=args.resolution_nm * NM,
    )
    project.fidelity = args.fidelity
    materials = default_materials()
    state = kernel.initial_state(project, materials=materials)
    probe = probe_openvdb if args.kernel == "openvdb" else probe_slab
    excluded = {tuple(item) for item in checks.get("exclude_from_stats", [])}
    worst_all = 0.0
    total = 0.0
    for item in [None] + flow():
        name = "s01_substrate" if item is None else item.name
        took = 0.0
        if item is not None:
            start = time.perf_counter()
            state = kernel.run_step(
                state, item, project=project, recipes={}, sketches=SKETCHES,
                logger=lambda _m: None, materials=materials,
            )
            took = time.perf_counter() - start
            total += took
        expect = checks["expect"].get(name)
        line = f"{name:16s} {took:7.2f}s"
        if expect is not None:
            for probe_name, wanted in expect.items():
                wanted = _flat(wanted)
                s, labels, _ = probe(state, checks["probes"][probe_name])
                found = interfaces(s, labels)
                if len(found) != len(wanted):
                    line += f"  {probe_name}: {len(found)} interfaces, expected {len(wanted)} ({[round(v, 2) for v in found]})"
                    continue
                error = max((abs(a - b) for a, b in zip(found, wanted)), default=0.0)
                line += f"  {probe_name} {error:5.2f}nm"
                if (name, probe_name) not in excluded:
                    worst_all = max(worst_all, error)
        print(line, flush=True)
    print(f"total {total:.1f}s; worst interface error {worst_all:.2f} nm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
