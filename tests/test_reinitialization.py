import numpy as np

from process_studio.kernel.grid import UniformGrid2D
from process_studio.kernel.level_set import (
    reinitialize_signed_distance,
    reinitialize_signed_distance_subcell,
)
from process_studio.kernel.metrics import positive_x_zero_crossing


def test_reinitialization_preserves_sign_and_interface_within_one_cell() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 201, 201)
    phi = grid.circle((0.0, 0.0), 0.37)
    distorted = 3.0 * phi + 0.15 * phi**3

    before = positive_x_zero_crossing(distorted, grid.x, grid.y)
    rebuilt = reinitialize_signed_distance(distorted, grid.dx)
    after = positive_x_zero_crossing(rebuilt, grid.x, grid.y)

    assert np.array_equal(rebuilt <= 0, distorted <= 0)
    assert abs(after - before) <= grid.dx


def test_reinitialized_gradient_is_near_one_around_interface() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 301, 301)
    phi = grid.circle((0.0, 0.0), 0.4)
    rebuilt = reinitialize_signed_distance(2.5 * phi, grid.dx)
    grad_y, grad_x = np.gradient(rebuilt, grid.dy, grid.dx)
    grad_norm = np.hypot(grad_x, grad_y)
    narrow_band = np.abs(rebuilt) <= 4 * grid.dx

    assert np.median(np.abs(grad_norm[narrow_band] - 1.0)) < 0.15


def test_subcell_reinitialization_preserves_circle_interface() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 151, 151)
    phi = grid.circle((0.0, 0.0), 0.373)
    distorted = 2.7 * phi + 0.2 * phi**3

    before = positive_x_zero_crossing(distorted, grid.x, grid.y)
    rebuilt = reinitialize_signed_distance_subcell(distorted, grid.dx)
    after = positive_x_zero_crossing(rebuilt, grid.x, grid.y)

    assert abs(after - before) < 0.1 * grid.dx
