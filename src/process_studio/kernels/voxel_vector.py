"""Pictures as outlines: each material's region as closed loops.

A section or a top view is a flat map of materials. Drawn as pixels it
blurs when magnified; drawn as outlines it stays sharp at any zoom. The
outlines are found from the map's *boundary edges*: every edge between two
different materials, once for each of them, pointing so the material is on
its left. Chaining those edges end to start gives closed loops, and a
material's loops filled with the even-odd rule are exactly its region --
holes and islands included, however the loops happen to pair up where two
corners touch.

Loops are sent as points and loop starts (base64 float32 / int32) in the
picture's own coordinates, and the client builds its paths from them.
"""

from __future__ import annotations

import base64
from typing import Mapping, Sequence

import numpy as np

from .voxel import _union_find


def _ordered_loops(start: np.ndarray, end: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Chain directed edges (start and end vertex keys, integers) into
    closed loops. Returns the edges in loop order and where each loop
    starts in that order."""
    count = start.size
    if count == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    # at every vertex, the k-th edge in pairs with the k-th edge out
    incoming = np.argsort(end, kind="stable")
    outgoing = np.argsort(start, kind="stable")
    following = np.empty(count, dtype=np.int64)
    following[incoming] = outgoing
    # each loop is a cycle of `following`; its smallest edge heads it
    root = _union_find(count - 1, np.arange(count), following)[:count] if count > 1 else np.zeros(1, np.int64)
    head = root
    # rank along each cycle from its head, by pointer jumping on the list
    # the cycle becomes when the edge before the head lets go
    nxt = following.copy()
    last = nxt == head  # the edge whose next is its loop's head
    nxt[last] = -1
    distance = np.where(last, 0, 1).astype(np.int64)
    alive = nxt >= 0
    while alive.any():
        idx = np.flatnonzero(alive)
        step = nxt[idx]
        distance[idx] += distance[step]
        nxt[idx] = nxt[step]
        alive = nxt >= 0
    # distance is how far each edge is from its loop's last edge
    order = np.lexsort((-distance, head))
    heads = head[order]
    first = np.flatnonzero(np.concatenate([[True], heads[1:] != heads[:-1]]))
    return order, first


def loops(
    sx: np.ndarray, sy: np.ndarray, ex: np.ndarray, ey: np.ndarray, *, scale: float = 1e9,
    keep_straight: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Closed loops from directed edges (start and end points). Vertices are
    matched after rounding to 1/``scale``; points where a loop goes on
    straight are dropped unless ``keep_straight`` (a neighbour may turn
    there: two materials' loops then share every point of their edge).
    Returns points (N, 2) and loop starts (L,)."""
    if sx.size == 0:
        return np.zeros((0, 2)), np.zeros(0, np.int64)
    qs = np.round(np.stack([sx, sy], 1) * scale).astype(np.int64)
    qe = np.round(np.stack([ex, ey], 1) * scale).astype(np.int64)
    both = np.concatenate([qs, qe])
    _uniq, key = np.unique(both, axis=0, return_inverse=True)
    key = key.reshape(-1)
    start, end = key[: sx.size], key[sx.size :]
    order, first = _ordered_loops(start, end)
    px = sx[order]
    py = sy[order]
    loop_of = np.repeat(np.arange(first.size), np.diff(np.concatenate([first, [order.size]])))
    # drop a vertex where the loop goes on straight through it
    prev = np.arange(order.size) - 1
    prev[first] = np.concatenate([first[1:], [order.size]]) - 1
    nxt = np.arange(order.size) + 1
    last = np.concatenate([first[1:], [order.size]]) - 1
    nxt[last] = first
    ax, ay = px[prev], py[prev]
    bx, by = px[nxt], py[nxt]
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    span = np.abs(bx - ax) + np.abs(by - ay) + 1e-300
    keep = np.abs(cross) > 1e-12 * span * span
    if keep_straight:
        keep = np.ones_like(keep)
    points = np.stack([px[keep], py[keep]], 1)
    kept_loop = loop_of[keep]
    starts = np.flatnonzero(np.concatenate([[True], kept_loop[1:] != kept_loop[:-1]])) if kept_loop.size else np.zeros(0, np.int64)
    # a loop that lost all but two points is no area
    sizes = np.diff(np.concatenate([starts, [kept_loop.size]]))
    good = sizes >= 3
    if not good.all():
        keep_pts = np.repeat(good, sizes)
        points = points[keep_pts]
        sizes = sizes[good]
        starts = np.concatenate([[0], np.cumsum(sizes)[:-1]]) if sizes.size else np.zeros(0, np.int64)
    return points, starts.astype(np.int64)


def _b64(array: np.ndarray, dtype) -> str:
    return base64.b64encode(np.ascontiguousarray(array, dtype=dtype).tobytes()).decode("ascii")


def payload(
    fills: Sequence[tuple[str, np.ndarray, np.ndarray]],
    strokes: Sequence[tuple[str, np.ndarray, np.ndarray]],
    colors: Mapping[str, str],
    extent: tuple[float, float, float, float],
    width: int,
    height: int,
    darker=None,
) -> dict:
    """The picture as outlines, in its own pixel frame (x to the right, y
    down): ``fills`` are (material, points, loop starts) in world
    coordinates, drawn in order; ``strokes`` open lines the same way."""
    h0, h1, v0, v1 = extent
    sx = width / max(h1 - h0, 1e-300)
    sy = height / max(v1 - v0, 1e-300)

    def frame(points):
        out = np.empty_like(points, dtype=np.float64)
        out[:, 0] = (points[:, 0] - h0) * sx
        out[:, 1] = (v1 - points[:, 1]) * sy
        return out

    shapes = []
    for name, points, starts in fills:
        if points.shape[0] == 0:
            continue
        shapes.append({
            "material": name,
            "color": colors.get(name, "#7c83a0"),
            "points": _b64(frame(points), np.float32),
            "starts": _b64(starts, np.int32),
        })
    lines = []
    for name, points, starts in strokes:
        if points.shape[0] == 0:
            continue
        color = colors.get(name, "#7c83a0")
        lines.append({
            "material": name,
            "color": darker(color) if darker else color,
            "points": _b64(frame(points), np.float32),
            "starts": _b64(starts, np.int32),
        })
    return {"width": int(width), "height": int(height), "shapes": shapes, "lines": lines}


# -- a flat two-level map with cuts, as outlines -------------------------------

_OUT = -1


def _emit_vertical(out, a, b, line, u0, u1):
    """Edges on x = ``line`` from y = u0 to u1, ``a`` on the left, ``b`` on
    the right: each material keeps itself on its left."""
    for mat, up in ((a, True), (b, False)):
        keep = mat > 0
        if not keep.any():
            continue
        m, ln, p0, p1 = mat[keep], line[keep], u0[keep], u1[keep]
        if up:
            out.append((m, ln, p0, ln, p1))
        else:
            out.append((m, ln, p1, ln, p0))


def _emit_horizontal(out, a, b, line, u0, u1):
    """Edges on y = ``line`` from x = u0 to u1, ``a`` below, ``b`` above."""
    for mat, below in ((a, True), (b, False)):
        keep = mat > 0
        if not keep.any():
            continue
        m, ln, p0, p1 = mat[keep], line[keep], u0[keep], u1[keep]
        if below:
            out.append((m, p1, ln, p0, ln))
        else:
            out.append((m, p0, ln, p1, ln))


def _runs(a, b, line, u, length):
    """Join unit edges with the same materials, on the same line, one after
    another along u: (a, b, line, u0, u1)."""
    if a.size == 0:
        return a, b, line, u, u
    order = np.lexsort((u, line, b, a))
    a, b, line, u = a[order], b[order], line[order], u[order]
    fresh = np.ones(a.size, dtype=bool)
    fresh[1:] = (a[1:] != a[:-1]) | (b[1:] != b[:-1]) | (line[1:] != line[:-1]) | (u[1:] != u[:-1] + length)
    begin = np.flatnonzero(fresh)
    end = np.concatenate([begin[1:], [a.size]]) - 1
    return a[begin], b[begin], line[begin], u[begin], u[end] + length


def _runs_var(a, b, line, u0, u1):
    """Join edges with the same materials on the same line where one ends
    as the next starts: (a, b, line, u0, u1)."""
    if a.size == 0:
        return a, b, line, u0, u1
    order = np.lexsort((u0, line, b, a))
    a, b, line, u0, u1 = a[order], b[order], line[order], u0[order], u1[order]
    fresh = np.ones(a.size, dtype=bool)
    fresh[1:] = (a[1:] != a[:-1]) | (b[1:] != b[:-1]) | (line[1:] != line[:-1]) | (u0[1:] != u1[:-1])
    begin = np.flatnonzero(fresh)
    end = np.concatenate([begin[1:], [a.size]]) - 1
    return a[begin], b[begin], line[begin], u0[begin], u1[end]


def _place(out, a, b, line, u0, u1, inverse, line_origin, u_origin, emit) -> None:
    """Edges found once per distinct brick (``a`` and ``b`` either side,
    ``line``, ``u0`` .. ``u1`` along it, all brick-local and shaped (U,
    ...)), placed at every brick that holds it (``inverse``, and each
    brick's origin)."""
    U = a.shape[0]
    diff = ((a != b) & (u1 > u0)).reshape(U, -1)
    per = diff.sum(axis=1)
    if not per.any():
        return
    ub, where = np.nonzero(diff)
    a_f = a.reshape(U, -1)[ub, where]
    b_f = b.reshape(U, -1)[ub, where]
    l_f = np.broadcast_to(line, a.shape).reshape(U, -1)[ub, where]
    s_f = u0.reshape(U, -1)[ub, where]
    e_f = u1.reshape(U, -1)[ub, where]
    start = np.concatenate([[0], np.cumsum(per)[:-1]])
    count = per[inverse]
    brick = np.repeat(np.arange(inverse.size), count)
    offset = np.arange(brick.size) - np.repeat(np.cumsum(count) - count, count)
    pick = start[inverse][brick] + offset
    A, Bm, LN, U0, U1 = _runs_var(
        a_f[pick], b_f[pick], l_f[pick] + line_origin[brick], s_f[pick] + u_origin[brick], e_f[pick] + u_origin[brick]
    )
    emit(out, A, Bm, LN, U0, U1)


def _shared(a, b, side_a: int, side_b: int, *heights):
    """A side shared by fine cells ``a`` and ``b`` (label, code, other
    arrays each), in up to three pieces: (starts, stops, material of a,
    material of b, *heights of a and b) each (3,) + the cells' shape, in
    steps along the side. ``heights`` are ((left, right) of a, (left,
    right) of b) when wanted."""
    from . import voxel_cut

    ea = (heights[0],) if heights else ()
    eb = (heights[1],) if heights else ()
    sa = voxel_cut.side_materials(a[0], a[1], a[2], side_a, *ea)
    sb = voxel_cut.side_materials(b[0], b[1], b[2], side_b, *eb)
    values = [(sa[1], sa[2], sa[0]), (sb[1], sb[2], sb[0])]
    if heights:
        values += [(sa[3], sa[4], sa[0]), (sb[3], sb[4], sb[0])]
    return voxel_cut.pieces(sa[0], sb[0], *values)


def field_edges(coarse: np.ndarray, cells: np.ndarray, lab: np.ndarray, cut: np.ndarray) -> list:
    """Boundary edges of a flat two-level map, on the anchor lattice
    (:data:`voxel_cut.STEPS` units a fine cell): ``coarse`` (ny, nx) labels,
    MIXED where ``cells`` (sorted, flat) hold fine labels ``lab`` and cuts
    ``cut`` (m, B, B). Void (0) is nothing. Returns (material, sx, sy, ex,
    ey) arrays, each material on the left of its edges."""
    from . import voxel_cut
    from .voxel import MIXED

    Q = voxel_cut.STEPS
    ny, nx = coarse.shape
    # the fine cells per cell side, from the blocks' shape even when there
    # are none of them: a map with no refined cell is still on its grid
    B = lab.shape[1] if lab.ndim == 3 else 1
    out: list = []
    c16 = coarse.astype(np.int64)
    span = Q * B
    # plain against plain (or the picture's edge)
    pad = np.pad(c16, ((0, 0), (1, 1)), constant_values=_OUT)
    left, right = pad[:, :-1], pad[:, 1:]  # (ny, nx + 1): line i between i-1 and i
    face = (left != right) & (left != MIXED) & (right != MIXED)
    j, i = np.nonzero(face)
    a, b, ln, u0, u1 = _runs(left[j, i], right[j, i], i * span, j * span, span)
    _emit_vertical(out, a, b, ln, u0, u1)
    pad = np.pad(c16, ((1, 1), (0, 0)), constant_values=_OUT)
    low, high = pad[:-1, :], pad[1:, :]
    face = (low != high) & (low != MIXED) & (high != MIXED)
    j, i = np.nonzero(face)
    a, b, ln, u0, u1 = _runs(low[j, i], high[j, i], j * span, i * span, span)
    _emit_horizontal(out, a, b, ln, u0, u1)
    if cells.size:
        code = voxel_cut.code_of(cut)
        other = voxel_cut.other_of(cut).astype(np.int64)
        flat_coarse = c16.reshape(-1)

        def side_of(flat_cells, axis, index):
            """One row (axis 1) or column (axis 2) of fine labels, cuts and
            materials across them, of any cells: a brick's, or a plain
            label repeated -- without drawing out whole blocks."""
            m = flat_cells.size
            L = np.repeat(flat_coarse[flat_cells][:, None], B, 1)
            C = np.zeros((m, B), dtype=np.int64)
            O = np.zeros((m, B), dtype=np.int64)
            at = np.minimum(np.searchsorted(cells, flat_cells), cells.size - 1)
            hit = np.flatnonzero(cells[at] == flat_cells)
            src = at[hit]
            if axis == 1:
                L[hit] = lab[src, index, :]
                C[hit] = code[src, index, :]
                O[hit] = other[src, index, :]
            else:
                L[hit] = lab[src, :, index]
                C[hit] = code[src, :, index]
                O[hit] = other[src, :, index]
            return L, C, O

        def outside(cell, ok):
            L, C, O = cell
            L[~ok], C[~ok] = _OUT, 0
            return L, C, O

        along = np.arange(B)[None, None, :] * Q
        mixed = coarse == MIXED
        # coarse sides with a refined cell on either side
        pad = np.pad(mixed, ((0, 0), (1, 1)))
        j, i = np.nonzero(pad[:, :-1] | pad[:, 1:])  # line i between cells i-1 and i
        lcell = outside(side_of(j * nx + np.clip(i - 1, 0, nx - 1), 2, -1), i > 0)
        rcell = outside(side_of(j * nx + np.clip(i, 0, nx - 1), 2, 0), i < nx)
        st, sp, ma, mb = _shared(lcell, rcell, 1, 3)
        base = (j * span)[None, :, None] + along
        line = np.broadcast_to((i * span)[None, :, None], ma.shape)
        diff = (ma != mb) & (sp > st)
        _emit_vertical(out, *_runs_var(ma[diff], mb[diff], line[diff], (base + st)[diff], (base + sp)[diff]))
        pad = np.pad(mixed, ((1, 1), (0, 0)))
        j, i = np.nonzero(pad[:-1, :] | pad[1:, :])  # line j between rows j-1 and j
        lcell = outside(side_of(np.clip(j - 1, 0, ny - 1) * nx + i, 1, -1), j > 0)
        hcell = outside(side_of(np.clip(j, 0, ny - 1) * nx + i, 1, 0), j < ny)
        st, sp, ma, mb = _shared(lcell, hcell, 0, 2)
        base = (i * span)[None, :, None] + along
        line = np.broadcast_to((j * span)[None, :, None], ma.shape)
        diff = (ma != mb) & (sp > st)
        _emit_horizontal(out, *_runs_var(ma[diff], mb[diff], line[diff], (base + st)[diff], (base + sp)[diff]))
        # inside the refined cells: each different brick once, then placed
        # at every cell that holds it
        cy, cx = np.divmod(cells, nx)
        if B > 1:
            from .voxel import _pair_hash, _unique

            _u, first, inverse = _unique(_pair_hash(lab, cut), return_index=True, return_inverse=True)
            inverse = inverse.reshape(-1)
            ul, uc, uo = lab[first].astype(np.int64), code[first], other[first]
            fine = np.arange(B)
            # vertical lines: between fine columns r and r + 1 of a brick; along y
            a = (ul[:, :, :-1], uc[:, :, :-1], uo[:, :, :-1])
            b = (ul[:, :, 1:], uc[:, :, 1:], uo[:, :, 1:])
            st, sp, ma, mb = (x.transpose(1, 0, 2, 3) for x in _shared(a, b, 1, 3))  # (U, 3, rows, lines)
            line = np.broadcast_to(((fine[1:]) * Q)[None, None, None, :], ma.shape)
            u = (fine * Q)[None, None, :, None]
            _place(out, ma, mb, line, u + st, u + sp, inverse, cx * span, cy * span, _emit_vertical)
            # horizontal lines: between fine rows r and r + 1; along x
            a = (ul[:, :-1, :], uc[:, :-1, :], uo[:, :-1, :])
            b = (ul[:, 1:, :], uc[:, 1:, :], uo[:, 1:, :])
            st, sp, ma, mb = (x.transpose(1, 0, 2, 3) for x in _shared(a, b, 0, 2))  # (U, 3, lines, columns)
            line = np.broadcast_to(((fine[1:]) * Q)[None, None, :, None], ma.shape)
            u = (fine * Q)[None, None, None, :]
            _place(out, ma, mb, line, u + st, u + sp, inverse, cy * span, cx * span, _emit_horizontal)
        # along the cuts: the label on the left of start -> end
        c, ry, rx = np.nonzero(code > 0)
        if c.size:
            k = code[c, ry, rx]
            s, e = voxel_cut.START[k], voxel_cut.END[k]
            ox = (cx[c] * B + rx) * Q
            oy = (cy[c] * B + ry) * Q
            sx_ = ox + voxel_cut.ANCHOR_UNITS[s, 0]
            sy_ = oy + voxel_cut.ANCHOR_UNITS[s, 1]
            ex_ = ox + voxel_cut.ANCHOR_UNITS[e, 0]
            ey_ = oy + voxel_cut.ANCHOR_UNITS[e, 1]
            mine, theirs = lab[c, ry, rx].astype(np.int64), other[c, ry, rx]
            keep = mine > 0
            out.append((mine[keep], sx_[keep], sy_[keep], ex_[keep], ey_[keep]))
            keep = theirs > 0
            out.append((theirs[keep], ex_[keep], ey_[keep], sx_[keep], sy_[keep]))
    if not out:
        z = np.zeros(0, np.int64)
        return [z, z, z, z, z]
    return [np.concatenate([part[i] for part in out]).astype(np.int64) for i in range(5)]


def field_loops(
    coarse, cells, lab, cut, x0: float, y0: float, unit_x: float, unit_y: float, *, keep_straight: bool = False,
):
    """Each material's loops of a flat two-level map, in world coordinates
    (``x0``, ``y0`` the map's corner, ``unit_*`` one anchor step: a fine
    cell over :data:`voxel_cut.STEPS`): {material label: (points, starts)}."""
    mat, sx, sy, ex, ey = field_edges(coarse, cells, lab, cut)
    out = {}
    for m in np.unique(mat):
        pick = mat == m
        points, starts = loops(sx[pick].astype(np.float64), sy[pick].astype(np.float64),
                               ex[pick].astype(np.float64), ey[pick].astype(np.float64), scale=1.0,
                               keep_straight=keep_straight)
        points[:, 0] = x0 + points[:, 0] * unit_x
        points[:, 1] = y0 + points[:, 1] * unit_y
        out[int(m)] = (points, starts)
    return out


# -- a section: runs along a line in every slab --------------------------------


def runs_edges(s: np.ndarray, material: np.ndarray, z: np.ndarray, s_min: float, s_max: float):
    """Boundary edges of a section drawn as runs: ``s`` (slabs, m + 1) the
    run boundaries along the cut in each slab (increasing, from ``s_min``
    to ``s_max``), ``material`` (slabs, m) the runs' materials, ``z`` the
    slab boundaries. Returns (material, sx, sy, ex, ey) with the material
    on the left of each edge."""
    out: list = []
    n = material.shape[0]
    for k in range(n):
        # between runs of one slab, and at the picture's sides
        m = material[k]
        a = np.concatenate([[_OUT], m])
        b = np.concatenate([m, [_OUT]])
        diff = a != b
        line = s[k][diff]
        count = int(diff.sum())
        _emit_vertical(out, a[diff], b[diff], line, np.full(count, z[k]), np.full(count, z[k + 1]))
    for k in range(n + 1):
        # between slab k - 1 and slab k: every stretch the two disagree on
        below_s = s[k - 1] if k > 0 else np.array([s_min, s_max])
        below_m = material[k - 1] if k > 0 else np.array([_OUT])
        above_s = s[k] if k < n else np.array([s_min, s_max])
        above_m = material[k] if k < n else np.array([_OUT])
        cuts = np.unique(np.concatenate([below_s, above_s]))
        mid = 0.5 * (cuts[:-1] + cuts[1:])
        lo = below_m[np.clip(np.searchsorted(below_s, mid, side="right") - 1, 0, below_m.size - 1)]
        hi = above_m[np.clip(np.searchsorted(above_s, mid, side="right") - 1, 0, above_m.size - 1)]
        diff = lo != hi
        if diff.any():
            count = int(diff.sum())
            _emit_horizontal(out, lo[diff], hi[diff], np.full(count, z[k]), cuts[:-1][diff], cuts[1:][diff])
    if not out:
        zero = np.zeros(0)
        return [zero.astype(np.int64), zero, zero, zero, zero]
    parts = [np.concatenate([p[i] for p in out]) for i in range(5)]
    parts[0] = parts[0].astype(np.int64)
    return parts


def runs_loops(s_list, m_list, z, s_min, s_max):
    """Each material's loops of a section given as runs per slab (lists of
    arrays): {material label: (points, starts)}."""
    n = len(m_list)
    width = max((m.size for m in m_list), default=0)
    s = np.full((n, width + 1), s_max)
    material = np.full((n, width), _OUT, dtype=np.int64)
    for k in range(n):
        s[k, : s_list[k].size] = s_list[k]
        material[k, : m_list[k].size] = m_list[k]
        # pad: a repeat of the last material over no length
        if m_list[k].size < width:
            material[k, m_list[k].size :] = m_list[k][-1] if m_list[k].size else _OUT
    mat, sx, sy, ex, ey = runs_edges(s, material, z, s_min, s_max)
    # zero-length edges from padding
    keep = (sx != ex) | (sy != ey)
    mat, sx, sy, ex, ey = mat[keep], sx[keep], sy[keep], ex[keep], ey[keep]
    out = {}
    for m in np.unique(mat):
        pick = mat == m
        out[int(m)] = loops(sx[pick], sy[pick], ex[pick], ey[pick])
    return out


def step_edges(coarse, top_h, cells, lab, code, other, h_left, h_right):
    """Where one material meets itself at another height, on the anchor
    lattice (:data:`voxel_cut.STEPS` units a fine cell): (material, x0,
    y0, x1, y1). A fine side is read in the pieces its cuts' ends split it
    into, a cut's two sides each with its own material and height, and a
    cut with one material at two heights is a step along its line."""
    from . import voxel_cut
    from .voxel import MIXED, Z_EPS

    Q = voxel_cut.STEPS
    ny, nx = coarse.shape
    # the fine cells per cell side, from the blocks' shape even when there
    # are none of them: a map with no refined cell is still on its grid
    B = lab.shape[1] if lab.ndim == 3 else 1
    span = Q * B
    out: list = []
    c64 = coarse.astype(np.int64)

    def step(a, b, ha, hb):
        with np.errstate(invalid="ignore"):
            return (a == b) & (a > 0) & (a != MIXED) & (np.abs(ha - hb) > Z_EPS)

    # plain against plain
    face = step(c64[:, :-1], c64[:, 1:], top_h[:, :-1], top_h[:, 1:])
    j, i = np.nonzero(face)
    a, _b, ln, u0, u1 = _runs(c64[j, i], np.zeros(j.size, np.int64), (i + 1) * span, j * span, span)
    out.append((a, ln, u0, ln, u1))
    face = step(c64[:-1, :], c64[1:, :], top_h[:-1, :], top_h[1:, :])
    j, i = np.nonzero(face)
    a, _b, ln, u0, u1 = _runs(c64[j, i], np.zeros(j.size, np.int64), (j + 1) * span, i * span, span)
    out.append((a, u0, ln, u1, ln))
    if cells.size:

        def fine_of(flat_cells):
            """Labels, codes, others and heights of any cells, plain ones repeated."""
            m = flat_cells.size
            L = np.empty((m, B, B), dtype=np.int64)
            K = np.zeros((m, B, B), dtype=np.int64)
            O = np.zeros((m, B, B), dtype=np.int64)
            HL = np.empty((m, B, B))
            HR = np.full((m, B, B), -np.inf)
            L[...] = c64.reshape(-1)[flat_cells][:, None, None]
            HL[...] = top_h.reshape(-1)[flat_cells][:, None, None]
            at = np.minimum(np.searchsorted(cells, flat_cells), cells.size - 1)
            hit = cells[at] == flat_cells
            L[hit], K[hit], O[hit] = lab[at[hit]], code[at[hit]], other[at[hit]]
            HL[hit], HR[hit] = h_left[at[hit]], h_right[at[hit]]
            return L, K, O, HL, HR

        def shared(A, side_a, Bn, side_b):
            st, sp, ma, mb, ha, hb = _shared(A[:3], Bn[:3], side_a, side_b, (A[3], A[4]), (Bn[3], Bn[4]))
            return st, sp, ma, ha, step(ma, mb, ha, hb) & (sp > st)

        along = np.arange(B)[None, None, :] * Q
        mixed = coarse == MIXED
        pad = np.pad(mixed, ((0, 0), (0, 1)))
        j, i = np.nonzero((pad[:, :-1] | pad[:, 1:])[:, : nx - 1])  # between i and i + 1
        A = [x[:, :, -1] for x in fine_of(j * nx + i)]
        Bn = [x[:, :, 0] for x in fine_of(j * nx + i + 1)]
        st, sp, ma, _h, sel = shared(A, 1, Bn, 3)
        x = np.broadcast_to(((i + 1) * span)[None, :, None], ma.shape)
        y = (j * span)[None, :, None] + along
        out.append((ma[sel], x[sel], (y + st)[sel], x[sel], (y + sp)[sel]))
        pad = np.pad(mixed, ((0, 1), (0, 0)))
        j, i = np.nonzero((pad[:-1, :] | pad[1:, :])[: ny - 1, :])
        A = [x[:, -1, :] for x in fine_of(j * nx + i)]
        Bn = [x[:, 0, :] for x in fine_of((j + 1) * nx + i)]
        st, sp, ma, _h, sel = shared(A, 0, Bn, 2)
        y = np.broadcast_to(((j + 1) * span)[None, :, None], ma.shape)
        x = (i * span)[None, :, None] + along
        out.append((ma[sel], (x + st)[sel], y[sel], (x + sp)[sel], y[sel]))
        cy, cx = np.divmod(cells, nx)
        if B > 1:
            # inside refined cells: right sides of all but the last column
            # against left sides of all but the first
            arrays = (lab, code, other, h_left, h_right)
            for axis in (0, 1):
                if axis == 0:
                    A = [x[:, :, :-1] for x in arrays]
                    Bn = [x[:, :, 1:] for x in arrays]
                    st, sp, ma, _h, sel = shared(A, 1, Bn, 3)  # (3, m, rows, lines)
                    _, c_, r_, q_ = np.nonzero(sel)
                    x = (cx[c_] * B + q_ + 1) * Q
                    y = (cy[c_] * B + r_) * Q
                    out.append((ma[sel], x, y + st[sel], x, y + sp[sel]))
                else:
                    A = [x[:, :-1, :] for x in arrays]
                    Bn = [x[:, 1:, :] for x in arrays]
                    st, sp, ma, _h, sel = shared(A, 0, Bn, 2)  # (3, m, lines, columns)
                    _, c_, r_, q_ = np.nonzero(sel)
                    y = (cy[c_] * B + r_ + 1) * Q
                    x = (cx[c_] * B + q_) * Q
                    out.append((ma[sel], x + st[sel], y, x + sp[sel], y))
        # a cut with one material at two heights: a step along its line
        c_, r_, q_ = np.nonzero((code > 0) & step(lab, other, h_left, h_right))
        if c_.size:
            k = code[c_, r_, q_]
            s_, e_ = voxel_cut.START[k], voxel_cut.END[k]
            ox = (cx[c_] * B + q_) * Q
            oy = (cy[c_] * B + r_) * Q
            out.append((lab[c_, r_, q_],
                        ox + voxel_cut.ANCHOR_UNITS[s_, 0], oy + voxel_cut.ANCHOR_UNITS[s_, 1],
                        ox + voxel_cut.ANCHOR_UNITS[e_, 0], oy + voxel_cut.ANCHOR_UNITS[e_, 1]))
    parts = [np.concatenate([p[i] for p in out]) for i in range(5)] if out else [np.zeros(0, np.int64)] * 5
    return parts
