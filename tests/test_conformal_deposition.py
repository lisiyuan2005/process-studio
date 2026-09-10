import numpy as np
import pytest

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.masks import full_exposure, rectangle
from process_studio.kernel.material_state import MaterialState
from process_studio.kernel.multimaterial import deposit_material, selective_etch
from process_studio.kernel.metrics import (
    column_surface_height,
    sealed_void_mask,
    trench_opening_width,
)
from process_studio.kernel.processes import (
    conformal_deposition,
    directional_trench_etch,
    mixed_trench_etch,
)


@pytest.fixture(scope="module")
def deposition_geometry() -> tuple[UniformGrid3D, np.ndarray, np.ndarray]:
    grid = UniformGrid3D(-0.6, 0.6, -0.6, 0.6, -0.6, 0.4, 61, 61, 51)
    xx, yy = grid.mesh_xy
    mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.4, 0.4))
    trench, _ = directional_trench_etch(
        grid.substrate(), mask, grid.dx, target_depth=0.3
    )
    return grid, mask, trench


def test_thin_film_has_equal_top_bottom_and_sidewall_thickness(
    deposition_geometry: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, _, trench = deposition_geometry
    thickness = 0.04
    combined, _, steps = conformal_deposition(trench, grid.dx, thickness)

    outside_x = int(np.argmin(np.abs(grid.x - 0.55)))
    top = column_surface_height(combined, grid.z, grid.ny // 2, outside_x)
    bottom = column_surface_height(
        combined, grid.z, grid.ny // 2, grid.nx // 2
    )
    width_before = trench_opening_width(trench, grid.x, grid.z, depth=0.1)
    width_after = trench_opening_width(combined, grid.x, grid.z, depth=0.1)

    assert steps == 1
    assert top == pytest.approx(thickness, abs=0.1 * grid.dz)
    assert bottom == pytest.approx(-0.3 + thickness, abs=0.1 * grid.dz)
    assert width_before - width_after == pytest.approx(
        2.0 * thickness, abs=0.1 * grid.dx
    )


def test_deposited_film_is_separate_from_original_material(
    deposition_geometry: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, _, trench = deposition_geometry
    combined, film, _ = conformal_deposition(trench, grid.dx, 0.04)
    outside_x = int(np.argmin(np.abs(grid.x - 0.55)))
    film_height = int(np.argmin(np.abs(grid.z - 0.02)))
    deep_substrate = int(np.argmin(np.abs(grid.z + 0.4)))

    assert trench[film_height, grid.ny // 2, outside_x] > 0.0
    assert combined[film_height, grid.ny // 2, outside_x] < 0.0
    assert film[film_height, grid.ny // 2, outside_x] < 0.0
    assert film[deep_substrate, grid.ny // 2, outside_x] > 0.0


def test_zero_thickness_preserves_geometry(
    deposition_geometry: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, _, trench = deposition_geometry
    combined, film, steps = conformal_deposition(trench, grid.dx, 0.0)

    assert steps == 0
    assert np.array_equal(combined, trench)
    assert not np.any(film < 0.0)


def test_conformal_film_can_seal_a_reentrant_wet_etched_cavity(
    deposition_geometry: tuple[UniformGrid3D, np.ndarray, np.ndarray],
) -> None:
    grid, _, _ = deposition_geometry
    xx, yy = grid.mesh_xy
    narrow_mask = rectangle(xx, yy, center=(0.0, 0.0), size=(0.2, 0.2))
    wet_trench, _ = mixed_trench_etch(
        grid.substrate(),
        narrow_mask,
        grid.z,
        grid.dx,
        target_depth=0.3,
        directional_rate=0.0,
        isotropic_rate=0.1,
    )

    still_open, _, _ = conformal_deposition(wet_trench, grid.dx, 0.10)
    pinched_off, _, _ = conformal_deposition(wet_trench, grid.dx, 0.12)

    assert not np.any(sealed_void_mask(still_open, grid.z))
    assert np.any(sealed_void_mask(pinched_off, grid.z))


def test_a_film_over_a_resist_masked_etch_keeps_its_nominal_thickness() -> None:
    """A patterned mask must not leave a membrane for the film to grow on.

    When the wafer surface sampled exactly on a node, subtracting the etched
    void could not lift those nodes out of the material. They survived as a
    zero-thickness sheet over the opening, the distance rebuild treated it as a
    real surface, and the film's outer boundary rose near every rim.
    """
    grid = UniformGrid3D(-0.4, 0.4, -0.4, 0.4, -0.5, 0.3, 41, 41, 41)
    _, yy, xx = np.meshgrid(grid.z, grid.y, grid.x, indexing="ij")
    zz = grid.substrate()
    state = MaterialState(grid)
    state.add_material("Si", zz)
    state.add_material(
        "Photoresist",
        np.maximum(np.maximum(-zz, zz - 0.1), -(np.hypot(xx, yy) - 0.13)),
    )

    etched = selective_etch(
        state,
        full_exposure((grid.ny, grid.nx)),
        0.25,
        {"Si": 0.1, "Photoresist": 0.0},
        directional_fraction=0.92,
        surface_z=0.1,
    )
    # The membrane, stated directly: a node sitting exactly on the interface
    # with open space underneath is not a body, and nothing may deposit on it.
    union = np.minimum.reduce([etched.fields[name] for name in etched.priority])
    hanging = (union[1:] == 0.0) & (union[:-1] > 0.0)
    assert not hanging.any(), "the etch left interface nodes with nothing under them"

    stripped = selective_etch(
        etched,
        full_exposure((grid.ny, grid.nx)),
        0.2,
        {"Photoresist": 0.2, "Si": 0.0},
        directional_fraction=1.0,
        surface_z=0.1,
    )
    assert not np.any(stripped.fields["Photoresist"] <= 0.0), "the strip must clear the resist"

    thickness = 0.05
    grown = deposit_material(stripped, "Al2O3", thickness)
    film = np.minimum(grown.fields["Si"], grown.fields["Al2O3"])

    iy = int(np.argmin(np.abs(grid.y)))
    tops = []
    for column in range(grid.nx):
        values = film[:, iy, column]
        inside = np.flatnonzero(values <= 0.0)
        if not inside.size or inside.max() + 1 >= len(values):
            continue
        k = inside.max()
        tops.append(
            grid.z[k] + grid.dz * (-values[k]) / (values[k + 1] - values[k])
        )
    # Offsetting a flat top by the film thickness cannot exceed that thickness,
    # anywhere. A surface pinned to the node plane used to overshoot by 0.77 of
    # a cell at the rim.
    assert max(tops) == pytest.approx(thickness, abs=1e-6)
