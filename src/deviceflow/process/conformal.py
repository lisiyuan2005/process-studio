"""Conformal deposition: equal normal thickness on every exposed surface.

Definition: the new material is the Minkowski dilation of the existing solid
by a ball of radius ``t`` (clipped to the device bounds), minus the existing
solid. Consequences: sidewalls, floors and tops receive exactly ``t``;
convex outer corners round with radius ``t``; concave inner corners stay
sharp; an opening narrower than ``2t`` pinches off.

Exact 2D evaluation at a height z
---------------------------------
The solid is piecewise constant in z (slabs). For a slab k with XY region
S_k at vertical distance dz_k from z (0 if z lies inside it), the union of
the ball slices over all heights of that slab is a single disc of radius
``r_k = sqrt(t^2 - dz_k^2)``, so

    dilated(z) = U_k  buffer(S_k, r_k)          over slabs within +-t of z
    new(z)     = dilated(z) - solid(z)

with no discretisation in dz. Below the device floor the wafer continues
with the footprint of the lowest solid, so nothing deposits below the floor
and a hole etched through the whole stack keeps an open bottom.

Z sampling
----------
``new(z)`` is constant except within ``t`` of a plane where the solid
changes (there the r_k vary and corners round). Those bands are sampled at
``conformal_resolution``; everything else gets one sample per interval. Each
sample z (interval midpoint) becomes one slab of the new material.
"""

from __future__ import annotations

import bisect
import math

import shapely
from shapely.geometry import MultiPolygon

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState, znorm
from ..cancellation import check_cancelled
from ..exceptions import ProcessError
from ..material import Material

MAX_SAMPLES = 20000


def deposit_conformal(
    state: ProcessState,
    material: Material,
    thickness: float,
    resolution: float,
    xy_resolution: float | None = None,
    *,
    from_above: bool = False,
) -> tuple[float, float, float]:
    """Deposit ``thickness`` conformally; returns (z_low, z_high, volume added).

    ``resolution`` is the z step the film is sampled at, and the scale of
    the staircase that sampling leaves; ``xy_resolution`` is the largest
    sagitta an XY arc may have. It defaults to ``resolution``, which ties
    the two; setting it separately keeps rings cheap while z is fine.

    With ``from_above`` the film only forms where material can arrive from
    straight up: a point gets film if no solid lies anywhere above it in
    its XY column. Walls, floors and tops that face the sky are coated as
    in a conformal film, corners included, so an overhang's lower edge
    curls a lip down and the wall under it caps up; a recess under the
    overhang stays empty.
    """
    t = float(thickness)
    if not t > 0:
        raise ProcessError(f"deposition thickness must be positive, got {thickness}")
    if not resolution > 0:
        raise ProcessError("conformal_resolution must be positive")
    xy = resolution if xy_resolution is None else float(xy_resolution)
    if not xy > 0:
        raise ProcessError("xy_resolution must be positive")

    # snapshot of the solid before deposition
    if state.floor is None:
        raise ProcessError(
            "conformal deposition on an empty device: there is no surface to coat; "
            "deposit a planar layer (the substrate) first"
        )
    floor, top = state.floor, state.top
    # The wafer continues below the floor with the footprint of the lowest
    # solid: nothing deposits under it, and a hole etched through the whole
    # stack keeps an open bottom (no phantom film there). It comes first so
    # that the source list stays sorted by z0.
    bottom = state.slab_at(floor)
    slabs = [(-math.inf, floor, bottom.occupied() if bottom is not None else P.EMPTY)]
    slabs.extend((s.z0, s.z1, s.occupied()) for s in state.slabs)
    planes = sorted({floor, top} | {s.z0 for s in state.slabs} | {s.z1 for s in state.slabs})

    # Never finer than the z step: a film thinner than it is one sample thick.
    samples = _sample_intervals(planes, floor, znorm(top + t), t, resolution)
    quad_segs = _quad_segs(t, xy)

    def solid_at(z: float) -> MultiPolygon:
        index = bisect.bisect_right(starts, z) - 1
        if 0 <= index < len(slabs):
            z0, z1, occ = slabs[index]
            if z0 <= z < z1:
                return occ
        return P.EMPTY

    new_regions: list[tuple[float, float, MultiPolygon]] = []
    previous: MultiPolygon | None = None
    # Consecutive samples whose dilations differ by less than a quarter of
    # the resolution are given the same dilation: the staircase then only has
    # steps at the resolution scale, never picometre ledges between rings.
    # It is the dilation that is compared and snapped, not the film ring cut
    # from it, because a ring has two boundaries: the free surface, which is
    # the one the staircase concerns, and the interface with the solid it
    # grows on. Snapping the ring would move that interface off the solid by
    # up to the tolerance, and a film of the material already there would
    # then meet its own earlier film along a gap of a few nanometres: a hole
    # narrower than the resolution but wider than the grid, which survives
    # every cleaning step and makes the mesh non-manifold where it ends.
    # Cutting the ring from the snapped dilation keeps the interface exact.
    # The welding tolerance between consecutive z samples belongs to the z
    # step: it is what bounds the staircase. Tied to the XY value it would
    # glue samples whose outline moved less than that, and a fine z step
    # under a coarse XY value would still leave XY-sized steps.
    merge_tol = resolution / 4
    # Sources are sorted by z0 and tile the stack, so the ones a sample can
    # reach form a contiguous run and are found by bisection instead of by
    # scanning every slab for every sample.
    starts = [z0 for z0, _, _ in slabs]
    ends = [z1 for _, z1, _ in slabs]
    # Footprint of everything from a source upward, for the from-above
    # cut: a sample at z is shadowed by every source whose z0 lies above it.
    roof: list[MultiPolygon] = []
    if from_above:
        cover = P.EMPTY
        for _, _, occ in reversed(slabs):
            if not occ.is_empty:
                cover = P.as_multipolygon(shapely.unary_union([cover, occ]))
            roof.append(cover)
        roof.reverse()
        roof.append(P.EMPTY)
    # A dilation is a pure function of its geometry, radius and segment count,
    # and the same three recur across samples, so each distinct one is
    # computed once per deposition. The radius is not quantised: only exactly
    # equal radii share a result.
    dilations: dict[tuple[int, float], MultiPolygon] = {}
    for za, zb in samples:
        # The walk over samples is where a conformal film spends its time,
        # so it is where a caller asking to stop gets heard.
        check_cancelled()
        zm = (za + zb) / 2
        first = bisect.bisect_right(ends, zm - t)
        last = bisect.bisect_left(starts, zm + t)
        parts = []
        for index in range(first, last):
            z0, z1, occ = slabs[index]
            if occ.is_empty:
                continue
            if z0 <= zm < z1:
                dz = 0.0
            else:
                dz = z0 - zm if zm < z0 else zm - z1
            r = math.sqrt(max(t * t - dz * dz, 0.0))
            if r <= 0:
                parts.append(occ)
                continue
            key = (id(occ), r)
            dilated_part = dilations.get(key)
            if dilated_part is None:
                dilated_part = occ.buffer(r, quad_segs=quad_segs, join_style="round")
                dilations[key] = dilated_part
            parts.append(dilated_part)
        if not parts:
            continue
        dilated = state.clean(shapely.unary_union(parts))
        if previous is not None:
            if _nearly_same(previous, dilated, merge_tol):
                dilated = previous
            else:
                # where the new surface runs within merge_tol of the previous one, make
                # it identical: no picometre slivers between consecutive samples
                dilated = state.clean(shapely.snap(dilated, previous, merge_tol))
        previous = dilated
        new = state.clean(dilated.difference(solid_at(zm)))
        if from_above and not new.is_empty:
            shadow = roof[bisect.bisect_right(starts, zm)]
            if not shadow.is_empty:
                new = state.clean(new.difference(shadow))
        if not new.is_empty:
            new_regions.append((za, zb, new))

    before = state.volume(material)
    changed = apply_film(state, material, new_regions)
    state.harmonize(changed)
    state.consolidate()
    state.validate()
    z_high = state.top if state.top is not None else top
    return floor, z_high, state.volume(material) - before


def _nearly_same(a: MultiPolygon, b: MultiPolygon, tol: float) -> bool:
    """True if the two regions differ by less than ``tol`` everywhere.

    The two cheap tests in front are exact rejections, not guesses, and
    they carry most of the answers: the Hausdorff distance below is
    quadratic in the vertex count and is the single most expensive thing
    in an isotropic etch once the fronts stop being trivial.
    """
    if a.is_empty or b.is_empty:
        return False
    if abs(a.area - b.area) > tol * (a.length + b.length):
        return False
    # An extreme point of one is that far from every point of the other,
    # so a bounding box that has moved by more than tol settles it.
    ba, bb = a.bounds, b.bounds
    if any(abs(x - y) > tol for x, y in zip(ba, bb)):
        return False
    edge_a, edge_b = a.boundary, b.boundary
    # The distance below walks every vertex of one against every segment
    # of the other, so it costs the product of the two vertex counts: in
    # a stack etch, where a reach follows a whole hole array, it was a
    # third of the entire run and answered yes every single time.
    #
    # A ribbon of width tol drawn about one boundary that swallows the
    # other means every point of that one lies within tol of it -- the
    # Hausdorff distance between the two curves, which the vertex-by-
    # vertex distance can only be smaller than. So a yes here is a yes.
    # It is drawn with straight segments inside the true round ribbon, so
    # it errs towards saying no, never towards a wrong yes; a no is not an
    # answer and falls through to the walk.
    #
    # Only worth it on big outlines. The ribbon is built in about linear
    # time but with a constant the walk does not have, and below a couple
    # of hundred vertices the walk simply wins.
    if shapely.get_num_coordinates(a) + shapely.get_num_coordinates(b) >= RIBBON_WORTH_IT:
        if shapely.covered_by(edge_a, edge_b.buffer(tol)) and shapely.covered_by(
            edge_b, edge_a.buffer(tol)
        ):
            return True
    return shapely.hausdorff_distance(edge_a, edge_b) <= tol


def _sample_intervals(planes, lo, hi, t, h, offsets=None) -> list[tuple[float, float]]:
    """Sample heights between ``lo`` and ``hi``.

    The profile can only kink at a plane of the stack or at one shifted by
    a reach, so those heights bound the intervals; ``t`` is the largest
    reach and sets the band within which a curved profile is sampled at
    ``h``. ``offsets`` are the reaches that kink the profile, defaulting to
    ``t`` alone: an etch with one reach per material has one per depth, and
    leaving the smaller ones out puts a kink in the middle of an interval,
    where a single sample cannot see it.
    """
    offsets = (t,) if offsets is None else tuple(offsets)
    critical = {lo, hi}
    for p in planes:
        for c in (p, *(p - o for o in offsets), *(p + o for o in offsets)):
            c = znorm(c)
            if lo <= c <= hi:
                critical.add(c)
    critical = sorted(critical)
    out: list[tuple[float, float]] = []
    for a, b in zip(critical[:-1], critical[1:]):
        if b - a <= 0:
            continue
        mid = (a + b) / 2
        in_band = any(abs(mid - p) < t for p in planes)
        n = max(1, math.ceil((b - a) / h - 1e-9)) if in_band else 1
        edges = [znorm(a + (b - a) * i / n) for i in range(n + 1)]
        edges[0], edges[-1] = a, b
        out.extend((e0, e1) for e0, e1 in zip(edges[:-1], edges[1:]) if e1 > e0)
    if len(out) > MAX_SAMPLES:
        raise ProcessError(
            f"conformal deposition needs {len(out)} z samples (> {MAX_SAMPLES}); "
            "raise conformal_resolution or simplify the structure"
        )
    return out


#: Vertices, both outlines together, above which the ribbon test below is
#: cheaper than the quadratic walk it stands in for. Measured: the two
#: cost the same at about 260 together, and the walk is four times dearer
#: for every doubling after that while the ribbon is barely twice.
RIBBON_WORTH_IT = 260

MAX_QUAD_SEGS = 64


def _quad_segs(t: float, resolution: float) -> int:
    """Segments per quarter circle so the arc sagitta stays below the resolution."""
    ratio = min(resolution / t, 0.5)
    angle = 2 * math.acos(1 - ratio)
    segs = max(2, math.ceil((math.pi / 2) / angle))
    if segs > MAX_QUAD_SEGS:
        reachable = t * (1 - math.cos(math.pi / (4 * MAX_QUAD_SEGS)))
        raise ProcessError(
            f"conformal_resolution {resolution:g} um cannot be met for a {t:g} um film: the XY arcs are "
            f"limited to {MAX_QUAD_SEGS} segments per quarter circle (sagitta {reachable:.3g} um); "
            "raise conformal_resolution"
        )
    return int(segs)


def apply_film(state: ProcessState, material: Material, new_regions) -> list:
    """Merge ``(z0, z1, region)`` pieces of ``material`` into the stack; returns the slabs touched.

    Pieces must be sorted by ``z0``. One above the stack becomes a new slab
    (with a void slab under it if there is a gap); one inside it is merged
    into the slabs it spans, splitting them at its planes; one straddling
    the top is both.
    """
    changed = []
    stack_top = state.slabs[-1].z1 if state.slabs else None
    for za, zb, new in new_regions:
        if stack_top is None or za >= stack_top:
            if stack_top is not None and za > stack_top:
                state.add_slab(stack_top, za, {})
            slab = state.add_slab(za, zb, {material: new})
            stack_top = zb
            changed.append(slab)
            continue
        if zb > stack_top:  # interval straddles the current top: split it
            inside = (za, stack_top)
            above = (stack_top, zb)
            state.split_at(inside[0])
            for slab in state.slabs_between(*inside):
                _merge_into(state, slab, material, new)
                changed.append(slab)
            slab = state.add_slab(above[0], above[1], {material: new})
            stack_top = zb
            changed.append(slab)
            continue
        state.split_at(za)
        state.split_at(zb)
        for slab in state.slabs_between(za, zb):
            _merge_into(state, slab, material, new)
            changed.append(slab)
    return changed


def _merge_into(state: ProcessState, slab, material: Material, new: MultiPolygon) -> None:
    existing = slab.regions.get(material)
    region = new if existing is None else state.clean(shapely.unary_union([existing, new]))
    # never overwrite other materials: the dilation was already reduced by
    # the solid, so the film at most touches them, and the overlay is only
    # built when a predicate says it does more than touch.
    others = [
        g for m, g in slab.regions.items()
        if m is not material and new.intersects(g) and not new.touches(g)
    ]
    if others:
        region = state.clean(region.difference(shapely.unary_union(others)))
    if region.is_empty:
        slab.regions.pop(material, None)
    else:
        slab.regions[material] = region
