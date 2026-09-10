"""Regression tests independent of the 1T1C dimensions and cell layout."""

import math

import gdstk
import numpy as np
import pytest

from process_studio.engine import ProcessEngine
from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.kernel.multimaterial import deposit_material
from process_studio.kernel.level_set import reinitialize_signed_distance_subcell
from process_studio.kernel.refinement import merge_regions
from process_studio.layout.distance import polygon_distance, path_distance
from process_studio.layout.gds import gds_level_set
from process_studio.layout.quick_sketch import QuickSketch, SketchShape
from process_studio.models import FlowBranch, ProjectDefinition, Recipe, ProcessType, ProcessStep


def initial(grid):
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())
    return state


def setup(shapes, *, depth=0.02, fraction=1.0):
    grid = UniformGrid3D(-0.6, 0.6, -0.6, 0.6, -0.2, 0.2, 31, 31, 11)
    recipe = Recipe("etch", ProcessType.ETCH, parameters={
        "target": depth, "material": "Si", "rate": 1,
        "surface_z": 0, "directional_fraction": fraction,
    })
    step = ProcessStep("etch", recipe.id, mask_source="quick_sketch")
    engine = ProcessEngine({recipe.id: recipe}, sketches={"default": QuickSketch(shapes=shapes)})
    return engine, grid, ProjectDefinition("general", grid.__dict__), FlowBranch("main", [step])


@pytest.mark.parametrize("shape", [
    SketchShape("circle", parameters={"center": (0.013, -0.017), "radius": 0.09}),
    SketchShape("rectangle", parameters={"center": (-0.037, 0.023), "size": (0.19, 0.13)}),
    SketchShape("polygon", parameters={"points": [(-0.1, -0.05), (0.12, -0.02), (0.03, 0.13), (-0.05, 0.04)]}),
    SketchShape("path", parameters={"points": [(-0.16, -0.06), (0.03, 0.13), (0.17, -0.02)], "width": 0.055}),
])
@pytest.mark.parametrize("fraction", [1.0, 0.65, 0.0])
def test_local_replay_matches_monolithic_for_arbitrary_masks(shape, fraction):
    engine, grid, project, branch = setup([shape], fraction=fraction)
    adaptive, plan = engine.run_refined_branch(initial, project, branch, factor=2)
    assert plan.mode == "bounded-local"
    assert len(plan.boxes) == 1
    from process_studio.kernel.adaptive import RefinementBox
    full = RefinementBox("reference", -0.6, 0.6, -0.6, 0.6, -0.2, 0.2, 2).make_grid(grid)
    reference = engine.run_branch(initial(full), project, branch)
    patch = adaptive.patches[0]
    local = patch.state.grid
    cx0, cx1, cy0, cy1 = patch.box.core_bounds
    xs = (local.x >= cx0-1e-12) & (local.x <= cx1+1e-12)
    ys = (local.y >= cy0-1e-12) & (local.y <= cy1+1e-12)
    ix = np.rint((local.x[xs]-full.x_min)/full.dx).astype(int)
    iy = np.rint((local.y[ys]-full.y_min)/full.dy).astype(int)
    np.testing.assert_allclose(
        patch.state.fields["Si"][:, ys][:, :, xs],
        reference.fields["Si"][:, iy][:, :, ix], atol=2e-12, rtol=0,
    )


def test_overlap_merge_is_transitive_and_order_independent():
    regions = [(0, 2, 0, 2), (4, 6, 0, 2), (2, 4, 0, 2), (20, 21, 0, 2)]
    assert merge_regions(regions) == merge_regions(regions[::-1]) == [(0, 6, 0, 2), (20, 21, 0, 2)]


def test_multistep_mixed_etch_accumulates_support_and_crossing_masks_merge():
    engine, grid, project, branch = setup([
        SketchShape("circle", parameters={"center": (-.12, .013), "radius": .055}),
        SketchShape("circle", parameters={"center": (.12, -.017), "radius": .055}),
    ], fraction=.6)
    first = engine.plan_refinement(grid, project, branch, factor=2)
    branch.steps.append(ProcessStep("second etch", branch.steps[0].recipe_id, mask_source="quick_sketch"))
    second = engine.plan_refinement(grid, project, branch, factor=2)
    assert len(second.boxes) == 1
    assert second.node_count >= first.node_count
    adaptive, _ = engine.run_refined_branch(initial, project, branch, factor=2)
    from process_studio.kernel.adaptive import RefinementBox
    full = RefinementBox("full", -.6, .6, -.6, .6, -.2, .2, 2).make_grid(grid)
    reference = engine.run_branch(initial(full), project, branch)
    patch = adaptive.patches[0]
    g = patch.state.grid
    x0, x1, y0, y1 = patch.box.core_bounds
    xs = (g.x >= x0-1e-12) & (g.x <= x1+1e-12)
    ys = (g.y >= y0-1e-12) & (g.y <= y1+1e-12)
    ix = np.rint((g.x[xs]-full.x_min)/full.dx).astype(int)
    iy = np.rint((g.y[ys]-full.y_min)/full.dy).astype(int)
    np.testing.assert_allclose(
        patch.state.fields["Si"][:, ys][:, :, xs],
        reference.fields["Si"][:, iy][:, :, ix], atol=2e-12, rtol=0,
    )


def test_boolean_hole_and_inversion_use_the_same_continuous_boundary():
    engine, grid, project, branch = setup([
        SketchShape("rectangle", parameters={"size": (.36, .24)}),
        SketchShape("circle", operation="subtract", parameters={"center": (.013, -.017), "radius": .061}),
    ])
    state = initial(grid)
    phi = engine.resolve_mask_level_set(state, branch.steps[0], project)
    np.testing.assert_array_equal(engine.resolve_mask(state, branch.steps[0], project), phi <= 0)
    branch.steps[0].keep = "outside"
    np.testing.assert_array_equal(engine.resolve_mask_level_set(state, branch.steps[0], project), -phi)


def test_second_order_engine_updates_local_support_and_uses_synchronized_tiles():
    engine, grid, project, branch = setup([
        SketchShape("circle", parameters={"center": (.013, -.017), "radius": .07})
    ], depth=.008, fraction=.8)
    order1 = engine.plan_refinement(grid, project, branch, factor=2)
    branch.steps[0].overrides.update({"solver_order": 2, "tile_shape": [9, 11, 13]})
    order2 = engine.plan_refinement(grid, project, branch, factor=2)
    assert order2.node_count > order1.node_count
    adaptive, _ = engine.run_refined_branch(initial, project, branch, factor=2)
    from process_studio.kernel.adaptive import RefinementBox
    full = RefinementBox("full", -.6, .6, -.6, .6, -.2, .2, 2).make_grid(grid)
    branch.steps[0].overrides["tile_shape"] = None
    reference = engine.run_branch(initial(full), project, branch)
    patch = adaptive.patches[0]
    g = patch.state.grid
    x0, x1, y0, y1 = patch.box.core_bounds
    xs = (g.x >= x0-1e-12) & (g.x <= x1+1e-12)
    ys = (g.y >= y0-1e-12) & (g.y <= y1+1e-12)
    ix = np.rint((g.x[xs]-full.x_min)/full.dx).astype(int)
    iy = np.rint((g.y[ys]-full.y_min)/full.dy).astype(int)
    np.testing.assert_allclose(
        patch.state.fields["Si"][:, ys][:, :, xs],
        reference.fields["Si"][:, iy][:, :, ix], atol=2e-12, rtol=0,
    )


def test_separated_regions_remain_separate_and_use_project_grid_origin():
    shapes = [SketchShape("circle", parameters={"center": (x, 0.013), "radius": 0.045})
              for x in [-0.403, 0.397]]
    engine, grid, project, branch = setup(shapes)
    plan = engine.plan_refinement(grid, project, branch, factor=2)
    assert len(plan.boxes) == 2
    for box in plan.boxes:
        assert (box.x_min-grid.x_min)/(grid.dx/2) == pytest.approx(round((box.x_min-grid.x_min)/(grid.dx/2)))


@pytest.mark.parametrize("kind,params", [
    (ProcessType.DEPOSIT, {"target": 0.03}),
    (ProcessType.CMP, {"target_z": 0}),
    (ProcessType.ETCH, {"target": 0.03, "material": "Si"}),
])
def test_global_operations_cannot_be_independently_tiled(kind, params):
    grid = UniformGrid3D(-0.2, 0.2, -0.2, 0.2, -0.2, 0.2, 11, 11, 11)
    recipe = Recipe("global", kind, output_material="oxide", parameters=params)
    engine = ProcessEngine({recipe.id: recipe})
    project = ProjectDefinition("test", grid.__dict__)
    branch = FlowBranch("main", [ProcessStep("global", recipe.id)])
    plan = engine.plan_refinement(grid, project, branch, factor=2)
    assert plan.mode == "full-domain"
    assert plan.node_count == 21**3
    assert plan.boxes[0].x_min == grid.x_min
    calls = []
    with pytest.raises(MemoryError, match="No coarser solve"):
        engine.run_refined_branch(lambda g: calls.append(g), project, branch, factor=2, max_nodes=100)
    assert not calls  # refuse before allocating/simulating


@pytest.mark.parametrize("shape", ["sphere", "tilted_plane", "two_objects"])
def test_conformal_deposition_never_rewrites_existing_interfaces(shape):
    grid = UniformGrid3D(-0.3, 0.3, -0.3, 0.3, -0.3, 0.3, 31, 31, 31)
    z, y, x = np.meshgrid(grid.z, grid.y, grid.x, indexing="ij")
    if shape == "tilted_plane":
        phi = (z + 0.37*x - 0.23*y - 0.013) / math.sqrt(1+0.37**2+0.23**2)
    else:
        phi = np.sqrt((x-0.013)**2 + (y+0.027)**2 + z*z) - 0.12
        if shape == "two_objects":
            phi = np.minimum(phi, np.sqrt((x+0.15)**2 + (y-0.07)**2 + (z+0.03)**2)-0.08)
    state = MaterialState(grid)
    state.add_material("seed", phi)
    first = deposit_material(state, "film1", 0.04)
    second = deposit_material(first, "film2", 0.04)
    np.testing.assert_array_equal(state.fields["seed"], first.fields["seed"])
    np.testing.assert_array_equal(first.fields["seed"], second.fields["seed"])
    np.testing.assert_array_equal(first.fields["film1"], second.fields["film1"])
    assert not np.any((phi < 0) & (first.fields["film1"] < 0))
    unchanged = deposit_material(first, "not_created", 0)
    assert unchanged.priority == first.priority


def test_polygon_closes_last_edge_and_handles_concavity():
    xx = np.array([0.1, 0.9, 0.9, 1.1])
    yy = np.array([0.9, 0.1, 0.9, 0.0])
    points = [(0, 0), (1, 0), (1, .4), (.4, .4), (.4, 1), (0, 1)]
    phi = polygon_distance(xx, yy, points)
    np.testing.assert_allclose(phi, [-.1, -.1, .5, .1])
    assert np.allclose(phi, polygon_distance(xx, yy, points[::-1]))
    np.testing.assert_allclose(path_distance(np.array([.5]), np.array([.3]), [(0,0), (1,0)], .2), [.2])


def test_gds_transformed_nondefault_units_matches_polygon(tmp_path):
    library = gdstk.Library(unit=1e-9, precision=1e-12)
    cell = library.new_cell("unit")
    cell.add(gdstk.rectangle((0, 0), (200, 100), layer=3, datatype=8))
    top = library.new_cell("top")
    top.add(gdstk.Reference(cell, origin=(30, -20), rotation=math.pi/2))
    path = tmp_path / "nm.gds"
    library.write_gds(path)
    xy = np.linspace(-.25, .25, 51)
    xx, yy = np.meshgrid(xy, xy)
    actual = gds_level_set(path, xx, yy, layer=3, datatype=8)
    expected = polygon_distance(xx, yy, [(0.03,-.02),(.03,.18),(-.07,.18),(-.07,-.02)])
    np.testing.assert_allclose(actual, expected, atol=1e-12)
    np.testing.assert_allclose(gds_level_set(path, xx, yy, layer=3, datatype=8, keep="outside"), -actual)


def test_fast_marching_circle_offset_converges_without_grid_recentering():
    errors = []
    for n in [41, 81, 161]:
        xy = np.linspace(-.5, .5, n)
        xx, yy = np.meshgrid(xy, xy)
        exact = np.hypot(xx-.013, yy+.027)-.19
        distance = reinitialize_signed_distance_subcell(exact, xy[1]-xy[0])
        band = (exact > .03) & (exact < .10)
        errors.append(float(np.sqrt(np.mean((distance[band]-exact[band])**2))))
    assert errors[2] < errors[1] < errors[0]
    assert errors[2] < .25 * (1/160)


def test_later_deposition_cannot_fill_a_preexisting_sealed_pore():
    grid = UniformGrid3D(-.3, .3, -.3, .3, -.3, .3, 31, 31, 31)
    z, y, x = np.meshgrid(grid.z, grid.y, grid.x, indexing="ij")
    radius = np.sqrt(x*x+y*y+z*z)
    shell = np.maximum(radius-.20, .09-radius)
    state = MaterialState(grid)
    state.add_material("shell", shell)
    deposited = deposit_material(state, "film", .12)
    pore = radius < .07
    assert np.all(deposited.fields["film"][pore] > 0)
    np.testing.assert_array_equal(deposited.fields["shell"], shell)
    assert deposited.fields["film"][15, 15, 27] < 0  # outer growth is allowed
