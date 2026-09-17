"""The planar figure the materials are cut from, and the wall pass.

Building one material used to rebuild that figure -- every material's
rings noded together, polygonized and labelled against every slab -- and
then ask, edge by edge and slab by slab, where a wall stands. On a real
31-step flow (253 slabs, 7 materials) that was 46 s of shared work done
seven times and 24 million Python steps per material, the same 24 million
whether the material came out with 277,000 triangles or 68. Sharing the
figure and asking numpy took that stack's free surface from 88.2 s to
19.7 s and its full mesh from 118.1 s to 48.4 s, with the meshes identical
to the byte. So what these tests hold is the identity, not the speed.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from shapely.geometry import box

from deviceflow import Device
from deviceflow._internal.geometry import polygons as P
from deviceflow._internal.mesh import builder
from deviceflow._internal.mesh.builder import (
    arrange,
    build_material_meshes,
    build_one_material,
    materials_in_order,
)
from deviceflow.exceptions import MeshError


def _stack() -> object:
    """Three materials, one of them a plug through the other two."""
    device = Device("builder", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.02, verbose=False)
    silicon, oxide, nitride = (device.material(n) for n in ("Si", "SiO2", "SiN"))
    window = P.as_multipolygon(box(-0.4, -0.4, 0.4, 0.4))
    plug = P.as_multipolygon(box(-0.1, -0.1, 0.1, 0.1))
    state = device._state
    state.add_slab(0.0, 0.1, {silicon: window})
    for z0 in (0.1, 0.2, 0.3):
        state.add_slab(
            z0,
            z0 + 0.1,
            {silicon: P.as_multipolygon(window.difference(plug)), oxide: plug},
        )
    state.add_slab(0.4, 0.5, {nitride: window})
    state.harmonize()
    state.validate()
    return state


def _fingerprint(meshes) -> str:
    digest = hashlib.blake2b(digest_size=16)
    for material, mesh in meshes.items():
        digest.update(material.name.encode())
        digest.update(np.ascontiguousarray(mesh.vertices).tobytes())
        digest.update(np.ascontiguousarray(mesh.faces).tobytes())
        digest.update(np.ascontiguousarray(mesh.metadata["neighbour_faces"]).tobytes())
    return digest.hexdigest()


@pytest.mark.parametrize("buried", [False, True], ids=["free", "full"])
def test_sharing_the_figure_builds_the_same_mesh(buried):
    """The figure says nothing about any one material, and proving that is
    the whole licence for building it once."""
    state = _stack()
    materials = materials_in_order(state)
    figure = arrange(state, materials)
    for material in materials:
        alone = build_one_material(
            state, material, manifold=False, materials=materials, buried=buried
        )
        shared = build_one_material(
            state,
            material,
            manifold=False,
            materials=materials,
            buried=buried,
            arrangement=figure,
        )
        assert np.array_equal(alone.vertices, shared.vertices), material.name
        assert np.array_equal(alone.faces, shared.faces), material.name
        assert np.array_equal(
            alone.metadata["neighbour_faces"], shared.metadata["neighbour_faces"]
        ), material.name


@pytest.mark.parametrize("buried", [False, True], ids=["free", "full"])
def test_the_wall_pass_does_not_notice_its_chunk_size(buried, monkeypatch):
    """The columns are met in edge order, in chunks, and the order they are
    met in is the order the walls come out in -- so a chunk boundary
    anywhere must make no difference at all."""
    state = _stack()
    whole = _fingerprint(build_material_meshes(state, manifold=False, buried=buried))
    for chunk in (1, 2, 3, 7):
        monkeypatch.setattr(builder, "WALL_CHUNK", chunk)
        assert (
            _fingerprint(build_material_meshes(state, manifold=False, buried=buried)) == whole
        ), f"chunk of {chunk}"


def test_two_materials_in_one_place_is_an_error_not_a_mesh():
    """One number per face says which material holds it, which is only
    enough because a slab's regions are disjoint.

    ``harmonize`` and ``validate`` both refuse an overlap, so the regions
    are put there behind their backs: the point is that the figure does not
    quietly mesh two materials into one place if one ever gets through.
    """
    device = Device("overlap", (-0.4, -0.4, 0.4, 0.4), conformal_resolution=0.02, verbose=False)
    silicon, oxide = device.material("Si"), device.material("SiO2")
    state = device._state
    state.add_slab(0.0, 0.1, {silicon: P.as_multipolygon(box(-0.3, -0.3, 0.1, 0.1))})
    state.harmonize()
    state.slabs[0].regions[oxide] = P.as_multipolygon(box(-0.1, -0.1, 0.3, 0.3))

    with pytest.raises(MeshError, match="overlaps another material"):
        arrange(state)
