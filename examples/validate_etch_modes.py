"""Compare directional, mixed, and simplified wet etch in three dimensions."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.masks import rectangle
from process_studio.kernel.metrics import column_surface_height
from process_studio.kernel.processes import mixed_trench_etch


MODES = {
    "Directional dry etch": (0.10, 0.00, "#4378bf"),
    "Mixed dry etch": (0.08, 0.02, "#8a63b8"),
    "Simplified wet etch": (0.00, 0.10, "#28a483"),
}


def sampled_width(phi: np.ndarray, grid: UniformGrid3D, depth: float) -> float:
    z_index = int(np.argmin(np.abs(grid.z + depth)))
    row = phi[z_index, grid.ny // 2, :]
    coordinates = grid.x[row > 0.0]
    if coordinates.size < 2:
        return 0.0
    return float(coordinates[-1] - coordinates[0])


def sidewall_angle(top_width: float, bottom_width: float, separation: float) -> float:
    lateral_change = abs(top_width - bottom_width) / 2.0
    if lateral_change < 1e-12:
        return 90.0
    return math.degrees(math.atan2(separation, lateral_change))


def build_cases() -> tuple[UniformGrid3D, np.ndarray, dict[str, dict[str, object]]]:
    grid = UniformGrid3D(-0.6, 0.6, -0.6, 0.6, -0.6, 0.2, 61, 61, 41)
    xx, yy = grid.mesh_xy
    mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.4, 0.4))
    initial = grid.substrate()
    target_depth = 0.2
    cases: dict[str, dict[str, object]] = {}

    for name, (directional_rate, isotropic_rate, color) in MODES.items():
        etched, steps = mixed_trench_etch(
            initial,
            mask,
            grid.z,
            grid.dx,
            target_depth,
            directional_rate=directional_rate,
            isotropic_rate=isotropic_rate,
        )
        measured_depth = -column_surface_height(
            etched, grid.z, grid.ny // 2, grid.nx // 2
        )
        top_width = sampled_width(etched, grid, 0.04)
        middle_width = sampled_width(etched, grid, 0.10)
        bottom_width = sampled_width(etched, grid, 0.16)
        cases[name] = {
            "phi": etched,
            "steps": steps,
            "color": color,
            "depth": measured_depth,
            "top_width": top_width,
            "middle_width": middle_width,
            "bottom_width": bottom_width,
            "angle": sidewall_angle(top_width, bottom_width, 0.12),
            "directional_rate": directional_rate,
            "isotropic_rate": isotropic_rate,
        }
    return grid, mask, cases


def plot_cases(
    grid: UniformGrid3D,
    mask: np.ndarray,
    cases: dict[str, dict[str, object]],
    output: Path,
) -> None:
    fig = plt.figure(figsize=(15, 9))
    fig.subplots_adjust(
        left=0.06,
        right=0.96,
        bottom=0.08,
        top=0.87,
        wspace=0.20,
        hspace=0.30,
    )
    fig.suptitle(
        "One Level-Set Kernel, Three Etch Regimes",
        fontsize=17,
        fontweight="bold",
    )

    center_y = grid.ny // 2
    x_crop = np.flatnonzero(np.abs(grid.x) <= 0.5)
    y_crop = np.flatnonzero(np.abs(grid.y) <= 0.5)
    z_crop = np.flatnonzero((grid.z >= -0.24) & (grid.z <= 0.0))

    for column, (name, case) in enumerate(cases.items()):
        phi = case["phi"]
        color = case["color"]

        section_ax = fig.add_subplot(2, 3, column + 1)
        material = phi[:, center_y, :] <= 0.0
        section_ax.imshow(
            material,
            origin="lower",
            extent=(grid.x_min, grid.x_max, grid.z_min, grid.z_max),
            cmap=ListedColormap(["#f8fafc", color]),
            interpolation="nearest",
            aspect="auto",
        )
        section_ax.contour(
            grid.x,
            grid.z,
            phi[:, center_y, :],
            levels=[0.0],
            colors=["#151b23"],
            linewidths=1.4,
        )
        section_ax.axhline(0.0, color="#cbd2da", linewidth=0.8, linestyle="--")
        section_ax.set_xlim(-0.52, 0.52)
        section_ax.set_ylim(-0.28, 0.08)
        section_ax.set_xlabel("x (µm)")
        if column == 0:
            section_ax.set_ylabel("z (µm)")
        section_ax.set_title(
            f"{name}\nRdir={case['directional_rate']:.2f}, "
            f"Riso={case['isotropic_rate']:.2f} µm/min"
        )
        section_ax.text(
            0.03,
            0.04,
            (
                f"Depth {case['depth']:.3f} µm\n"
                f"Mid-CD {case['middle_width']:.3f} µm\n"
                f"Sidewall ≈ {case['angle']:.1f}°"
            ),
            transform=section_ax.transAxes,
            fontsize=9.5,
            bbox={"facecolor": "white", "alpha": 0.86, "edgecolor": "#cbd2da"},
        )

        volume_ax = fig.add_subplot(2, 3, column + 4, projection="3d")
        cropped = phi[np.ix_(z_crop, y_crop, x_crop)]
        cavity = np.transpose(cropped > 0.0, (2, 1, 0))
        volume_ax.voxels(
            cavity,
            facecolors=color,
            edgecolors=(0.08, 0.1, 0.14, 0.16),
            linewidth=0.18,
            alpha=0.88,
        )
        volume_ax.set_title("3D removed volume")
        volume_ax.set_xlabel("x")
        volume_ax.set_ylabel("y")
        volume_ax.set_zlabel("depth")
        volume_ax.set_xticks([0, len(x_crop) // 2, len(x_crop) - 1], ["−0.5", "0", "0.5"])
        volume_ax.set_yticks([0, len(y_crop) // 2, len(y_crop) - 1], ["−0.5", "0", "0.5"])
        volume_ax.set_zticks(
            [0, len(z_crop) // 2, len(z_crop) - 1],
            ["−0.24", "−0.12", "0"],
        )
        volume_ax.view_init(elev=24, azim=-55)
        volume_ax.set_box_aspect((1.0, 1.0, 0.55))

    fig.text(
        0.5,
        0.025,
        "Mask opening: 0.40 × 0.40 µm | target depth: 0.20 µm | voxel views show etched-away volume",
        ha="center",
        fontsize=10,
    )
    fig.savefig(output, dpi=175, bbox_inches="tight")
    plt.close(fig)


def save_cases(
    grid: UniformGrid3D,
    mask: np.ndarray,
    cases: dict[str, dict[str, object]],
    output: Path,
) -> None:
    np.savez_compressed(
        output,
        x=grid.x,
        y=grid.y,
        z=grid.z,
        mask=mask,
        dry_phi=cases["Directional dry etch"]["phi"],
        mixed_phi=cases["Mixed dry etch"]["phi"],
        wet_phi=cases["Simplified wet etch"]["phi"],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("validation-etch-modes.png"))
    parser.add_argument("--data", type=Path, default=Path("etch-modes-data.npz"))
    args = parser.parse_args()

    grid, mask, cases = build_cases()
    plot_cases(grid, mask, cases, args.output)
    save_cases(grid, mask, cases, args.data)
    for name, case in cases.items():
        print(
            f"{name}: depth={case['depth']:.4f} um, "
            f"mid-CD={case['middle_width']:.4f} um, "
            f"sidewall={case['angle']:.1f} deg"
        )


if __name__ == "__main__":
    main()
