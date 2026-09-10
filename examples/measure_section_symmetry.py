"""Measure sampled zero-contour roots; never modify or mirror simulation data."""

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates

from process_studio.kernel.material_state import MaterialState


def measure(state, material, center_x, section_y, half_window, depths):
    g = state.grid
    rows = []
    for depth in depths:
        coords = np.vstack((
            np.full(g.nx, (depth-g.z_min)/g.dz),
            np.full(g.nx, (section_y-g.y_min)/g.dy),
            np.arange(g.nx),
        ))
        values = map_coordinates(state.fields[material], coords, order=1, mode="nearest")
        edges = np.flatnonzero((values[:-1] > 0) != (values[1:] > 0))
        roots = [
            float(g.x[i]-values[i]*g.dx/(values[i+1]-values[i])) for i in edges
        ]
        roots = [x for x in roots if center_x-half_window < x < center_x+half_window]
        left = [x for x in roots if x < center_x]
        right = [x for x in roots if x > center_x]
        imbalance = abs(min(left)+max(right)-2*center_x)*1000 if left and right else None
        paired = [
            abs(roots[i]+roots[-1-i]-2*center_x)*1000
            for i in range(len(roots)//2)
        ] if len(roots) % 2 == 0 else []
        rows.append({
            "z_um": depth, "roots_um": roots,
            "outer_half_width_imbalance_nm": imbalance,
            "paired_interface_imbalances_nm_outer_to_inner": paired,
        })
    return {"material": material, "spacing_nm": g.dx*1000, "x_center_um": center_x,
            "section_y_um": section_y, "rows": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--material", required=True)
    parser.add_argument("--center-x", type=float, required=True)
    parser.add_argument("--section-y", type=float, required=True)
    parser.add_argument("--half-window", type=float, required=True)
    parser.add_argument("--depths", type=float, nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    state = MaterialState.load(args.state)
    report = measure(state, args.material, args.center_x, args.section_y, args.half_window, args.depths)
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
