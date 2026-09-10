import numpy as np
import pytest

from process_studio.kernel.grid import UniformGrid2D
from process_studio.kernel.masks import (
    circle,
    intersect,
    invert,
    merge,
    rectangle,
    signed_distance,
    subtract,
)


def test_boolean_mask_operations() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 101, 101)
    xx, yy = grid.mesh
    base = rectangle(xx, yy, center=(0.0, 0.0), size=(1.2, 0.8))
    hole = circle(xx, yy, center=(0.0, 0.0), radius=0.2)
    detached = circle(xx, yy, center=(0.8, 0.8), radius=0.1)

    cut = subtract(base, hole)
    combined = merge(cut, detached)
    overlap = intersect(base, hole)

    center = (grid.ny // 2, grid.nx // 2)
    assert not cut[center]
    assert combined[np.argmin(np.abs(grid.y - 0.8)), np.argmin(np.abs(grid.x - 0.8))]
    assert np.array_equal(overlap, hole)
    assert np.array_equal(invert(invert(base)), base)


def test_rectangle_has_expected_grid_extent() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 101, 101)
    xx, yy = grid.mesh
    mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.8, 0.4))
    exposed_x = grid.x[np.any(mask, axis=0)]
    exposed_y = grid.y[np.any(mask, axis=1)]

    assert exposed_x[0] == pytest.approx(-0.4)
    assert exposed_x[-1] == pytest.approx(0.4)
    assert exposed_y[0] == pytest.approx(-0.2)
    assert exposed_y[-1] == pytest.approx(0.2)


def test_mask_signed_distance_has_expected_sign() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 101, 101)
    xx, yy = grid.mesh
    mask = circle(xx, yy, center=(0.0, 0.0), radius=0.3)
    phi = signed_distance(mask, grid.dx)

    assert phi[grid.ny // 2, grid.nx // 2] < 0.0
    assert phi[0, 0] > 0.0


def test_full_exposure_signed_distance_is_inside_everywhere() -> None:
    phi = signed_distance(np.ones((7, 9), dtype=bool), 0.1)
    assert np.all(phi < 0.0)
