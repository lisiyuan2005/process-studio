"""Interface measurements used by numerical validation tests."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import binary_propagation, generate_binary_structure


def positive_x_zero_crossing(phi: np.ndarray, x: np.ndarray, y: np.ndarray) -> float:
    """Locate the zero contour on the positive x axis by linear interpolation."""
    y_index = int(np.argmin(np.abs(y)))
    row = phi[y_index]
    positive = np.flatnonzero(x >= 0)

    for left, right in zip(positive[:-1], positive[1:], strict=True):
        if row[left] <= 0 < row[right]:
            weight = -row[left] / (row[right] - row[left])
            return float(x[left] + weight * (x[right] - x[left]))

    raise ValueError("no positive-x zero crossing was found")


def diagonal_radius_zero_crossing(phi: np.ndarray, x: np.ndarray) -> float:
    """Measure radial distance where the zero contour crosses y=x, x>=0."""
    if phi.shape[0] != phi.shape[1] or phi.shape[0] != x.size:
        raise ValueError("diagonal measurement requires a square grid")

    diagonal = np.diag(phi)
    positive = np.flatnonzero(x >= 0)
    for left, right in zip(positive[:-1], positive[1:], strict=True):
        if diagonal[left] <= 0 < diagonal[right]:
            weight = -diagonal[left] / (diagonal[right] - diagonal[left])
            coordinate = x[left] + weight * (x[right] - x[left])
            return float(np.sqrt(2.0) * coordinate)

    raise ValueError("no positive diagonal zero crossing was found")


def plane_zero_crossing(phi: np.ndarray, x: np.ndarray) -> float:
    """Locate a vertical plane interface using the center row."""
    row = phi[phi.shape[0] // 2]
    crossings = np.flatnonzero(row[:-1] * row[1:] <= 0)
    if crossings.size == 0:
        raise ValueError("no plane zero crossing was found")
    left = int(crossings[0])
    right = left + 1
    if row[right] == row[left]:
        return float(x[left])
    weight = -row[left] / (row[right] - row[left])
    return float(x[left] + weight * (x[right] - x[left]))


def column_surface_height(phi: np.ndarray, z: np.ndarray, y_index: int, x_index: int) -> float:
    """Measure the highest negative-to-positive interface in one z column."""
    column = phi[:, y_index, x_index]
    crossings = np.flatnonzero((column[:-1] <= 0) & (column[1:] > 0))
    if crossings.size == 0:
        raise ValueError("no material surface was found in the selected column")
    lower = int(crossings[-1])
    upper = lower + 1
    weight = -column[lower] / (column[upper] - column[lower])
    return float(z[lower] + weight * (z[upper] - z[lower]))


def surface_height_map(phi: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Return the highest material surface for every y/x location."""
    if phi.ndim != 3 or phi.shape[0] != z.size:
        raise ValueError("phi must have shape (len(z), ny, nx)")
    heights = np.empty(phi.shape[1:], dtype=float)
    for y_index in range(phi.shape[1]):
        for x_index in range(phi.shape[2]):
            heights[y_index, x_index] = column_surface_height(
                phi, z, y_index, x_index
            )
    return heights


def centered_positive_width(
    values: np.ndarray,
    coordinates: np.ndarray,
    *,
    center: float = 0.0,
) -> float:
    """Measure the positive interval containing ``center`` with subcell crossings."""
    if values.ndim != 1 or coordinates.ndim != 1 or values.size != coordinates.size:
        raise ValueError("values and coordinates must be matching 1D arrays")
    center_index = int(np.argmin(np.abs(coordinates - center)))
    if values[center_index] <= 0:
        return 0.0

    left_inside = center_index
    while left_inside > 0 and values[left_inside - 1] > 0:
        left_inside -= 1
    right_inside = center_index
    while right_inside < values.size - 1 and values[right_inside + 1] > 0:
        right_inside += 1

    if left_inside == 0:
        left_crossing = float(coordinates[0])
    else:
        left_outside = left_inside - 1
        fraction = -values[left_outside] / (
            values[left_inside] - values[left_outside]
        )
        left_crossing = float(
            coordinates[left_outside]
            + fraction * (coordinates[left_inside] - coordinates[left_outside])
        )

    if right_inside == values.size - 1:
        right_crossing = float(coordinates[-1])
    else:
        right_outside = right_inside + 1
        fraction = -values[right_inside] / (
            values[right_outside] - values[right_inside]
        )
        right_crossing = float(
            coordinates[right_inside]
            + fraction * (coordinates[right_outside] - coordinates[right_inside])
        )

    return right_crossing - left_crossing


def trench_opening_width(
    phi: np.ndarray,
    x: np.ndarray,
    z: np.ndarray,
    *,
    depth: float,
    y_index: int | None = None,
    x_center: float = 0.0,
) -> float:
    """Measure the center trench's void width at a requested depth."""
    if phi.ndim != 3 or phi.shape[0] != z.size or phi.shape[2] != x.size:
        raise ValueError("phi must have shape (len(z), ny, len(x))")
    if depth < 0:
        raise ValueError("depth cannot be negative")
    if y_index is None:
        y_index = phi.shape[1] // 2
    z_index = int(np.argmin(np.abs(z + depth)))
    return centered_positive_width(
        phi[z_index, y_index, :],
        x,
        center=x_center,
    )


def sealed_void_mask(
    phi: np.ndarray,
    z: np.ndarray,
    *,
    surface_z: float = 0.0,
) -> np.ndarray:
    """Return subsurface void cells that are disconnected from the top boundary."""
    if phi.ndim != 3 or phi.shape[0] != z.size:
        raise ValueError("phi must have shape (len(z), ny, nx)")
    void = phi > 0.0
    inlet = np.zeros_like(void)
    inlet[-1] = void[-1]
    accessible = binary_propagation(
        inlet,
        structure=generate_binary_structure(3, 1),
        mask=void,
    )
    subsurface = np.broadcast_to(z[:, None, None] < surface_z, phi.shape)
    return void & ~accessible & subsurface
