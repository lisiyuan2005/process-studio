"""The display mesh's caps, and the two ways they get triangulated.

Building the 3D view is the slow part of the slab kernel, and half of that
was GEOS's constrained Delaunay over the caps: a layer with a hole array
is a thousand vertices in one polygon and GEOS takes 40 ms over it. Ear
clipping does the same job about thirty times faster. It is an optional
accelerator, so both paths must hold the properties the mesh builder
relies on -- no vertex invented, none dropped -- and must describe the
same surface, since the walls are built from the caps' vertices and a
vertex only one of them uses is a crack in the solid.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Point, Polygon, box

from deviceflow import Device
from deviceflow._internal.mesh import triangulate as triangulation
from deviceflow._internal.mesh.builder import build_material_meshes
from deviceflow.exceptions import MeshError


@pytest.fixture
def without_earcut(monkeypatch):
    """Force the GEOS fallback, as on a machine without the wheel."""
    monkeypatch.setattr(triangulation, "mapbox_earcut", None)


def _faces() -> list[Polygon]:
    return [
        box(0, 0, 1, 1),
        # A straight edge carrying vertices that no corner needs: the walls
        # put them there, so the caps have to keep them.
        Polygon([(0, 0), (1, 0), (2, 0), (3, 0), (3, 3), (0, 3)]),
        box(0, 0, 4, 4).difference(box(1, 1, 2, 2)),
        # A cap of a layer with a hole array, which is the slow case.
        box(-1, -1, 1, 1).difference(
            Point(0, 0).buffer(0.2, quad_segs=16).union(Point(0.5, 0.5).buffer(0.2, quad_segs=16))
        ),
    ]


def _covered(face: Polygon, triangles) -> float:
    corners = np.asarray(triangles, dtype=float)
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
    twice = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    assert (twice > 0).all(), "every triangle is wound counter-clockwise"
    return float(twice.sum()) / 2


@pytest.mark.parametrize("face", _faces(), ids=["square", "collinear", "notched", "hole array"])
def test_both_triangulators_cover_the_face_with_its_own_vertices(face, monkeypatch):
    results = {}
    for name, engine in (("ears", triangulation.mapbox_earcut), ("delaunay", None)):
        monkeypatch.setattr(triangulation, "mapbox_earcut", engine)
        triangles = triangulation.triangulate(face)
        assert _covered(face, triangles) == pytest.approx(face.area, rel=1e-9)
        corners = {corner for triangle in triangles for corner in triangle}
        on_face = {(x, y) for x, y in face.exterior.coords[:-1]}
        for ring in face.interiors:
            on_face |= {(x, y) for x, y in ring.coords[:-1]}
        assert corners == on_face, "no vertex invented, and none dropped"
        results[name] = len(triangles)
    # Any triangulation that adds no vertex and drops none has V + 2H - 2
    # triangles, so the two agree on the count whatever diagonals they pick.
    assert results["ears"] == results["delaunay"]


def test_an_empty_or_degenerate_face_has_no_triangles():
    assert triangulation.triangulate(Polygon()) == []
    assert triangulation.triangulate(Polygon([(0, 0), (1, 0), (2, 0)])) == []


def test_ear_clipping_that_loses_a_vertex_falls_back_instead_of_failing(monkeypatch):
    """The accelerator is never allowed to be the reason a mesh is wrong."""
    monkeypatch.setattr(
        triangulation.mapbox_earcut, "triangulate_float64",
        lambda points, ends: np.zeros(3, dtype=np.uint32),
    )
    face = box(0, 0, 1, 1)
    triangles = triangulation.triangulate(face)
    assert _covered(face, triangles) == pytest.approx(face.area, rel=1e-9)


def _stack() -> Device:
    device = Device("holes", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.01, verbose=False)
    for name in ("Si", "SiO2", "SiN"):
        device.material(name)
    device.deposit("Si", 0.1, mode="planar")
    device.deposit("SiO2", 0.05, mode="planar")
    device.deposit("SiN", 0.05, mode="planar")
    holes = device.masks.circle((-0.15, -0.15), 0.16)
    device.etch(holes, target=["SiO2", "SiN"], depth=0.1)
    return device


def test_the_display_mesh_is_the_same_solid_either_way(monkeypatch):
    built = {}
    for name, engine in (("ears", triangulation.mapbox_earcut), ("delaunay", None)):
        monkeypatch.setattr(triangulation, "mapbox_earcut", engine)
        state = _stack()._state
        state.validate()
        built[name] = {
            material.name: mesh for material, mesh in build_material_meshes(state, manifold=False).items()
        }
    ears, delaunay = built["ears"], built["delaunay"]
    assert ears.keys() == delaunay.keys()
    for name, mesh in ears.items():
        other = delaunay[name]
        assert len(mesh.vertices) == len(other.vertices)
        assert len(mesh.faces) == len(other.faces)
        assert mesh.area == pytest.approx(other.area, rel=1e-9)
        assert mesh.volume == pytest.approx(other.volume, rel=1e-9)


def test_the_kernel_keeps_a_mesh_per_triangulator(tmp_path):
    """The 3D view can be asked for either, and each is cached on its own.

    They describe the same solid, so this is a comparison switch; but a
    mesh built one way is not the triangles the other asked to look at, so
    neither the in-memory cache nor the file beside the snapshot may hand
    back the wrong one.
    """
    from process_studio.defaults import default_grid
    from process_studio.kernels import get_kernel
    from process_studio.kernels.slab import SlabState, display_meshes
    from process_studio.models import ProjectDefinition
    from process_studio.worker.serialize import grid_dict

    kernel = get_kernel("slab")
    project = ProjectDefinition("mesh", grid_dict(default_grid()), kernel="slab", resolution_um=0.01)
    state = SlabState(device=_stack(), z_offset=0.0)
    state.path = tmp_path / "step.dfz"

    ears = kernel.surfaces(state, project=project)
    assert ears["triangulation"] == "ears"  # the default
    delaunay = kernel.surfaces(state, project=project, triangulation="delaunay")
    assert delaunay["triangulation"] == "delaunay"
    assert set(state.display_meshes) == {"ears", "delaunay"}
    # Same solid: the payloads agree on how much of it there is.
    by_name = {item["material"]: item for item in ears["surfaces"]}
    for item in delaunay["surfaces"]:
        assert item["triangleCount"] == by_name[item["material"]]["triangleCount"]
        assert item["vertexCount"] == by_name[item["material"]]["vertexCount"]

    # Each engine remembers itself beside the snapshot, and neither file is
    # read back for the other engine.
    written = sorted(path.name for path in tmp_path.iterdir() if path.name != "step.dfz")
    assert written == ["step.dfz.mesh-delaunay.npz", "step.dfz.mesh.npz"]
    fresh = SlabState(device=_stack(), z_offset=0.0)
    fresh.path = state.path
    assert len(display_meshes(fresh, "delaunay")) == len(delaunay["surfaces"])


def test_a_sidecar_from_before_the_engine_was_recorded_is_rebuilt(tmp_path):
    import numpy as np

    from process_studio.kernels.slab import SlabState, display_meshes

    state = SlabState(device=_stack(), z_offset=0.0)
    state.path = tmp_path / "step.dfz"
    sidecar = tmp_path / "step.dfz.mesh.npz"
    np.savez(sidecar, materials=np.array(["Nonsense"], dtype=str))
    meshes = display_meshes(state)
    assert "Nonsense" not in meshes and "SiN" in meshes


def test_an_unknown_triangulator_is_refused():
    from deviceflow._internal.mesh.triangulate import using

    with pytest.raises(MeshError, match="unknown triangulator"):
        with using("marching cubes"):
            pass
