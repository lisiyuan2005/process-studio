"""View-model helpers shared by the desktop UI and validation examples."""

from __future__ import annotations

import numpy as np

from .kernel.material_state import MaterialState


def top_view_labels(state: MaterialState) -> np.ndarray:
    labels, _ = state.labels()
    top = np.full((state.grid.ny, state.grid.nx), -1, dtype=np.int16)
    for z_index in range(state.grid.nz):
        occupied = labels[z_index] >= 0
        top[occupied] = labels[z_index][occupied]
    return top


def line_section_labels(
    state: MaterialState,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    samples: int = 201,
) -> tuple[np.ndarray, np.ndarray]:
    if samples < 2:
        raise ValueError("samples must be at least 2")
    x = np.linspace(start[0], end[0], samples)
    y = np.linspace(start[1], end[1], samples)
    x_index = np.clip(
        np.rint((x - state.grid.x_min) / state.grid.dx).astype(int),
        0,
        state.grid.nx - 1,
    )
    y_index = np.clip(
        np.rint((y - state.grid.y_min) / state.grid.dy).astype(int),
        0,
        state.grid.ny - 1,
    )
    labels, _ = state.labels()
    section = labels[:, y_index, x_index]
    distance = np.linspace(
        0.0,
        float(np.hypot(end[0] - start[0], end[1] - start[1])),
        samples,
    )
    return distance, section


def downsampled_material_voxels(
    state: MaterialState,
    *,
    maximum_axis: int = 36,
) -> tuple[dict[str, np.ndarray], int]:
    stride = max(1, int(np.ceil(max(state.shape) / maximum_axis)))
    labels, names = state.labels()
    sampled = labels[::stride, ::stride, ::stride]
    return {
        name: np.transpose(sampled == index, (2, 1, 0))
        for index, name in enumerate(names)
    }, stride


# A sequential palette (viridis anchors) for surfaces coloured by height:
# low is dark blue, high is yellow, so a taller feature reads as brighter.
_HEIGHT_ANCHORS = ((0.27, 0.00, 0.33), (0.23, 0.32, 0.55), (0.13, 0.57, 0.55), (0.37, 0.79, 0.38), (0.99, 0.91, 0.15))


def height_color(fraction: float) -> str:
    """The palette colour at ``fraction`` of the way from the lowest to the highest surface."""
    f = min(1.0, max(0.0, float(fraction))) * (len(_HEIGHT_ANCHORS) - 1)
    index = min(int(f), len(_HEIGHT_ANCHORS) - 2)
    weight = f - index
    a, b = _HEIGHT_ANCHORS[index], _HEIGHT_ANCHORS[index + 1]
    rgb = [round(255 * (a[i] + (b[i] - a[i]) * weight)) for i in range(3)]
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def height_levels(heights: list[float]) -> list[dict]:
    """Distinct surface heights, ascending, each with its palette colour.

    A single level gets the top of the palette; many levels are spread
    evenly over it by rank, so neighbouring steps stay distinguishable
    however uneven their spacing.
    """
    distinct = sorted({round(float(z), 9) for z in heights})
    if not distinct:
        return []
    count = len(distinct)
    return [
        {"z": z, "color": height_color(1.0 if count == 1 else index / (count - 1))}
        for index, z in enumerate(distinct)
    ]


def surface_heights(state: MaterialState) -> np.ndarray:
    """The height of the topmost occupied node per column; NaN where nothing is."""
    labels, _ = state.labels()
    z = state.grid.z
    heights = np.full((state.grid.ny, state.grid.nx), np.nan, dtype=float)
    for z_index in range(state.grid.nz):
        occupied = labels[z_index] >= 0
        heights[occupied] = z[z_index]
    return heights
