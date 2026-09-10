import gdstk
import numpy as np

from process_studio.kernel.grid import UniformGrid2D
from process_studio.layout.gds import available_gds_layers, rasterize_gds
from process_studio.layout.quick_sketch import QuickSketch, SketchShape


def test_quick_sketch_boolean_array_and_round_trip(tmp_path) -> None:
    grid = UniformGrid2D(-1, 1, -1, 1, 101, 101)
    sketch = QuickSketch("contact array")
    sketch.add(
        SketchShape(
            "rectangle",
            parameters={"center": (0.0, 0.0), "size": (1.2, 0.8)},
        )
    )
    sketch.add(
        SketchShape(
            "circle",
            operation="subtract",
            parameters={"center": (0.0, 0.0), "radius": 0.12},
            array=(3, 1, 0.3, 0.0),
        )
    )
    mask = sketch.render(*grid.mesh)
    assert mask[grid.ny // 2, grid.nx // 2] == 0
    assert mask[int(np.argmin(abs(grid.y - 0.3))), grid.nx // 2] == 1

    path = tmp_path / "sketch.json"
    sketch.save(path)
    restored = QuickSketch.load(path)
    assert np.array_equal(restored.render(*grid.mesh), mask)


def test_quick_sketch_circle_retains_analytic_subcell_boundary() -> None:
    grid = UniformGrid2D(-0.2, 0.2, -0.2, 0.2, 17, 17)
    sketch = QuickSketch(
        "analytic circle",
        [SketchShape("circle", parameters={"center": (0.013, -0.007), "radius": 0.083})],
    )
    xx, yy = grid.mesh
    phi = sketch.signed_distance(xx, yy)
    expected = np.hypot(xx - 0.013, yy + 0.007) - 0.083

    assert np.allclose(phi, expected)
    center_value_in_cells = phi[grid.ny // 2, grid.nx // 2] / grid.dx
    assert not np.isclose(center_value_in_cells, round(center_value_in_cells))


def test_gds_layer_discovery_and_rasterization(tmp_path) -> None:
    library = gdstk.Library()
    cell = library.new_cell("TOP")
    cell.add(gdstk.rectangle((-0.3, -0.2), (0.3, 0.2), layer=7, datatype=2))
    path = tmp_path / "mask.gds"
    library.write_gds(path)

    grid = UniformGrid2D(-1, 1, -1, 1, 101, 101)
    mask = rasterize_gds(path, *grid.mesh, layer=7, datatype=2)

    assert available_gds_layers(path) == [(7, 2)]
    assert mask[grid.ny // 2, grid.nx // 2]
    assert not mask[0, 0]
