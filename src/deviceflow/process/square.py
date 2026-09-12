"""Square-cornered films: the fast, simplified model of a deposition.

At every height the film is the solid's outline at that height pushed out
by ``t`` (a wall film that ends flat at the top and the bottom of the wall
it grows on) together with every exposed horizontal face moved by ``t``: a
top face raised, and for a conformal film an underside lowered, each
reaching ``t`` past its edge so that it covers the wall film beside it. No
corner is rounded in z (in XY the outline is offset with round joins), so
the film is piecewise constant between the planes of the stack and those
planes shifted by ``t``, and one sample per interval is exact: a film
costs a handful of polygon offsets instead of one per resolution step.

With ``from_above`` the film is the planar one: only faces the sky sees
are raised, no underside gets film, and nothing forms in a column with a
solid anywhere above it. A recess under an overhang stays empty and both
lips of its mouth end flat.
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


def deposit_square(
    state: ProcessState,
    material: Material,
    thickness: float,
    xy_resolution: float,
    *,
    from_above: bool = False,
) -> tuple[float, float, float]:
    """Deposit ``thickness`` with square corners; returns (z_low, z_high, volume added)."""
    t = float(thickness)
    if not t > 0:
        raise ProcessError(f"deposition thickness must be positive, got {thickness}")
    if not xy_resolution > 0:
        raise ProcessError("xy_resolution must be positive")
    if state.floor is None:
        raise ProcessError(
            "deposition on an empty device: there is no surface to coat; "
            "deposit a planar layer (the substrate) first"
        )
    quad_segs = _quad_segs(t, float(xy_resolution))
    floor, top = state.floor, state.top
    slabs = [(s.z0, s.z1, s.occupied()) for s in state.slabs]
    starts = [z0 for z0, _, _ in slabs]

    raised: list[tuple[float, MultiPolygon]] = []
    lowered: list[tuple[float, MultiPolygon]] = []
    roof: list[MultiPolygon] = []
    if from_above:
        # The faces the sky sees, walked from the top down: the first solid
        # in a column is what that column's film lands on, and the columns
        # it takes are closed to everything below it.
        open_columns = state.clean(box(*state.bounds))
        cover: MultiPolygon = P.EMPTY
        for z0, z1, occ in reversed(slabs):
            if not occ.is_empty:
                if not open_columns.is_empty:
                    face = state.clean(occ.intersection(open_columns))
                    if not face.is_empty:
                        raised.append((z1, face))
                    open_columns = state.clean(open_columns.difference(occ))
                cover = P.as_multipolygon(shapely.unary_union([cover, occ]))
            # Everything from this slab upward: the shadow over a sample below it.
            roof.append(cover)
        roof.reverse()
        roof.append(P.EMPTY)
    else:
        # Every exposed face: a top where the slab above is void, an
        # underside where the slab below is void. Nothing grows under the
        # floor: the wafer continues there.
        for index, (z0, z1, occ) in enumerate(slabs):
            if occ.is_empty:
                continue
            above = slabs[index + 1][2] if index + 1 < len(slabs) else P.EMPTY
            face = occ if above.is_empty else state.clean(occ.difference(above))
            if not face.is_empty:
                raised.append((z1, face))
            if index > 0:
                below = slabs[index - 1][2]
                face = occ if below.is_empty else state.clean(occ.difference(below))
                if not face.is_empty:
                    lowered.append((z0, face))

    planes = {floor, top, *(z0 for z0, _, _ in slabs), *(z1 for _, z1, _ in slabs)}
    critical = sorted(
        {znorm(z) for z in planes} | {znorm(z + t) for z in planes} | {znorm(z - t) for z in planes}
    )
    z_high = znorm(top + t)
    critical = [z for z in critical if floor <= z <= z_high]

    def solid_at(z: float) -> MultiPolygon:
        index = bisect.bisect_right(starts, z) - 1
        if 0 <= index < len(slabs) and slabs[index][0] <= z < slabs[index][1]:
            return slabs[index][2]
        return P.EMPTY

    before = state.volume(material)
    new_regions: list[tuple[float, float, MultiPolygon]] = []
    for za, zb in zip(critical[:-1], critical[1:]):
        if zb - za <= 0:
            continue
        zm = (za + zb) / 2
        occ = solid_at(zm)
        seeds = [] if occ.is_empty else [occ]
        seeds.extend(face for h, face in raised if h <= zm < znorm(h + t))
        seeds.extend(face for h, face in lowered if znorm(h - t) <= zm < h)
        if not seeds:
            continue
        seed = seeds[0] if len(seeds) == 1 else shapely.unary_union(seeds)
        film = state.clean(seed.buffer(t, quad_segs=quad_segs, join_style="round"))
        if not occ.is_empty:
            film = state.clean(film.difference(occ))
        if from_above:
            shadow = roof[bisect.bisect_right(starts, zm)]
            if not shadow.is_empty:
                film = state.clean(film.difference(shadow))
        if not film.is_empty:
            new_regions.append((za, zb, film))

    changed = apply_film(state, material, new_regions)
    state.harmonize(changed)
    state.consolidate()
    state.validate()
    return floor, state.top if state.top is not None else top, state.volume(material) - before
