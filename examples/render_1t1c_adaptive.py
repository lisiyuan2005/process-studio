"""Re-simulate and render the 2x2 1T1C demo with block-adaptive grids."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap, to_rgb
from matplotlib.patches import Patch
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from skimage.measure import marching_cubes

from build_1t1c_demo import MATERIALS, make_flow, make_recipes, make_sketches
from process_studio.engine import ProcessEngine
from process_studio.kernel.adaptive import (
    AdaptiveMaterialState,
    AdaptivePatch,
    tiled_refinement_boxes,
)
from process_studio.kernel.material_state import MaterialState
from process_studio.models import ProjectDefinition


COLORS = {material.name: material.color for material in MATERIALS}
ALPHAS = {"Si": 0.62, "SiO2": 0.46, "Al2O3": 0.98, "TiN": 1.0, "W": 1.0}


def simulate_adaptive(
    coarse: MaterialState,
    *,
    array_count: int,
    pitch: float,
    factor: int,
    logger=print,
) -> AdaptiveMaterialState:
    centers = [
        (index - (array_count - 1) / 2.0) * pitch for index in range(array_count)
    ]
    boxes = tiled_refinement_boxes(
        coarse.grid,
        centers_x=centers,
        centers_y=centers,
        tile_size=pitch,
        z_min=-0.50,
        z_max=0.30,
        factor=factor,
    )
    sketches = make_sketches(array_count, pitch)
    recipes = make_recipes()
    flow = make_flow(recipes)
    engine = ProcessEngine(
        {recipe.id: recipe for recipe in recipes},
        sketches=sketches,
        logger=lambda message: None,
    )
    patches: list[AdaptivePatch] = []
    for index, box in enumerate(boxes, start=1):
        grid = box.make_grid(coarse.grid)
        state = MaterialState(grid)
        state.add_material("Si", grid.substrate())
        project = ProjectDefinition(
            f"Adaptive {box.name}",
            dict(grid.__dict__),
            active_branch_id=flow.id,
            id=f"adaptive-{box.name}",
        )
        started = time.perf_counter()
        logger(
            f"AMR {index}/{len(boxes)} {box.name}: "
            f"{grid.nx}×{grid.ny}×{grid.nz}, dx={grid.dx * 1000:.2f} nm"
        )
        final = engine.run_branch(state, project, flow)
        patches.append(AdaptivePatch(box, final))
        logger(f"AMR {box.name} completed in {time.perf_counter() - started:.1f} s")
    return AdaptiveMaterialState(coarse, patches)


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


def _lit_facecolors(triangles: np.ndarray, color: str, alpha: float) -> np.ndarray:
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
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
    order = [name for name in ["Si", "SiO2", "Al2O3", "TiN", "W"] if name in adaptive.material_names]
    for material in order:
        for patch in adaptive.patches:
            if patch.box.y_max < cut_y or material not in patch.state.fields:
                continue
            vertices, faces = _surface_mesh(patch.state, material)
            triangles = vertices[faces]
            keep = triangles[:, :, 1].mean(axis=1) >= cut_y
            triangles = triangles[keep]
            if not len(triangles):
                continue
            axis.add_collection3d(
                Poly3DCollection(
                    triangles,
                    facecolors=_lit_facecolors(
                        triangles,
                        COLORS.get(material, "#87929D"),
                        ALPHAS.get(material, 0.95),
                    ),
                    edgecolors="none",
                    linewidths=0.0,
                    antialiased=True,
                )
            )
            triangle_count += len(triangles)
    return triangle_count


def render(adaptive: AdaptiveMaterialState, output: Path) -> None:
    coarse = adaptive.coarse.grid
    fine_spacing = min(patch.state.grid.dx for patch in adaptive.patches)
    x = np.arange(coarse.x_min, coarse.x_max + fine_spacing / 2.0, fine_spacing)
    y = np.arange(coarse.y_min, coarse.y_max + fine_spacing / 2.0, fine_spacing)
    z = np.arange(-0.50, 0.30 + fine_spacing / 2.0, fine_spacing)
    section_y = 0.225
    section, names, section_level = adaptive.section_labels(section_y, x, z)
    top, top_names, top_level = adaptive.top_labels(x, y)
    if top_names != names:
        raise RuntimeError("adaptive views produced inconsistent material order")

    colors = ["#F7F9FC", *[COLORS.get(name, "#87929D") for name in names]]
    cmap = ListedColormap(colors)
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
        "Process Studio — Block-Adaptive 2×2 3D 1T1C Simulation",
        fontsize=18,
        fontweight="bold",
    )

    axis_3d = figure.add_subplot(layout[:, 0], projection="3d")
    triangle_count = _add_patch_surfaces(axis_3d, adaptive, cut_y=0.0)
    axis_3d.set_xlim(-0.48, 0.48)
    axis_3d.set_ylim(0.0, 0.48)
    axis_3d.set_zlim(-0.50, 0.30)
    axis_3d.set_xlabel("x (µm)", labelpad=8)
    axis_3d.set_ylabel("y (µm)", labelpad=8)
    axis_3d.set_zlabel("z (µm)", labelpad=6)
    axis_3d.set_title("6.25 nm Level Set patches · front-half cutaway", pad=16)
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
        section + 1,
        origin="lower",
        extent=(x[0], x[-1], z[0], z[-1]),
        cmap=cmap,
        vmin=0,
        vmax=len(colors) - 1,
        interpolation="nearest",
        aspect="equal",
    )
    axis_section.set_xlim(coarse.x_min, coarse.x_max)
    axis_section.set_ylim(-0.50, 0.30)
    axis_section.set_xlabel("x (µm)")
    axis_section.set_ylabel("z (µm)")
    axis_section.set_title("AA section · unified labels (no false gaps)")
    axis_section.grid(alpha=0.12, linewidth=0.5)

    axis_top = figure.add_subplot(layout[1, 1])
    axis_top.imshow(
        top + 1,
        origin="lower",
        extent=(x[0], x[-1], y[0], y[-1]),
        cmap=cmap,
        vmin=0,
        vmax=len(colors) - 1,
        interpolation="nearest",
    )
    for patch in adaptive.patches:
        axis_top.add_patch(
            plt.Rectangle(
                (patch.box.x_min, patch.box.y_min),
                patch.box.x_max - patch.box.x_min,
                patch.box.y_max - patch.box.y_min,
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
    axis_top.set_title("Top view · dashed outlines are refined blocks")

    coarse_cells = coarse.nx * coarse.ny * coarse.nz
    fine_cells = sum(np.prod(patch.state.shape) for patch in adaptive.patches)
    refined_pixels = int(np.count_nonzero(section_level) + np.count_nonzero(top_level))
    figure.text(
        0.5,
        0.016,
        (
            f"AMR level 0: {coarse.dx * 1000:.1f} nm ({coarse_cells:,} nodes) · "
            f"level 2: {fine_spacing * 1000:.2f} nm ({fine_cells:,} local nodes) · "
            f"{triangle_count:,} triangles · categorical compositing prevents renderer-created voids"
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
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()

    if args.reuse and (args.adaptive_dir / "adaptive-state.json").exists():
        adaptive = AdaptiveMaterialState.load(args.adaptive_dir)
        print(f"loaded {args.adaptive_dir}")
    else:
        adaptive = simulate_adaptive(
            MaterialState.load(args.state),
            array_count=args.array,
            pitch=args.pitch,
            factor=args.factor,
        )
        adaptive.save(args.adaptive_dir)
        print(f"saved {args.adaptive_dir}")
    render(adaptive, args.output)


if __name__ == "__main__":
    main()
