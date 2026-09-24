"""Cut fine cells: one straight line through a fine cell, a material each side.

A fine cell's boundary carries eight *anchors* -- its four corners and the
middles of its four sides -- numbered clockwise from the top-left corner
(x to the right, y up)::

    0 --- 1 --- 2
    |           |
    7           3
    |           |
    6 --- 5 --- 4

A boundary between two materials that crosses the cell is drawn as the
straight line between two anchors that are not on the same side of the
cell: sixteen lines, at 0, 26.6, 45, 63.4 and 90 degrees and their
mirrors. Neighbouring cells share the anchors on their common side, so a
boundary is one unbroken polyline from cell to cell -- a round hole is a
polygon, not a staircase.

A cut is stored as a *code* (1 to 32; 0 is no cut): the line and its
direction. The cell's own label is the material on the left of the
directed line, and the cut records the material on the right. The
direction is chosen so the cell's centre is on the left (the side the
label is), so a cell's label is still the material at its centre, as it
is for every cell that is not cut.

Where a process knows what it makes at any point, :func:`fit` turns that
into cuts: the labels at the eight anchors, and at the middle of each
half side the boundary crosses (which says whether the crossing is nearer
the corner or the side's middle, and so which anchor it snaps to). Two
cells sharing a side read the same points there, so they agree.
"""

from __future__ import annotations

import itertools

import numpy as np

#: The anchors in the unit cell, clockwise from the top-left corner.
ANCHORS = np.array(
    [(0.0, 1.0), (0.5, 1.0), (1.0, 1.0), (1.0, 0.5), (1.0, 0.0), (0.5, 0.0), (0.0, 0.0), (0.0, 0.5)]
)

_SIDES = ({0, 1, 2}, {2, 3, 4}, {4, 5, 6}, {6, 7, 0})

#: Whether two anchors lie on one side of the cell (or are the same one):
#: no line is drawn between them.
SAME_SIDE = np.array(
    [[a == b or any(a in s and b in s for s in _SIDES) for b in range(8)] for a in range(8)], dtype=bool
)

#: The sixteen lines, as anchor pairs.
LINES = [(a, b) for a, b in itertools.combinations(range(8), 2) if not SAME_SIDE[a, b]]

#: Code -> (start anchor, end anchor); code 0 is no cut.
START = np.full(33, -1, dtype=np.int64)
END = np.full(33, -1, dtype=np.int64)
for _line, (_a, _b) in enumerate(LINES):
    START[1 + 2 * _line], END[1 + 2 * _line] = _a, _b
    START[2 + 2 * _line], END[2 + 2 * _line] = _b, _a

#: Code of the line from anchor a to anchor b (0 where there is none).
CODE_OF = np.zeros((8, 8), dtype=np.int64)
for _code in range(1, 33):
    CODE_OF[START[_code], END[_code]] = _code


def _arc(a: int, b: int) -> list[int]:
    """Anchors from ``a`` to ``b`` going clockwise, both included."""
    out = [a]
    while out[-1] != b:
        out.append((out[-1] + 1) % 8)
    return out


#: The left and right parts of a cut cell as polygons (anchor indices,
#: clockwise). Clockwise round the cell from the line's start to its end
#: is the left side.
LEFT_POLY: list[list[int]] = [list(range(8))]
RIGHT_POLY: list[list[int]] = [[]]
for _code in range(1, 33):
    LEFT_POLY.append(_arc(int(START[_code]), int(END[_code])))
    RIGHT_POLY.append(_arc(int(END[_code]), int(START[_code])))


def _area(indices: list[int]) -> float:
    if len(indices) < 3:
        return 0.0
    p = ANCHORS[indices]
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


#: The share of the cell on the left of each code (1 for no cut).
LEFT_AREA = np.array([1.0] + [_area(LEFT_POLY[c]) for c in range(1, 33)])


def _mirrored(swap: dict[int, int]) -> np.ndarray:
    """Codes after a mirror that moves anchor ``a`` to ``swap[a]``: a mirror
    turns left into right, so the line is also turned round."""
    out = np.zeros(33, dtype=np.uint16)
    for code in range(1, 33):
        out[code] = CODE_OF[swap[int(END[code])], swap[int(START[code])]]
    return out


#: Codes after mirroring x (x -> 1 - x) and y (y -> 1 - y).
MIRROR_X = _mirrored({0: 2, 1: 1, 2: 0, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3})
MIRROR_Y = _mirrored({0: 6, 1: 5, 2: 4, 3: 3, 4: 2, 5: 1, 6: 0, 7: 7})


def pack(code: np.ndarray, other: np.ndarray) -> np.ndarray:
    """A cut as stored: the code, and the material across it."""
    code = np.asarray(code).astype(np.uint16)
    return np.where(code > 0, code | (np.asarray(other).astype(np.uint16) << 8), 0).astype(np.uint16)


def code_of(cut: np.ndarray) -> np.ndarray:
    return (np.asarray(cut) & 0xFF).astype(np.int64)


def other_of(cut: np.ndarray) -> np.ndarray:
    return (np.asarray(cut) >> 8).astype(np.uint8)


def left_of(code: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Whether (u, v) in the unit cell is on the left of the line ``code``
    (on the line counts as left; no cut is all left)."""
    code = np.asarray(code, dtype=np.int64)
    a, b = START[code], END[code]
    ax, ay = ANCHORS[a, 0], ANCHORS[a, 1]
    bx, by = ANCHORS[b, 0], ANCHORS[b, 1]
    cross = (bx - ax) * (v - ay) - (by - ay) * (u - ax)
    return (code == 0) | (cross >= -1e-12)


#: For half side h (anchor h to anchor h + 1), the snap that goes to the
#: side's middle: every half side has one corner and one middle.
TO_MIDDLE = np.array([1 if (h + 1) % 2 == 1 else 0 for h in range(8)], dtype=np.int64)


def fit(anchor: np.ndarray, centre: np.ndarray, snap: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Labels and cuts for cells from their anchors.

    ``anchor`` is (cells, 8) labels at the anchors, ``centre`` the label at
    each cell's centre, and ``snap[:, h]`` (1 or 0) whether the boundary
    crossing half side ``h`` -- from anchor ``h`` to ``h + 1`` -- is nearer
    anchor ``h + 1`` or anchor ``h``; without it, the middle of the side.

    A cell whose anchors hold two materials in two runs round the cell is
    cut between the anchors the two crossings snap to; any other cell --
    one material all round, three, a thin strip crossing it twice, or two
    crossings that snap onto one side -- is left whole, as its centre.
    """
    anchor = np.asarray(anchor, dtype=np.uint8)
    centre = np.asarray(centre, dtype=np.uint8)
    count = centre.size
    label = centre.copy()
    cut = np.zeros(count, dtype=np.uint16)
    if count == 0:
        return label, cut
    if snap is None:
        snap = np.broadcast_to(TO_MIDDLE, (count, 8))
    following = np.roll(anchor, -1, axis=1)
    crossing = anchor != following
    two = crossing.sum(axis=1) == 2
    rows = np.flatnonzero(two)
    if rows.size == 0:
        return label, cut
    across = crossing[rows]
    h1 = across.argmax(axis=1)
    h2 = 7 - across[:, ::-1].argmax(axis=1)
    s = np.asarray(snap)[rows]
    pick = np.arange(rows.size)
    e1 = (h1 + s[pick, h1]) % 8
    e2 = (h2 + s[pick, h2]) % 8
    mat_a = anchor[rows, (h1 + 1) % 8]  # anchors h1 + 1 .. h2: clockwise from e1 to e2, the left of e1 -> e2
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


def half_middles() -> np.ndarray:
    """The middle of each half side (8, 2): where the snap is read."""
    return 0.5 * (ANCHORS + np.roll(ANCHORS, -1, axis=0))


def polygon(code: int, left: bool) -> np.ndarray:
    """The part of the unit cell on one side of a cut, as (m, 2) points
    clockwise, without the anchors that lie mid-way along a straight side."""
    idx = LEFT_POLY[code] if left else RIGHT_POLY[code]
    pts = ANCHORS[idx]
    keep = np.ones(len(pts), dtype=bool)
    for i in range(len(pts)):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        if abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < 1e-12:
            keep[i] = False
    return pts[keep]


# -- exact geometry for the 3D mesh --------------------------------------------
#
# The mesh works on an integer lattice of LATTICE points per fine cell:
# anchors are at 0, LATTICE / 2 and LATTICE, and where two lines through
# anchors cross (a cut below a slab boundary and another above it), the
# crossing is a lattice point too -- every such crossing has a denominator
# dividing 60.

from fractions import Fraction
from functools import lru_cache

LATTICE = 60

_EXACT = [(Fraction(int(round(x * 2)), 2), Fraction(int(round(y * 2)), 2)) for x, y in ANCHORS]


def _side(code: int, p) -> int:
    """+1 left of the line, -1 right, 0 on it (exact)."""
    (ax, ay), (bx, by) = _EXACT[int(START[code])], _EXACT[int(END[code])]
    value = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
    return (value > 0) - (value < 0)


def _clip(poly, code: int, keep_left: bool):
    """The part of convex ``poly`` (exact points, counter-clockwise) on one side of a line."""
    out = []
    want = 1 if keep_left else -1
    count = len(poly)
    for i in range(count):
        p, q = poly[i], poly[(i + 1) % count]
        sp, sq = _side(code, p), _side(code, q)
        if sp == want or sp == 0:
            out.append(p)
        if sp * sq < 0:
            (ax, ay), (bx, by) = _EXACT[int(START[code])], _EXACT[int(END[code])]
            dx, dy = q[0] - p[0], q[1] - p[1]
            ex, ey = bx - ax, by - ay
            t = ((ax - p[0]) * ey - (ay - p[1]) * ex) / (dx * ey - dy * ex)
            out.append((p[0] + t * dx, p[1] + t * dy))
    # drop repeats
    clean = []
    for p in out:
        if not clean or clean[-1] != p:
            clean.append(p)
    if len(clean) > 1 and clean[0] == clean[-1]:
        clean.pop()
    return clean


def _twice_area(poly) -> Fraction:
    total = Fraction(0)
    for i in range(len(poly)):
        p, q = poly[i], poly[(i + 1) % len(poly)]
        total += p[0] * q[1] - p[1] * q[0]
    return total


@lru_cache(maxsize=None)
def regions(below: int, above: int) -> tuple[tuple[np.ndarray, bool, bool], ...]:
    """A fine cell's floor split by the cut below it and the cut above it:
    (polygon on the lattice, counter-clockwise; left of the cut below;
    left of the cut above) for each part. Code 0 is no cut (all left)."""
    square = [(Fraction(0), Fraction(0)), (Fraction(1), Fraction(0)), (Fraction(1), Fraction(1)), (Fraction(0), Fraction(1))]
    parts = [(square, True)] if below == 0 else [(_clip(square, below, True), True), (_clip(square, below, False), False)]
    out = []
    for poly, left_b in parts:
        pieces = [(poly, True)] if above == 0 else [(_clip(poly, above, True), True), (_clip(poly, above, False), False)]
        for piece, left_a in pieces:
            if len(piece) >= 3 and _twice_area(piece) > 0:
                points = np.array([(int(p[0] * LATTICE), int(p[1] * LATTICE)) for p in piece], dtype=np.int64)
                out.append((points, left_b, left_a))
    return tuple(out)


@lru_cache(maxsize=None)
def crossing(code: int, other: int) -> tuple[int, int] | None:
    """Where line ``other`` crosses line ``code`` strictly between its ends,
    on the lattice; None if it does not."""
    if code == 0 or other == 0:
        return None
    (ax, ay), (bx, by) = _EXACT[int(START[code])], _EXACT[int(END[code])]
    (cx, cy), (dx, dy) = _EXACT[int(START[other])], _EXACT[int(END[other])]
    ex, ey, fx_, fy_ = bx - ax, by - ay, dx - cx, dy - cy
    den = ex * fy_ - ey * fx_
    if den == 0:
        return None
    t = ((cx - ax) * fy_ - (cy - ay) * fx_) / den
    if not 0 < t < 1:
        return None
    return int((ax + t * ex) * LATTICE), int((ay + t * ey) * LATTICE)


def _edge_side_table() -> np.ndarray:
    """EDGE_LEFT[code, side, half]: whether the half of a side of the cell
    (side 0 top, 1 right, 2 bottom, 3 left; half 0 nearer the smaller x or
    y) is on the left of the cut."""
    eps = 1e-6
    points = {
        0: [(0.25, 1 - eps), (0.75, 1 - eps)],
        1: [(1 - eps, 0.25), (1 - eps, 0.75)],
        2: [(0.25, eps), (0.75, eps)],
        3: [(eps, 0.25), (eps, 0.75)],
    }
    table = np.ones((33, 4, 2), dtype=bool)
    for code in range(1, 33):
        for side, pts in points.items():
            for half, (u, v) in enumerate(pts):
                table[code, side, half] = bool(left_of(np.array([code]), np.array(u), np.array(v))[0])
    return table


EDGE_LEFT = _edge_side_table()


def _swapped() -> np.ndarray:
    """Codes after swapping x and y (reading a column as a row): a mirror in
    the diagonal, so the line also turns round."""
    swap = {}
    for a in range(8):
        x, y = ANCHORS[a]
        swap[a] = int(np.flatnonzero((ANCHORS[:, 0] == y) & (ANCHORS[:, 1] == x))[0])
    return _mirrored(swap)


SWAP_XY = _swapped()
