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
    assert set(state.display_meshes) == {("ears", False), ("delaunay", False)}
    # Same solid: the payloads agree on how much of it there is.
    by_name = {item["material"]: item for item in ears["surfaces"]}
    for item in delaunay["surfaces"]:
        assert item["triangleCount"] == by_name[item["material"]]["triangleCount"]
        assert item["vertexCount"] == by_name[item["material"]]["vertexCount"]

    # Each engine remembers itself beside the snapshot, and neither file is
    # read back for the other engine.
    written = sorted(path.name for path in tmp_path.iterdir() if path.name != "step.dfz")
    assert written == ["step.dfz.mesh-delaunay-free.npz", "step.dfz.mesh-ears-free.npz"]
    fresh = SlabState(device=_stack(), z_offset=0.0)
    fresh.path = state.path
    assert len(display_meshes(fresh, "delaunay")) == len(delaunay["surfaces"])


def test_a_sidecar_from_before_the_engine_was_recorded_is_rebuilt(tmp_path):
    import numpy as np

    from process_studio.kernels.slab import SlabState, display_meshes

    state = SlabState(device=_stack(), z_offset=0.0)
    state.path = tmp_path / "step.dfz"
    sidecar = tmp_path / "step.dfz.mesh-ears-free.npz"
    np.savez(sidecar, materials=np.array(["Nonsense"], dtype=str))
    meshes = display_meshes(state)
    assert "Nonsense" not in meshes and "SiN" in meshes


def test_an_unknown_triangulator_is_refused():
    from deviceflow._internal.mesh.triangulate import using

    with pytest.raises(MeshError, match="unknown triangulator"):
        with using("marching cubes"):
            pass


def _stack_with_a_buried_layer() -> Device:
    """An oxide sandwiched between two metals: it shows only its rim."""
    device = Device("sandwich", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.01, verbose=False)
    for name in ("Si", "SiO2", "W"):
        device.material(name)
    device.deposit("Si", 0.1, mode="planar")
    device.deposit("SiO2", 0.05, mode="planar")
    device.deposit("W", 0.05, mode="planar")
    return device


def test_the_view_leaves_out_the_faces_nothing_can_see():
    """The mesh the 3D view opens with is the free surface alone.

    A face between two materials is in both meshes at the same place, and
    the viewer draws neither while both are shown -- two copies fight for
    the pixels. In a stack that is nearly the whole mesh, so building them
    is most of the work for something nobody looks at until a material is
    hidden.
    """
    from deviceflow._internal.mesh.builder import build_material_meshes

    state = _stack_with_a_buried_layer()._state
    state.validate()
    free = build_material_meshes(state, manifold=False, buried=False)
    full = build_material_meshes(state, manifold=False, buried=True)

    assert free.keys() == full.keys()
    for material, mesh in free.items():
        other = full[material]
        assert len(mesh.faces) < len(other.faces)
        # What is left is exactly the faces that lie against nothing.
        assert not mesh.metadata["interface_faces"].any()
        assert len(mesh.faces) == int(np.count_nonzero(other.metadata["neighbour_faces"] < 0))
    assert sum(len(m.faces) for m in free.values()) < sum(len(m.faces) for m in full.values())


def test_a_material_with_no_free_surface_is_empty_rather_than_an_error():
    """A layer buried on every side shows nothing until one is hidden."""
    from deviceflow._internal.mesh.builder import build_material_meshes
    from shapely.geometry import box

    from deviceflow._internal.geometry import polygons as P

    device = Device("buried", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.01, verbose=False)
    silicon, oxide = device.material("Si"), device.material("SiO2")
    window = P.as_multipolygon(box(-0.4, -0.4, 0.4, 0.4))
    inner = P.as_multipolygon(box(-0.2, -0.2, 0.2, 0.2))
    state = device._state
    state.add_slab(0.0, 0.1, {silicon: window})
    state.add_slab(0.1, 0.2, {silicon: P.as_multipolygon(window.difference(inner)), oxide: inner})
    state.add_slab(0.2, 0.3, {silicon: window})
    state.harmonize()
    state.validate()

    free = build_material_meshes(state, manifold=False, buried=False)
    assert len(free[oxide].faces) == 0  # sealed in on every side
    assert len(free[silicon].faces) > 0
    # With them it is a solid again, and a full one.
    full = build_material_meshes(state, manifold=False, buried=True)
    assert len(full[oxide].faces) > 0
    assert full[oxide].metadata["interface_faces"].all()


def test_the_kernel_keeps_the_free_and_the_full_mesh_apart(tmp_path):
    from process_studio.defaults import default_grid
    from process_studio.kernels import get_kernel
    from process_studio.kernels.slab import SlabState
    from process_studio.models import ProjectDefinition
    from process_studio.worker.serialize import grid_dict

    kernel = get_kernel("slab")
    project = ProjectDefinition("mesh", grid_dict(default_grid()), kernel="slab", resolution_um=0.01)
    state = SlabState(device=_stack_with_a_buried_layer(), z_offset=0.0)
    state.path = tmp_path / "step.dfz"

    opened = kernel.surfaces(state, project=project)
    behind = kernel.surfaces(state, project=project, buried=True)
    assert opened["buried"] is False and behind["buried"] is True
    assert sum(s["triangleCount"] for s in opened["surfaces"]) < sum(
        s["triangleCount"] for s in behind["surfaces"]
    )
    assert set(state.display_meshes) == {("ears", False), ("ears", True)}
    written = sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".npz")
    assert written == ["step.dfz.mesh-ears-free.npz", "step.dfz.mesh-ears-full.npz"]


def test_hiding_the_shell_reveals_the_material_sealed_inside(tmp_path):
    """The whole point of fetching the buried faces when something is hidden.

    A material enclosed on every side sends nothing while the shell is
    shown -- there is nothing of it to see. Hiding the shell is asking to
    look inside, so the view refetches with the buried faces, and the
    sealed material is then made entirely of faces whose neighbour is
    hidden, which is exactly what the viewer draws.

    The two payloads must also agree on the order of the materials: a face
    says what it lies against by index into that list, so a list that
    shifted between them would colour the cavity by the wrong material.
    """
    import base64

    from shapely.geometry import box

    from deviceflow._internal.geometry import polygons as P
    from process_studio.defaults import default_grid
    from process_studio.kernels import get_kernel
    from process_studio.kernels.slab import SlabState
    from process_studio.models import ProjectDefinition
    from process_studio.worker.serialize import grid_dict

    device = Device("sealed", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.01, verbose=False)
    silicon, oxide = device.material("Si"), device.material("SiO2")
    window = P.as_multipolygon(box(-0.4, -0.4, 0.4, 0.4))
    inner = P.as_multipolygon(box(-0.2, -0.2, 0.2, 0.2))
    state = device._state
    state.add_slab(0.0, 0.1, {silicon: window})
    state.add_slab(0.1, 0.2, {silicon: P.as_multipolygon(window.difference(inner)), oxide: inner})
    state.add_slab(0.2, 0.3, {silicon: window})
    state.harmonize()
    state.validate()

    def drawn(payload, hidden):
        """The viewer's own rule: a face against a shown material is left out."""
        counts = {}
        for surface in payload["surfaces"]:
            if surface["material"] in hidden:
                continue
            against = np.frombuffer(base64.b64decode(surface["neighbourFaces"]), dtype=np.uint8)
            names = surface["neighbourMaterials"]
            keep = [not (index != 255 and names[index] not in hidden) for index in against]
            counts[surface["material"]] = int(sum(keep))
        return counts

    kernel = get_kernel("slab")
    project = ProjectDefinition("m", grid_dict(default_grid()), kernel="slab", resolution_um=0.01)
    slab_state = SlabState(device=device, z_offset=0.0)

    opened = kernel.surfaces(slab_state, project=project)
    assert drawn(opened, set())["SiO2"] == 0  # sealed in, and nothing sent for it
    behind = kernel.surfaces(slab_state, project=project, buried=True)
    assert drawn(behind, {"Si"})["SiO2"] > 0  # every face of it, once the shell is gone

    assert [s["material"] for s in opened["surfaces"]] == [
        s["material"] for s in behind["surfaces"]
    ]
    assert (
        opened["surfaces"][0]["neighbourMaterials"]
        == behind["surfaces"][0]["neighbourMaterials"]
    )
