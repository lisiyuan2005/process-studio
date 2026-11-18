"""Dense signed-distance operations behind the OpenVDB kernel.

The state keeps one narrow-band level set per material in OpenVDB grids
(see :mod:`.state`). A step loads the materials it needs into dense numpy
blocks, works on those, and writes the result back. Everything here works
on such blocks: arrays indexed ``[i, j, k]`` (x, y, z), negative inside,
with the window padded sideways by a margin that continues the wafer
(edge replication: a closed window, whose walls are not surfaces) or wraps
it (periodic).

The two film models are two structuring elements:

* **detailed** -- a ball: the exact Euclidean offset, from the surface mesh
  through OpenVDB (:func:`ball_dilate`), so corners are round with the
  film's radius;
* **simplified** -- a cylinder: round in XY, square in z
  (:func:`cylinder_dilate`), the same film the slab kernel's simplified
  model grows: wall films end flat at the top and bottom of the wall, faces
  move by the thickness with square corners.

Both return a function whose negative region is exactly the dilated set and
whose positive values are the Euclidean distance to it, so the result is a
proper level set again with its surfaces placed below the voxel size.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, Mapping

import numpy as np
from scipy import ndimage

#: Half-width of the narrow band, in voxels. Values beyond it are clamped.
HALF_WIDTH = 3.0


def pad_xy(array: np.ndarray, margin: int, periodic: bool) -> np.ndarray:
    """The window with ``margin`` voxels of wafer on every side."""
    if margin <= 0:
        return array
    return np.pad(array, ((margin, margin), (margin, margin), (0, 0)), mode="wrap" if periodic else "edge")


def shifted(array: np.ndarray, di: int, dj: int, dk: int = 0) -> np.ndarray:
    """``array`` read at index + (di, dj, dk), edges continued."""
    if di == 0 and dj == 0 and dk == 0:
        return array
    pad = max(abs(di), abs(dj), abs(dk))
    big = np.pad(array, ((abs(di),) * 2, (abs(dj),) * 2, (abs(dk),) * 2), mode="edge") if pad else array
    nx, ny, nz = array.shape
    return big[abs(di) + di: abs(di) + di + nx, abs(dj) + dj: abs(dj) + dj + ny, abs(dk) + dk: abs(dk) + dk + nz]


def _bilinear_xy(padded: np.ndarray, pad: int, shape, fx: float, fy: float) -> np.ndarray:
    """``padded`` (padded by ``pad`` in x and y) read at index + (fx, fy)."""
    nx, ny = shape[0], shape[1]
    ix, iy = math.floor(fx), math.floor(fy)
    wx, wy = fx - ix, fy - iy
    out = None
    for ox, oy, w in ((0, 0, (1 - wx) * (1 - wy)), (1, 0, wx * (1 - wy)), (0, 1, (1 - wx) * wy), (1, 1, wx * wy)):
        if w <= 1e-9:
            continue
        a = pad + ix + ox
        b = pad + iy + oy
        part = padded[a: a + nx, b: b + ny, :]
        out = part * np.float32(w) if out is None else out + part * np.float32(w)
    return out


def disc_min(phi: np.ndarray, radius_voxels: float) -> np.ndarray:
    """min of ``phi`` over a horizontal disc of the given radius round each
    point: the disc's rim, sampled finely enough that the chord misses the
    arc by an eighth of a voxel, and its centre.

    For a distance function the minimum over a disc that just touches the
    set lies on its rim, so near the dilated surface this is exact up to
    the chord; deep inside the set it is merely negative, which is all the
    sign needs.
    """
    r = float(radius_voxels)
    if r <= 1e-9:
        return phi.copy()
    # sagitta r (1 - cos(pi / n)) <= 1/8 voxel
    n = max(8, math.ceil(math.pi / math.acos(max(-1.0, 1.0 - 0.125 / r))))
    # an even count, so every direction has its opposite and the disc is
    # the same whichever way round it is read
    n += n % 2
    pad = math.ceil(r) + 2
    padded = np.pad(phi, ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    out = phi.copy()
    for index in range(n):
        angle = 2.0 * math.pi * index / n
        sample = _bilinear_xy(padded, pad, phi.shape, r * math.cos(angle), r * math.sin(angle))
        np.minimum(out, sample, out=out)
    return out


def segment_min(phi: np.ndarray, half_voxels: float) -> np.ndarray:
    """min of ``phi`` over the vertical segment [z - h, z + h] round each
    point, with ``phi`` linear between voxel centres: the lattice nodes in
    the segment and its two ends."""
    h = float(half_voxels)
    if h <= 1e-9:
        return phi.copy()
    whole = math.floor(h + 1e-9)
    out = ndimage.minimum_filter1d(phi, size=2 * whole + 1, axis=2, mode="nearest") if whole else phi.copy()
    frac = h - whole
    if frac > 1e-6:
        for sign in (1, -1):
            low = shifted(phi, 0, 0, sign * whole)
            high = shifted(phi, 0, 0, sign * (whole + 1))
            np.minimum(out, low * np.float32(1 - frac) + high * np.float32(frac), out=out)
    return out


def cylinder_dilate(phi: np.ndarray, thickness: float, g: float) -> np.ndarray:
    """The set grown by a cylinder of radius and half-height ``thickness``:
    the simplified film, round in XY and square in z."""
    return segment_min(disc_min(phi, thickness / g), thickness / g)


def ball_dilate(phi: np.ndarray, thickness: float, frame) -> np.ndarray:
    """The set grown by a ball of radius ``thickness``: the exact Euclidean
    offset, computed by OpenVDB from the surface mesh."""
    from . import _vdbcore as vc

    lo = (-frame.margin, -frame.margin, frame.k0)
    sdf = vc.from_dense(np.ascontiguousarray(phi, dtype=np.float32), lo, frame.g, frame.origin, HALF_WIDTH)
    reach = (frame.margin + HALF_WIDTH + 2) * frame.g + thickness
    grown = vc.offset(sdf, float(thickness), frame.x0, frame.x1, frame.y0, frame.y1, reach, False)
    return np.asarray(vc.to_dense(grown, lo, phi.shape))


def dilate(phi: np.ndarray, thickness: float, frame, square: bool) -> np.ndarray:
    """``phi`` grown by ``thickness``, with the ball or the cylinder.

    Only the layers with a surface in them, and those within the thickness
    of one, can change: a layer all inside stays inside and one far above
    stays outside. So the work is done on that run of layers alone, which
    for a film on a flat stack is a few dozen layers of several hundred.
    """
    g = frame.g
    # Layers with open space and solid in them, and the two either side of
    # where a layer all solid meets one all open. Not "small values": the
    # union of several materials is small all along their shared faces.
    has_open = (phi > 0).any(axis=(0, 1))
    has_solid = (phi <= 0).any(axis=(0, 1))
    mixed = has_open & has_solid
    change = np.zeros_like(mixed)
    flips = np.nonzero(has_open[1:] != has_open[:-1])[0]
    change[flips] = True
    change[flips + 1] = True
    surface = np.nonzero(mixed | change)[0]
    if surface.size == 0:
        return phi.copy()
    pad = int(math.ceil(thickness / g)) + int(HALF_WIDTH) + 2
    low = max(0, int(surface[0]) - pad)
    high = min(phi.shape[2], int(surface[-1]) + pad + 1)
    part = np.ascontiguousarray(phi[:, :, low:high])
    if square:
        grown = cylinder_dilate(part, thickness, g)
    else:
        grown = ball_dilate(part, thickness, _Shifted(frame, low))
    out = phi.copy()
    out[:, :, low:high] = grown
    return out


class _Shifted:
    """A block frame starting ``low`` layers higher."""

    def __init__(self, frame, low: int) -> None:
        self._frame = frame
        self.k0 = frame.k0 + low

    def __getattr__(self, name):
        return getattr(self._frame, name)


def shadow(solid: np.ndarray) -> np.ndarray:
    """Negative where solid lies at or anywhere above the point in its
    column: the downward extrusion of the solid. Its positive part is the
    space the sky sees, and its zero set sits on the solid's walls where
    they are, not on the voxel between."""
    return np.minimum.accumulate(solid[:, :, ::-1], axis=2)[:, :, ::-1]


def union(fields: Iterable[np.ndarray], like: np.ndarray | None = None, background: float = 1.0) -> np.ndarray:
    out = None
    for field in fields:
        out = field.copy() if out is None else np.minimum(out, field, out=out)
    if out is None:
        if like is None:
            raise ValueError("the union of nothing needs a shape")
        out = np.full_like(like, background)
    return out


def solid_field(fields: Iterable[np.ndarray], g: float, like: np.ndarray | None = None) -> np.ndarray:
    """The level set of all the materials together: negative in any of them.

    The plain minimum of the materials' fields has the right sign and, in
    the open, the right distance; but inside, next to the face two
    materials share, it is only the distance to that face -- nearly zero
    where the face meets the open surface. Interpolated there it puts the
    surface up to a third of a voxel into the solid: a groove along every
    line where two materials and the open space meet, which a film then
    follows and a picture shows as a crack. Inside points next to the open
    space and to such a face therefore take their distance from the
    outside instead: every open neighbour ``q`` is the centre of a ball of
    radius ``phi(q)`` with nothing solid in it, so the open space is no
    farther than ``|p - q| - phi(q)``, which is exact for a flat surface.
    """
    fields = list(fields)
    if not fields:
        if like is None:
            raise ValueError("the union of nothing needs a shape")
        return np.full_like(like, HALF_WIDTH * g)
    lowest = fields[0].copy()
    second = np.full_like(lowest, np.inf)
    for field in fields[1:]:
        np.minimum(second, np.maximum(lowest, field), out=second)
        np.minimum(lowest, field, out=lowest)
    open_ = lowest > 0
    candidates = (lowest <= 0) & (second < 2.0 * g)
    if not candidates.any() or not open_.any():
        return lowest
    candidates &= ndimage.binary_dilation(open_, structure=np.ones((3, 3, 3), dtype=bool))
    points = np.nonzero(candidates)
    if points[0].size == 0:
        return lowest
    shape = lowest.shape
    reach = np.full(points[0].size, np.inf, dtype=np.float64)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for dk in (-1, 0, 1):
                if di == dj == dk == 0:
                    continue
                qi = np.clip(points[0] + di, 0, shape[0] - 1)
                qj = np.clip(points[1] + dj, 0, shape[1] - 1)
                qk = np.clip(points[2] + dk, 0, shape[2] - 1)
                value = lowest[qi, qj, qk]
                bound = np.where(value > 0, g * math.sqrt(di * di + dj * dj + dk * dk) - value, np.inf)
                np.minimum(reach, bound, out=reach)
    fixed = lowest.copy()
    finite = np.isfinite(reach)
    fixed[tuple(axis[finite] for axis in points)] = -np.maximum(reach[finite], 0.0)
    return fixed


# -- connectivity ---------------------------------------------------------


def _label(free: np.ndarray, periodic: bool, margin: int) -> tuple[np.ndarray, int]:
    labels, count = ndimage.label(free)
    if not periodic or count == 0:
        return labels, count
    # join components that meet across the period
    nx, ny = free.shape[0] - 2 * margin, free.shape[1] - 2 * margin
    parent = np.arange(count + 1)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    pairs = []
    a = labels[margin, :, :], labels[margin + nx - 1, :, :]
    pairs.append(a)
    b = labels[:, margin, :], labels[:, margin + ny - 1, :]
    pairs.append(b)
    for left, right in pairs:
        both = (left > 0) & (right > 0)
        for x, y in set(zip(left[both].tolist(), right[both].tolist())):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry
    roots = np.array([find(index) for index in range(count + 1)])
    return roots[labels], count


def reached(
    solid: np.ndarray,
    opening: np.ndarray | None,
    frame,
) -> tuple[np.ndarray, np.ndarray]:
    """The open space an etchant or a gas gets to, and the resist.

    ``opening`` is the mask's signed XY distance (negative inside), or None
    for no mask. Resist is spun on from above: outside the opening it fills
    the space over the wafer and every hole open straight up to it. What
    reaches in through the opening goes on through any open space joined
    to it -- under the covered part too -- but not into the resist. Sealed
    cavities are not reached. Returns (reached, resist) as boolean blocks.
    """
    void = solid > 0
    sky = shadow(solid) > 0
    if opening is None:
        resist = np.zeros_like(void)
        seeds = sky
    else:
        outside = (opening > 0)[:, :, None]
        resist = sky & outside
        seeds = sky & ~outside
    free = void & ~resist
    labels, count = _label(free, frame.periodic, frame.margin)
    if count == 0:
        return np.zeros_like(void), resist
    hit = np.unique(labels[seeds & free])
    hit = hit[hit > 0]
    return np.isin(labels, hit), resist


def skin_distance(solid: np.ndarray, open_space: np.ndarray, g: float, square: bool) -> np.ndarray:
    """Distance from every point to the surface of the solid where it faces
    ``open_space``: Euclidean for the round film, and the cylinder's own
    measure (the larger of the horizontal and the vertical gap) for the
    square one, so a film cut to it keeps its square corners."""
    inside = solid <= 0
    near = np.zeros_like(inside)
    for di, dj, dk in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        near |= shifted(open_space, di, dj, dk)
    skin = inside & near
    if not skin.any():
        return np.full(solid.shape, np.inf, dtype=np.float32)
    _, (qi, qj, qk) = ndimage.distance_transform_edt(~skin, return_distances=True, return_indices=True)
    shape = solid.shape
    pi, pj, pk = np.indices(shape, sparse=True)
    di, dj, dk = (qi - pi) * g, (qj - pj) * g, (qk - pk) * g
    if square:
        gap = np.maximum(np.sqrt(di * di + dj * dj), np.abs(dk))
    else:
        gap = np.sqrt(di * di + dj * dj + dk * dk)
    # The skin voxels sit inside the solid by -solid; the surface is that much nearer.
    return (gap + solid[qi, qj, qk]).astype(np.float32)


# -- vertical etch --------------------------------------------------------


def column_front(
    phis: Mapping[str, np.ndarray],
    costs: Mapping[str, float],
    budget: float,
    frame,
    columns: np.ndarray,
    supersample: int = 8,
) -> np.ndarray:
    """Height of a vertical etch front in each column.

    Going down from the top, a length ``h`` of material ``m`` costs
    ``h * costs[m]`` of the ``budget`` (time); open space costs nothing; a
    material with infinite cost, or none given, stops the column. The profile is read at
    ``supersample`` points per voxel with every level set linear between
    voxel centres, so interfaces are placed below the voxel size. Columns
    not in ``columns`` keep the front above everything (``+inf``).
    """
    nx, ny, nz = next(iter(phis.values())).shape
    names = list(phis)
    unit = np.array([costs.get(name, np.inf) for name in names] + [0.0], dtype=np.float64)
    front = np.full((nx, ny), np.inf)
    steps = (nz - 1) * supersample
    dz = frame.g / supersample
    weights = (np.arange(supersample, dtype=np.float32) + 0.5) / supersample
    rows = np.nonzero(columns.any(axis=1))[0]
    for i in rows:
        js = np.nonzero(columns[i])[0]
        if js.size == 0:
            continue
        # (materials, columns, samples) from the top down
        stack = np.stack([phis[name][i, js, :] for name in names])
        lower, upper = stack[:, :, :-1], stack[:, :, 1:]
        fine = lower[..., None] * (1 - weights) + upper[..., None] * weights  # (m, c, nz-1, s)
        fine = fine.reshape(len(names), js.size, steps)[:, :, ::-1]
        best = fine.min(axis=0)
        which = np.where(best < 0, fine.argmin(axis=0), len(names))
        cost = unit[which] * dz
        spent = np.cumsum(cost, axis=1)
        over = spent > budget
        first = np.where(over.any(axis=1), over.argmax(axis=1), steps)
        before = np.where(first > 0, spent[np.arange(js.size), np.maximum(first - 1, 0)], 0.0)
        before = np.where(first > 0, before, 0.0)
        step_cost = np.where(first < steps, cost[np.arange(js.size), np.minimum(first, steps - 1)], 1.0)
        part = np.where(np.isfinite(step_cost) & (step_cost > 0), (budget - before) / np.where(step_cost > 0, step_cost, 1.0), 0.0)
        part = np.clip(part, 0.0, 1.0)
        # sample index s from the top covers heights [top - (s+1) dz, top - s dz]
        top = frame.z_of(frame.k0 + nz - 1)
        depth = (first + part) * dz
        front[i, js] = np.where(first < steps, top - depth, frame.z_of(frame.k0))
    return front


# -- isotropic fronts -----------------------------------------------------


def travel_time(
    solid: np.ndarray,
    phis: Mapping[str, np.ndarray],
    rates: Mapping[str, float],
    live: np.ndarray,
    blocked: np.ndarray,
    g: float,
) -> np.ndarray:
    """Time for an isotropic front leaving the live open space to reach each
    point, moving at each target's rate through it (fast-marching).

    Non-targets are walls the front goes round, never through; ``blocked``
    (the resist) likewise. Open space that is not live (a sealed cavity) is
    no source, but once reached the front crosses it at once.
    """
    import skfmm

    targets = [name for name, rate in rates.items() if rate > 0 and name in phis]
    speed = np.ones(solid.shape, dtype=np.float64)
    target_mask = np.zeros(solid.shape, dtype=bool)
    fastest = max(rates[name] for name in targets)
    for name in targets:
        inside = phis[name] < 0
        speed[inside] = rates[name]
        target_mask |= inside
    void = solid > 0
    dead = void & ~live & ~blocked
    speed[dead] = 1e3 * fastest
    phi = solid.astype(np.float64).copy()
    phi[dead] = -0.5 * g
    barrier = (~void & ~target_mask) | blocked
    if not (live & ~barrier).any():
        return np.full(solid.shape, np.inf)
    masked = np.ma.MaskedArray(phi, mask=barrier)
    try:
        time = skfmm.travel_time(masked, speed, dx=g, order=1)
    except ValueError:
        return np.full(solid.shape, np.inf)
    return np.ma.filled(time, np.inf)


_OFFSETS = [
    (di, dj, dk)
    for di in (-1, 0, 1)
    for dj in (-1, 0, 1)
    for dk in (-1, 0, 1)
    if (di, dj, dk) != (0, 0, 0)
]


def redistance(phi: np.ndarray, g: float) -> np.ndarray:
    """``phi`` with its inside made a distance again near its zero level.

    The outside of a grown set is already a distance; the inside is only
    the minimum the growth carried in. Points next to the outside take the
    bound every outside neighbour ``q`` gives (``|p - q| - phi(q)``: a ball
    of that radius round ``q`` is all outside), the next two shells their
    neighbours' depth plus the step to them. That is the band a growth of
    up to one and a half voxels reads; deeper points keep the clamp.
    """
    bg = np.float32(HALF_WIDTH * g)
    out = np.clip(phi, -bg, bg).astype(np.float32)
    inside = out < 0
    if not inside.any() or inside.all():
        return out
    shape = out.shape
    # the three shells of inside points nearest the outside; anything
    # deeper is past the band and keeps the clamp
    cross = ndimage.generate_binary_structure(3, 1)
    band = inside & ndimage.binary_dilation(~inside, structure=cross)
    shell = band
    for _ in range(2):
        shell = inside & ~band & ndimage.binary_dilation(shell, structure=cross)
        band |= shell
    out[inside & ~band] = -bg
    points = np.nonzero(band)
    count = points[0].size
    if count == 0:
        return out
    where = np.full(shape, -1, dtype=np.int64)
    where[points] = np.arange(count)
    depth = np.full(count, np.inf)
    neighbours = []
    for di, dj, dk in _OFFSETS:
        qi = np.clip(points[0] + di, 0, shape[0] - 1)
        qj = np.clip(points[1] + dj, 0, shape[1] - 1)
        qk = np.clip(points[2] + dk, 0, shape[2] - 1)
        step = g * math.sqrt(di * di + dj * dj + dk * dk)
        value = out[qi, qj, qk]
        np.minimum(depth, np.where(value > 0, np.maximum(step - value, 0.0), np.inf), out=depth)
        neighbours.append((where[qi, qj, qk], step))
    for _ in range(2):
        for index, step in neighbours:
            known = index >= 0
            candidate = np.full(count, np.inf)
            candidate[known] = depth[index[known]] + step
            np.minimum(depth, candidate, out=depth)
    out[points] = -np.minimum(depth, bg).astype(np.float32)
    return out


def square_front(
    solid: np.ndarray,
    phis: Mapping[str, np.ndarray],
    rates: Mapping[str, float],
    live: np.ndarray,
    duration: float,
    g: float,
    should_cancel: Callable[[], None] | None = None,
) -> dict[str, np.ndarray]:
    """The simplified (square) isotropic front. Returns, per target, the
    region it took as a level set.

    The live space grows into each target by a cylinder -- round in XY,
    square in z, the box-cornered front of the slab kernel's simplified
    model -- in steps of at most one and a half voxels at each target's
    rate, so it goes round a barrier rather than through it. Between steps
    the front is measured again (:func:`redistance`): a growth is exact only
    of a distance field. Each step works only where the front meets a
    target it has not taken yet, a few voxels round each such place: in a
    long lateral etch that is a thin sheet at the head of every channel.
    """
    targets = [name for name, rate in rates.items() if rate > 0 and name in phis]
    fastest = max(rates[name] for name in targets)
    count = max(1, math.ceil(fastest * duration / (1.5 * g) - 1e-9))
    dt = duration / count
    bg = np.float32(HALF_WIDTH * g)
    space = np.where(live | (solid <= 0), solid, -bg).astype(np.float32)
    front = -redistance(space, g)
    lost = {name: np.full_like(solid, bg) for name in targets}
    inside = {name: phis[name] < 0 for name in targets}
    largest = fastest * dt
    pad = int(math.ceil(largest / g)) + 4
    shape = front.shape
    for _ in range(count):
        if should_cancel is not None:
            should_cancel()
        left = np.zeros(shape, dtype=bool)
        for name in targets:
            left |= inside[name] & (lost[name] > 0)
        leading = left & (front < largest + 1.5 * g)
        if not leading.any():
            break
        labels, _ = ndimage.label(leading, structure=np.ones((3, 3, 3), dtype=bool))
        boxes = []
        for found in ndimage.find_objects(labels):
            if found is None:
                continue
            boxes.append(tuple(
                slice(max(0, part.start - pad), min(size, part.stop + pad))
                for part, size in zip(found, shape)
            ))
        new_front = front.copy()
        for box in boxes:
            local = front[box]
            grown_by: dict[float, np.ndarray] = {}
            here = new_front[box]
            for name in targets:
                radius = rates[name] * dt
                if radius not in grown_by:
                    grown_by[radius] = cylinder_dilate(local, radius, g)
                # Against the whole target, not what is left of it: a part
                # taken earlier then gets its depth behind the new front.
                taken = np.maximum(grown_by[radius], phis[name][box])
                np.minimum(lost[name][box], taken, out=lost[name][box])
                np.minimum(here, taken, out=here)
            new_front[box] = redistance(here, g)
        front = new_front
    return {name: np.maximum(phis[name], lost[name]) for name in targets}
