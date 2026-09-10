"""Generate a visual and numerical validation case for 3D trench etching."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.masks import rectangle
from process_studio.kernel.metrics import column_surface_height, surface_height_map
from process_studio.kernel.processes import directional_trench_etch


def build_case(target_depth: float = 0.36) -> dict[str, object]:
    """Run a rectangular-mask vertical trench etch on a planar substrate."""
    grid = UniformGrid3D(-1.0, 1.0, -1.0, 1.0, -0.8, 0.4, 101, 101, 61)
    xx, yy = grid.mesh_xy
    mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.8, 0.6))
    initial = grid.substrate(surface_z=0.0)
    etched, steps = directional_trench_etch(
        initial,
        mask,
        grid.dx,
        target_depth,
        etch_rate=0.12,
    )
    heights = surface_height_map(etched, grid.z)

    center_height = column_surface_height(
        etched, grid.z, grid.ny // 2, grid.nx // 2
    )
    outside_x = int(np.argmin(np.abs(grid.x - 0.8)))
    outside_height = column_surface_height(
        etched, grid.z, grid.ny // 2, outside_x
    )

    return {
        "grid": grid,
        "xx": xx,
        "yy": yy,
        "mask": mask,
        "initial": initial,
        "etched": etched,
        "heights": heights,
        "steps": steps,
        "target_depth": target_depth,
        "center_height": center_height,
        "outside_height": outside_height,
    }


def save_volume(case: dict[str, object], output: Path) -> None:
    """Save enough data to reproduce 3D, top-view, and arbitrary-section views."""
    grid = case["grid"]
    np.savez_compressed(
        output,
        x=grid.x,
        y=grid.y,
        z=grid.z,
        phi=case["etched"],
        mask=case["mask"],
        surface_height=case["heights"],
        target_depth=case["target_depth"],
        center_height=case["center_height"],
        outside_height=case["outside_height"],
        steps=case["steps"],
    )


def plot_case(case: dict[str, object], output: Path) -> None:
    grid = case["grid"]
    xx = case["xx"]
    yy = case["yy"]
    mask = case["mask"]
    etched = case["etched"]
    heights = case["heights"]
    target_depth = float(case["target_depth"])
    center_height = float(case["center_height"])
    outside_height = float(case["outside_height"])

    fig = plt.figure(figsize=(15, 5.2), constrained_layout=True)
    fig.suptitle(
        "3D Directional Trench Etch Validation",
        fontsize=16,
        fontweight="bold",
    )

    ax_mask = fig.add_subplot(1, 3, 1)
    ax_mask.imshow(
        mask,
        origin="lower",
        extent=(grid.x_min, grid.x_max, grid.y_min, grid.y_max),
        cmap=ListedColormap(["#263238", "#ffb300"]),
        interpolation="nearest",
    )
    ax_mask.set_title("Top view: exposure mask")
    ax_mask.set_xlabel("x (µm)")
    ax_mask.set_ylabel("y (µm)")
    ax_mask.set_aspect("equal")
    ax_mask.text(
        0.03,
        0.04,
        "0.80 × 0.60 µm opening",
        transform=ax_mask.transAxes,
        color="white",
        fontsize=10,
        bbox={"facecolor": "black", "alpha": 0.45, "edgecolor": "none"},
    )

    ax_section = fig.add_subplot(1, 3, 2)
    center_y = grid.ny // 2
    material = etched[:, center_y, :] <= 0.0
    ax_section.imshow(
        material,
        origin="lower",
        extent=(grid.x_min, grid.x_max, grid.z_min, grid.z_max),
        cmap=ListedColormap(["#f7f9fb", "#6a5acd"]),
        interpolation="nearest",
        aspect="auto",
    )
    ax_section.contour(
        grid.x,
        grid.z,
        etched[:, center_y, :],
        levels=[0.0],
        colors=["#18202a"],
        linewidths=1.5,
    )
    ax_section.axhline(0.0, color="#d8dde3", linewidth=0.8, linestyle="--")
    ax_section.set_title("AA section: etched material")
    ax_section.set_xlabel("x (µm)")
    ax_section.set_ylabel("z (µm)")
    ax_section.set_ylim(-0.55, 0.15)
    ax_section.text(
        0.03,
        0.05,
        f"Measured depth = {-center_height:.3f} µm",
        transform=ax_section.transAxes,
        fontsize=10,
        bbox={"facecolor": "white", "alpha": 0.82, "edgecolor": "#c9d0d8"},
    )

    ax_3d = fig.add_subplot(1, 3, 3, projection="3d")
    stride = 2
    ax_3d.plot_surface(
        xx[::stride, ::stride],
        yy[::stride, ::stride],
        heights[::stride, ::stride],
        cmap="viridis",
        vmin=-target_depth,
        vmax=0.0,
        linewidth=0.15,
        edgecolor=(0.1, 0.12, 0.16, 0.18),
        antialiased=True,
    )
    ax_3d.set_title("3D material surface")
    ax_3d.set_xlabel("x (µm)")
    ax_3d.set_ylabel("y (µm)")
    ax_3d.set_zlabel("z (µm)")
    ax_3d.set_zlim(-0.5, 0.15)
    ax_3d.view_init(elev=30, azim=-55)
    ax_3d.set_box_aspect((1.0, 1.0, 0.55))

    error_nm = abs(center_height + target_depth) * 1000.0
    ax_3d.text2D(
        0.03,
        0.86,
        (
            f"Target {target_depth:.3f} µm | error {error_nm:.2f} nm\n"
            f"Protected surface {outside_height:.3f} µm | {case['steps']} steps"
        ),
        fontsize=10,
        transform=ax_3d.transAxes,
        bbox={"facecolor": "white", "alpha": 0.82, "edgecolor": "#c9d0d8"},
    )

    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("validation-3d-trench.png"))
    parser.add_argument("--data", type=Path, default=Path("trench-3d-data.npz"))
    parser.add_argument("--target-depth", type=float, default=0.36)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    case = build_case(target_depth=args.target_depth)
    plot_case(case, args.output)
    save_volume(case, args.data)
    print(
        f"saved {args.output} and {args.data}; "
        f"center={case['center_height']:.6f} um, "
        f"outside={case['outside_height']:.6f} um, steps={case['steps']}"
    )


if __name__ == "__main__":
    main()
