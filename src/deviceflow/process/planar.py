"""Planar deposition: the film arrives from straight above and walls are coated.

At every height the film is the solid's outline at that height pushed out
by ``t`` (the wall film, which ends flat at the top and the bottom of the
wall it grows on) together with every horizontal face the sky can see
raised by ``t``, the raised face reaching ``t`` past its edge so that it
covers the wall film below it (a square outer corner). Nothing forms where
a solid lies anywhere above in the same column, so a recess under an
overhang stays empty and both lips of its mouth end flat: no lip hangs
down from the overhang and no cap grows up from the wall under it. A step
stays a step of the same height, a hole narrows by ``t`` and keeps its
depth while its floor rises. No corner is rounded in z, so the film is
piecewise constant between the planes of the stack and those planes plus
``t``, and one sample per interval is exact. On an empty device the film
is a blanket slab from z = 0; that is how a substrate is made.
"""

from __future__ import annotations

import bisect

import shapely
from shapely.geometry import MultiPolygon, box

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material
from .conformal import _quad_segs, apply_film


def deposit_planar(
    state: ProcessState,
    material: Material,
    thickness: float,
    resolution: float,
    xy_resolution: float | None = None,
) -> tuple[float, float]:
    """Land ``thickness`` of ``material`` from above; returns (z_low, z_high)."""
    t = float(thickness)
    if not t > 0:
        raise ProcessError(f"deposition thickness must be positive, got {thickness}")
    if state.top is None:
        z0, z1 = 0.0, znorm(t)
        state.add_slab(z0, z1, {material: box(*state.bounds)})
        state.consolidate()
        state.validate()
        return z0, z1
    xy = resolution if xy_resolution is None else float(xy_resolution)
    if not xy > 0:
        raise ProcessError("xy_resolution must be positive")
    quad_segs = _quad_segs(t, xy)
    floor, top = state.floor, state.top
    slabs = [(s.z0, s.z1, s.occupied()) for s in state.slabs]
    starts = [z0 for z0, _, _ in slabs]

    # The faces the sky sees, walked from the top down: the first solid in a
    # column is what that column's film lands on, and the columns it takes
    # are closed to everything below it.
    open_columns = state.clean(box(*state.bounds))
    faces: list[tuple[float, MultiPolygon]] = []
    for z0, z1, occ in reversed(slabs):
        if open_columns.is_empty:
            break
        if occ.is_empty:
            continue
        face = state.clean(occ.intersection(open_columns))
        if not face.is_empty:
            faces.append((z1, face))
        open_columns = state.clean(open_columns.difference(occ))
    # Everything from a slab upward, for the shadow: a sample at z lies
    # under every slab whose z0 is above it.
    roof: list[MultiPolygon] = []
    cover: MultiPolygon = P.EMPTY
    for _, _, occ in reversed(slabs):
        if not occ.is_empty:
            cover = P.as_multipolygon(shapely.unary_union([cover, occ]))
        roof.append(cover)
    roof.reverse()
    roof.append(P.EMPTY)

    planes = {floor, top, *(z0 for z0, _, _ in slabs), *(z1 for _, z1, _ in slabs)}
    critical = sorted({znorm(z) for z in planes} | {znorm(z + t) for z in planes})
    critical = [z for z in critical if floor <= z <= znorm(top + t)]

    def solid_at(z: float) -> MultiPolygon:
        index = bisect.bisect_right(starts, z) - 1
        if 0 <= index < len(slabs) and slabs[index][0] <= z < slabs[index][1]:
            return slabs[index][2]
        return P.EMPTY

    new_regions: list[tuple[float, float, MultiPolygon]] = []
    for za, zb in zip(critical[:-1], critical[1:]):
        if zb - za <= 0:
            continue
        zm = (za + zb) / 2
        occ = solid_at(zm)
        raised = [face for h, face in faces if h <= zm < znorm(h + t)]
        seeds = [occ, *raised] if not occ.is_empty else raised
        if not seeds:
            continue
        seed = shapely.unary_union(seeds)
        film = state.clean(seed.buffer(t, quad_segs=quad_segs, join_style="round"))
        if not occ.is_empty:
            film = state.clean(film.difference(occ))
        shadow = roof[bisect.bisect_right(starts, zm)]
        if not shadow.is_empty:
            film = state.clean(film.difference(shadow))
        if not film.is_empty:
            new_regions.append((za, zb, film))

    changed = apply_film(state, material, new_regions)
    state.harmonize(changed)
    state.consolidate()
    state.validate()
    z_high = state.top
    return floor, z_high if z_high is not None else top
