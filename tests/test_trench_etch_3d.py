import numpy as np
import pytest

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.masks import invert, rectangle
from process_studio.kernel.metrics import column_surface_height
from process_studio.kernel.processes import directional_trench_etch


@pytest.fixture
def trench_case() -> tuple[UniformGrid3D, np.ndarray, np.ndarray]:
    grid = UniformGrid3D(-1, 1, -1, 1, -0.8, 0.4, 101, 101, 61)
    xx, yy = grid.mesh_xy
    mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.8, 0.6))
    return grid, grid.substrate(), mask


def test_rectangular_mask_produces_target_depth(
    trench_case: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, initial, mask = trench_case
    target_depth = 0.36
    etched, steps = directional_trench_etch(
        initial, mask, grid.dx, target_depth, etch_rate=0.12
    )

    center_y = grid.ny // 2
    center_x = grid.nx // 2
    outside_x = int(np.argmin(np.abs(grid.x - 0.8)))
    center_height = column_surface_height(etched, grid.z, center_y, center_x)
    outside_height = column_surface_height(etched, grid.z, center_y, outside_x)

    assert steps > 0
    assert center_height == pytest.approx(-target_depth, abs=0.25 * grid.dz)
    assert outside_height == pytest.approx(0.0, abs=0.25 * grid.dz)


def test_mask_inversion_switches_protected_region(
    trench_case: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, initial, mask = trench_case
    etched, _ = directional_trench_etch(initial, invert(mask), grid.dx, 0.2)

    center = column_surface_height(etched, grid.z, grid.ny // 2, grid.nx // 2)
    outside_x = int(np.argmin(np.abs(grid.x - 0.8)))
    outside = column_surface_height(etched, grid.z, grid.ny // 2, outside_x)

    assert center == pytest.approx(0.0, abs=0.25 * grid.dz)
    assert outside == pytest.approx(-0.2, abs=0.25 * grid.dz)


def test_mid_depth_void_matches_exposure_mask(
    trench_case: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, initial, mask = trench_case
    etched, _ = directional_trench_etch(initial, mask, grid.dx, 0.3)
    mid_depth_index = int(np.argmin(np.abs(grid.z + 0.15)))
    void_at_mid_depth = etched[mid_depth_index] > 0

    assert np.array_equal(void_at_mid_depth, mask)


def test_zero_depth_preserves_structure(
    trench_case: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, initial, mask = trench_case
    etched, steps = directional_trench_etch(initial, mask, grid.dx, 0.0)

    assert steps == 0
    assert np.array_equal(etched, initial)
