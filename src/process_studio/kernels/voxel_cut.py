"""Cut fine cells: one straight line through a fine cell, a material each side.

A fine cell's boundary carries :data:`ANCHORS` *anchors*: every side is
divided into :data:`STEPS` equal steps, and the anchors are the division
points, numbered clockwise from the top-left corner (x to the right, y
up), :data:`STEPS` a side::

    0 -- 1 -- ... -- Q           (top: 0 .. Q, right: Q .. 2Q,
    |                |            bottom: 2Q .. 3Q, left: 3Q .. 4Q = 0)
    4Q-1             Q+1
    |                |
    3Q -- ... ---- 2Q

A boundary between two materials that crosses the cell is drawn as the
straight line between two anchors that are not on the same side of the
cell. Neighbouring cells share the anchors on their common side, so a
boundary is one unbroken polyline from cell to cell. With 32 steps a side
the line's ends are within 1/64 of a cell of where the boundary crosses
the sides, so a curve is drawn to a small fraction of a cell -- the ends
are found by bisection on what a process makes (see :func:`fit`), not by
snapping to the corners and the middles of the sides.

A cut is stored as a *code* (0 is no cut, else ``1 + start * ANCHORS +
end``): the line and its direction. The cell's own label is the material
on the left of the directed line, and the cut records the material on the
right. The direction is chosen so the cell's centre is on the left (the
side the label is), so a cell's label is still the material at its
centre, as it is for every cell that is not cut. :func:`pack` puts the
code and the other material into one ``uint32``.

The eight anchors at the corners and the middles of the sides
(:data:`COARSE`, every ``STEPS // 2``-th anchor) are where a process is
first asked what it makes; a half side between two of them whose ends
differ is where the boundary crosses, and the crossing is then found on it.
"""

from __future__ import annotations

import numpy as np

#: Steps a side is divided into; anchors are the division points.
STEPS = 32
#: Anchors round a cell.
ANCHORS_COUNT = 4 * STEPS
#: Anchor steps in half a side: from a corner to the middle of a side.
HALF = STEPS // 2


def _anchor_units() -> np.ndarray:
    """Anchors in units of one step (a side is STEPS long), clockwise from
    the top-left corner."""
    Q = STEPS
    k = np.arange(Q)
    top = np.stack([k, np.full(Q, Q)], 1)
    right = np.stack([np.full(Q, Q), Q - k], 1)
    bottom = np.stack([Q - k, np.zeros(Q, np.int64)], 1)
    left = np.stack([np.zeros(Q, np.int64), k], 1)
    return np.concatenate([top, right, bottom, left]).astype(np.int64)


#: The anchors in steps (integers), and in the unit cell.
ANCHOR_UNITS = _anchor_units()
ANCHORS = ANCHOR_UNITS / STEPS

#: The corners and the middles of the sides: every HALF-th anchor.
COARSE_INDEX = np.arange(8) * HALF
COARSE = ANCHORS[COARSE_INDEX]


def _on_side() -> np.ndarray:
    """ON_SIDE[a, s]: anchor a lies on side s (0 top, 1 right, 2 bottom,
    3 left); a corner lies on two."""
    out = np.zeros((ANCHORS_COUNT, 4), dtype=bool)
    for s in range(4):
        for k in range(STEPS + 1):
            out[(s * STEPS + k) % ANCHORS_COUNT, s] = True
    return out


ON_SIDE = _on_side()

#: Whether two anchors lie on one side of the cell (or are the same one):
#: no line is drawn between them.
SAME_SIDE = (ON_SIDE[:, None, :] & ON_SIDE[None, :, :]).any(axis=2) | np.eye(ANCHORS_COUNT, dtype=bool)

#: Codes run 0 .. CODES - 1; code 0 is no cut.
CODES = 1 + ANCHORS_COUNT * ANCHORS_COUNT
_all = np.arange(CODES) - 1
#: Code -> start and end anchor (-1 for code 0).
START = np.where(_all >= 0, _all // ANCHORS_COUNT, -1).astype(np.int64)
END = np.where(_all >= 0, _all % ANCHORS_COUNT, -1).astype(np.int64)
#: Whether a code is a line (its anchors are not on one side).
VALID = np.zeros(CODES, dtype=bool)
VALID[1:] = ~SAME_SIDE[START[1:], END[1:]]
#: Code of the line from anchor a to anchor b (0 where there is none).
CODE_OF = np.where(SAME_SIDE, 0, 1 + np.arange(ANCHORS_COUNT)[:, None] * ANCHORS_COUNT + np.arange(ANCHORS_COUNT)[None, :]).astype(np.int64)
del _all


def _left_area() -> np.ndarray:
    """The share of the cell on the left of each code (1 for no cut): the
    polygon from the start round the cell clockwise to the end."""
    out = np.ones(CODES)
    codes = np.flatnonzero(VALID)
    a, b = START[codes], END[codes]
    corners = np.arange(4) * STEPS
    # the corners strictly between a and b going clockwise, in that order
    along = (corners[None, :] - a[:, None]) % ANCHORS_COUNT
    span = (b - a) % ANCHORS_COUNT
    inside = (along > 0) & (along < span[:, None])
    order = np.argsort(np.where(inside, along, ANCHORS_COUNT + 1), axis=1)
    pts = np.zeros((codes.size, 6, 2))
    pts[:, 0] = ANCHORS[a]
    count = inside.sum(axis=1)
    for j in range(4):
        corner = corners[order[:, j]]
        take = j < count
        pts[take, 1 + j] = ANCHORS[corner[take]]
    rows = np.arange(codes.size)
    pts[rows, 1 + count] = ANCHORS[b]
    # pad with the last point: no area
    for j in range(6):
        pad = j > 1 + count
        pts[pad, j] = pts[rows[pad], 1 + count[pad]]
    x, y = pts[:, :, 0], pts[:, :, 1]
    out[codes] = 0.5 * np.abs((x * np.roll(y, -1, axis=1) - y * np.roll(x, -1, axis=1)).sum(axis=1))
    return out


#: The share of the cell on the left of each code (1 for no cut).
LEFT_AREA = _left_area()


def _permuted(to: np.ndarray) -> np.ndarray:
    """Codes after a mirror that moves anchor ``a`` to ``to[a]``: a mirror
    turns left into right, so the line is also turned round."""
    out = np.zeros(CODES, dtype=np.int64)
    codes = np.flatnonzero(VALID)
    out[codes] = CODE_OF[to[END[codes]], to[START[codes]]]
    return out


def _anchor_at(points: np.ndarray) -> np.ndarray:
    lookup = {(int(x), int(y)): i for i, (x, y) in enumerate(ANCHOR_UNITS)}
    return np.array([lookup[(int(x), int(y))] for x, y in points], dtype=np.int64)


_Q = STEPS
#: Codes after mirroring x (x -> 1 - x), y (y -> 1 - y), and after swapping
#: x and y (reading a column as a row).
MIRROR_X = _permuted(_anchor_at(np.stack([_Q - ANCHOR_UNITS[:, 0], ANCHOR_UNITS[:, 1]], 1)))
MIRROR_Y = _permuted(_anchor_at(np.stack([ANCHOR_UNITS[:, 0], _Q - ANCHOR_UNITS[:, 1]], 1)))
SWAP_XY = _permuted(_anchor_at(ANCHOR_UNITS[:, ::-1]))


def pack(code: np.ndarray, other: np.ndarray) -> np.ndarray:
    """A cut as stored: the code, and the material across it."""
    code = np.asarray(code).astype(np.uint32)
    return np.where(code > 0, code | (np.asarray(other).astype(np.uint32) << np.uint32(16)), 0).astype(np.uint32)


def code_of(cut: np.ndarray) -> np.ndarray:
    return (np.asarray(cut).astype(np.uint32) & np.uint32(0xFFFF)).astype(np.int64)


def other_of(cut: np.ndarray) -> np.ndarray:
    return ((np.asarray(cut).astype(np.uint32) >> np.uint32(16)) & np.uint32(0xFF)).astype(np.uint8)


def from_old(cut: np.ndarray) -> np.ndarray:
    """Cuts stored before anchors were divided (``uint16``: code 1..32 over
    eight anchors in the low byte, the other material in the high one) as
    cuts now: the eight anchors are every HALF-th of today's."""
    cut = np.asarray(cut).astype(np.int64)
    code = cut & 0xFF
    other = (cut >> 8) & 0xFF
    # the old sixteen lines, in their old order
    old_same = [{0, 1, 2}, {2, 3, 4}, {4, 5, 6}, {6, 7, 0}]
    lines = [(a, b) for a in range(8) for b in range(a + 1, 8) if not any(a in s and b in s for s in old_same)]
    table = np.zeros(33, dtype=np.int64)
    for i, (a, b) in enumerate(lines):
        table[1 + 2 * i] = CODE_OF[a * HALF, b * HALF]
        table[2 + 2 * i] = CODE_OF[b * HALF, a * HALF]
    return pack(table[np.clip(code, 0, 32)], other)


def ends(code: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Start and end of each line in the unit cell (ax, ay, bx, by); code
    0 gives zeros."""
    code = np.asarray(code, dtype=np.int64)
    a = np.where(code > 0, START[code], 0)
    b = np.where(code > 0, END[code], 0)
    return ANCHORS[a, 0], ANCHORS[a, 1], ANCHORS[b, 0], ANCHORS[b, 1]


def left_of(code: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Whether (u, v) in the unit cell is on the left of the line ``code``
    (on the line counts as left; no cut is all left)."""
    code = np.asarray(code, dtype=np.int64)
    ax, ay, bx, by = ends(code)
    cross = (bx - ax) * (v - ay) - (by - ay) * (u - ax)
    return (code == 0) | (cross >= -1e-12)


def fit(anchor: np.ndarray, centre: np.ndarray, place: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Labels and cuts for cells from what is at their eight coarse anchors.

    ``anchor`` is (cells, 8) labels at :data:`COARSE` (corners and the
    middles of the sides), ``centre`` the label at each cell's centre, and
    ``place[:, h]`` (0 .. HALF) how many anchor steps from coarse anchor
    ``h`` towards ``h + 1`` the boundary crosses half side ``h`` -- found
    by the caller where the two ends of the half side differ; without it,
    the middle of the half side.

    A cell whose anchors hold two materials in two runs round the cell is
    cut between the two crossings; any other cell -- one material all
    round, three, a thin strip crossing it twice, or two crossings on one
    side -- is left whole, as its centre.
    """
    anchor = np.asarray(anchor, dtype=np.uint8)
    centre = np.asarray(centre, dtype=np.uint8)
    count = centre.size
    label = centre.copy()
    cut = np.zeros(count, dtype=np.uint32)
    if count == 0:
        return label, cut
    if place is None:
        place = np.full((count, 8), HALF // 2, dtype=np.int64)
    following = np.roll(anchor, -1, axis=1)
    crossing = anchor != following
    two = crossing.sum(axis=1) == 2
    rows = np.flatnonzero(two)
    if rows.size == 0:
        return label, cut
    across = crossing[rows]
    h1 = across.argmax(axis=1)
    h2 = 7 - across[:, ::-1].argmax(axis=1)
    p = np.asarray(place)[rows]
    pick = np.arange(rows.size)
    e1 = (h1 * HALF + p[pick, h1]) % ANCHORS_COUNT
    e2 = (h2 * HALF + p[pick, h2]) % ANCHORS_COUNT
    mat_a = anchor[rows, (h1 + 1) % 8]  # coarse anchors h1 + 1 .. h2: clockwise from e1 to e2, the left of e1 -> e2
    mat_b = anchor[rows, (h2 + 1) % 8]
    ok = ~SAME_SIDE[e1, e2]
    here = centre[rows]
    ok &= (here == mat_a) | (here == mat_b)
    rows, e1, e2, mat_a, mat_b, here = rows[ok], e1[ok], e2[ok], mat_a[ok], mat_b[ok], here[ok]
    # the centre's side: the label is the material there
    ax, ay = ANCHORS[e1, 0], ANCHORS[e1, 1]
    bx, by = ANCHORS[e2, 0], ANCHORS[e2, 1]
    cross = (bx - ax) * (0.5 - ay) - (by - ay) * (0.5 - ax)
    a_side = np.where(np.abs(cross) > 1e-12, cross > 0, here == mat_a)
    code = np.where(a_side, CODE_OF[e1, e2], CODE_OF[e2, e1])
    label[rows] = np.where(a_side, mat_a, mat_b)
    cut[rows] = pack(code, np.where(a_side, mat_b, mat_a))
    return label, cut


def half_side_point(h: np.ndarray, steps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The point ``steps`` anchor steps (may be fractional) from coarse
    anchor ``h`` towards ``h + 1``, in the unit cell."""
    h = np.asarray(h, dtype=np.int64)
    f = np.asarray(steps, dtype=np.float64) / HALF
    a, b = COARSE[h], COARSE[(h + 1) % 8]
    return a[..., 0] + f * (b[..., 0] - a[..., 0]), a[..., 1] + f * (b[..., 1] - a[..., 1])


def polygon(code: int, left: bool) -> np.ndarray:
    """The part of the unit cell on one side of a cut, as (m, 2) points
    clockwise: the line's ends and the corners between them."""
    code = int(code)
    if code == 0:
        return ANCHORS[np.arange(4) * STEPS] if left else np.zeros((0, 2))
    a, b = (int(START[code]), int(END[code])) if left else (int(END[code]), int(START[code]))
    out = [a]
    span = (b - a) % ANCHORS_COUNT
    for step in range(1, span):
        p = (a + step) % ANCHORS_COUNT
        if p % STEPS == 0:
            out.append(p)
    out.append(b)
    return ANCHORS[out]


# -- along a side ------------------------------------------------------------------


def side_split(code: np.ndarray, side: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """How a fine cell's side is shared out by its cut: (break, low left,
    high left). The side runs along x (top 0, bottom 2) or y (right 1,
    left 3) from 0 to STEPS; ``break`` is where the cut ends on it
    (STEPS where it does not end inside it), and ``low left`` and ``high
    left`` say whether the part before and after the break is on the left
    of the cut (the cell's own label) or the right (the other material)."""
    code = np.asarray(code, dtype=np.int64)
    Q = STEPS
    brk = np.full(code.shape, Q, dtype=np.int64)
    lo_left = np.ones(code.shape, dtype=bool)
    hi_left = np.ones(code.shape, dtype=bool)
    cut = np.flatnonzero(code.reshape(-1) > 0)
    if cut.size == 0:
        return brk, lo_left, hi_left
    c = code.reshape(-1)[cut]
    a, b = START[c], END[c]
    br = np.full(c.shape, Q, dtype=np.int64)
    for e in (a, b):
        inside = (e // Q == side) & (e % Q != 0)
        k = e % Q
        along = k if side in (0, 3) else Q - k
        br = np.where(inside, along, br)
    eps = 1e-3

    def at(t):
        t = t / Q
        if side == 0:
            return t, np.full(t.shape, 1 - eps)
        if side == 1:
            return np.full(t.shape, 1 - eps), t
        if side == 2:
            return t, np.full(t.shape, eps)
        return np.full(t.shape, eps), t

    brk.reshape(-1)[cut] = br
    lo_left.reshape(-1)[cut] = left_of(c, *at(0.5 * br))
    hi_left.reshape(-1)[cut] = left_of(c, *at(0.5 * (br + Q)))
    return brk, lo_left, hi_left


def side_materials(lab, code, other, side: int, *extra):
    """(break, before, after) along a side of fine cells with labels
    ``lab``, cut codes ``code`` and materials across ``other`` (see
    :func:`side_split`); ``extra`` pairs (value on the left, value on the
    right) add their before and after too."""
    brk, lo_left, hi_left = side_split(code, side)
    out = [brk, np.where(lo_left, lab, other), np.where(hi_left, lab, other)]
    for on_left, on_right in extra:
        out += [np.where(lo_left, on_left, on_right), np.where(hi_left, on_left, on_right)]
    return tuple(out)


def pieces(a_brk, b_brk, *values):
    """A shared side from both cells' accounts: up to three pieces between
    the ends and the two breaks. ``values`` are (before, after, brk) triples
    -- each cell's materials (or heights) before and after its own break.
    Returns (starts, stops, *per piece values), each shaped (3,) + the
    input's shape; a piece not there has ``start == stop``. Most sides have
    no break: one piece, the whole side."""
    Q = STEPS
    a_brk = np.asarray(a_brk)
    shape = a_brk.shape
    starts = np.zeros((3,) + shape, dtype=np.int64)
    stops = np.zeros((3,) + shape, dtype=np.int64)
    stops[0] = Q
    outs = []
    for before, _after, _brk in values:
        before = np.asarray(before)
        out = np.zeros((3,) + shape, dtype=np.result_type(before, _after))
        out[:] = before[None]
        outs.append(out)
    split = np.flatnonzero(((a_brk < Q) | (np.asarray(b_brk) < Q)).reshape(-1))
    if split.size:
        fa = a_brk.reshape(-1)[split]
        fb = np.asarray(b_brk).reshape(-1)[split]
        first = np.minimum(fa, fb)
        second = np.maximum(fa, fb)
        st = np.stack([np.zeros_like(first), first, second])
        sp = np.stack([first, second, np.full_like(first, Q)])
        mid = 0.5 * (st + sp)
        starts.reshape(3, -1)[:, split] = st
        stops.reshape(3, -1)[:, split] = sp
        for out, (before, after, brk) in zip(outs, values):
            b_ = np.broadcast_to(np.asarray(before), shape).reshape(-1)[split]
            a_ = np.broadcast_to(np.asarray(after), shape).reshape(-1)[split]
            k_ = np.broadcast_to(np.asarray(brk), shape).reshape(-1)[split]
            out.reshape(3, -1)[:, split] = np.where(mid < k_[None], b_[None], a_[None])
    return (starts, stops, *outs)


# -- exact geometry for the 3D mesh --------------------------------------------
#
# The mesh works on an integer lattice of LATTICE points per fine cell:
# anchors are every LATTICE // STEPS points. Where two lines cross (a cut
# below a slab boundary and another above it) the crossing is found
# exactly and put on the nearest lattice point; both lines' faces use that
# same point, so the mesh stays closed.

LATTICE = 4 * STEPS
_SCALE = LATTICE // STEPS
ANCHOR_LATTICE = ANCHOR_UNITS * _SCALE


def crossing(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """Where line ``second`` crosses line ``first`` strictly inside both,
    on the lattice within the cell: (count, 2), -1 where they do not. The
    same point whichever is first."""
    c1 = np.asarray(first, dtype=np.int64)
    c2 = np.asarray(second, dtype=np.int64)
    out = np.full((c1.size, 2), -1, dtype=np.int64)
    both = (c1 > 0) & (c2 > 0)
    if not both.any():
        return out
    i = np.flatnonzero(both)
    A, B = ANCHOR_UNITS[START[c1[i]]], ANCHOR_UNITS[END[c1[i]]]
    C, D = ANCHOR_UNITS[START[c2[i]]], ANCHOR_UNITS[END[c2[i]]]
    r, s, w = B - A, D - C, C - A
    den = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
    t = w[:, 0] * s[:, 1] - w[:, 1] * s[:, 0]
    u = w[:, 0] * r[:, 1] - w[:, 1] * r[:, 0]
    sign = np.where(den < 0, -1, 1)
    den, t, u = den * sign, t * sign, u * sign
    hit = (den != 0) & (t > 0) & (t < den) & (u > 0) & (u < den)
    i, A, r, den, t = i[hit], A[hit], r[hit], den[hit], t[hit]
    # the exact point in lattice units, to the nearest lattice point
    num = (A * den[:, None] + r * t[:, None]) * _SCALE
    out[i] = (2 * num + den[:, None]) // (2 * den[:, None])
    return out


_CORNERS = np.array([(0, 0), (LATTICE, 0), (LATTICE, LATTICE), (0, LATTICE)], dtype=np.int64)
#: The most points a floor piece has.
PIECE_POINTS = 9


def floor_pieces(below: np.ndarray, above: np.ndarray):
    """A fine cell's floor split by the cut below it and the cut above it.

    Returns (points (n, 4, PIECE_POINTS, 2) on the lattice, counter-
    clockwise; count (n, 4); left of the cut below (4,); left of the cut
    above (4,)): piece j is the part left (or right) of each cut, with
    ``count`` 0 where there is no such part. Code 0 is no cut (all left).
    """
    below = np.asarray(below, dtype=np.int64)
    above = np.asarray(above, dtype=np.int64)
    n = below.size
    # candidates: the corners, both lines' ends, and where they cross
    cand = np.zeros((n, 9, 2), dtype=np.int64)
    cand[:, :4] = _CORNERS[None]
    real = np.ones((n, 9), dtype=bool)
    for j, code in enumerate((below, above)):
        s = np.where(code > 0, START[code], 0)
        e = np.where(code > 0, END[code], 0)
        cand[:, 4 + 2 * j] = ANCHOR_LATTICE[s]
        cand[:, 5 + 2 * j] = ANCHOR_LATTICE[e]
        real[:, 4 + 2 * j] = real[:, 5 + 2 * j] = code > 0
    x = crossing(below, above)
    cand[:, 8] = np.maximum(x, 0)
    real[:, 8] = x[:, 0] >= 0

    def side(code):
        s = np.where(code > 0, START[code], 0)
        e = np.where(code > 0, END[code], 0)
        A, B = ANCHOR_LATTICE[s][:, None], ANCHOR_LATTICE[e][:, None]
        cr = (B[..., 0] - A[..., 0]) * (cand[..., 1] - A[..., 1]) - (B[..., 1] - A[..., 1]) * (cand[..., 0] - A[..., 0])
        out = np.sign(cr)
        out[code == 0] = 1
        out[:, 8] = np.where(code > 0, 0, 1)  # the crossing is on both lines
        return out

    sb, sa = side(below), side(above)
    left_b = np.array([True, True, False, False])
    left_a = np.array([True, False, True, False])
    points = np.zeros((n, 4, PIECE_POINTS, 2), dtype=np.int64)
    count = np.zeros((n, 4), dtype=np.int64)
    for j in range(4):
        want_b = 1 if left_b[j] else -1
        want_a = 1 if left_a[j] else -1
        take = real & ((sb == want_b) | (sb == 0)) & ((sa == want_a) | (sa == 0))
        # a piece of the right of "no cut" does not exist
        take &= ~((below == 0)[:, None] & (not left_b[j]))
        take &= ~((above == 0)[:, None] & (not left_a[j]))
        # distinct points, in order round their middle
        key = cand[..., 0] * (LATTICE + 1) + cand[..., 1]
        key = np.where(take, key, -1)
        order = np.argsort(key, axis=1)
        sk = np.take_along_axis(key, order, axis=1)
        fresh = np.ones_like(take)
        fresh[:, 1:] = sk[:, 1:] != sk[:, :-1]
        keep_sorted = fresh & (sk >= 0)
        keep = np.zeros_like(take)
        np.put_along_axis(keep, order, keep_sorted, axis=1)
        cnt = keep.sum(axis=1)
        pts = cand.astype(np.float64)
        w = keep.astype(np.float64)
        cx = (pts[..., 0] * w).sum(axis=1) / np.maximum(cnt, 1)
        cy = (pts[..., 1] * w).sum(axis=1) / np.maximum(cnt, 1)
        angle = np.arctan2(pts[..., 1] - cy[:, None], pts[..., 0] - cx[:, None])
        angle = np.where(keep, angle, np.inf)
        rank = np.argsort(angle, axis=1)
        ordered = np.take_along_axis(cand, rank[..., None], axis=1)
        # area: a piece of three points in a line is none
        valid = np.arange(9)[None, :] < cnt[:, None]
        px = np.where(valid, ordered[..., 0], 0)
        py = np.where(valid, ordered[..., 1], 0)
        last = np.maximum(cnt - 1, 0)
        nx_ = np.where(np.arange(9)[None, :] == last[:, None], ordered[:, 0, 0][:, None], np.roll(ordered[..., 0], -1, axis=1))
        ny_ = np.where(np.arange(9)[None, :] == last[:, None], ordered[:, 0, 1][:, None], np.roll(ordered[..., 1], -1, axis=1))
        twice = (np.where(valid, px * ny_ - py * nx_, 0)).sum(axis=1)
        good = (cnt >= 3) & (twice > 0)
        points[:, j] = ordered[:, :PIECE_POINTS]
        count[:, j] = np.where(good, cnt, 0)
    return points, count, left_b, left_a
