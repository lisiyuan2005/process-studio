"""Analytic, geometry-independent numerical checks; no fitted/mirrored output."""

import argparse
import json
from pathlib import Path
import time

import numpy as np

from process_studio.kernel.transport import evolve_hamilton_jacobi


def benchmark():
    rows = []
    for name, ndim, sizes in [
        ("offset_circle_expansion", 2, [41, 81, 161]),
        ("offset_circle_contraction", 2, [41, 81, 161]),
        ("offset_sphere_expansion", 3, [31, 41, 61]),
    ]:
        speed = -.1 if "contraction" in name else .1
        for n in sizes:
            h = 1/(n-1)
            axes = np.meshgrid(*([np.linspace(-.5, .5, n)]*ndim), indexing="ij")
            phi = np.sqrt(sum((x-offset)**2 for x, offset in zip(axes, [.013, -.027, .019], strict=False)))-.22
            exact = phi-speed*.3
            band = abs(exact) < .03
            for order in [1, 2]:
                started = time.perf_counter()
                actual, _ = evolve_hamilton_jacobi(phi, h, speed, (0,)*ndim, .3, order=order)
                rows.append({
                    "case": name, "grid_spacing_nm": h*1000, "order": order,
                    "near_interface_field_rms_nm": float(np.sqrt(np.mean((actual[band]-exact[band])**2))*1000),
                    "elapsed_seconds": time.perf_counter()-started,
                })
    phi = np.random.default_rng(82).normal(size=(23, 27, 31))
    dense, _ = evolve_hamilton_jacobi(phi, .02, .1, (.02, -.01, .03), .1, order=2)
    stats = {}
    tiled, _ = evolve_hamilton_jacobi(phi, .02, .1, (.02, -.01, .03), .1, order=2, tile_shape=(9, 11, 13), diagnostics=stats)
    return {
        "description": "Raw signed-field error in a fixed 30 nm band about analytic circles/spheres; not fab accuracy.",
        "analytic_checks": rows,
        "synchronized_tiles": {**stats, "dense_max_abs_difference": float(np.max(abs(dense-tiled)))},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = benchmark()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
