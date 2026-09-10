"""Validate conformal coverage, opening shrinkage, and pinch-off."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.masks import rectangle
from process_studio.kernel.metrics import (
    column_surface_height,
    sealed_void_mask,
    trench_opening_width,
)
from process_studio.kernel.processes import (
    conformal_deposition,
    directional_trench_etch,
    mixed_trench_etch,
)


SUBSTRATE = "#6255b3"
FILM = "#f2a51a"
SEALED_VOID = "#52b7d8"
BACKGROUND = "#f8fafc"


def build_cases() -> dict[str, object]:
    grid = UniformGrid3D(-0.6, 0.6, -0.6, 0.6, -0.6, 0.4, 61, 61, 51)
    xx, yy = grid.mesh_xy

    vertical_mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.4, 0.4))
    vertical_trench, _ = directional_trench_etch(
        grid.substrate(), vertical_mask, grid.dx, target_depth=0.3
    )
    thin_thickness = 0.04
    coated, thin_film, _ = conformal_deposition(
        vertical_trench, grid.dx, thin_thickness
    )

    narrow_mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.2, 0.2))
    wet_trench, _ = mixed_trench_etch(
        grid.substrate(),
        narrow_mask,
        grid.z,
        grid.dx,
        target_depth=0.3,
        directional_rate=0.0,
        isotropic_rate=0.1,
    )
    pinch_thickness = 0.12
    pinched, pinch_film, _ = conformal_deposition(
        wet_trench, grid.dx, pinch_thickness
    )
    sealed = sealed_void_mask(pinched, grid.z)

    outside_x = int(np.argmin(np.abs(grid.x - 0.55)))
    top_thickness = column_surface_height(
        coated, grid.z, grid.ny // 2, outside_x
    )
    bottom_before = column_surface_height(
        vertical_trench, grid.z, grid.ny // 2, grid.nx // 2
    )
    bottom_after = column_surface_height(
        coated, grid.z, grid.ny // 2, grid.nx // 2
    )
    width_before = trench_opening_width(
        vertical_trench, grid.x, grid.z, depth=0.1
    )
    width_after = trench_opening_width(coated, grid.x, grid.z, depth=0.1)

    sweep_thickness = np.linspace(0.0, 0.14, 8)
    sweep_width = []
    sweep_sealed = []
    for thickness in sweep_thickness:
        result = (
            wet_trench
            if thickness == 0
            else conformal_deposition(wet_trench, grid.dx, float(thickness))[0]
        )
        sweep_width.append(
            trench_opening_width(result, grid.x, grid.z, depth=0.0)
        )
        sweep_sealed.append(bool(np.any(sealed_void_mask(result, grid.z))))

    return {
        "grid": grid,
        "vertical_trench": vertical_trench,
        "coated": coated,
        "thin_film": thin_film,
        "thin_thickness": thin_thickness,
        "wet_trench": wet_trench,
        "pinched": pinched,
        "pinch_film": pinch_film,
        "pinch_thickness": pinch_thickness,
        "sealed": sealed,
        "coverage": np.array(
            [
                top_thickness,
                bottom_after - bottom_before,
                (width_before - width_after) / 2.0,
            ]
        ),
        "sweep_thickness": sweep_thickness,
        "sweep_width": np.array(sweep_width),
        "sweep_sealed": np.array(sweep_sealed),
    }


def material_labels(
    original: np.ndarray,
    combined: np.ndarray,
    _film: np.ndarray,
    y_index: int,
) -> np.ndarray:
    labels = np.zeros(original[:, y_index, :].shape, dtype=np.uint8)
    labels[original[:, y_index, :] <= 0.0] = 1
    deposited = (combined[:, y_index, :] <= 0.0) & (
        original[:, y_index, :] > 0.0
    )
    labels[deposited] = 2
    return labels


def plot_cross_section(
    ax: plt.Axes,
    grid: UniformGrid3D,
    original: np.ndarray,
    combined: np.ndarray,
    film: np.ndarray,
    title: str,
    *,
    sealed: np.ndarray | None = None,
) -> None:
    center_y = grid.ny // 2
    labels = material_labels(original, combined, film, center_y)
    ax.imshow(
        labels,
        origin="lower",
        extent=(grid.x_min, grid.x_max, grid.z_min, grid.z_max),
        cmap=ListedColormap([BACKGROUND, SUBSTRATE, FILM]),
        interpolation="nearest",
        vmin=0,
        vmax=2,
        aspect="auto",
    )
    ax.contour(
        grid.x,
        grid.z,
        combined[:, center_y, :],
        levels=[0.0],
        colors=["#18202a"],
        linewidths=1.2,
    )
    if sealed is not None:
        overlay = np.ma.masked_where(~sealed[:, center_y, :], sealed[:, center_y, :])
        ax.imshow(
            overlay,
            origin="lower",
            extent=(grid.x_min, grid.x_max, grid.z_min, grid.z_max),
            cmap=ListedColormap([SEALED_VOID]),
            interpolation="nearest",
            alpha=0.9,
            aspect="auto",
        )
    ax.set_xlim(-0.5, 0.5)
    ax.set_ylim(-0.38, 0.12)
    ax.set_xlabel("x (µm)")
    ax.set_ylabel("z (µm)")
    ax.set_title(title)


def plot_cases(case: dict[str, object], output: Path) -> None:
    grid = case["grid"]
    fig = plt.figure(figsize=(15, 9))
    fig.subplots_adjust(
        left=0.06,
        right=0.96,
        bottom=0.13,
        top=0.88,
        wspace=0.24,
        hspace=0.34,
    )
    fig.suptitle(
        "Conformal Deposition Validation",
        fontsize=18,
        fontweight="bold",
    )

    ax_before = fig.add_subplot(2, 3, 1)
    no_film = np.abs(case["vertical_trench"])
    plot_cross_section(
        ax_before,
        grid,
        case["vertical_trench"],
        case["vertical_trench"],
        no_film,
        "1. Etched trench",
    )

    ax_coated = fig.add_subplot(2, 3, 2)
    plot_cross_section(
        ax_coated,
        grid,
        case["vertical_trench"],
        case["coated"],
        case["thin_film"],
        f"2. Conformal film: {case['thin_thickness']:.2f} µm",
    )

    ax_pinch = fig.add_subplot(2, 3, 3)
    plot_cross_section(
        ax_pinch,
        grid,
        case["wet_trench"],
        case["pinched"],
        case["pinch_film"],
        f"3. Pinch-off: {case['pinch_thickness']:.2f} µm film",
        sealed=case["sealed"],
    )

    ax_coverage = fig.add_subplot(2, 3, 4)
    coverage = case["coverage"]
    bars = ax_coverage.bar(
        ["Top", "Bottom", "Sidewall"],
        coverage,
        color=["#e8a028", "#efb54a", "#f5c978"],
        edgecolor="#7a5a1c",
    )
    ax_coverage.axhline(
        case["thin_thickness"],
        color="#303841",
        linestyle="--",
        linewidth=1.2,
        label="Target",
    )
    ax_coverage.bar_label(bars, fmt="%.3f µm", padding=4)
    ax_coverage.set_ylim(0.0, 0.055)
    ax_coverage.set_ylabel("Measured thickness (µm)")
    ax_coverage.set_title("Equal-thickness check")
    ax_coverage.legend(loc="lower right")
    ax_coverage.grid(axis="y", alpha=0.25)

    ax_sweep = fig.add_subplot(2, 3, 5)
    ax_sweep.plot(
        case["sweep_thickness"],
        case["sweep_width"],
        marker="o",
        color="#385a9f",
        linewidth=2,
    )
    sealed_indices = np.flatnonzero(case["sweep_sealed"])
    if sealed_indices.size:
        threshold = case["sweep_thickness"][sealed_indices[0]]
        ax_sweep.axvline(
            threshold,
            color="#c93f4b",
            linestyle="--",
            label=f"Pinch-off at {threshold:.2f} µm",
        )
        ax_sweep.axvspan(threshold, 0.145, color="#c93f4b", alpha=0.08)
    ax_sweep.set_xlim(-0.005, 0.145)
    ax_sweep.set_ylim(-0.005, 0.255)
    ax_sweep.set_xlabel("Film thickness (µm)")
    ax_sweep.set_ylabel("Top opening CD (µm)")
    ax_sweep.set_title("Opening closure and pinch-off")
    ax_sweep.grid(alpha=0.25)
    ax_sweep.legend(loc="upper right")

    ax_void = fig.add_subplot(2, 3, 6, projection="3d")
    sealed = case["sealed"]
    x_crop = np.flatnonzero(np.abs(grid.x) <= 0.5)
    y_crop = np.flatnonzero(np.abs(grid.y) <= 0.5)
    z_crop = np.flatnonzero((grid.z >= -0.38) & (grid.z <= 0.02))
    trapped = np.transpose(sealed[np.ix_(z_crop, y_crop, x_crop)], (2, 1, 0))
    ax_void.voxels(
        trapped,
        facecolors=SEALED_VOID,
        edgecolors=(0.04, 0.16, 0.22, 0.16),
        linewidth=0.18,
        alpha=0.9,
    )
    ax_void.set_title(f"3D trapped void: {int(np.sum(sealed))} cells")
    ax_void.set_xlabel("x")
    ax_void.set_ylabel("y")
    ax_void.set_xticks([])
    ax_void.set_yticks([])
    ax_void.set_zticks([])
    ax_void.view_init(elev=25, azim=-55)
    ax_void.set_box_aspect((1.0, 1.0, 0.65))

    fig.text(
        0.5,
        0.045,
        "Purple: original material | orange: deposited film | blue: sealed void",
        ha="center",
        fontsize=10.5,
    )
    fig.savefig(output, dpi=175, bbox_inches="tight")
    plt.close(fig)


def save_case(case: dict[str, object], output: Path) -> None:
    grid = case["grid"]
    np.savez_compressed(
        output,
        x=grid.x,
        y=grid.y,
        z=grid.z,
        vertical_trench=case["vertical_trench"],
        coated=case["coated"],
        thin_film=case["thin_film"],
        wet_trench=case["wet_trench"],
        pinched=case["pinched"],
        pinch_film=case["pinch_film"],
        sealed_void=case["sealed"],
        coverage=case["coverage"],
        sweep_thickness=case["sweep_thickness"],
        sweep_width=case["sweep_width"],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("validation-conformal-deposition.png")
    )
    parser.add_argument(
        "--data", type=Path, default=Path("conformal-deposition-data.npz")
    )
    args = parser.parse_args()

    case = build_cases()
    plot_cases(case, args.output)
    save_case(case, args.data)
    print(
        "coverage top/bottom/sidewall = "
        + ", ".join(f"{value:.4f} um" for value in case["coverage"])
    )
    print(f"sealed void cells = {int(np.sum(case['sealed']))}")


if __name__ == "__main__":
    main()
