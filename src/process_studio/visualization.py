"""View-model helpers shared by the desktop UI and validation examples."""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import numpy as np

if TYPE_CHECKING:
    # Type-only: MaterialState (and the scipy it is built on) is the
    # level-set kernel's geometry, and a slab-only build never has scipy
    # installed to import the real class.
    from .kernel.material_state import MaterialState


def top_view_labels(state: MaterialState, hidden: Sequence[str] = ()) -> np.ndarray:
    """The topmost material per column, as an index into ``state.priority``.

    A hidden material is not there to be seen through: the column reports
    whatever is under it, which is the point of hiding one -- looking into
    the hole a resist or a liner is filling.
    """
    labels, _ = state.labels()
    top = np.full((state.grid.ny, state.grid.nx), -1, dtype=np.int16)
    skip = _hidden_labels(state, hidden)
    for z_index in range(state.grid.nz):
        layer = labels[z_index]
        occupied = layer >= 0
        if skip.size:
            occupied &= ~np.isin(layer, skip)
        top[occupied] = layer[occupied]
    return top


def _hidden_labels(state: MaterialState, hidden: Sequence[str]) -> np.ndarray:
    """The label indices of the materials to see through, if any."""
    if not hidden:
        return np.empty(0, dtype=np.int16)
    names = set(hidden)
    return np.array(
        [index for index, name in enumerate(state.priority) if name in names],
        dtype=np.int16,
    )


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


def surface_heights(state: MaterialState, hidden: Sequence[str] = ()) -> np.ndarray:
    """The height of the topmost occupied node per column; NaN where nothing is.

    A hidden material is seen through, so the height is that of the first
    thing under it.
    """
    labels, _ = state.labels()
    z = state.grid.z
    heights = np.full((state.grid.ny, state.grid.nx), np.nan, dtype=float)
    skip = _hidden_labels(state, hidden)
    for z_index in range(state.grid.nz):
        layer = labels[z_index]
        occupied = layer >= 0
        if skip.size:
            occupied &= ~np.isin(layer, skip)
        heights[occupied] = z[z_index]
    return heights
