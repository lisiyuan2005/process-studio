"""High-quality full-resolution surface rendering of a saved 1T1C state."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import to_rgb
from matplotlib.patches import Patch
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from scipy.ndimage import zoom
from skimage.measure import marching_cubes

from process_studio.kernel.material_state import MaterialState
from process_studio.visualization import top_view_labels


COLORS = {
    "Si": "#6656B3",
    "SiO2": "#64B8CF",
    "Al2O3": "#F29B2E",
    "TiN": "#D5B437",
    "W": "#87929D",
}

ALPHAS = {"Si": 0.58, "SiO2": 0.42, "Al2O3": 0.96, "TiN": 1.0, "W": 1.0}


def surface_mesh(
    state: MaterialState,
    material: str,
    interpolation: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Extract the complete phi=0 surface without voxel downsampling."""
    field = state.fields[material]
    if interpolation > 1:
        # Linear interpolation preserves the zero crossing without the ringing
        # and overshoot that cubic splines introduce around thin films.
        field = zoom(field, interpolation, order=1, mode="nearest", prefilter=False)
    nz, ny, nx = field.shape
    spacing = (
        (state.grid.z_max - state.grid.z_min) / (nz - 1),
        (state.grid.y_max - state.grid.y_min) / (ny - 1),
        (state.grid.x_max - state.grid.x_min) / (nx - 1),
    )
    vertices_zyx, faces, _, _ = marching_cubes(
        field,
        level=0.0,
        spacing=spacing,
        step_size=1,
        allow_degenerate=False,
        method="lewiner",
    )
    vertices = np.column_stack(
        (
            vertices_zyx[:, 2] + state.grid.x_min,
            vertices_zyx[:, 1] + state.grid.y_min,
            vertices_zyx[:, 0] + state.grid.z_min,
        )
    )
    return vertices, faces


def lit_facecolors(
    triangles: np.ndarray,
    color: str,
    alpha: float,
) -> np.ndarray:
    edges_a = triangles[:, 1] - triangles[:, 0]
    edges_b = triangles[:, 2] - triangles[:, 0]
    normals = np.cross(edges_a, edges_b)
    lengths = np.linalg.norm(normals, axis=1)
    normals /= np.maximum(lengths[:, None], 1e-12)
    light = np.asarray([-0.45, -0.60, 0.82])
    light /= np.linalg.norm(light)
    diffuse = np.clip(normals @ light, -0.15, 1.0)
    brightness = 0.58 + 0.42 * (diffuse + 0.15) / 1.15
    base = np.asarray(to_rgb(color))
    rgba = np.empty((len(triangles), 4))
    rgba[:, :3] = np.clip(base[None, :] * brightness[:, None], 0.0, 1.0)
    rgba[:, 3] = alpha
    return rgba


def add_material_surface(
    axis,
    state: MaterialState,
    material: str,
    interpolation: int,
    cut_y: float,
) -> int:
    vertices, faces = surface_mesh(state, material, interpolation)
    triangles = vertices[faces]
    # Keep the back half of the array to create a real cutaway while retaining
    # the complete source mesh and full original Level Set resolution.
    keep = triangles[:, :, 1].mean(axis=1) >= cut_y
    triangles = triangles[keep]
    if not len(triangles):
        return 0
    surface = Poly3DCollection(
        triangles,
        facecolors=lit_facecolors(
            triangles,
            COLORS.get(material, "#87929D"),
            ALPHAS.get(material, 0.95),
        ),
        edgecolors="none",
        linewidths=0.0,
        antialiased=True,
    )
    axis.add_collection3d(surface)
    return len(triangles)


def render_section(axis, state: MaterialState, section_y: float) -> None:
    iy = int(np.argmin(np.abs(state.grid.y - section_y)))
    factor = 8
    for material in state.priority:
        section = zoom(
            state.fields[material][:, iy, :],
            factor,
            order=1,
            mode="nearest",
            prefilter=False,
        )
        # scipy.zoom returns n*factor samples; use coordinates matching it.
        x = np.linspace(state.grid.x_min, state.grid.x_max, section.shape[1])
        z = np.linspace(state.grid.z_min, state.grid.z_max, section.shape[0])
        if section.min() <= 0.0 <= section.max():
            axis.contourf(
                x,
                z,
                section,
                levels=[float(section.min()) - 1e-9, 0.0],
                colors=[COLORS.get(material, "#87929D")],
                antialiased=True,
            )
            axis.contour(
                x,
                z,
                section,
                levels=[0.0],
                colors=["#27313A"],
                linewidths=0.38,
                alpha=0.65,
            )
    axis.set_xlim(state.grid.x_min, state.grid.x_max)
    axis.set_ylim(-0.50, 0.30)
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("x (µm)")
    axis.set_ylabel("z (µm)")
    axis.set_title(f"AA section at y = {state.grid.y[iy]:.3f} µm")
    axis.grid(alpha=0.14, linewidth=0.5)


def render_top(axis, state: MaterialState, section_y: float) -> None:
    top = top_view_labels(state)
    factor = 8
    for index, material in enumerate(state.priority):
        occupancy = zoom(
            (top == index).astype(float),
            factor,
            order=1,
            mode="nearest",
            prefilter=False,
        )
        x = np.linspace(state.grid.x_min, state.grid.x_max, occupancy.shape[1])
        y = np.linspace(state.grid.y_min, state.grid.y_max, occupancy.shape[0])
        if occupancy.max() >= 0.5:
            axis.contourf(
                x,
                y,
                occupancy,
                levels=[0.5, float(occupancy.max()) + 1e-9],
                colors=[COLORS.get(material, "#87929D")],
                antialiased=True,
            )
            axis.contour(
                x,
                y,
                occupancy,
                levels=[0.5],
                colors=["#27313A"],
                linewidths=0.36,
                alpha=0.55,
            )
    axis.axhline(section_y, color="#D64045", linewidth=1.5, linestyle="--")
    axis.text(state.grid.x_min + 0.03, section_y + 0.025, "A", color="#B11F2A", weight="bold")
    axis.text(state.grid.x_max - 0.06, section_y + 0.025, "A", color="#B11F2A", weight="bold")
    axis.set_xlim(state.grid.x_min, state.grid.x_max)
    axis.set_ylim(state.grid.y_min, state.grid.y_max)
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("x (µm)")
    axis.set_ylabel("y (µm)")
    axis.set_title("Top view with AA cut line")


def render(state: MaterialState, output: Path, interpolation: int) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.titlesize": 13,
            "axes.labelsize": 10,
            "figure.facecolor": "#F7F9FC",
            "axes.facecolor": "#F7F9FC",
        }
    )
    figure = plt.figure(figsize=(16, 10))
    figure.subplots_adjust(
        left=0.045,
        right=0.98,
        bottom=0.095,
        top=0.90,
        wspace=0.20,
        hspace=0.32,
    )
    grid = figure.add_gridspec(2, 2, width_ratios=(1.12, 1.0), height_ratios=(1.12, 1.0))
    figure.suptitle(
        "Process Studio — Full-Resolution 2×2 3D 1T1C Surface Reconstruction",
        fontsize=18,
        fontweight="bold",
    )

    axis_3d = figure.add_subplot(grid[:, 0], projection="3d")
    triangle_counts = {}
    render_order = [name for name in ["Si", "SiO2", "Al2O3", "TiN", "W"] if name in state.fields]
    for material in render_order:
        triangle_counts[material] = add_material_surface(
            axis_3d,
            state,
            material,
            interpolation,
            cut_y=0.0,
        )
    axis_3d.set_xlim(state.grid.x_min, state.grid.x_max)
    axis_3d.set_ylim(state.grid.y_min, state.grid.y_max)
    axis_3d.set_zlim(-0.50, 0.30)
    axis_3d.set_xlabel("x (µm)", labelpad=8)
    axis_3d.set_ylabel("y (µm)", labelpad=8)
    axis_3d.set_zlabel("z (µm)", labelpad=6)
    axis_3d.set_title("Smooth φ = 0 surfaces · front-half cutaway", pad=16)
    axis_3d.view_init(elev=25, azim=-56)
    axis_3d.set_box_aspect((1.0, 1.0, 0.75))
    axis_3d.grid(alpha=0.16)
    axis_3d.legend(
        handles=[
            Patch(facecolor=COLORS.get(name, "#87929D"), label=name, alpha=ALPHAS.get(name, 1.0))
            for name in render_order
        ],
        loc="upper left",
        frameon=True,
        framealpha=0.92,
    )

    section_y = 0.225
    axis_section = figure.add_subplot(grid[0, 1])
    render_section(axis_section, state, section_y)

    axis_top = figure.add_subplot(grid[1, 1])
    render_top(axis_top, state, section_y)

    original_cells = state.grid.nx * state.grid.ny * state.grid.nz
    reconstructed_triangles = sum(triangle_counts.values())
    figure.text(
        0.5,
        0.008,
        (
            f"Source: {state.grid.nx}×{state.grid.ny}×{state.grid.nz} Level Set grid "
            f"({original_cells:,} cells) · {interpolation}× zero-crossing-preserving linear interpolation · "
            f"{reconstructed_triangles:,} rendered triangles · no voxel downsampling"
        ),
        ha="center",
        color="#46515B",
        fontsize=9.5,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=260, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    print(f"saved {output}")
    print(f"triangles: {triangle_counts}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interpolation", type=int, default=3, choices=range(1, 5))
    args = parser.parse_args()
    render(MaterialState.load(args.state), args.output, args.interpolation)


if __name__ == "__main__":
    main()
