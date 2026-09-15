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
move, so the front only ever starts from the surface it just exposed. Each
dilated front is clipped by non-target materials and only its components
connected to the live void are kept, so it cannot jump through a thin
lateral or vertical barrier. A target uncovered during the etch is etched
for the remaining time. Each step re-samples the curved front, so the
profile error grows with the number of steps (about one
``conformal_resolution`` per step); with a single target and no barrier one
exact step is used.

A barrier layer is a run of consecutive slabs whose non-target footprint
is the same, that borders void or a target above or below: a 25 nm oxide
between two sacrificial nitrides is a 25 nm barrier however finely the
sampling planes of a film elsewhere have split it into slabs, and a film
liner that continues into the slab above and below is no barrier at all.

The slabs are only harmonised (rings noded across slabs, seams removed)
once, after the last step: between steps each slab is a clean polygon
set of its own, which is all the next step reads.

What a step passes on is the *void* it created, folded back to regions:
it cuts the stack at every z sample it takes, so the pieces arrive one
per slab and the same region comes back dozens of times over. A piece
costs a buffer at every sample within reach of it and its two z planes
are places the reach may kink, so the sampling around them is fine --
a front that grows with the sampling makes the etch quadratic in it.
:func:`_merge_front` joins the pieces back; see its note for why that
changes no geometry.

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
from ..cancellation import check_cancelled
from ..exceptions import ProcessError
from ..material import Material
from .conformal import _nearly_same, _quad_segs, _sample_intervals


MIN_STEPS = 4

#: Two z intervals this close are touching (the z coordinates are rounded
#: to Z_DECIMALS, so anything smaller is the same plane).
Z_TOUCH = 1e-9


def etch_isotropic(
    state: ProcessState,
    depths: dict[Material, float],
    resolution: float,
    opening: MultiPolygon | None = None,
    xy_resolution: float | None = None,
    *,
    square: bool = False,
) -> dict[Material, float]:
    """Etch each target by its depth from every exposed surface; returns the
    removed volume per target. ``xy_resolution`` is the XY arc sagitta,
    defaulting to ``resolution`` (see :func:`deposit_conformal``).

    ``square`` is the simplified front: the void grows by the depth
    sideways and vertically alike (a box instead of a ball), so the etched
    outline has square corners in z and is constant between the planes of
    the stack and those planes shifted by the depth. One sample per such
    interval is then exact, which makes a step cost a few offsets instead
    of one per resolution step."""
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
    front = _merge_front(_live_void(state, _covered(state, opening)))
    per_step = {m: d / n_steps for m, d in depths.items()}
    mark = state.regions_mark()
    blocker_cache: dict = {}
    for _ in range(n_steps):
        check_cancelled()
        if not front:
            break  # nothing was exposed last step, so nothing more can be reached
        front = _step(
            state, per_step, resolution, front, xy, square=square, blocker_cache=blocker_cache
        )
    _yield_to_barriers(state, depths)
    state.harmonize(state.changed_since(mark))
    state.consolidate()
    state.validate()
    return {m: before[m] - state.volume(m) for m in depths}


def _barrier_thickness(state: ProcessState, depths: dict[Material, float]) -> float:
    """The thinnest layer the front must not jump over, in z.

    Consecutive slabs whose non-target footprints mostly overlap are one
    layer: the sampling slabs of a flat oxide have the same footprint, and
    the slabs of a film's rounded corner shift by a fraction of the film's
    width from one to the next. A layer counts when void or a target lies
    against its footprint above or below it; a layer wrapped in non-target
    on both sides separates nothing the etch could reach.
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
        while end + 1 < len(slabs) and _mostly_overlap(footprints[end], footprints[end + 1]):
            end += 1
        footprint = P.as_multipolygon(shapely.unary_union(footprints[index : end + 1]))
        below = open_at[index - 1] if index > 0 else P.EMPTY  # the floor is inert
        above = open_at[end + 1] if end + 1 < len(slabs) else window  # void above the top
        borders_open = (
            footprint.intersection(below).area > eps or footprint.intersection(above).area > eps
        )
        if borders_open:
            thinnest = min(thinnest, znorm(slabs[end].z1 - slabs[index].z0))
        index = end + 1
    return thinnest


def _mostly_overlap(a: MultiPolygon, b: MultiPolygon) -> bool:
    if a.is_empty or b.is_empty:
        return False
    if a is b or a.equals(b):
        return True
    return a.intersection(b).area >= 0.5 * max(a.area, b.area)


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


def _merge_front(front: Front) -> Front:
    """Fold pieces that share a region and touch in z into one tall piece.

    The front arrives cut into slabs -- one per sample plane the step
    split, one per slab of the stack -- and most neighbours carry the very
    same polygon, because the reach is deliberately shared between
    neighbouring samples and because a void column spans a run of slabs.
    A step of an etch through a stack routinely hands on 156 pieces that
    are four.

    Joining them changes nothing. The reach of a piece at a sample is its
    region buffered by ``sqrt(r^2 - dz^2)``, with ``dz`` the distance from
    the sample to the piece's z interval, and the union of two touching
    intervals is at the distance of the nearer one: the taller piece
    buffers the same region by the larger of the two radii, which is what
    the union of the two reaches already was. The square front is the same
    argument with ``d`` in place of the root.

    It is worth a great deal: each piece costs one buffer at every sample
    within reach of it, and each piece's two z planes are a place the
    reach may kink, so the samples are taken finely around them. Merging
    cuts both -- the buffers by the fold, the samples with the planes that
    were never boundaries of anything.
    """
    if len(front) < 2:
        return front
    # Identical rings, not merely equal areas: the pieces to fold are the
    # same polygon handed back over and over, so this is a dict lookup.
    spans: dict[bytes, list[float]] = {}
    regions: dict[bytes, MultiPolygon] = {}
    for z0, z1, v in front:
        key = shapely.to_wkb(v)
        spans.setdefault(key, []).append((z0, z1))
        regions.setdefault(key, v)
    merged: Front = []
    for key, pieces in spans.items():
        region = regions[key]
        pieces.sort()
        z0, z1 = pieces[0]
        for a, b in pieces[1:]:
            if a <= z1 + Z_TOUCH:
                z1 = max(z1, b)
            else:
                merged.append((z0, z1, region))
                z0, z1 = a, b
        merged.append((z0, z1, region))
    return merged


def _live_void(state: ProcessState, covered) -> Front:
    """Void connected to the ambient through the top of the stack.

    Empty space in a sealed cavity is not an etchant source. The mask may
    cover part of both the internal void and the half-space above the stack,
    so connectivity is evaluated after applying it.
    """
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
        voids = _connected_to_sources(
            voids,
            [(state.top, math.inf, above)],
            state.grid * state.grid,
        )
        voids.append((state.top, math.inf, above))
    else:
        voids = []
    return voids


def _open_overlap(a, b, area_eps: float) -> bool:
    """Whether two XY regions share a finite opening, not only an edge."""
    if _apart(a.bounds, b.bounds) or not a.intersects(b):
        return False
    return a.intersection(b).area > area_eps


def _connected_to_sources(pieces: Front, sources: Front, area_eps: float) -> Front:
    """Keep the 3-D components of ``pieces`` connected to ``sources``.

    Every piece is constant in one Z interval. Its polygon components are
    graph nodes; components in touching Z intervals are joined when their
    horizontal-face intersection has finite area. This is the exact
    connectivity test for a slab model and needs no voxel grid or optional
    numerical dependency.
    """
    if not pieces or not sources:
        return []

    layers: list[tuple[float, float, MultiPolygon, list]] = []
    for z0, z1, region in pieces:
        components = list(region.geoms)
        if components:
            layers.append((z0, z1, region, components))
    if not layers:
        return []

    offsets: list[int] = []
    count = 0
    for _z0, _z1, _region, components in layers:
        offsets.append(count)
        count += len(components)
    parent = list(range(count))
    rank = [0] * count

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a == b:
            return
        if rank[a] < rank[b]:
            a, b = b, a
        parent[b] = a
        if rank[a] == rank[b]:
            rank[a] += 1

    for index in range(len(layers) - 1):
        _za, zb, _region_a, components_a = layers[index]
        zc, _zd, _region_b, components_b = layers[index + 1]
        if abs(zb - zc) > Z_TOUCH:
            continue
        for ia, a in enumerate(components_a):
            for ib, b in enumerate(components_b):
                if _open_overlap(a, b, area_eps):
                    union(offsets[index] + ia, offsets[index + 1] + ib)

    seeded: set[int] = set()
    for index, (za, zb, _region, components) in enumerate(layers):
        touching = [
            region
            for z0, z1, region in sources
            if z0 <= zb + Z_TOUCH and z1 >= za - Z_TOUCH
        ]
        if not touching:
            continue
        source = touching[0] if len(touching) == 1 else shapely.unary_union(touching)
        for component_index, component in enumerate(components):
            if _open_overlap(component, source, area_eps):
                seeded.add(find(offsets[index] + component_index))

    if not seeded:
        return []
    seeded = {find(root) for root in seeded}

    connected: Front = []
    for index, (z0, z1, original, components) in enumerate(layers):
        kept = [
            component
            for component_index, component in enumerate(components)
            if find(offsets[index] + component_index) in seeded
        ]
        if not kept:
            continue
        if len(kept) == len(components):
            region = original
        else:
            region = P.as_multipolygon(shapely.unary_union(kept))
        connected.append((z0, z1, region))
    return connected


def _accessible_reach(
    state: ProcessState,
    pieces: Front,
    front: Front,
    blockers: dict[tuple[float, float], MultiPolygon],
) -> Front:
    """Remove barriers from a dilated front and reject enclosed components.

    This is a fast no-op when no blocker intersects the reach, preserving
    the exact one-step path for the common single-material case.
    """
    if not blockers:
        return pieces
    clipped: Front = []
    changed = False
    for za, zb, reach in pieces:
        planes = [za, *(z for z in state.z_planes if za < z < zb), zb]
        for z0, z1 in zip(planes, planes[1:]):
            slab = state.slab_at((z0 + z1) / 2)
            blocker = P.EMPTY if slab is None else blockers.get((slab.z0, slab.z1), P.EMPTY)
            if (
                blocker.is_empty
                or _apart(reach.bounds, blocker.bounds)
                or not reach.intersects(blocker)
            ):
                clipped.append((z0, z1, reach))
                continue
            changed = True
            passable = P.as_multipolygon(reach.difference(blocker))
            if not passable.is_empty:
                clipped.append((z0, z1, passable))
    if not changed:
        return pieces
    return _connected_to_sources(clipped, front, state.grid * state.grid)


def _fold_runs(pieces: list[tuple[float, float, MultiPolygon]]) -> list[tuple[float, float, MultiPolygon]]:
    """Join neighbouring samples that share one reach into a single span."""
    folded: list[tuple[float, float, MultiPolygon]] = []
    for za, zb, reach in pieces:
        if folded and folded[-1][2] is reach and abs(folded[-1][1] - za) <= Z_TOUCH:
            folded[-1] = (folded[-1][0], zb, reach)
        else:
            folded.append((za, zb, reach))
    return folded


def _apart(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """True if the two XY bounding boxes do not overlap."""
    return a[0] > b[2] or b[0] > a[2] or a[1] > b[3] or b[1] > a[3]


def _blocker_union(parts: list[MultiPolygon], cache: dict) -> MultiPolygon:
    """The union of a slab's impermeable regions, made once per set of them.

    Keyed by the regions themselves rather than their ids: a freed
    geometry's address is handed straight to the next one, which would
    serve the wrong union.
    """
    key = tuple(id(part) for part in parts)
    held = cache.get(key)
    if held is not None and len(held[0]) == len(parts) and all(
        a is b for a, b in zip(held[0], parts)
    ):
        return held[1]
    union = P.as_multipolygon(shapely.unary_union(parts))
    cache[key] = (tuple(parts), union)
    return union


def _step(
    state: ProcessState,
    depths: dict[Material, float],
    resolution: float,
    front: Front,
    xy: float,
    *,
    square: bool = False,
    blocker_cache: dict | None = None,
) -> Front:
    """Advance the front by the (small) per-material depths from ``front``,
    the void to dilate; returns the void this step created, which is all
    the next step needs to dilate."""
    if state.floor is None or all(state.volume(m) == 0.0 for m in depths):
        return []
    if blocker_cache is None:
        blocker_cache = {}
    floor, top = state.floor, state.top
    d_max = max(depths.values())
    # Only heights within reach of the front can change, and the reach only
    # curves near the front pieces' own top and bottom planes.
    lo = max(floor, min(z0 for z0, _z1, _v in front) - d_max)
    hi = min(top, max(min(z1, top) for _z0, z1, _v in front) + d_max)
    if hi <= lo:
        return []
    planes = sorted({lo, hi} | {znorm(z) for z0, z1, _v in front for z in (z0, z1) if lo <= z <= hi})
    # The square front is constant between the planes and the planes ± d:
    # one sample per interval; the round one curves there and is sampled.
    samples = _sample_intervals(
        planes, lo, hi, d_max, math.inf if square else min(resolution, d_max / 4),
        offsets=set(depths.values()),
    )
    segs = {m: _quad_segs(d, xy) for m, d in depths.items()}
    merge_tol = resolution / 4

    # Non-target materials are impermeable. What blocks a slab never moves
    # while the etch runs -- only targets are cut back, and splitting a
    # slab hands both halves the regions the whole had -- so the union is
    # made once for a given set of regions and reused for every step and
    # every slab that still has those same regions. Recomputing it per
    # step was the second most expensive thing in a stack etch.
    blockers: dict[tuple[float, float], MultiPolygon] = {}
    for slab in state.slabs:
        parts = [region for material, region in slab.regions.items() if material not in depths]
        if parts:
            blockers[(slab.z0, slab.z1)] = _blocker_union(parts, blocker_cache)

    removed: dict[Material, list[tuple[float, float, MultiPolygon]]] = {m: [] for m in depths}
    previous: dict[Material, MultiPolygon | None] = {m: None for m in depths}
    for za, zb in samples:
        check_cancelled()
        zm = (za + zb) / 2
        for m, d in depths.items():
            parts = []
            for z0, z1, v in front:
                if z1 <= zm - d or z0 >= zm + d:
                    continue
                dz = 0.0 if z0 <= zm < z1 else (z0 - zm if zm < z0 else zm - z1)
                if square:
                    r = d if dz < d else 0.0
                else:
                    r = math.sqrt(max(d * d - dz * dz, 0.0))
                parts.append(v.buffer(r, quad_segs=segs[m], join_style="round") if r > 0 else v)
            if not parts:
                previous[m] = None
                continue
            reach = shapely.unary_union(parts)
            prev = previous[m]
            reach = P.as_multipolygon(reach)
            if prev is not None and not reach.is_empty:
                # Consecutive samples within tolerance share one ring (no
                # slivers). The box front is exact per interval: only an
                # identical reach is shared, and that is settled cheaply.
                if square:
                    if P.equals(prev, reach):
                        reach = prev
                elif _nearly_same(prev, reach, merge_tol):
                    reach = prev
            previous[m] = reach if not reach.is_empty else None
            if not reach.is_empty:
                removed[m].append((za, zb, reach))

    created: Front = []
    for m, pieces in removed.items():
        pieces = _accessible_reach(state, pieces, front, blockers)
        # Neighbouring samples within the merge tolerance were deliberately
        # given the same reach, ring for ring. Cutting it out of [za, zb]
        # and then out of [zb, zc] is cutting it out of [za, zc], so the
        # run is applied once: one pair of splits and one difference per
        # slab instead of one per sample. At a fine resolution most of a
        # step's samples fall into a handful of runs.
        for za, zb, reach in _fold_runs(pieces):
            state.split_at(za)
            state.split_at(zb)
            reach_bounds = reach.bounds
            for slab in state.slabs_between(za, zb):
                region = slab.regions.get(m)
                if region is None:
                    continue
                if _apart(region.bounds, reach_bounds):
                    continue  # nothing of this material is within reach
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
    # harmonise at the end of the etch settles every slab together. The
    # sample planes did split slabs, though, and most halves came out the
    # same: merge them back, or the slabs multiply with every step.
    state.consolidate()
    return _merge_front(created)
