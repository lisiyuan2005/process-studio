import numpy as np

from process_studio.kernel.adaptive import (
    AdaptiveMaterialState,
    AdaptivePatch,
    tiled_refinement_boxes,
)
from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState


def make_state(grid: UniformGrid3D) -> MaterialState:
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())
    return state


def test_tiled_refinement_uses_true_fine_spacing() -> None:
    coarse_grid = UniformGrid3D(-0.5, 0.5, -0.5, 0.5, -0.5, 0.5, 41, 41, 41)
    boxes = tiled_refinement_boxes(
        coarse_grid,
        centers_x=[-0.125, 0.125],
        centers_y=[-0.125, 0.125],
        tile_size=0.25,
        z_min=-0.25,
        z_max=0.25,
        factor=4,
    )
    assert len(boxes) == 4
    assert np.isclose(boxes[0].make_grid(coarse_grid).dx, 0.00625)


def test_fine_patch_overrides_coarse_section_without_unassigned_seams() -> None:
    coarse_grid = UniformGrid3D(-0.5, 0.5, -0.5, 0.5, -0.5, 0.5, 41, 41, 41)
    coarse = make_state(coarse_grid)
    box = tiled_refinement_boxes(
        coarse_grid,
        centers_x=[0.0],
        centers_y=[0.0],
        tile_size=0.5,
        z_min=-0.25,
        z_max=0.25,
        factor=4,
    )[0]
    fine = make_state(box.make_grid(coarse_grid))
    adaptive = AdaptiveMaterialState(coarse, [AdaptivePatch(box, fine)])
    x = np.arange(-0.5, 0.5001, fine.grid.dx)
    z = np.arange(-0.25, 0.2501, fine.grid.dz)
    labels, names, level = adaptive.section_labels(0.0, x, z)
    refined_x = (x >= box.x_min) & (x <= box.x_max)

    assert names == ["Si"]
    assert np.all(labels[z < -fine.grid.dz / 2.0] == 0)
    assert np.all(labels[np.ix_(z > fine.grid.dz / 2.0, refined_x)] == -1)
    assert np.all(level[:, refined_x] == 2)


def test_adaptive_state_round_trip(tmp_path) -> None:
    coarse_grid = UniformGrid3D(-0.5, 0.5, -0.5, 0.5, -0.5, 0.5, 41, 41, 41)
    coarse = make_state(coarse_grid)
    box = tiled_refinement_boxes(
        coarse_grid,
        centers_x=[0.0],
        centers_y=[0.0],
        tile_size=0.5,
        z_min=-0.25,
        z_max=0.25,
        factor=4,
    )[0]
    adaptive = AdaptiveMaterialState(coarse, [AdaptivePatch(box, make_state(box.make_grid(coarse_grid)))])
    adaptive.save(tmp_path)
    restored = AdaptiveMaterialState.load(tmp_path)

    assert restored.patches[0].box == box
    assert np.isclose(restored.patches[0].state.grid.dx, coarse_grid.dx / 4.0)
    assert restored.material_names == ["Si"]
