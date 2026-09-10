"""Generate a convergence figure for circular deposition and etching."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from process_studio.kernel.grid import UniformGrid2D
from process_studio.kernel.level_set import evolve_constant_normal_speed
from process_studio.kernel.metrics import diagonal_radius_zero_crossing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    radius_initial = 0.30
    duration = 0.40
    cases = [("Deposition", 0.20), ("Etch", -0.20)]
    sizes = (101, 201, 401)

    figure, axes = plt.subplots(1, 2, figsize=(10, 4.2), constrained_layout=True)

    for label, speed in cases:
        spacings = []
        errors = []
        for size in sizes:
            grid = UniformGrid2D(-1, 1, -1, 1, size, size)
            phi = grid.circle((0.0, 0.0), radius_initial)
            evolved, _ = evolve_constant_normal_speed(phi, grid.dx, speed, duration)
            measured = diagonal_radius_zero_crossing(evolved, grid.x)
            expected = radius_initial + speed * duration
            spacings.append(grid.dx)
            errors.append(abs(measured - expected))

        axes[0].loglog(spacings, errors, "o-", label=label)

    display_grid = UniformGrid2D(-0.6, 0.6, -0.6, 0.6, 241, 241)
    initial = display_grid.circle((0.0, 0.0), radius_initial)
    deposited, _ = evolve_constant_normal_speed(initial, display_grid.dx, 0.20, duration)
    etched, _ = evolve_constant_normal_speed(initial, display_grid.dx, -0.20, duration)
    xx, yy = display_grid.mesh
    axes[1].contour(xx, yy, initial, levels=[0], colors=["#555555"], linewidths=2)
    axes[1].contour(xx, yy, deposited, levels=[0], colors=["#2764d8"], linewidths=2)
    axes[1].contour(xx, yy, etched, levels=[0], colors=["#d65f35"], linewidths=2)

    axes[0].invert_xaxis()
    axes[0].set_title("Interface error under grid refinement")
    axes[0].set_xlabel("Grid spacing")
    axes[0].set_ylabel("Absolute radius error")
    axes[0].grid(True, which="both", alpha=0.25)
    axes[0].legend()

    axes[1].set_title("Constant normal-speed evolution")
    axes[1].set_xlabel("x")
    axes[1].set_ylabel("y")
    axes[1].set_aspect("equal")
    axes[1].grid(True, alpha=0.2)
    axes[1].plot([], [], color="#2764d8", label="Deposition")
    axes[1].plot([], [], color="#555555", label="Initial")
    axes[1].plot([], [], color="#d65f35", label="Etch")
    axes[1].legend()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=180)
    plt.close(figure)


if __name__ == "__main__":
    main()
