"""Two materials meeting along a boundary, and the arithmetic that leaves seams.

Cutting one region out of another gives back edges that should be identical
and are not, to the last bits of a double. What is left between them is a
seam: thousands of parts a few attometres wide strung along the join. It is
not geometry -- nothing is, at 1e-11 micrometres -- but it is real enough to
have stopped a 19-step flow dead at its last step, because the check for two
materials in one place compared the seam's *area* against a fixed epsilon,
and a seam's area grows with the length of the boundary it lies along.

A real 3D AND cell reached this at step 19 with a TiN/HfO2 seam of
2.45e-11 um^2 spread over 2676 parts along 2.5 um of boundary: a mean width
of 2e-11 um, which is 1/50000 of the snapping grid.
"""

from __future__ import annotations

import pytest

pytest.importorskip("shapely")

import shapely
from shapely.geometry import MultiPolygon, box

from deviceflow._internal.geometry.state import ProcessState
from deviceflow.exceptions import GeometryError
from deviceflow.material import MaterialRegistry

GRID = 1e-6


def registry() -> MaterialRegistry:
    materials = MaterialRegistry()
    materials.add("TiN")
    materials.add("HfO2")
    return materials


def seam(width: float, parts: int = 2676, along: float = 2.5) -> MultiPolygon:
    """Slivers of ``width`` strung along y = 0, as a cut along it leaves them.

    They hang *below* the join, which is what makes them an overlap: the
    material underneath still holds that side, to the last bits of a double.
    """
    step = along / parts
    return MultiPolygon(
        [
            box(-along / 2 + index * step, -width, -along / 2 + index * step + step * 0.9, 0.0)
            for index in range(parts)
        ]
    )


def multi(geom) -> MultiPolygon:
    """A slab's regions are MultiPolygons, whatever shape built them."""
    return geom if isinstance(geom, MultiPolygon) else MultiPolygon([geom])


def with_regions(state: ProcessState, regions) -> ProcessState:
    """Put regions into a slab the way an operation does.

    ``add_slab`` cleans what it is given, and cleaning snaps a seam out of
    existence before anything looks at it. Real seams do not arrive that
    way: they are written into a slab's regions by an etch or a deposition
    rebuilding the stack (``harmonize``), and are next looked at by
    ``validate``. So this writes them in directly, as those do.
    """
    materials = list(regions)
    state.add_slab(0.0, 0.1, {materials[0]: regions[materials[0]]})
    state.slabs[0].regions.update({key: multi(value) for key, value in regions.items()})
    return state


def test_a_seam_between_two_materials_is_rubbed_out_not_reported():
    materials = registry()
    tin, hfo2 = materials.resolve("TiN"), materials.resolve("HfO2")
    block = box(-1.25, 0.0, 1.25, 0.5)
    state = with_regions(
        ProcessState((-1.5, -1.5, 1.5, 1.5), GRID),
        {tin: box(-1.25, -0.5, 1.25, 0.0), hfo2: shapely.union_all([block, seam(2e-11)])},
    )

    state.validate()

    slab = state.slabs[0]
    kept = slab.regions[tin].intersection(slab.regions[hfo2])
    assert kept.is_empty or kept.area == 0.0
    # The block is still there: only the seam was cut away.
    assert slab.regions[hfo2].area == pytest.approx(block.area, rel=1e-9)


def test_two_materials_in_the_same_place_is_still_an_error():
    materials = registry()
    tin, hfo2 = materials.resolve("TiN"), materials.resolve("HfO2")
    # 10 nm square of one material inside the other: a modelling mistake,
    # not arithmetic, and it survives being eroded by a snap step.
    state = with_regions(
        ProcessState((-1.5, -1.5, 1.5, 1.5), GRID),
        {tin: box(-1.0, -1.0, 1.0, 1.0), hfo2: box(0.0, 0.0, 0.01, 0.01)},
    )
    with pytest.raises(GeometryError, match="overlap"):
        state.validate()


def test_a_real_overlap_is_found_even_with_seams_around_it():
    """A seam does not hide a genuine overlap: the test is per part, not on a sum."""
    materials = registry()
    tin, hfo2 = materials.resolve("TiN"), materials.resolve("HfO2")
    state = with_regions(
        ProcessState((-1.5, -1.5, 1.5, 1.5), GRID),
        {
            tin: box(-1.25, -0.5, 1.25, 0.5),
            hfo2: shapely.union_all([seam(2e-11), box(0.2, 0.2, 0.21, 0.21)]),
        },
    )
    with pytest.raises(GeometryError, match="overlap"):
        state.validate()


def test_the_mesh_of_a_state_with_a_seam_can_still_be_built():
    """Why a seam is removed rather than tolerated.

    The mesh builder polygonizes every material's rings together and asks
    which material owns each face. A seam left in the state is a face owned
    twice, so it reads as "two materials in one place" the moment somebody
    opens the 3D view -- long after the step that made it.
    """
    pytest.importorskip("trimesh")
    from deviceflow._internal.mesh.builder import build_material_meshes

    materials = registry()
    tin, hfo2 = materials.resolve("TiN"), materials.resolve("HfO2")
    state = with_regions(
        ProcessState((-1.5, -1.5, 1.5, 1.5), GRID),
        {
            tin: box(-1.25, -0.5, 1.25, 0.0),
            hfo2: shapely.union_all([box(-1.25, 0.0, 1.25, 0.5), seam(2e-11)]),
        },
    )
    state.validate()

    meshes = build_material_meshes(state)
    assert set(meshes) == {tin, hfo2}
    assert all(len(mesh.faces) > 0 for mesh in meshes.values())


def test_rubbing_out_a_seam_never_turns_solid_into_void():
    """The strip is kept by the other material, not dropped.

    The overlap belongs to both, so cutting it from one leaves it covered
    by the other. This is the property a wet etch depends on: void is what
    the etchant creeps through, and a seam that became a gap would be a
    channel through a barrier that nobody drew.
    """
    materials = registry()
    tin, hfo2 = materials.resolve("TiN"), materials.resolve("HfO2")
    state = with_regions(
        ProcessState((-1.5, -1.5, 1.5, 1.5), GRID),
        {
            tin: box(-1.25, -0.5, 1.25, 0.0),
            hfo2: shapely.union_all([box(-1.25, 0.0, 1.25, 0.5), seam(2e-11)]),
        },
    )
    slab = state.slabs[0]
    before = shapely.union_all(list(slab.regions.values()))

    state.validate()

    after = shapely.union_all(list(slab.regions.values()))
    # Nothing solid was lost: what the two held together, they still hold.
    assert before.difference(after).area == pytest.approx(0.0, abs=1e-15)
    assert after.area == pytest.approx(before.area, abs=1e-15)


def _sealed_shell(seam_width: float | None = None, opening: bool = False):
    """A target sealed behind a 5 nm wall, optionally with a seam or a hole."""
    from deviceflow import Device
    from shapely.geometry import Point

    device = Device(
        "sealed shell", (-1.0, -1.0, 1.0, 1.0), conformal_resolution=0.005, verbose=False
    )
    target = device.material("Target", role="dielectric")
    barrier = device.material("Barrier", role="metal")
    outer = Point(0.0, 0.0).buffer(0.5, quad_segs=32)
    inner = Point(0.0, 0.0).buffer(0.495, quad_segs=32)
    device._state.add_slab(0.0, 0.1, {barrier: outer})
    device._state.add_slab(0.1, 0.9, {barrier: outer.difference(inner), target: inner})
    device._state.add_slab(0.9, 1.0, {barrier: outer})
    if seam_width is not None:
        # The target bleeds a hair into the wall: two materials in one
        # place, and the repair has to take that strip out of one of them.
        middle = device._state.slabs[1]
        middle.regions[target] = multi(inner.buffer(seam_width))
        device._state.validate()
    if opening:
        top = device._state.slabs[-1]
        hole = Point(0.0, 0.0).buffer(0.08, quad_segs=16)
        top.regions[barrier] = device._state.clean(top.regions[barrier].difference(hole))
    return device, target


def test_a_wet_etch_is_still_stopped_by_a_barrier_that_had_a_seam():
    """The question a seam raises: can the etchant get through where one was?

    A wet etch creeps through void, so half a picometre of void left behind
    by the repair would be a channel nobody drew. It is cut against the
    whole of the other region and not snapped afterwards, so the edge that
    comes back is the other region's own and the two stay in contact.

    Measured on this shell: sealed and untouched, the etch removes 1e-16
    um^3 -- nothing. Sealed after a seam was rubbed out, 1e-7 um^3, and
    *upwards*, which is the outline being re-snapped rather than etched.
    With a real hole in the cap, 3.8e-2 um^3 -- six percent of the target,
    which is what getting through looks like.
    """
    pytest.importorskip("trimesh")

    device, target = _sealed_shell(seam_width=2e-11)
    before = device.volume(target)
    device.wet_etch(target=target, depth=0.2)
    assert abs(device.volume(target) - before) < 1e-6

    # The same test on a shell with a hole in it: this is what it looks
    # like when the etchant does get in, and it is 5 orders larger.
    opened, target = _sealed_shell(opening=True)
    before = opened.volume(target)
    opened.wet_etch(target=target, depth=0.2)
    assert before - opened.volume(target) > 1e-3
