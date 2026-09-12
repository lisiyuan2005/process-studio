"""Wet (isotropic) etch: every exposed surface of a target recedes by the
etch depth in all directions.

Definition: the etch front is the boundary of the *void* (the space inside
the device window that no material occupies, plus everything above the top
surface) and advances into each target m at its rate, so that after the
etch time every point of m within ``d_m`` (rate x time) of the void *along
a path through void and targets* is removed. Materials that are not
targets are barriers: the front cannot pass through them, but it does
creep around them (undercut). The four window boundaries and the device
floor are not surfaces: outside the window the wafer continues, and below
the floor sits the inert substrate, so neither is void and neither starts
an etch.

The front is advanced in steps: the first dilates the live void by a
small radius and removes what that reaches of each target; every later
step dilates only the void the previous step created, since everything
within a step of the older void is already gone and the barriers do not
move, so the front only ever starts from the surface it just exposed. A step is smaller
than half the thinnest barrier layer, so the dilation cannot jump over
one, and a target uncovered during the etch is etched for the remaining
time. Each step re-samples the curved front, so the profile error grows
with the number of steps (about one ``conformal_resolution`` per step);
with a single target and no barrier one exact step is used.

A barrier layer is a run of consecutive slabs whose non-target footprint
is the same, that borders void or a target above or below: a 25 nm oxide
between two sacrificial nitrides is a 25 nm barrier however finely the
sampling planes of a film elsewhere have split it into slabs, and a film
liner that continues into the slab above and below is no barrier at all.

The slabs are only harmonised (rings noded across slabs, seams removed)
once, after the last step: between steps each slab is a clean polygon
set of its own, which is all the next step reads.

With a mask, the void that exists *before* the etch and lies outside the
mask column is covered and never starts the etch (the mask is a vertical
projection, like every mask in this library); void created by the etch
itself is always live, so the front creeps on under the mask edge.

Exact 2D evaluation of one step at a height z
---------------------------------------------
Void is piecewise constant in z (slabs). For a void slab k with XY region
V_k at vertical distance dz_k from z (0 if z lies inside it), the union of
the ball slices over the slab's heights is one disc of radius
``sqrt(r^2 - dz_k^2)``, so with the step radius r_m of material m

    reach_m(z)   = U_k buffer(V_k, sqrt(r_m^2 - dz_k^2))   over void slabs within +-r_m of z
    removed_m(z) = reach_m(z) & m(z)

Z sampling follows the conformal deposition: within ``d`` of any plane the
profile curves and is sampled at ``conformal_resolution``, elsewhere one
sample per interval. Each sample becomes one slab.
"""

from __future__ import annotations

import math

import shapely
from shapely.geometry import MultiPolygon, box

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material
from .conformal import _nearly_same, _quad_segs, _sample_intervals


MIN_STEPS = 4


def etch_isotropic(
    state: ProcessState,
    depths: dict[Material, float],
    resolution: float,
    opening: MultiPolygon | None = None,
    xy_resolution: float | None = None,
) -> dict[Material, float]:
    """Etch each target by its depth from every exposed surface; returns the
    removed volume per target. ``xy_resolution`` is the XY arc sagitta,
    defaulting to ``resolution`` (see :func:`deposit_conformal`)."""
    depths = {m: float(d) for m, d in depths.items() if float(d) > 0}
    if not depths:
        raise ProcessError("wet etch needs at least one material with a positive depth")
    if not resolution > 0:
        raise ProcessError("conformal_resolution must be positive")
    xy = resolution if xy_resolution is None else float(xy_resolution)
    if not xy > 0:
        raise ProcessError("xy_resolution must be positive")
    before = {m: state.volume(m) for m in depths}
    if state.floor is None or all(state.volume(m) == 0.0 for m in depths):
        return {m: 0.0 for m in depths}

    d_max = max(depths.values())
    # a step must not jump over a barrier: half the thinnest barrier layer
    barrier = _barrier_thickness(state, depths)
    if math.isinf(barrier) and len(depths) == 1:
        n_steps = 1  # nothing to creep around, nothing to uncover: one exact dilation
    else:
        step = min(d_max / MIN_STEPS, barrier / 2)
        n_steps = max(MIN_STEPS, math.ceil(d_max / step - 1e-9))
    front = _live_void(state, _covered(state, opening))
    per_step = {m: d / n_steps for m, d in depths.items()}
    for _ in range(n_steps):
        if not front:
            break  # nothing was exposed last step, so nothing more can be reached
        front = _step(state, per_step, resolution, front, xy)
    _yield_to_barriers(state, depths)
    state.harmonize()
    state.consolidate()
    state.validate()
    return {m: before[m] - state.volume(m) for m in depths}


def _barrier_thickness(state: ProcessState, depths: dict[Material, float]) -> float:
    """The thinnest layer the front must not jump over, in z.

    Consecutive slabs with the same non-target footprint are one layer. A
    layer counts when void or a target lies against that footprint above
    or below it; a layer wrapped in non-target on both sides separates
    nothing the etch could reach.
    """
    slabs = state.slabs
    if not slabs:
        return math.inf
    window = box(*state.bounds)
    footprints: list[MultiPolygon] = []
    for s in slabs:
        parts = [g for m, g in s.regions.items() if m not in depths]
        footprints.append(P.as_multipolygon(shapely.unary_union(parts)) if parts else P.EMPTY)
    # what is open (void or target) at each slab, and above the stack
    open_at = [state.clean(window.difference(f)) if not f.is_empty else state.clean(window) for f in footprints]
    eps = state.grid * state.grid
    thinnest = math.inf
    index = 0
    while index < len(slabs):
        if footprints[index].is_empty:
            index += 1
            continue
        end = index
        while end + 1 < len(slabs) and P.equals(footprints[end + 1], footprints[index]):
            end += 1
        footprint = footprints[index]
        below = open_at[index - 1] if index > 0 else P.EMPTY  # the floor is inert
        above = open_at[end + 1] if end + 1 < len(slabs) else window  # void above the top
        borders_open = (
            footprint.intersection(below).area > eps or footprint.intersection(above).area > eps
        )
        if borders_open:
            thinnest = min(thinnest, znorm(slabs[end].z1 - slabs[index].z0))
        index = end + 1
    return thinnest


def _yield_to_barriers(state: ProcessState, depths: dict[Material, float]) -> None:
    """Any sliver a step's arithmetic left of a target inside a barrier goes.

    The steps only ever shrink the targets, so an overlap can only be a
    target that was not cut back cleanly; the barrier keeps its shape.
    """
    for s in state.slabs:
        others = [g for m, g in s.regions.items() if m not in depths]
        if not others:
            continue
        blocked = shapely.unary_union(others)
        for m in list(s.regions):
            if m not in depths:
                continue
            region = s.regions[m]
            if not region.intersects(blocked):
                continue
            left = state.clean(region.difference(blocked))
            if left.is_empty:
                del s.regions[m]
            else:
                s.regions[m] = left


def _covered(state: ProcessState, opening) -> list[tuple[float, float, MultiPolygon]]:
    """Void present before the etch that the mask covers: (z0, z1, region),
    including the half-space above the initial top outside the opening."""
    if opening is None:
        return []
    window = box(*state.bounds)
    out = []
    for s in state.slabs:
        v = state.clean(window.difference(s.occupied()).difference(opening))
        if not v.is_empty:
            out.append((s.z0, s.z1, v))
    above = state.clean(window.difference(opening))
    if not above.is_empty:
        out.append((state.top, math.inf, above))
    return out


Front = list[tuple[float, float, MultiPolygon]]  # (z0, z1, region) pieces of void


def _live_void(state: ProcessState, covered) -> Front:
    """The void the etch starts from: everything empty that the mask does
    not cover, slab by slab, plus the half-space above the top."""
    if state.floor is None:
        return []
    window = box(*state.bounds)

    def uncover(z0: float, z1: float, region: MultiPolygon) -> MultiPolygon:
        hidden = [c for c0, c1, c in covered if c0 < z1 and c1 > z0]
        if not hidden or region.is_empty:
            return region
        return state.clean(region.difference(shapely.unary_union(hidden)))

    voids: Front = []
    for s in state.slabs:
        v = uncover(s.z0, s.z1, state.clean(window.difference(s.occupied())))
        if not v.is_empty:
            voids.append((s.z0, s.z1, v))
    above = uncover(state.top, math.inf, state.clean(window))
    if not above.is_empty:
        voids.append((state.top, math.inf, above))
    return voids


def _step(
    state: ProcessState, depths: dict[Material, float], resolution: float, front: Front, xy: float
) -> Front:
    """Advance the front by the (small) per-material depths from ``front``,
    the void to dilate; returns the void this step created, which is all
    the next step needs to dilate."""
    if state.floor is None or all(state.volume(m) == 0.0 for m in depths):
        return []
    floor, top = state.floor, state.top
    d_max = max(depths.values())
    # Only heights within reach of the front can change, and the reach only
    # curves near the front pieces' own top and bottom planes.
    lo = max(floor, min(z0 for z0, _z1, _v in front) - d_max)
    hi = min(top, max(min(z1, top) for _z0, z1, _v in front) + d_max)
    if hi <= lo:
        return []
    planes = sorted({lo, hi} | {znorm(z) for z0, z1, _v in front for z in (z0, z1) if lo <= z <= hi})
    samples = _sample_intervals(planes, lo, hi, d_max, min(resolution, d_max / 4))
    segs = {m: _quad_segs(d, xy) for m, d in depths.items()}
    merge_tol = resolution / 4

    removed: dict[Material, list[tuple[float, float, MultiPolygon]]] = {m: [] for m in depths}
    previous: dict[Material, MultiPolygon | None] = {m: None for m in depths}
    for za, zb in samples:
        zm = (za + zb) / 2
        for m, d in depths.items():
            parts = []
            for z0, z1, v in front:
                if z1 <= zm - d or z0 >= zm + d:
                    continue
                dz = 0.0 if z0 <= zm < z1 else (z0 - zm if zm < z0 else zm - z1)
                r = math.sqrt(max(d * d - dz * dz, 0.0))
                parts.append(v.buffer(r, quad_segs=segs[m], join_style="round") if r > 0 else v)
            if not parts:
                previous[m] = None
                continue
            reach = shapely.unary_union(parts)
            prev = previous[m]
            reach = P.as_multipolygon(reach)
            if prev is not None and not reach.is_empty and _nearly_same(prev, reach, merge_tol):
                reach = prev  # consecutive samples within tolerance share one ring (no slivers)
            previous[m] = reach if not reach.is_empty else None
            if not reach.is_empty:
                removed[m].append((za, zb, reach))

    created: Front = []
    for m, pieces in removed.items():
        for za, zb, reach in pieces:
            state.split_at(za)
            state.split_at(zb)
            for slab in state.slabs_between(za, zb):
                region = slab.regions.get(m)
                if region is None:
                    continue
                left = state.clean(region.difference(reach))
                if P.equals(left, region):
                    continue
                gone = state.clean(region.difference(left)) if not left.is_empty else region
                if left.is_empty:
                    del slab.regions[m]
                else:
                    slab.regions[m] = left
                if not gone.is_empty:
                    created.append((slab.z0, slab.z1, gone))
    # No harmonise here: the next step reads each slab on its own, and one
    # harmonise at the end of the etch settles every slab together.
    return created
