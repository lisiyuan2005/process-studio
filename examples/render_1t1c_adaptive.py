"""Re-simulate and render the 2x2 1T1C demo with block-adaptive grids."""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import to_rgb
from matplotlib.patches import Patch
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from scipy.ndimage import map_coordinates
from skimage.measure import marching_cubes

from build_1t1c_demo import MATERIALS, make_flow, make_recipes, make_sketches
from process_studio.engine import ProcessEngine
from process_studio.kernel.adaptive import (
    AdaptiveMaterialState,
)
from process_studio.kernel.material_state import MaterialState
from process_studio.models import ProjectDefinition
from process_studio.visualization import top_view_labels


COLORS = {material.name: material.color for material in MATERIALS}
ALPHAS = {"Si": 0.62, "SiO2": 0.46, "Al2O3": 0.98, "TiN": 1.0, "W": 1.0, "Photoresist": 0.5}


def _labels_to_rgb(labels: np.ndarray, names: list[str]) -> np.ndarray:
    rgb = np.empty((*labels.shape, 3), dtype=np.float32)
    rgb[:] = to_rgb("#F7F9FC")
    for index, name in enumerate(names):
        rgb[labels == index] = to_rgb(COLORS.get(name, "#87929D"))
    return rgb


def _sample_section(
    state: MaterialState,
    section_y: float,
    x: np.ndarray,
    z: np.ndarray,
    names: list[str],
) -> np.ndarray:
    zz, xx = np.meshgrid(
        (z - state.grid.z_min) / state.grid.dz,
        (x - state.grid.x_min) / state.grid.dx,
        indexing="ij",
    )
    yy = np.full_like(xx, (section_y - state.grid.y_min) / state.grid.dy)
    fields = np.stack(
        [
            map_coordinates(
                state.fields[name], [zz, yy, xx], order=1, mode="nearest"
            )
            for name in state.priority
        ]
    )
    labels = np.full(xx.shape, -1, dtype=np.int16)
    for local_index, name in enumerate(state.priority):
        labels[fields[local_index] <= 0.0] = names.index(name)
    return labels


def continuous_section(
    adaptive: AdaptiveMaterialState,
    section_y: float,
    x: np.ndarray,
    z: np.ndarray,
) -> tuple[np.ndarray, list[str]]:
    names = adaptive.material_names
    result = _sample_section(adaptive.coarse, section_y, x, z, names)
    for patch in adaptive.patches:
        core_x_min, core_x_max, core_y_min, core_y_max = patch.box.core_bounds
        if not core_y_min <= section_y <= core_y_max:
            continue
        x_mask = (x >= core_x_min) & (x <= core_x_max)
        z_mask = (z >= patch.box.z_min) & (z <= patch.box.z_max)
        result[np.ix_(z_mask, x_mask)] = _sample_section(
            patch.state, section_y, x[x_mask], z[z_mask], names
        )
    return result, names


def simulate_adaptive(
    coarse: MaterialState,
    *,
    array_count: int,
    pitch: float,
    factor: int,
    solver_order: int = 1,
    tile_size: int | None = None,
    logger=print,
) -> AdaptiveMaterialState:
    recipes = make_recipes()
    for recipe in recipes:
        if recipe.process_type.value == "etch":
            recipe.parameters["solver_order"] = solver_order
            recipe.parameters["tile_shape"] = tile_size
    flow = make_flow(recipes)
    engine = ProcessEngine(
        {recipe.id: recipe for recipe in recipes},
        sketches=make_sketches(array_count, pitch), logger=logger,
    )
    project = ProjectDefinition("1T1C general engine", dict(coarse.grid.__dict__))
    def initialize(grid):
        state = MaterialState(grid)
        state.add_material("Si", grid.substrate())
        return state
    adaptive, plan = engine.run_refined_branch(
        initialize, project, flow, factor=factor,
    )
    logger(f"Execution plan: {plan.mode}; {plan.node_count:,} fine nodes")
    return adaptive


def _surface_mesh(state: MaterialState, material: str) -> tuple[np.ndarray, np.ndarray]:
    field = state.fields[material]
    if field.min() > 0.0 or field.max() < 0.0:
        return np.empty((0, 3)), np.empty((0, 3), dtype=int)
    vertices_zyx, faces, _, _ = marching_cubes(
        field,
        level=0.0,
        spacing=(state.grid.dz, state.grid.dy, state.grid.dx),
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


def _lit_facecolors(
    vertices: np.ndarray,
    faces: np.ndarray,
    color: str,
    alpha: float,
) -> np.ndarray:
    triangles = vertices[faces]
    raw_normals = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    vertex_normals = np.zeros_like(vertices)
    for corner in range(3):
        np.add.at(vertex_normals, faces[:, corner], raw_normals)
    vertex_normals /= np.maximum(
        np.linalg.norm(vertex_normals, axis=1)[:, None], 1e-12
    )
    normals = vertex_normals[faces].mean(axis=1)
    normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-12)
    light = np.asarray([-0.45, -0.60, 0.82])
    light /= np.linalg.norm(light)
    diffuse = np.clip(normals @ light, -0.15, 1.0)
    brightness = 0.58 + 0.42 * (diffuse + 0.15) / 1.15
    base = np.asarray(to_rgb(color))
    rgba = np.empty((len(triangles), 4))
    rgba[:, :3] = np.clip(base[None, :] * brightness[:, None], 0.0, 1.0)
    rgba[:, 3] = alpha
    return rgba


def _add_patch_surfaces(axis, adaptive: AdaptiveMaterialState, cut_y: float) -> int:
    triangle_count = 0
    order = [material.name for material in MATERIALS if material.name in adaptive.material_names]
    for material in order:
        for patch in adaptive.patches:
            core_x_min, core_x_max, core_y_min, core_y_max = patch.box.core_bounds
            if core_y_max < cut_y or material not in patch.state.fields:
                continue
            vertices, faces = _surface_mesh(patch.state, material)
            triangles = vertices[faces]
            centroids = triangles.mean(axis=1)
            keep = (
                (centroids[:, 0] >= core_x_min)
                & (centroids[:, 0] <= core_x_max)
                & (centroids[:, 1] >= max(cut_y, core_y_min))
                & (centroids[:, 1] <= core_y_max)
            )
            triangles = triangles[keep]
            if not len(triangles):
                continue
            axis.add_collection3d(
                Poly3DCollection(
                    triangles,
                    facecolors=_lit_facecolors(
                        vertices,
                        faces,
                        COLORS.get(material, "#87929D"),
                        ALPHAS.get(material, 0.95),
                    )[keep],
                    edgecolors="none",
                    linewidths=0.0,
                    antialiased=True,
                )
            )
            triangle_count += len(triangles)
    return triangle_count


def _add_state_surfaces(axis, state: MaterialState, cut_y: float) -> int:
    """Draw raw zero-isosurfaces from one uniform-grid state."""
    triangle_count = 0
    order = [material.name for material in MATERIALS if material.name in state.fields]
    for material in order:
        vertices, faces = _surface_mesh(state, material)
        if not len(faces):
            continue
        triangles = vertices[faces]
        keep = triangles.mean(axis=1)[:, 1] >= cut_y
        triangles = triangles[keep]
        if not len(triangles):
            continue
        axis.add_collection3d(
            Poly3DCollection(
                triangles,
                facecolors=_lit_facecolors(
                    vertices,
                    faces,
                    COLORS.get(material, "#87929D"),
                    ALPHAS.get(material, 0.95),
                )[keep],
                edgecolors="none",
                linewidths=0.0,
                antialiased=True,
            )
        )
        triangle_count += len(triangles)
    return triangle_count


def render_step_state(
    state: MaterialState,
    output: Path,
    *,
    step_number: int,
    step_name: str,
    section_y: float = 0.225,
) -> None:
    """Render a consistent 3D/AA/top-view plate for one saved process step."""
    grid = state.grid
    names = state.priority
    display_spacing = grid.dx / 4.0
    x = np.arange(grid.x_min, grid.x_max + display_spacing / 2.0, display_spacing)
    z = np.arange(-0.50, 0.30 + display_spacing / 2.0, display_spacing)
    section = _sample_section(state, section_y, x, z, names)
    top = top_view_labels(state)

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.titlesize": 12,
            "axes.labelsize": 9,
            "figure.facecolor": "#F7F9FC",
            "axes.facecolor": "#F7F9FC",
        }
    )
    figure = plt.figure(figsize=(14, 8.2))
    figure.subplots_adjust(
        left=0.05, right=0.98, bottom=0.105, top=0.87, wspace=0.22, hspace=0.34
    )
    layout = figure.add_gridspec(2, 2, width_ratios=(1.08, 1.0))
    figure.suptitle(
        f"1T1C process result {step_number:02d}/09 — {step_name}",
        fontsize=17,
        fontweight="bold",
    )

    axis_3d = figure.add_subplot(layout[:, 0], projection="3d")
    triangle_count = _add_state_surfaces(axis_3d, state, cut_y=0.0)
    axis_3d.set_xlim(grid.x_min, grid.x_max)
    axis_3d.set_ylim(0.0, grid.y_max)
    axis_3d.set_zlim(-0.50, 0.30)
    axis_3d.set_xlabel("x (µm)")
    axis_3d.set_ylabel("y (µm)")
    axis_3d.set_zlabel("z (µm)")
    axis_3d.set_title("Raw zero-isosurface · front-half cutaway", pad=14)
    axis_3d.view_init(elev=25, azim=-56)
    axis_3d.set_box_aspect((1.0, 0.55, 0.82))
    axis_3d.grid(alpha=0.16)
    axis_3d.legend(
        handles=[
            Patch(facecolor=COLORS.get(name, "#87929D"), label=name,
                  alpha=ALPHAS.get(name, 1.0))
            for name in names
        ],
        loc="upper left",
        framealpha=0.92,
    )

    axis_section = figure.add_subplot(layout[0, 1])
    axis_section.imshow(
        _labels_to_rgb(section, names),
        origin="lower",
        extent=(x[0], x[-1], z[0], z[-1]),
        interpolation="nearest",
        aspect="equal",
    )
    axis_section.set_xlim(grid.x_min, grid.x_max)
    axis_section.set_ylim(-0.50, 0.30)
    axis_section.set_xlabel("x (µm)")
    axis_section.set_ylabel("z (µm)")
    axis_section.set_title(f"AA section at y={section_y:g} µm · continuous field sample")
    axis_section.grid(alpha=0.12, linewidth=0.5)

    axis_top = figure.add_subplot(layout[1, 1])
    axis_top.imshow(
        _labels_to_rgb(top, names),
        origin="lower",
        extent=(grid.x_min, grid.x_max, grid.y_min, grid.y_max),
        interpolation="nearest",
    )
    axis_top.axhline(section_y, color="#D64045", linewidth=1.25, linestyle="--")
    axis_top.text(grid.x_min + 0.03, section_y + 0.025, "A", color="#B11F2A", weight="bold")
    axis_top.text(grid.x_max - 0.06, section_y + 0.025, "A", color="#B11F2A", weight="bold")
    axis_top.set_xlim(grid.x_min, grid.x_max)
    axis_top.set_ylim(grid.y_min, grid.y_max)
    axis_top.set_aspect("equal", adjustable="box")
    axis_top.set_xlabel("x (µm)")
    axis_top.set_ylabel("y (µm)")
    axis_top.set_title("Top view · native solver-grid labels")

    figure.text(
        0.5,
        0.027,
        f"actual solver grid: {grid.dx*1000:g} nm · {grid.nx}×{grid.ny}×{grid.nz} "
        f"({grid.nx*grid.ny*grid.nz:,} nodes) · no interface smoothing · "
        f"{triangle_count:,} rendered triangles",
        ha="center",
        color="#46515B",
        fontsize=9.2,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=180, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    print(f"saved {output}; triangles={triangle_count:,}", flush=True)


def render_step_contact_sheet(
    state_paths: list[Path],
    step_names: list[str],
    output: Path,
    *,
    section_y: float = 0.225,
) -> None:
    """Render all AA sections with identical bounds in one comparison image."""
    if len(state_paths) != len(step_names) or not state_paths:
        raise ValueError("state paths and step names must have the same non-zero length")
    plates: list[np.ndarray] = []
    spacing_nm = 0.0
    x = z = None
    for path in state_paths:
        state = MaterialState.load(path)
        spacing_nm = state.grid.dx * 1000.0
        display_spacing = state.grid.dx / 4.0
        x = np.arange(
            state.grid.x_min,
            state.grid.x_max + display_spacing / 2.0,
            display_spacing,
        )
        z = np.arange(-0.50, 0.30 + display_spacing / 2.0, display_spacing)
        section = _sample_section(state, section_y, x, z, state.priority)
        plates.append(_labels_to_rgb(section, state.priority))
    assert x is not None and z is not None

    # The sheet sizes itself to the flow rather than assuming nine steps.
    columns = 3
    rows = math.ceil(len(plates) / columns)
    figure, axes = plt.subplots(
        rows, columns, figsize=(15, 3 * rows), sharex=True, sharey=True, squeeze=False
    )
    figure.subplots_adjust(
        left=0.06, right=0.985, bottom=0.085, top=0.89, wspace=0.13, hspace=0.28
    )
    figure.suptitle(
        "1T1C process evolution — AA section after each saved step",
        fontsize=18,
        fontweight="bold",
    )
    for index, (axis, plate, name) in enumerate(
        zip(axes.flat, plates, step_names), start=1
    ):
        axis.imshow(
            plate,
            origin="lower",
            extent=(x[0], x[-1], z[0], z[-1]),
            interpolation="nearest",
            aspect="equal",
        )
        axis.set_title(f"{index:02d}  {name}", fontsize=10.5)
        axis.set_xlim(x[0], x[-1])
        axis.set_ylim(z[0], z[-1])
        axis.grid(alpha=.10, linewidth=.4)
    for axis in axes.flat[len(plates):]:
        axis.set_axis_off()
    for axis in axes[-1, :]:
        axis.set_xlabel("x (µm)")
    for axis in axes[:, 0]:
        axis.set_ylabel("z (µm)")
    figure.text(
        .5,
        .025,
        f"AA y={section_y:g} µm · actual solver grid {spacing_nm:g} nm · "
        "continuous-field sampling · no symmetry correction or interface smoothing",
        ha="center",
        color="#46515B",
        fontsize=9.5,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=180, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    print(f"saved {output}", flush=True)


def render(adaptive: AdaptiveMaterialState, output: Path) -> None:
    coarse = adaptive.coarse.grid
    fine_spacing = min(patch.state.grid.dx for patch in adaptive.patches)
    display_spacing = fine_spacing / 4.0
    x = np.arange(coarse.x_min, coarse.x_max + display_spacing / 2.0, display_spacing)
    y = np.arange(coarse.y_min, coarse.y_max + fine_spacing / 2.0, fine_spacing)
    z = np.arange(-0.50, 0.30 + display_spacing / 2.0, display_spacing)
    section_y = 0.225
    section, names = continuous_section(adaptive, section_y, x, z)
    top, top_names, top_level = adaptive.top_labels(x, y)
    if top_names != names:
        raise RuntimeError("adaptive views produced inconsistent material order")

    section_rgb = _labels_to_rgb(section, names)
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
    figure.subplots_adjust(left=0.045, right=0.98, bottom=0.10, top=0.90, wspace=0.20, hspace=0.32)
    layout = figure.add_gridspec(2, 2, width_ratios=(1.12, 1.0), height_ratios=(1.12, 1.0))
    figure.suptitle(
        "Process Studio — 2×2 3D 1T1C / General Engine",
        fontsize=18,
        fontweight="bold",
    )

    axis_3d = figure.add_subplot(layout[:, 0], projection="3d")
    triangle_count = _add_patch_surfaces(axis_3d, adaptive, cut_y=0.0)
    axis_3d.set_xlim(coarse.x_min, coarse.x_max)
    axis_3d.set_ylim(0.0, coarse.y_max)
    axis_3d.set_zlim(-0.50, 0.30)
    axis_3d.set_xlabel("x (µm)", labelpad=8)
    axis_3d.set_ylabel("y (µm)", labelpad=8)
    axis_3d.set_zlabel("z (µm)", labelpad=6)
    axis_3d.set_title(f"{fine_spacing*1000:g} nm solver grid · front-half cutaway", pad=16)
    axis_3d.view_init(elev=25, azim=-56)
    axis_3d.set_box_aspect((1.0, 0.55, 0.82))
    axis_3d.grid(alpha=0.16)
    axis_3d.legend(
        handles=[Patch(facecolor=COLORS[name], label=name, alpha=ALPHAS.get(name, 1.0)) for name in names],
        loc="upper left",
        frameon=True,
        framealpha=0.92,
    )

    axis_section = figure.add_subplot(layout[0, 1])
    axis_section.imshow(
        section_rgb,
        origin="lower",
        extent=(x[0], x[-1], z[0], z[-1]),
        interpolation="antialiased",
        aspect="equal",
    )
    axis_section.set_xlim(coarse.x_min, coarse.x_max)
    axis_section.set_ylim(-0.50, 0.30)
    axis_section.set_xlabel("x (µm)")
    axis_section.set_ylabel("z (µm)")
    axis_section.set_title("AA section · raw interpolated zero interfaces")
    axis_section.grid(alpha=0.12, linewidth=0.5)

    axis_top = figure.add_subplot(layout[1, 1])
    axis_top.imshow(
        _labels_to_rgb(top, names),
        origin="lower",
        extent=(x[0], x[-1], y[0], y[-1]),
        interpolation="nearest",
    )
    for patch in adaptive.patches:
        core_x_min, core_x_max, core_y_min, core_y_max = patch.box.core_bounds
        axis_top.add_patch(
            plt.Rectangle(
                (core_x_min, core_y_min),
                core_x_max - core_x_min,
                core_y_max - core_y_min,
                fill=False,
                edgecolor="#27313A",
                linewidth=0.65,
                linestyle=(0, (3, 3)),
                alpha=0.42,
            )
        )
    axis_top.axhline(section_y, color="#D64045", linewidth=1.35, linestyle="--")
    axis_top.text(coarse.x_min + 0.03, section_y + 0.025, "A", color="#B11F2A", weight="bold")
    axis_top.text(coarse.x_max - 0.06, section_y + 0.025, "A", color="#B11F2A", weight="bold")
    axis_top.set_xlim(coarse.x_min, coarse.x_max)
    axis_top.set_ylim(coarse.y_min, coarse.y_max)
    axis_top.set_aspect("equal", adjustable="box")
    axis_top.set_xlabel("x (µm)")
    axis_top.set_ylabel("y (µm)")
    axis_top.set_title("Top view · native grid labels (no smoothing)")

    coarse_cells = coarse.nx * coarse.ny * coarse.nz
    fine_cells = sum(np.prod(patch.state.shape) for patch in adaptive.patches)
    refined_pixels = int(np.count_nonzero(top_level))
    figure.text(
        0.5,
        0.016,
        (
            f"base grid: {coarse.dx * 1000:.1f} nm ({coarse_cells:,} nodes) · "
            f"fine solve: {fine_spacing * 1000:.2f} nm ({fine_cells:,} nodes) · "
            f"automatic solve regions · no mesh smoothing · {triangle_count:,} triangles"
        ),
        ha="center",
        color="#46515B",
        fontsize=9.4,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=260, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    print(f"saved {output}")
    print(f"composite refined pixels: {refined_pixels:,}; triangles: {triangle_count:,}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adaptive-dir", type=Path, required=True)
    parser.add_argument("--array", type=int, default=2)
    parser.add_argument("--pitch", type=float, default=0.45)
    parser.add_argument("--factor", type=int, default=4)
    parser.add_argument("--solver-order", type=int, choices=[1, 2], default=1)
    parser.add_argument("--tile-size", type=int)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()

    if args.reuse and (args.adaptive_dir / "adaptive-state.json").exists():
        print("REUSE: no simulation; solver-order, factor and tile-size do not alter saved data.")
        adaptive = AdaptiveMaterialState.load(args.adaptive_dir)
        print(f"loaded {args.adaptive_dir}")
    else:
        adaptive = simulate_adaptive(
            MaterialState.load(args.state),
            array_count=args.array,
            pitch=args.pitch,
            factor=args.factor,
            solver_order=args.solver_order,
            tile_size=args.tile_size,
        )
        adaptive.save(args.adaptive_dir)
        print(f"saved {args.adaptive_dir}")
    render(adaptive, args.output)


if __name__ == "__main__":
    main()
