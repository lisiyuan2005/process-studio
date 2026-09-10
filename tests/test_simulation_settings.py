import pytest

from process_studio.kernel.grid import UniformGrid3D
from process_studio.simulation_settings import estimate_grid, grid_for_target_spacing


@pytest.mark.parametrize(
    ("target_nm", "expected_shape"),
    [(25.0, (57, 57, 57)), (12.5, (113, 113, 113)), (6.25, (225, 225, 225))],
)
def test_grid_presets_preserve_bounds_and_equal_spacing(target_nm, expected_shape):
    original = UniformGrid3D(-.7, .7, -.7, .7, -.7, .7, 57, 57, 57)
    result = grid_for_target_spacing(original, target_nm)

    assert (result.nx, result.ny, result.nz) == expected_shape
    assert result.dx * 1000 == pytest.approx(target_nm)
    assert (result.x_min, result.x_max, result.z_min, result.z_max) == (-.7, .7, -.7, .7)


def test_custom_spacing_chooses_nearest_uniform_lattice():
    original = UniformGrid3D(-.8, .8, -.8, .8, -.8, .4, 41, 41, 31)
    result = grid_for_target_spacing(original, 7.0)

    assert result.dx == pytest.approx(result.dy)
    assert result.dx == pytest.approx(result.dz)
    assert result.dx * 1000 == pytest.approx(7.0, abs=.05)


def test_estimate_uses_node_count_without_allocating_fields():
    grid = UniformGrid3D(-.2, .2, -.2, .2, -.2, .2, 11, 11, 11)
    estimate = estimate_grid(grid, 5)

    assert estimate.node_count == 1331
    assert estimate.state_bytes == 1331 * 8 * 5
    assert estimate.recommended_bytes > estimate.state_bytes
