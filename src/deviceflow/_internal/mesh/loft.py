"""Lofting: the sampled bands of a film drawn as the surface they sample.

Conformal deposition walks the surface in z steps: each step is a slab
whose outline is exact at that height, and between heights the outline
jumps. Meshed as stored, a film is a staircase whose step is the z step.
The film it stands for is smooth, so a display mesh can instead join the
outline of each thin band, placed at the band's mid height, to the next
one with slanted triangles. Nothing stored changes; the exact staircase is
one option away.

Two bands are joined when both are thin (no thicker than the loft
threshold), each ring of one is the same feature as a ring of the other
(same count of outer rings and of holes, matched one to one by an
intersection-over-union above a half) and no vertex of either ring is
further from the other ring than the reach. The reach is what keeps a flat
floor flat: the band on a floor and the band of the sidewall film above it
overlap well, but the floor's outline is far from the sidewall's, so they
keep their horizontal cap. Where a check fails, at a pinch-off, a split, a
mask edge, the bands are drawn as stored.

The rings come from the same planar arrangement the caps and walls are
built from, node for node, so the loft meets the half-walls it replaces at
identical vertices and the surface stays closed.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

import numpy as np
import shapely
from shapely.geometry import LinearRing, Polygon

XY = tuple[float, float]
Edge = tuple[XY, XY, bool]  # p -> q with the solid on the left, interface flag
Ring = tuple[list[XY], list[bool]]  # closed vertex loop (no repeat), one flag per edge


def chain_rings(edges: Sequence[Edge]) -> list[Ring] | None:
    """The closed loops the directed boundary edges of one slab form.

    None when a vertex starts two edges, which is a region touching itself
    at a point; such a slab is drawn as stored.
    """
    following: dict[XY, tuple[XY, bool]] = {}
    for p, q, flag in edges:
        if p in following:
            return None
        following[p] = (q, flag)
    if len(following) != len(edges):
        return None
    seen: set[XY] = set()
    rings: list[Ring] = []
    for start in following:
        if start in seen:
            continue
        loop: list[XY] = []
        flags: list[bool] = []
        p = start
        while p not in seen:
            seen.add(p)
            loop.append(p)
            q, flag = following.get(p, (None, False))
            if q is None:
                return None
            flags.append(flag)
            p = q
        if p != start or len(loop) < 3:
            return None
        rings.append((loop, flags))
    return rings


def _signed_area(loop: Sequence[XY]) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1]):
        total += x0 * y1 - x1 * y0
    return 0.5 * total


def match_rings(
    below: list[Ring], above: list[Ring], reach: float
) -> list[tuple[int, int, shapely.Geometry]]:
    """The rings of one band paired with the rings of the next they continue.

    Outer rings pair with outer rings and holes with holes, each with the
    one ring of the other band it overlaps best, when that overlap is more
    than half of their union and neither ring strays further than the reach
    from the other. Rings with no partner are left alone: their band is
    drawn as stored there, while their neighbours are still joined. Each
    pair comes with the region between its two rings, which is what the
    slanted faces cover instead of a cap.

    A ring without a partner that crosses the region between a matched
    pair would leave that pair's faces open, so such a pair is dropped.
    """
    outer = [index for index, ring in enumerate(below) if _signed_area(ring[0]) > 0]
    holes = [index for index, ring in enumerate(below) if _signed_area(ring[0]) <= 0]
    outer_above = [index for index, ring in enumerate(above) if _signed_area(ring[0]) > 0]
    holes_above = [index for index, ring in enumerate(above) if _signed_area(ring[0]) <= 0]
    try:
        polygons_below = [Polygon(loop) for loop, _ in below]
        polygons_above = [Polygon(loop) for loop, _ in above]
        pairs = _pair_off(below, polygons_below, outer, above, polygons_above, outer_above, reach)
        pairs += _pair_off(below, polygons_below, holes, above, polygons_above, holes_above, reach)
        if not pairs:
            return []
        loose = [LinearRing(below[i][0]) for i in range(len(below)) if i not in {i for i, _, _ in pairs}]
        loose += [LinearRing(above[j][0]) for j in range(len(above)) if j not in {j for _, j, _ in pairs}]
        if loose:
            pairs = [
                (i, j, between) for i, j, between in pairs
                if not any(between.intersects(ring) for ring in loose)
            ]
        return pairs
    except (ValueError, shapely.errors.GEOSException):
        return []


def _pair_off(
    below: list[Ring],
    polygons_below: list[Polygon],
    candidates_below: list[int],
    above: list[Ring],
    polygons_above: list[Polygon],
    candidates_above: list[int],
    reach: float,
) -> list[tuple[int, int, shapely.Geometry]]:
    pairs: list[tuple[int, int, shapely.Geometry]] = []
    used: set[int] = set()
    for i in candidates_below:
        polygon = polygons_below[i]
        if not polygon.is_valid:
            continue
        best, best_score = None, 0.5
        for j in candidates_above:
            if j in used or not polygons_above[j].is_valid:
                continue
            if _same_loop(below[i][0], above[j][0]):
                best = None  # a wall continues straight through; nothing to join
                break
            score = _iou(polygon, polygons_above[j])
            if score > best_score:
                best, best_score = j, score
        if best is None or not _within_reach(below[i][0], above[best][0], reach):
            continue
        used.add(best)
        between = polygon.symmetric_difference(polygons_above[best])
        pairs.append((i, best, between))
    return pairs


def _same_loop(loop_a: Sequence[XY], loop_b: Sequence[XY]) -> bool:
    if len(loop_a) != len(loop_b):
        return False
    return set(zip(loop_a, loop_a[1:] + loop_a[:1])) == set(zip(loop_b, loop_b[1:] + loop_b[:1]))


def _iou(a: Polygon, b: Polygon) -> float:
    if not a.intersects(b):
        return 0.0
    inter = a.intersection(b).area
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def _within_reach(loop_a: Sequence[XY], loop_b: Sequence[XY], reach: float) -> bool:
    ring_a, ring_b = LinearRing(loop_a), LinearRing(loop_b)
    shapely.prepare(ring_a)
    shapely.prepare(ring_b)
    if shapely.distance(ring_a, shapely.points(np.asarray(loop_b, dtype=float))).max() > reach:
        return False
    return bool(shapely.distance(ring_b, shapely.points(np.asarray(loop_a, dtype=float))).max() <= reach)


def zipper(
    emit: Callable[[tuple, tuple, tuple, bool], None],
    lower: Ring,
    z_lower: float,
    upper: Ring,
    z_upper: float,
) -> None:
    """Triangles between two loops, walked side by side by normalised arc length.

    Both loops run with the solid on the left, so a triangle that follows
    an edge of either loop and reaches across to the other faces outward,
    the way the builder's wall quads do. Each triangle carries the
    interface flag of the edge it follows.
    """
    loop_a, flags_a = lower
    loop_b, flags_b = upper
    # Start the upper loop at its vertex nearest the lower loop's first one.
    x0, y0 = loop_a[0]
    shift = min(range(len(loop_b)), key=lambda i: (loop_b[i][0] - x0) ** 2 + (loop_b[i][1] - y0) ** 2)
    loop_b = loop_b[shift:] + loop_b[:shift]
    flags_b = flags_b[shift:] + flags_b[:shift]
    param_a = _arc_parameters(loop_a)
    param_b = _arc_parameters(loop_b)
    n_a, n_b = len(loop_a), len(loop_b)
    i = j = 0
    while i < n_a or j < n_b:
        next_a = param_a[i + 1] if i < n_a else math.inf
        next_b = param_b[j + 1] if j < n_b else math.inf
        a0 = (*loop_a[i % n_a], z_lower)
        b0 = (*loop_b[j % n_b], z_upper)
        if next_a <= next_b:
            a1 = (*loop_a[(i + 1) % n_a], z_lower)
            emit(a0, a1, b0, flags_a[i])
            i += 1
        else:
            b1 = (*loop_b[(j + 1) % n_b], z_upper)
            emit(a0, b1, b0, flags_b[j])
            j += 1


def _arc_parameters(loop: Sequence[XY]) -> list[float]:
    lengths = [0.0]
    for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1]):
        lengths.append(lengths[-1] + math.hypot(x1 - x0, y1 - y0))
    total = lengths[-1] or 1.0
    return [length / total for length in lengths]
