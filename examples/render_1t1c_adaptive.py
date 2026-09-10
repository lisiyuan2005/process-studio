"""Re-simulate and render the 2x2 1T1C demo with block-adaptive grids."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import to_rgb
from matplotlib.patches import Patch
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from scipy.ndimage import binary_dilation, label, map_coordinates
from scipy.sparse import coo_matrix
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


def _labels_to_rgb(labels: np.ndarray, names: list[str]) -> np.ndarray:
    """Convert phase labels before resampling so colors cannot create false voids."""
    rgb = np.empty((*labels.shape, 3), dtype=np.float32)
    rgb[:] = to_rgb("#F7F9FC")
    for index, name in enumerate(names):
        rgb[labels == index] = to_rgb(COLORS.get(name, "#87929D"))
    return rgb


def _remove_small_label_islands(labels: np.ndarray, minimum_pixels: int = 32) -> np.ndarray:
    """Suppress sub-pixel phase specks without moving resolved interfaces."""
    result = labels.copy()
    structure = np.ones((3, 3), dtype=bool)
    for value in range(-1, int(labels.max()) + 1):
        components, count = label(result == value)
        if count == 0:
            continue
        sizes = np.bincount(components.ravel())
        for component_id in np.flatnonzero(
            (sizes < minimum_pixels) & (np.arange(len(sizes)) > 0)
        ):
            island = components == component_id
            ring = binary_dilation(island, structure=structure) & ~island
            neighbors = result[ring]
            neighbors = neighbors[neighbors != value]
            if neighbors.size:
                choices, frequencies = np.unique(neighbors, return_counts=True)
                result[island] = choices[np.argmax(frequencies)]
    return result


def _sample_field_section(
    field: np.ndarray,
    state: MaterialState,
    section_y: float,
    x: np.ndarray,
    z: np.ndarray,
) -> np.ndarray:
    zz, xx = np.meshgrid(
        (z - state.grid.z_min) / state.grid.dz,
        (x - state.grid.x_min) / state.grid.dx,
        indexing="ij",
    )
    yy = np.full_like(xx, (section_y - state.grid.y_min) / state.grid.dy)
    return map_coordinates(field, [zz, yy, xx], order=1, mode="nearest", prefilter=False)


def _continuous_state_section(
    state: MaterialState,
    section_y: float,
    x: np.ndarray,
    z: np.ndarray,
    global_names: list[str],
) -> np.ndarray:
    sampled_fields = np.stack(
        [_sample_field_section(state.fields[name], state, section_y, x, z) for name in state.priority]
    )
    sampled_union = _sample_field_section(state.combined_phi(), state, section_y, x, z)
    labels = np.full(sampled_union.shape, -1, dtype=np.int16)
    for local_index, name in enumerate(state.priority):
        labels[sampled_fields[local_index] <= 0.0] = global_names.index(name)
    # min(phi) and interpolate(min(phi)) do not commute.  At a material
    # junction that can leave a sub-pixel where every independently sampled
    # field is slightly positive.  The separately sampled union establishes
    # whether that point is actually solid, and the closest material owns it.
    missing_solid = (sampled_union <= 0.0) & (labels < 0)
    if missing_solid.any():
        closest = np.argmin(sampled_fields, axis=0)
        remap = np.asarray([global_names.index(name) for name in state.priority])
        labels[missing_solid] = remap[closest[missing_solid]]
    return labels


def continuous_adaptive_section(
    adaptive: AdaptiveMaterialState,
    section_y: float,
    x: np.ndarray,
    z: np.ndarray,
) -> tuple[np.ndarray, list[str]]:
    """Sample continuous phi fields, then resolve one material per sub-pixel."""
    names = adaptive.material_names
    result = _continuous_state_section(adaptive.coarse, section_y, x, z, names)
    for patch in sorted(adaptive.patches, key=lambda item: item.box.factor):
        tolerance = patch.state.grid.dx * 1e-9
        if not patch.box.y_min - tolerance <= section_y <= patch.box.y_max + tolerance:
            continue
        x_mask = (x >= patch.box.x_min - tolerance) & (x <= patch.box.x_max + tolerance)
        z_mask = (z >= patch.box.z_min - tolerance) & (z <= patch.box.z_max + tolerance)
        if x_mask.any() and z_mask.any():
            result[np.ix_(z_mask, x_mask)] = _continuous_state_section(
                patch.state,
                section_y,
                x[x_mask],
                z[z_mask],
                names,
            )
    return result, names


def _material_top_height(state: MaterialState, material: str) -> tuple[np.ndarray, np.ndarray]:
    inside = state.fields[material] <= 0.0
    present = inside.any(axis=0)
    top_indices = np.max(np.where(inside, np.arange(state.grid.nz)[:, None, None], -1), axis=0)
    height = np.where(present, state.grid.z[np.maximum(top_indices, 0)], state.grid.z_min)
    # The minimum Level Set value over z is a smooth lateral presence function.
    lateral_phi = np.min(state.fields[material], axis=0)
    return lateral_phi, height


def _continuous_state_top(
    state: MaterialState,
    x: np.ndarray,
    y: np.ndarray,
    global_names: list[str],
) -> np.ndarray:
    yy, xx = np.meshgrid(
        (y - state.grid.y_min) / state.grid.dy,
        (x - state.grid.x_min) / state.grid.dx,
        indexing="ij",
    )
    labels = np.full(xx.shape, -1, dtype=np.int16)
    visible_height = np.full(xx.shape, -np.inf)
    for name in state.priority:
        lateral_phi, height = _material_top_height(state, name)
        sampled_phi = map_coordinates(lateral_phi, [yy, xx], order=1, mode="nearest", prefilter=False)
        sampled_height = map_coordinates(height, [yy, xx], order=1, mode="nearest", prefilter=False)
        visible = (sampled_phi <= 0.0) & (sampled_height >= visible_height)
        labels[visible] = global_names.index(name)
        visible_height[visible] = sampled_height[visible]
    return labels


def continuous_adaptive_top(
    adaptive: AdaptiveMaterialState,
    x: np.ndarray,
    y: np.ndarray,
) -> tuple[np.ndarray, list[str]]:
    names = adaptive.material_names
    result = _continuous_state_top(adaptive.coarse, x, y, names)
    for patch in sorted(adaptive.patches, key=lambda item: item.box.factor):
        tolerance = patch.state.grid.dx * 1e-9
        x_mask = (x >= patch.box.x_min - tolerance) & (x <= patch.box.x_max + tolerance)
        y_mask = (y >= patch.box.y_min - tolerance) & (y <= patch.box.y_max + tolerance)
        if x_mask.any() and y_mask.any():
            result[np.ix_(y_mask, x_mask)] = _continuous_state_top(
                patch.state,
                x[x_mask],
                y[y_mask],
                names,
            )
    return result, names


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


def _taubin_smooth(
    vertices: np.ndarray,
    faces: np.ndarray,
    state: MaterialState,
    iterations: int,
) -> np.ndarray:
    """Apply non-shrinking display smoothing while pinning patch boundaries."""
    if iterations <= 0 or not len(faces):
        return vertices
    edges = np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    edges = np.vstack((edges, edges[:, ::-1]))
    adjacency = coo_matrix(
        (np.ones(len(edges)), (edges[:, 0], edges[:, 1])),
        shape=(len(vertices), len(vertices)),
    ).tocsr()
    degree = np.asarray(adjacency.sum(axis=1)).ravel()
    tolerance = state.grid.dx * 0.1
    pinned = (
        np.isclose(vertices[:, 0], state.grid.x_min, atol=tolerance)
        | np.isclose(vertices[:, 0], state.grid.x_max, atol=tolerance)
        | np.isclose(vertices[:, 1], state.grid.y_min, atol=tolerance)
        | np.isclose(vertices[:, 1], state.grid.y_max, atol=tolerance)
        | np.isclose(vertices[:, 2], state.grid.z_min, atol=tolerance)
        | np.isclose(vertices[:, 2], state.grid.z_max, atol=tolerance)
    )
    smoothed = vertices.copy()
    movable = (~pinned) & (degree > 0)
    for _ in range(iterations):
        for strength in (0.28, -0.29):
            average = adjacency @ smoothed
            average /= np.maximum(degree[:, None], 1.0)
            smoothed[movable] += strength * (average[movable] - smoothed[movable])
    return smoothed


def _surface_mesh(
    state: MaterialState,
    material: str,
    smoothing_iterations: int,
) -> tuple[np.ndarray, np.ndarray]:
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
    return _taubin_smooth(vertices, faces, state, smoothing_iterations), faces


def _lit_facecolors(
    vertices: np.ndarray,
    faces: np.ndarray,
    color: str,
    alpha: float,
) -> np.ndarray:
    """Approximate smooth shading using area-weighted vertex normals."""
    triangles = vertices[faces]
    face_normals = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    vertex_normals = np.zeros_like(vertices)
    for corner in range(3):
        np.add.at(vertex_normals, faces[:, corner], face_normals)
    vertex_normals /= np.maximum(np.linalg.norm(vertex_normals, axis=1)[:, None], 1e-12)
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


def _add_patch_surfaces(
    axis,
    adaptive: AdaptiveMaterialState,
    cut_y: float,
    smoothing_iterations: int,
) -> int:
    triangle_count = 0
    order = [name for name in ["Si", "SiO2", "Al2O3", "TiN", "W"] if name in adaptive.material_names]
    for material in order:
        for patch in adaptive.patches:
            if patch.box.y_max < cut_y or material not in patch.state.fields:
                continue
            vertices, faces = _surface_mesh(patch.state, material, smoothing_iterations)
            triangles = vertices[faces]
            keep = triangles[:, :, 1].mean(axis=1) >= cut_y
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


def render(
    adaptive: AdaptiveMaterialState,
    output: Path,
    interface_spacing_nm: float = 1.0,
    smoothing_iterations: int = 8,
) -> None:
    coarse = adaptive.coarse.grid
    fine_spacing = min(patch.state.grid.dx for patch in adaptive.patches)
    if interface_spacing_nm <= 0:
        raise ValueError("interface spacing must be positive")
    display_spacing = interface_spacing_nm / 1000.0
    x = np.arange(coarse.x_min, coarse.x_max + display_spacing / 2.0, display_spacing)
    y = np.arange(coarse.y_min, coarse.y_max + display_spacing / 2.0, display_spacing)
    z = np.arange(-0.50, 0.30 + display_spacing / 2.0, display_spacing)
    section_y = 0.225
    section, names = continuous_adaptive_section(adaptive, section_y, x, z)
    top, top_names = continuous_adaptive_top(adaptive, x, y)
    section = _remove_small_label_islands(section)
    top = _remove_small_label_islands(top)
    if top_names != names:
        raise RuntimeError("adaptive views produced inconsistent material order")

    section_rgb = _labels_to_rgb(section, names)
    top_rgb = _labels_to_rgb(top, names)
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
    triangle_count = _add_patch_surfaces(
        axis_3d,
        adaptive,
        cut_y=0.0,
        smoothing_iterations=smoothing_iterations,
    )
    axis_3d.set_xlim(-0.48, 0.48)
    axis_3d.set_ylim(0.0, 0.48)
    axis_3d.set_zlim(-0.50, 0.30)
    axis_3d.set_xlabel("x (µm)", labelpad=8)
    axis_3d.set_ylabel("y (µm)", labelpad=8)
    axis_3d.set_zlabel("z (µm)", labelpad=6)
    axis_3d.set_title("Continuous φ=0 surfaces · front-half cutaway", pad=16)
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
        interpolation="lanczos",
        resample=True,
        aspect="equal",
    )
    axis_section.set_xlim(coarse.x_min, coarse.x_max)
    axis_section.set_ylim(-0.50, 0.30)
    axis_section.set_xlabel("x (µm)")
    axis_section.set_ylabel("z (µm)")
    axis_section.set_title(f"AA section · {interface_spacing_nm:g} nm interface sampling")
    axis_section.grid(alpha=0.12, linewidth=0.5)

    axis_top = figure.add_subplot(layout[1, 1])
    axis_top.imshow(
        top_rgb,
        origin="lower",
        extent=(x[0], x[-1], y[0], y[-1]),
        interpolation="lanczos",
        resample=True,
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
    figure.text(
        0.5,
        0.016,
        (
            f"AMR level 0: {coarse.dx * 1000:.1f} nm ({coarse_cells:,} nodes) · "
            f"level 2: {fine_spacing * 1000:.2f} nm ({fine_cells:,} local nodes) · "
            f"display sampling: {display_spacing * 1000:.2f} nm · {triangle_count:,} triangles · "
            f"continuous Level Set reconstruction with {smoothing_iterations}× non-shrinking mesh smoothing"
        ),
        ha="center",
        color="#46515B",
        fontsize=9.4,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=260, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    print(f"saved {output}")
    print(f"display spacing: {display_spacing * 1000:.3f} nm; triangles: {triangle_count:,}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adaptive-dir", type=Path, required=True)
    parser.add_argument("--array", type=int, default=2)
    parser.add_argument("--pitch", type=float, default=0.45)
    parser.add_argument("--factor", type=int, default=4)
    parser.add_argument("--interface-spacing-nm", type=float, default=1.0)
    parser.add_argument("--mesh-smoothing", type=int, default=8, choices=range(0, 17))
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
    render(
        adaptive,
        args.output,
        interface_spacing_nm=args.interface_spacing_nm,
        smoothing_iterations=args.mesh_smoothing,
    )


if __name__ == "__main__":
    main()
