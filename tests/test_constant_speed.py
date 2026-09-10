import numpy as np
import pytest

from process_studio.kernel.grid import UniformGrid2D
from process_studio.kernel.level_set import evolve_constant_normal_speed
from process_studio.kernel.metrics import (
    diagonal_radius_zero_crossing,
    plane_zero_crossing,
    positive_x_zero_crossing,
)


@pytest.mark.parametrize("speed", [0.20, -0.20])
def test_circle_moves_in_correct_direction(speed: float) -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 201, 201)
    radius_initial = 0.35
    duration = 0.40
    phi = grid.circle((0.0, 0.0), radius_initial)

    evolved, _ = evolve_constant_normal_speed(phi, grid.dx, speed, duration)
    measured = positive_x_zero_crossing(evolved, grid.x, grid.y)
    expected = radius_initial + speed * duration

    assert measured == pytest.approx(expected, abs=1.5 * grid.dx)


def test_vertical_plane_translation() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 201, 201)
    x_initial = -0.15
    speed = 0.25
    duration = 0.50
    phi = grid.vertical_plane(x_initial)

    evolved, _ = evolve_constant_normal_speed(phi, grid.dx, speed, duration)
    measured = plane_zero_crossing(evolved, grid.x)

    assert measured == pytest.approx(x_initial + speed * duration, abs=0.25 * grid.dx)


def test_circle_error_decreases_with_grid_refinement() -> None:
    radius_initial = 0.30
    speed = 0.20
    duration = 0.40
    errors = []

    for size in (101, 201, 401):
        grid = UniformGrid2D(-1, 1, -1, 1, size, size)
        phi = grid.circle((0.0, 0.0), radius_initial)
        evolved, _ = evolve_constant_normal_speed(phi, grid.dx, speed, duration)
        measured = diagonal_radius_zero_crossing(evolved, grid.x)
        errors.append(abs(measured - (radius_initial + speed * duration)))

    assert errors[1] < errors[0]
    assert errors[2] < errors[1]
    assert errors[2] < 0.75 * errors[0]


def test_zero_speed_is_identity() -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 51, 51)
    phi = grid.circle((0.0, 0.0), 0.3)
    evolved, steps = evolve_constant_normal_speed(phi, grid.dx, 0.0, 1.0)

    assert steps == 0
    assert np.array_equal(evolved, phi)
