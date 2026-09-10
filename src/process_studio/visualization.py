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
