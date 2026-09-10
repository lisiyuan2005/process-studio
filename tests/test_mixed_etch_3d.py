import math

import numpy as np
import pytest

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.masks import rectangle
from process_studio.kernel.metrics import column_surface_height
from process_studio.kernel.processes import mixed_trench_etch


@pytest.fixture(scope="module")
def etch_modes() -> tuple[UniformGrid3D, np.ndarray, dict[str, np.ndarray]]:
    grid = UniformGrid3D(-0.6, 0.6, -0.6, 0.6, -0.6, 0.2, 61, 61, 41)
    xx, yy = grid.mesh_xy
    mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.4, 0.4))
    initial = grid.substrate()
    rates = {
        "dry": (0.10, 0.00),
        "mixed": (0.08, 0.02),
        "wet": (0.00, 0.10),
    }
    results = {
        name: mixed_trench_etch(
            initial,
            mask,
            grid.z,
            grid.dx,
            0.2,
            directional_rate=directional,
            isotropic_rate=isotropic,
        )[0]
        for name, (directional, isotropic) in rates.items()
    }
    return grid, mask, results


def sampled_void_width(
    phi: np.ndarray,
    grid: UniformGrid3D,
    *,
    depth: float,
) -> float:
    z_index = int(np.argmin(np.abs(grid.z + depth)))
    row = phi[z_index, grid.ny // 2, :]
    exposed_x = grid.x[row > 0.0]
    if exposed_x.size < 2:
        return 0.0
    return float(exposed_x[-1] - exposed_x[0])


@pytest.mark.parametrize("mode", ["dry", "mixed", "wet"])
def test_all_modes_reach_target_center_depth(
    etch_modes: tuple[UniformGrid3D, np.ndarray, dict[str, np.ndarray]],
    mode: str,
) -> None:
    grid, _, results = etch_modes
    height = column_surface_height(
        results[mode], grid.z, grid.ny // 2, grid.nx // 2
    )
    assert height == pytest.approx(-0.2, abs=0.25 * grid.dz)


def test_undercut_increases_with_isotropic_fraction(
    etch_modes: tuple[UniformGrid3D, np.ndarray, dict[str, np.ndarray]],
) -> None:
    grid, _, results = etch_modes
    widths = {
        mode: sampled_void_width(phi, grid, depth=0.04)
        for mode, phi in results.items()
    }

    assert widths["dry"] == pytest.approx(0.4, abs=grid.dx)
    assert widths["mixed"] > widths["dry"]
    assert widths["wet"] > widths["mixed"]


def test_protected_top_surface_remains_unchanged(
    etch_modes: tuple[UniformGrid3D, np.ndarray, dict[str, np.ndarray]],
) -> None:
    grid, _, results = etch_modes
    outside_x = int(np.argmin(np.abs(grid.x - 0.55)))
    for phi in results.values():
        height = column_surface_height(phi, grid.z, grid.ny // 2, outside_x)
        assert height == pytest.approx(0.0, abs=0.25 * grid.dz)


def test_accessibility_does_not_create_disconnected_bulk_voids(
    etch_modes: tuple[UniformGrid3D, np.ndarray, dict[str, np.ndarray]],
) -> None:
    grid, _, results = etch_modes
    deep_index = int(np.argmin(np.abs(grid.z + 0.3)))
    for phi in results.values():
        assert np.all(phi[deep_index] <= 0.0)


def test_wet_etch_mid_depth_width_converges_with_grid_refinement(
    etch_modes: tuple[UniformGrid3D, np.ndarray, dict[str, np.ndarray]],
) -> None:
    fine_grid, _, fine_results = etch_modes
    coarse_grid = UniformGrid3D(
        -0.6, 0.6, -0.6, 0.6, -0.6, 0.2, 31, 31, 21
    )
    xx, yy = coarse_grid.mesh_xy
    coarse_mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.4, 0.4))
    coarse, _ = mixed_trench_etch(
        coarse_grid.substrate(),
        coarse_mask,
        coarse_grid.z,
        coarse_grid.dx,
        0.2,
        directional_rate=0.0,
        isotropic_rate=0.1,
    )

    expected_width = 0.4 + 2.0 * math.sqrt(0.2**2 - 0.1**2)
    coarse_error = abs(sampled_void_width(coarse, coarse_grid, depth=0.1) - expected_width)
    fine_error = abs(
        sampled_void_width(fine_results["wet"], fine_grid, depth=0.1)
        - expected_width
    )

    assert fine_error < coarse_error
