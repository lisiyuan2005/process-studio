import numpy as np
import pytest

from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.kernel.multimaterial import (
    cmp_planarize,
    deposit_material,
    patterned_deposit,
    selective_etch,
)
from process_studio.kernel.masks import full_exposure


@pytest.fixture
def silicon_state() -> MaterialState:
    grid = UniformGrid3D(-0.4, 0.4, -0.4, 0.4, -0.4, 0.2, 41, 41, 31)
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())
    return state


def test_deposition_creates_separate_non_overlapping_material(
    silicon_state: MaterialState,
) -> None:
    deposited = deposit_material(silicon_state, "SiO2", 0.04)
    labels, names = deposited.labels()
    z_film = int(np.argmin(np.abs(deposited.grid.z - 0.02)))
    z_bulk = int(np.argmin(np.abs(deposited.grid.z + 0.2)))

    assert names == ["Si", "SiO2"]
    assert labels[z_film, 20, 20] == names.index("SiO2")
    assert labels[z_bulk, 20, 20] == names.index("Si")
    assert not np.any(
        (deposited.fields["Si"] < 0.0) & (deposited.fields["SiO2"] < 0.0)
    )


def test_selective_etch_removes_film_but_preserves_stop_material(
    silicon_state: MaterialState,
) -> None:
    deposited = deposit_material(silicon_state, "SiO2", 0.04)
    etched = selective_etch(
        deposited,
        full_exposure((deposited.grid.ny, deposited.grid.nx)),
        target_depth=0.06,
        material_rates={"SiO2": 0.1, "Si": 0.0},
        surface_z=0.04,
    )
    center = (deposited.grid.ny // 2, deposited.grid.nx // 2)
    surface_index = int(np.argmin(np.abs(deposited.grid.z)))
    bulk_index = int(np.argmin(np.abs(deposited.grid.z + 0.2)))

    assert etched.fields["SiO2"][surface_index, *center] > 0.0
    assert etched.fields["Si"][bulk_index, *center] <= 0.0


def test_cmp_clips_selected_material_to_target_plane(
    silicon_state: MaterialState,
) -> None:
    deposited = deposit_material(silicon_state, "SiO2", 0.08)
    planarized, plane = cmp_planarize(
        deposited, target_z=0.03, materials=["SiO2"]
    )
    above = deposited.grid.z > 0.03 + deposited.grid.dz * 0.25

    assert plane == pytest.approx(0.03)
    assert np.all(planarized.fields["SiO2"][above] > 0.0)


def test_patterned_deposit_creates_only_masked_vertical_structure(
    silicon_state: MaterialState,
) -> None:
    grid = silicon_state.grid
    xx, yy = grid.mesh_xy
    mask = (xx**2 + yy**2) <= 0.10**2
    deposited = patterned_deposit(
        silicon_state,
        "TiN",
        mask,
        thickness=0.10,
        base_z=0.0,
    )
    feature_z = int(np.argmin(np.abs(grid.z - 0.06)))
    center = (grid.ny // 2, grid.nx // 2)
    outside_x = int(np.argmin(np.abs(grid.x - 0.3)))

    assert deposited.fields["TiN"][feature_z, *center] <= 0.0
    assert deposited.fields["TiN"][feature_z, grid.ny // 2, outside_x] > 0.0


def test_material_state_round_trip(tmp_path, silicon_state: MaterialState) -> None:
    deposited = deposit_material(silicon_state, "SiO2", 0.04)
    path = tmp_path / "state.npz"
    deposited.save(path)
    restored = MaterialState.load(path)

    assert restored.priority == deposited.priority
    assert restored.grid == deposited.grid
    for name in deposited.priority:
        assert np.array_equal(restored.fields[name], deposited.fields[name])


def _resist_over_silicon(hole_radius: float) -> MaterialState:
    """Silicon with a 0.1 µm resist layer on top, opened by one round hole."""
    grid = UniformGrid3D(-0.4, 0.4, -0.4, 0.4, -0.5, 0.3, 41, 41, 41)
    zz, yy, xx = np.meshgrid(grid.z, grid.y, grid.x, indexing="ij")
    state = MaterialState(grid)
    state.add_material("Si", zz)
    slab = np.maximum(-zz, zz - 0.1)
    hole = np.hypot(xx, yy) - hole_radius
    state.add_material("Photoresist", np.maximum(slab, -hole))
    return state


def _top_of(state: MaterialState, material: str, x: float) -> float:
    grid = state.grid
    iy = int(np.argmin(np.abs(grid.y)))
    ix = int(np.argmin(np.abs(grid.x - x)))
    inside = np.flatnonzero(state.fields[material][:, iy, ix] <= 0.0)
    return float(grid.z[inside.max()]) if inside.size else float("nan")


def test_a_material_the_etch_cannot_consume_shields_what_is_under_it() -> None:
    state = _resist_over_silicon(0.12)
    etched = selective_etch(
        state,
        full_exposure((state.grid.ny, state.grid.nx)),
        0.25,
        {"Si": 0.1, "Photoresist": 0.0},
        directional_fraction=0.92,
        surface_z=0.1,
    )
    # Blanket exposure: the resist is the only thing patterning this etch.
    assert _top_of(etched, "Si", 0.30) == pytest.approx(0.0)
    assert _top_of(etched, "Si", 0.20) == pytest.approx(0.0)
    assert _top_of(etched, "Si", 0.0) < -0.1
    # The resist itself is untouched, having no rate of its own.
    assert np.array_equal(etched.fields["Photoresist"], state.fields["Photoresist"])


def test_a_wider_resist_opening_etches_a_wider_hole() -> None:
    narrow = selective_etch(
        _resist_over_silicon(0.08),
        full_exposure((41, 41)),
        0.25,
        {"Si": 0.1, "Photoresist": 0.0},
        directional_fraction=0.92,
        surface_z=0.1,
    )
    wide = selective_etch(
        _resist_over_silicon(0.20),
        full_exposure((41, 41)),
        0.25,
        {"Si": 0.1, "Photoresist": 0.0},
        directional_fraction=0.92,
        surface_z=0.1,
    )
    assert _top_of(narrow, "Si", 0.15) == pytest.approx(0.0)
    assert _top_of(wide, "Si", 0.15) < -0.05


def test_a_buried_stop_layer_blocks_the_front_instead_of_only_surviving() -> None:
    grid = UniformGrid3D(-0.4, 0.4, -0.4, 0.4, -0.5, 0.2, 41, 41, 36)
    zz, _, xx = np.meshgrid(grid.z, grid.y, grid.x, indexing="ij")
    state = MaterialState(grid)
    state.add_material("Si", zz)
    # A buried plate covering the left half, 0.1 µm below the surface.
    plate = np.maximum(np.maximum(-(zz + 0.15), zz + 0.1), xx)
    state.add_material("SiN", plate)

    etched = selective_etch(
        state,
        full_exposure((grid.ny, grid.nx)),
        0.3,
        {"Si": 0.1, "SiN": 0.0},
        directional_fraction=1.0,
    )
    # Over the plate the front stops at it, so the silicon underneath survives.
    assert _top_of(etched, "Si", -0.2) == pytest.approx(-0.15, abs=grid.dz)
    # Beside the plate nothing blocks the etch and it runs the full depth.
    assert _top_of(etched, "Si", 0.2) < -0.25


def test_an_etch_without_protected_material_is_unchanged(silicon_state: MaterialState) -> None:
    grid = silicon_state.grid
    etched = selective_etch(
        silicon_state, full_exposure((grid.ny, grid.nx)), 0.1, {"Si": 0.1},
        directional_fraction=0.9,
    )
    assert _top_of(etched, "Si", 0.0) == pytest.approx(-0.1, abs=grid.dx)
