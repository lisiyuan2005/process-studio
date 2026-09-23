"""The voxel model's wet etch: the etchant's arrival time, on the two-level grid.

The solver settles cells in order of arrival (a group marching method) on
a graph of *nodes*:

* every plain bulk cell of an etchable material is a node;
* inside a refined cell, every piece of one etchable material that hangs
  together (4-connected in the cell's fine grid) is a node of its own.

A barrier that runs through a refined cell splits the material there into
separate pieces, so the front cannot cross it -- yet it still runs along a
thin film that lies wholly in refined cells, piece to piece. Pieces link to
the plain cells and the pieces they touch, sideways and above and below.

Where the etchant comes from is kept at the fine level: every face between
open space the etchant is in and an etchable fine cell is an *entry face*.
Each node carries the face its front entered through and the time the
etchant stood on it, and its arrival is that time plus the straight-line
distance to the face over the material's rate -- inherited node to node
as long as the line of sight holds (the path bends at a corner where it
does not). The last step places the front at the fine level: plain cells
the front's edge crosses are refined and each fine row cut exactly
against the faces of the node and its neighbours; pieces near the edge
are settled fine cell by fine cell.

Approximations, all local: a sealed cavity inside a refined cell is taken
as a wall (plain cavities do fill when breached), and a crossing between
two etchable materials inside a refined cell is placed between the two
nodes' centres rather than on the fine interface.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

from .voxel import _unique

WALL, ETCHABLE, SOURCE, CAVITY, REFINED = 0, 1, 2, 3, 4


class Faces:
    """Entry faces: axis-aligned boxes (flat in at least one axis, or a
    point), each with the time the etchant stood on it."""

    def __init__(self) -> None:
        self.centre = np.zeros((0, 3))
        self.half = np.zeros((0, 3))
        self.lo = np.zeros((0, 3))
        self.hi = np.zeros((0, 3))
        self.time = np.zeros(0)

    def add(self, centre, half, time) -> np.ndarray:
        first = self.time.size
        centre = np.asarray(centre, float).reshape(-1, 3)
        half = np.asarray(half, float).reshape(-1, 3)
        self.centre = np.concatenate([self.centre, centre])
        self.half = np.concatenate([self.half, half])
        self.lo = np.concatenate([self.lo, centre - half])
        self.hi = np.concatenate([self.hi, centre + half])
        self.time = np.concatenate([self.time, np.asarray(time, float).reshape(-1)])
        return np.arange(first, self.time.size, dtype=np.int64)

    def nearest(self, face, point) -> np.ndarray:
        out = np.maximum(point, self.lo[face])
        np.minimum(out, self.hi[face], out=out)
        return out

    def distance(self, face, point) -> np.ndarray:
        gap = self.nearest(face, point) - point
        return np.sqrt((gap * gap).sum(axis=-1))




class Front:
    """Where the etchant got to: ``place(heights, slabs)`` says what is
    taken at a height in each slab, and ``flat_heights`` are the exact
    heights of fronts that lie level (to cut slabs at)."""

    def __init__(self, place, flat_heights) -> None:
        self.place = place
        self.flat_heights = flat_heights


def arrival(
    state, table, budget, opening, live, should_cancel: Callable[[], bool] | None,
    trace: dict | None = None, step: float | None = None,
):
    """Where the etchant gets to within ``budget``.

    Returns a :class:`Front`.
    """
    from .voxel import MIXED, VOID, Mask2, _check, _row_order, _unique, _unique_rows, components

    L, ny, nx, B = state.n, state.ny, state.nx, state.refine
    plane = ny * nx
    size = (L + 1) * plane
    x_min, y_min = state.bounds[0], state.bounds[1]
    cx, cy = state.cell_x, state.cell_y
    fx, fy = state.fine_x, state.fine_y
    thick = np.concatenate([np.diff(state.z), [cx]])
    zc = np.concatenate([0.5 * (state.z[:-1] + state.z[1:]), [state.top + 0.5 * cx]])
    zb = np.concatenate([state.z, [state.top + cx]])
    columns = opening if opening is not None else Mask2.uniform(ny, nx, B)
    slowness = np.zeros(256)
    slowness[table > 0.0] = 1.0 / table[table > 0.0]

    # -- what every cell is, at two levels ---------------------------------
    lab = np.concatenate([state.labels, np.zeros((1, ny, nx), np.uint8)])
    live_c = live.labels
    col_c = columns.coarse[None]
    kind = np.full(lab.shape, WALL, dtype=np.uint8)
    plain_etch = (lab != MIXED) & (lab != VOID) & (table[lab] > 0.0)
    kind[plain_etch] = ETCHABLE
    void = lab == VOID
    kind[void & (live_c == 1) & (col_c == 1)] = SOURCE
    kind[void & (live_c == 0)] = CAVITY
    # Open space under resist stays WALL. Cells to look at finely: refined
    # material, open space only partly reached, open space the mask edge
    # crosses.
    refined = (lab == MIXED) | (void & (live_c == MIXED)) | (void & (live_c == 1) & (col_c == 2))
    kind[refined] = REFINED
    kind_flat = kind.reshape(-1)
    lab_flat = lab.reshape(-1)
    rkeys = np.flatnonzero(refined.reshape(-1))
    R = rkeys.size
    rk, rcell = np.divmod(rkeys, plane)
    riy, rix = np.divmod(rcell, nx)
    fl = np.zeros((R, B, B), np.uint8)
    in_stack = rk < L
    if in_stack.any():
        fl[in_stack] = state.blocks(rkeys[in_stack])
    flive = live.blocks(rkeys) if R else np.zeros((0, B, B), np.uint8)
    fcol = columns.blocks_for(rcell, B) if R else np.zeros((0, B, B), bool)
    fkind = np.full((R, B, B), WALL, dtype=np.uint8)
    fkind[(fl != VOID) & (table[fl] > 0.0)] = ETCHABLE
    fkind[(fl == VOID) & (flive == 1) & fcol] = SOURCE

    # -- pieces of etchable material inside refined cells ------------------
    fnode = np.full((R, B, B), -1, dtype=np.int32)
    found: list[tuple[int, np.ndarray, np.ndarray]] = []
    next_id = size

    def add_pieces(m):
        """Number the pieces of material ``m``; their bricks and centres."""
        nonlocal next_id
        mask = (fkind == ETCHABLE) & (fl == m)
        ids = components(mask, connect_layers=False)[mask]
        # the pieces numbered 0, 1, ... (the labels are run numbers)
        used = np.zeros(int(ids.max()) + 1, dtype=bool)
        used[ids] = True
        inverse = (np.cumsum(used) - 1)[ids]
        del ids, used
        count = int(inverse.max()) + 1
        fnode[mask] = next_id + inverse
        r, by, bx = np.nonzero(mask)
        del mask
        weight = np.bincount(inverse, minlength=count).astype(float)
        brick = np.zeros(count, np.int64)
        brick[inverse] = r
        px = np.bincount(inverse, x_min + (rix[r] * B + bx + 0.5) * fx, count) / weight
        py = np.bincount(inverse, y_min + (riy[r] * B + by + 0.5) * fy, count) / weight
        found.append((int(m), brick, np.stack([px, py, zc[rk[brick]]], axis=1)))
        next_id += count

    for m in np.flatnonzero(np.bincount(fl[fkind == ETCHABLE], minlength=256)) if R else []:
        add_pieces(m)
    if (next_id) >= 2**31:
        raise MemoryError("too many pieces for this etch")
    piece_label = np.concatenate([np.full(b.size, m, np.int64) for m, b, _p in found]) if found else np.zeros(0, np.int64)
    piece_brick = np.concatenate([b for _m, b, _p in found]) if found else np.zeros(0, np.int64)
    piece_pos = np.concatenate([p for _m, _b, p in found]) if found else np.zeros((0, 3))
    del found
    pieces = next_id - size
    total = next_id

    def node_label(ids):
        out = np.empty(ids.size, np.int64)
        grid = ids < size
        out[grid] = lab_flat[ids[grid]]
        out[~grid] = piece_label[ids[~grid] - size]
        return out

    def node_pos(ids):
        out = np.empty((ids.size, 3))
        grid = ids < size
        g = ids[grid]
        k, rest = np.divmod(g, plane)
        iy, ix = np.divmod(rest, nx)
        out[grid] = np.stack([x_min + (ix + 0.5) * cx, y_min + (iy + 0.5) * cy, zc[k]], 1)
        out[~grid] = piece_pos[ids[~grid] - size]
        return out

    # -- links that involve pieces (plain-to-plain ones are the grid's) ----
    link_a: list[np.ndarray] = []
    link_b: list[np.ndarray] = []

    def refined_at(keys):
        if R == 0:
            return np.zeros(keys.size, np.int64), np.zeros(keys.size, bool)
        at = np.minimum(np.searchsorted(rkeys, keys), R - 1)
        return at, rkeys[at] == keys

    def facing(dx, dy, dk, part):
        """For the refined cells ``part`` with a neighbour that way: my fine
        cells on that side and the neighbour's fine cells facing them (a
        plain neighbour repeated), as (mine index, their kind/node/label
        arrays, my kind/node/label arrays, the neighbour key)."""
        if dk:
            ok = (rk[part] + dk >= 0) & (rk[part] + dk <= L)
        else:
            ok = (rix[part] + dx >= 0) & (rix[part] + dx < nx) & (riy[part] + dy >= 0) & (riy[part] + dy < ny)
        idx = part[ok]
        other = (rk[idx] + dk) * plane + (riy[idx] + dy) * nx + rix[idx] + dx
        at, hit = refined_at(other)

        def side(array, rows, flip):
            if dk:
                return array[rows]
            if dx == 1:
                return array[rows, :, 0 if flip else -1]
            if dx == -1:
                return array[rows, :, -1 if flip else 0]
            if dy == 1:
                return array[rows, 0 if flip else -1, :]
            return array[rows, -1 if flip else 0, :]

        mine_kind, mine_node, mine_lab = side(fkind, idx, False), side(fnode, idx, False), side(fl, idx, False)
        shape = mine_kind.shape
        their_kind = np.broadcast_to(kind_flat[other].reshape((-1,) + (1,) * (len(shape) - 1)), shape).copy()
        their_node = np.where(their_kind == ETCHABLE, np.broadcast_to(other.reshape((-1,) + (1,) * (len(shape) - 1)), shape), -1).astype(np.int64)
        their_lab = np.broadcast_to(lab_flat[other].reshape((-1,) + (1,) * (len(shape) - 1)), shape).copy()
        if hit.any():
            their_kind[hit] = side(fkind, at[hit], True)
            their_node[hit] = side(fnode, at[hit], True)
            their_lab[hit] = side(fl, at[hit], True)
        return idx, other, hit, mine_kind, mine_node, mine_lab, their_kind, their_node, their_lab

    directions = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    # Entry faces on the fine lattice: (node, orientation, plane, u0, u1,
    # v0, v1). An x-face lies on fine line x = plane, spanning fine rows
    # u and slabs v; a y-face likewise across; a z-face lies on slab
    # boundary ``plane``, spanning fine columns u and fine rows v.
    lattice: list[np.ndarray] = []

    def entry(node, orient, plane_at, u0, u1, v0, v1):
        n = np.asarray(node).size
        lattice.append(
            np.stack(
                [np.asarray(node, np.int64), np.full(n, orient, np.int64)]
                + [np.broadcast_to(np.asarray(a, np.int64), (n,)) for a in (plane_at, u0, u1, v0, v1)],
                1,
            )
        )

    def border(dx, dy, dk, part):
        idx, other, hit, mk, mn, ml, tk, tn, tl = facing(dx, dy, dk, part)
        link = (mn >= 0) & (tn >= 0) & (ml == tl)
        if link.any():
            a, b = _unique_pairs(mn[link], tn[link])
            link_a.append(a)
            link_b.append(b)
        # Entry faces on my side of this border: my etchable against their
        # open space, and a plain etchable neighbour against my open space.
        for mine, theirs, node, rows_from_mine in ((mk, tk, mn, True), (tk, mk, tn, False)):
            enter = (mine == ETCHABLE) & (theirs == SOURCE)
            if not rows_from_mine:
                # only where the neighbour is plain: a refined one reports its own
                enter &= ~np.broadcast_to(hit.reshape((-1,) + (1,) * (mine.ndim - 1)), mine.shape)
            if not enter.any():
                continue
            where = np.nonzero(enter)
            r = idx[where[0]]
            k = rk[r]
            if dk:
                by, bx = where[1], where[2]
                u0 = rix[r] * B + bx
                v0 = riy[r] * B + by
                entry(node[enter], 2, k + (1 if dk > 0 else 0), u0, u0 + 1, v0, v0 + 1)
            elif dx:
                by = where[1]
                u0 = riy[r] * B + by
                entry(node[enter], 0, (rix[r] + (1 if dx > 0 else 0)) * B, u0, u0 + 1, k, k + 1)
            else:
                bx = where[1]
                u0 = rix[r] * B + bx
                entry(node[enter], 1, (riy[r] + (1 if dy > 0 else 0)) * B, u0, u0 + 1, k, k + 1)

    for dx, dy, dk in directions:
        for start in range(0, R, 4096):  # a few million fine cells at a time
            border(dx, dy, dk, np.arange(start, min(R, start + 4096)))
    # Inside refined cells, sideways.
    if R:
        for axis in (2, 1):
            a = fkind[:, :, :-1] if axis == 2 else fkind[:, :-1, :]
            b = fkind[:, :, 1:] if axis == 2 else fkind[:, 1:, :]
            na = fnode[:, :, :-1] if axis == 2 else fnode[:, :-1, :]
            nb = fnode[:, :, 1:] if axis == 2 else fnode[:, 1:, :]
            for mine, theirs, node in ((a, b, na), (b, a, nb)):
                enter = (mine == ETCHABLE) & (theirs == SOURCE)
                if not enter.any():
                    continue
                r, i, j = np.nonzero(enter)
                k = rk[r]
                if axis == 2:  # between fine columns j and j+1 of row i
                    u0 = riy[r] * B + i
                    entry(node[enter], 0, rix[r] * B + j + 1, u0, u0 + 1, k, k + 1)
                else:
                    u0 = rix[r] * B + j
                    entry(node[enter], 1, riy[r] * B + i + 1, u0, u0 + 1, k, k + 1)
    # Plain etchable against plain open space.
    kind3 = kind.reshape(L + 1, ny, nx)
    for dx, dy, dk in directions:
        me = kind3 == ETCHABLE
        there = np.zeros_like(me)
        src = kind3 == SOURCE
        sl_me = [slice(None)] * 3
        sl_th = [slice(None)] * 3
        for axis, d in ((0, dk), (1, dy), (2, dx)):
            if d > 0:
                sl_me[axis], sl_th[axis] = slice(0, -1), slice(1, None)
            elif d < 0:
                sl_me[axis], sl_th[axis] = slice(1, None), slice(0, -1)
        there[tuple(sl_me)] = src[tuple(sl_th)]
        k, iy, ix = np.nonzero(me & there)
        if k.size == 0:
            continue
        node = k * plane + iy * nx + ix
        if dk:
            entry(node, 2, k + (1 if dk > 0 else 0), ix * B, ix * B + B, iy * B, iy * B + B)
        elif dx:
            entry(node, 0, (ix + (1 if dx > 0 else 0)) * B, iy * B, iy * B + B, k, k + 1)
        else:
            entry(node, 1, (iy + (1 if dy > 0 else 0)) * B, ix * B, ix * B + B, k, k + 1)

    # One source surface, not thousands of fine squares: coplanar entry
    # faces that touch are joined into the largest rectangles a row-then-
    # column sweep finds. A node inherits one face; were it one fine
    # square, a point further on would measure to that square instead of to
    # the edge of the opening it really is nearest to.
    faces = Faces()
    if lattice:
        table_e = np.concatenate(lattice)
        entry_node = table_e[:, 0]
        record = table_e[:, 1:]
        record, which = _unique_rows(record)
        # Along u: same orientation, plane and v span, touching in u.
        order = _row_order(record[:, [0, 1, 4, 5, 2]])
        rec = record[order]
        key_same = np.ones(rec.shape[0], dtype=bool)
        key_same[1:] = (
            (rec[1:, 0] == rec[:-1, 0]) & (rec[1:, 1] == rec[:-1, 1])
            & (rec[1:, 4] == rec[:-1, 4]) & (rec[1:, 5] == rec[:-1, 5])
        )
        start = np.ones(rec.shape[0], dtype=bool)
        start[1:] = ~key_same[1:] | (rec[1:, 2] > rec[:-1, 3])
        group = np.cumsum(start) - 1
        runs = np.zeros((int(group.max()) + 1 if group.size else 0, 6), np.int64)
        runs[group] = rec
        # sorted by u, so a run starts at its first face
        begins = np.flatnonzero(start)
        runs[:, 2], runs[:, 3] = rec[begins, 2], np.maximum.reduceat(rec[:, 3], begins)
        run_of = np.empty(rec.shape[0], np.int64)
        run_of[order] = group
        # Along v: same orientation, plane and u span, touching in v.
        order2 = _row_order(runs[:, [0, 1, 2, 3, 4]])
        rr = runs[order2]
        same2 = np.ones(rr.shape[0], dtype=bool)
        same2[1:] = (
            (rr[1:, 0] == rr[:-1, 0]) & (rr[1:, 1] == rr[:-1, 1])
            & (rr[1:, 2] == rr[:-1, 2]) & (rr[1:, 3] == rr[:-1, 3])
        )
        start2 = np.ones(rr.shape[0], dtype=bool)
        start2[1:] = ~same2[1:] | (rr[1:, 4] > rr[:-1, 5])
        group2 = np.cumsum(start2) - 1
        rects = np.zeros((int(group2.max()) + 1 if group2.size else 0, 6), np.int64)
        rects[group2] = rr
        begins2 = np.flatnonzero(start2)
        rects[:, 4], rects[:, 5] = rr[begins2, 4], np.maximum.reduceat(rr[:, 5], begins2)
        rect_of_run = np.empty(runs.shape[0], np.int64)
        rect_of_run[order2] = group2
        rect_of_entry = rect_of_run[run_of[which]]
        o, at, u0, u1, v0, v1 = (rects[:, i] for i in range(6))
        centre = np.zeros((rects.shape[0], 3))
        half = np.zeros((rects.shape[0], 3))
        xf, yf, zf = o == 0, o == 1, o == 2
        # x-faces: at x line, u along y, v over slabs
        centre[xf] = np.stack([x_min + at[xf] * fx, y_min + 0.5 * (u0[xf] + u1[xf]) * fy, 0.5 * (zb[v0[xf]] + zb[v1[xf]])], 1)
        half[xf] = np.stack([np.zeros(xf.sum()), 0.5 * (u1[xf] - u0[xf]) * fy, 0.5 * (zb[v1[xf]] - zb[v0[xf]])], 1)
        centre[yf] = np.stack([x_min + 0.5 * (u0[yf] + u1[yf]) * fx, y_min + at[yf] * fy, 0.5 * (zb[v0[yf]] + zb[v1[yf]])], 1)
        half[yf] = np.stack([0.5 * (u1[yf] - u0[yf]) * fx, np.zeros(yf.sum()), 0.5 * (zb[v1[yf]] - zb[v0[yf]])], 1)
        centre[zf] = np.stack([x_min + 0.5 * (u0[zf] + u1[zf]) * fx, y_min + 0.5 * (v0[zf] + v1[zf]) * fy, zb[at[zf]]], 1)
        half[zf] = np.stack([0.5 * (u1[zf] - u0[zf]) * fx, 0.5 * (v1[zf] - v0[zf]) * fy, np.zeros(zf.sum())], 1)
        first_face = faces.add(centre, half, np.zeros(rects.shape[0]))
        entry_node, entry_face = _unique_pairs(entry_node, first_face[rect_of_entry])
        if B > 1:
            adj_ptr, adj_idx = _touching(record, first_face[rect_of_run[run_of]], rects.shape[0])
        else:  # nodes a fine cell apart need no walk
            adj_ptr, adj_idx = np.zeros(rects.shape[0] + 1, np.int64), np.zeros(0, np.int64)
    else:
        entry_node = np.zeros(0, np.int64)
        entry_face = np.zeros(0, np.int64)
        adj_ptr, adj_idx = np.zeros(1, np.int64), np.zeros(0, np.int64)
    n_entry = adj_ptr.size - 1

    def climb(face, point, path=None):
        """Walk each entry face over the entry faces it touches to the one
        nearest ``point``: a node's neighbour's face is nearest to the
        neighbour, not always to the node. The distance to a curved wall
        falls steadily along it towards the nearest spot, so a walk to the
        nearest touching face, while it gets closer, gets there. Each step
        taken is added to ``path`` as (row, face)."""
        face = face.copy()
        active = np.flatnonzero(face < n_entry)
        if active.size == 0:
            return face
        dist = faces.distance(face[active], point[active])
        while active.size:
            f = face[active]
            lo = adj_ptr[f]
            many = adj_ptr[f + 1] - lo
            if not many.any():
                break
            rows = np.repeat(np.arange(active.size), many)
            at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
            cand = adj_idx[at]
            dc = faces.distance(cand, point[active[rows]])
            order = np.lexsort((dc, rows))
            first = np.ones(order.size, dtype=bool)
            first[1:] = rows[order][1:] != rows[order][:-1]
            pick = order[first]
            best = np.full(active.size, np.inf)
            best_face = np.full(active.size, -1, dtype=np.int64)
            best[rows[pick]] = dc[pick]
            best_face[rows[pick]] = cand[pick]
            better = best < dist - 1e-12 * (1.0 + dist)
            active, dist = active[better], best[better]
            face[active] = best_face[better]
            if path is not None:
                path.append(np.stack([active, face[active]], 1))
        return face

    # Links, both ways, sorted for lookup.
    if link_a:
        a = np.concatenate(link_a)
        b = np.concatenate(link_b)
        link_src, link_dst = _unique_pairs(np.concatenate([a, b]), np.concatenate([b, a]))
    else:
        link_src = link_dst = np.zeros(0, np.int64)

    # -- the march ----------------------------------------------------------
    T = np.full(total, np.inf)
    face_of = np.full(total, -1, dtype=np.int64)
    known = np.zeros(total, dtype=bool)
    queued = np.zeros(total, dtype=bool)
    sources = np.flatnonzero(kind_flat == SOURCE)
    T[sources] = 0.0
    known[sources] = True
    is_etch = np.zeros(total, dtype=bool)
    is_etch[:size] = kind_flat == ETCHABLE
    is_etch[size:] = True

    cavity_cells = cavity_ids = cavity_of = None
    if (kind_flat == CAVITY).any():
        cavity_of = components(kind3 == CAVITY).reshape(-1)
        cavity_cells = np.flatnonzero(kind_flat == CAVITY)
        ids = cavity_of[cavity_cells]
        order = np.argsort(ids, kind="stable")
        cavity_cells, cavity_ids = cavity_cells[order], ids[order]

    probe = 1.25 * min(cx, cy, float(np.min(thick)) if thick.size else cx)
    steps = ((-1, 0, -1), (1, 0, 1), (-nx, 1, -1), (nx, 1, 1), (-plane, 2, -1), (plane, 2, 1))

    def grid_where(cells):
        k, rest = np.divmod(cells, plane)
        y, x = np.divmod(rest, nx)
        inside = (x > 0, x < nx - 1, y > 0, y < ny - 1, k > 0, k < L)
        half = np.stack([np.full(cells.size, 0.5 * cx), np.full(cells.size, 0.5 * cy), 0.5 * thick[k]], 1)
        return inside, half

    def node_at(points):
        """The node holding each point (-1 for none) and whether it is open to the etchant."""
        ix = np.floor((points[:, 0] - x_min) / cx).astype(np.int64)
        iy = np.floor((points[:, 1] - y_min) / cy).astype(np.int64)
        iz = np.searchsorted(state.z, points[:, 2], side="right") - 1
        iz = np.where(points[:, 2] >= state.top, L, iz)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny) & (iz >= 0)
        key = np.where(ok, iz * plane + iy * nx + ix, 0)
        kc = np.where(ok, kind_flat[key], WALL)
        node = np.where(kc == ETCHABLE, key, -1)
        open_ = (kc == SOURCE) | ((kc == CAVITY) & known[np.minimum(key, size - 1)])
        fine = kc == REFINED
        if fine.any():
            at, hit = refined_at(key[fine])
            bx = np.clip(((points[fine, 0] - x_min) / fx).astype(np.int64) - ix[fine] * B, 0, B - 1)
            by = np.clip(((points[fine, 1] - y_min) / fy).astype(np.int64) - iy[fine] * B, 0, B - 1)
            node[fine] = np.where(hit, fnode[at, by, bx], -1)
            open_[fine] = hit & (fkind[at, by, bx] == SOURCE)
        return node, open_

    def update(cells):
        """Best arrival for each node from its settled neighbours, and how."""
        p = node_pos(cells)
        mylab = node_label(cells)
        s = slowness[mylab]
        best = np.full(cells.size, np.inf)
        how = np.full(cells.size, -1, dtype=np.int64)
        fresh = np.zeros(cells.size, dtype=bool)
        new_centre = np.zeros((cells.size, 3))
        new_half = np.zeros((cells.size, 3))
        new_time = np.zeros(cells.size)

        def offer(rows, t, face=None, centre=None, half=None, time=None):
            better = t < best[rows]
            if not better.any():
                return
            chosen = rows[better]
            best[chosen] = t[better]
            if face is not None:
                how[chosen] = face[better]
                fresh[chosen] = False
            else:
                how[chosen] = -1
                fresh[chosen] = True
                new_centre[chosen] = centre[better]
                new_half[chosen] = half[better]
                new_time[chosen] = time[better]

        def sight(rows, f):
            """Whether the way from each node to face ``f`` starts through
            settled material of its own kind or open space."""
            gap = faces.nearest(f, p[rows]) - p[rows]
            length = np.sqrt((gap * gap).sum(axis=1))
            clear = np.ones(rows.size, dtype=bool)
            far = length > probe
            if far.any():
                ahead = p[rows[far]] + gap[far] * (probe / length[far])[:, None]
                node, open_ = node_at(ahead)
                ok = node >= 0
                safe = np.where(ok, node, 0)
                clear[far] = (ok & known[safe] & (node_label(safe) == mylab[rows[far]])) | open_ | (node == cells[rows[far]])
            return clear

        def inherit(rows, nbrs):
            """Through settled neighbours of the same material: their face,
            in line of sight, else a corner at the neighbour."""
            f = face_of[nbrs]
            has = f >= 0
            rows, nbrs, f = rows[has], nbrs[has], f[has]
            if rows.size == 0:
                return
            target = faces.nearest(f, p[rows])
            gap = target - p[rows]
            length = np.sqrt((gap * gap).sum(axis=1))
            t = faces.time[f] + s[rows] * length
            clear = sight(rows, f)
            offer(rows[clear], t[clear], face=f[clear])
            bend = ~clear
            if bend.any():
                pn = node_pos(nbrs[bend])
                d = pn - p[rows[bend]]
                t = T[nbrs[bend]] + s[rows[bend]] * np.sqrt((d * d).sum(axis=1))
                offer(rows[bend], t, centre=pn, half=np.zeros_like(pn), time=T[nbrs[bend]])

        grid = np.flatnonzero(cells < size)
        if grid.size:
            g = cells[grid]
            inside, half = grid_where(g)
            pg = p[grid]
            for (step, axis, sign), ok in zip(steps, inside):
                n = np.where(ok, g + step, g)
                settled = ok & known[n]
                if not settled.any():
                    continue
                kn = kind_flat[n]
                open_side = settled & ((kn == SOURCE) | (kn == CAVITY))
                if open_side.any():
                    rows = grid[open_side]
                    centre = pg[open_side].copy()
                    centre[:, axis] += sign * half[open_side, axis]
                    fh = half[open_side].copy()
                    fh[:, axis] = 0.0
                    enter = T[n[open_side]]
                    offer(rows, enter + s[rows] * half[open_side, axis], centre=centre, half=fh, time=enter)
                etch_n = settled & (kn == ETCHABLE)
                same = etch_n & (lab_flat[n] == mylab[grid])
                if same.any():
                    inherit(grid[same], n[same])
                other = etch_n & ~same
                if other.any():
                    rows = grid[other]
                    nn = n[other]
                    enter = T[nn] + slowness[lab_flat[nn]] * half[other, axis]
                    centre = pg[other].copy()
                    centre[:, axis] += sign * half[other, axis]
                    fh = half[other].copy()
                    fh[:, axis] = 0.0
                    offer(rows, enter + s[rows] * half[other, axis], centre=centre, half=fh, time=enter)
        if link_src.size:
            lo = np.searchsorted(link_src, cells, side="left")
            hi = np.searchsorted(link_src, cells, side="right")
            many = hi - lo
            if many.any():
                rows = np.repeat(np.arange(cells.size), many)
                at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                nbrs = link_dst[at]
                settled = known[nbrs]
                rows, nbrs = rows[settled], nbrs[settled]
                same = node_label(nbrs) == mylab[rows]
                inherit(rows[same], nbrs[same])
                if (~same).any():
                    r2, n2 = rows[~same], nbrs[~same]
                    pn = node_pos(n2)
                    mid = 0.5 * (pn + p[r2])
                    d = np.sqrt(((pn - p[r2]) ** 2).sum(axis=1))
                    enter = T[n2] + slowness[node_label(n2)] * 0.5 * d
                    offer(r2, enter + s[r2] * 0.5 * d, centre=mid, half=np.zeros_like(mid), time=enter)
        return best, how, fresh, new_centre, new_half, new_time

    trial = np.zeros(0, dtype=np.int64)

    def settle(cells):
        nonlocal trial
        if cells.size == 0:
            return
        best, how, fresh, centre, half, time = update(cells)
        improve = best < T[cells]
        if not improve.any():
            return
        cells, best, how, fresh = cells[improve], best[improve], how[improve], fresh[improve]
        T[cells] = best
        made = faces.add(centre[improve][fresh], half[improve][fresh], time[improve][fresh])
        how[fresh] = made
        face_of[cells] = how
        new = cells[~queued[cells]]
        queued[new] = True
        trial = np.concatenate([trial, new])

    stamp = np.zeros(total, dtype=np.int64)

    def reachable(fronts):
        found = []
        grid = fronts[fronts < size]
        if grid.size:
            k, rest = np.divmod(grid, plane)
            y, x = np.divmod(rest, nx)
            inside = (x > 0, x < nx - 1, y > 0, y < ny - 1, k > 0, k < L)
            found.append(np.concatenate([grid[ok] + step for (step, _a, _s), ok in zip(steps, inside)]))
        if link_src.size:
            lo = np.searchsorted(link_src, fronts, side="left")
            hi = np.searchsorted(link_src, fronts, side="right")
            many = hi - lo
            if many.any():
                at = np.arange(many.sum()) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                found.append(link_dst[at])
        if not found:
            return np.zeros(0, np.int64)
        found = np.concatenate(found)
        found = found[is_etch[found] & ~known[found]]
        places = np.arange(found.size, dtype=np.int64)
        stamp[found] = places
        return found[stamp[found] == places]

    # Seed: every node an entry face enters, at its distance from the face.
    if entry_node.size:
        s_entry = slowness[node_label(entry_node)]
        t_entry = s_entry * faces.distance(entry_face, node_pos(entry_node))
        order = np.lexsort((t_entry, entry_node))
        first = np.ones(order.size, dtype=bool)
        first[1:] = entry_node[order][1:] != entry_node[order][:-1]
        chosen = order[first]
        nodes = entry_node[chosen]
        better = t_entry[chosen] < T[nodes]
        nodes = nodes[better]
        T[nodes] = t_entry[chosen][better]
        face_of[nodes] = entry_face[chosen][better]
        queued[nodes] = True
        trial = nodes.copy()
    settle(reachable(sources))
    rates = slowness[slowness > 0.0]
    delta = 0.5 * min(cx, cy) * (rates.min() if rates.size else 1.0)
    # On past the budget by a node's reach: a node whose centre the front
    # misses can still have a corner, or the top of its slab, within it.
    # Slabs no thicker than ``step`` may be placed at other heights than
    # their middle (cut where the front curves or lies level); thicker ones
    # the front crosses upright, and their middle is where the node is.
    step = float(step) if step else float(np.max(thick[:L], initial=cx))
    tall = np.where(thick[:L] <= step * (1 + 1e-9), 0.5 * thick[:L], 0.0)
    past = (rates.max() if rates.size else 1.0) * (math.hypot(cx, cy) + float(np.max(tall, initial=0.0)))
    while trial.size:
        _check(should_cancel, "an isotropic etch")
        times = T[trial]
        first_time = float(times.min())
        if first_time > budget + past:
            break
        take = times <= first_time + delta
        group = trial[take]
        trial = trial[~take]
        queued[group] = False
        known[group] = True
        fronts = [group]
        grid = group[group < size]
        if cavity_cells is not None and grid.size:
            k, rest = np.divmod(grid, plane)
            y, x = np.divmod(rest, nx)
            inside = (x > 0, x < nx - 1, y > 0, y < ny - 1, k > 0, k < L)
            wall = np.concatenate([grid[ok] for ok in inside])
            hit = np.concatenate([grid[ok] + step for (step, _a, _s), ok in zip(steps, inside)])
            breach = (kind_flat[hit] == CAVITY) & ~known[hit]
            wall, hit = wall[breach], hit[breach]
            for piece in _unique(cavity_of[hit]):
                lo = np.searchsorted(cavity_ids, piece, side="left")
                hi = np.searchsorted(cavity_ids, piece, side="right")
                cells = cavity_cells[lo:hi]
                through = wall[cavity_of[hit] == piece]
                T[cells] = float(np.min(T[through] + 0.5 * min(cx, cy) * slowness[lab_flat[through]]))
                known[cells] = True
                fronts.append(cells)
        settle(reachable(np.concatenate(fronts)))

    if trace is not None:
        trace.update(T=T, face_of=face_of, faces=faces, fnode=fnode, rkeys=rkeys, size=size,
                     piece_pos=piece_pos, entry_node=entry_node, entry_face=entry_face, known=known,
                     adj_ptr=adj_ptr, adj_idx=adj_idx)
    # -- place the front at the fine level ---------------------------------
    # The march settles nodes; where the front is is placed from the faces,
    # at whatever height the caller asks, so a slab can be cut finely in z
    # where the front curves and exactly where it lies flat.
    grid_T = T[:size].reshape(L + 1, ny, nx)[:L]
    grid_etch = (kind3 == ETCHABLE)[:L]
    grid_s = slowness[lab[:L]]
    reach_xy = 0.5 * math.hypot(cx, cy)
    # every point of a cell, up and down its slab too
    reach3 = np.sqrt(reach_xy**2 + tall**2)[:, None, None]
    whole = grid_etch & (grid_T + grid_s * reach3 <= budget)
    band = grid_etch & ~whole & np.isfinite(grid_T) & (grid_T - grid_s * reach3 <= budget)

    def candidates(nodes, walk=True):
        """(row, face) pairs: each node's own face and its settled same-material neighbours' faces."""
        rows_list, face_list = [], []
        own = face_of[nodes]
        ok = own >= 0
        rows_list.append(np.flatnonzero(ok))
        face_list.append(own[ok])
        labels_here = node_label(nodes)
        grid_rows = np.flatnonzero(nodes < size)
        if grid_rows.size:
            g = nodes[grid_rows]
            inside, _half = grid_where(g)
            for (step, _axis, _sign), okd in zip(steps, inside):
                n = np.where(okd, g + step, g)
                use = okd & known[n] & (kind_flat[n] == ETCHABLE) & (lab_flat[n] == labels_here[grid_rows]) & (face_of[n] >= 0)
                rows_list.append(grid_rows[use])
                face_list.append(face_of[n[use]])
        if link_src.size:
            lo = np.searchsorted(link_src, nodes, side="left")
            hi = np.searchsorted(link_src, nodes, side="right")
            many = hi - lo
            if many.any():
                rows = np.repeat(np.arange(nodes.size), many)
                at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                nb = link_dst[at]
                use = known[nb] & (node_label(nb) == labels_here[rows]) & (face_of[nb] >= 0)
                rows_list.append(rows[use])
                face_list.append(face_of[nb[use]])
        # entry faces of the node itself
        if entry_node.size:
            order = np.argsort(entry_node, kind="stable")
            en = entry_node[order]
            lo = np.searchsorted(en, nodes, side="left")
            hi = np.searchsorted(en, nodes, side="right")
            many = hi - lo
            if many.any():
                rows = np.repeat(np.arange(nodes.size), many)
                at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                rows_list.append(rows)
                face_list.append(entry_face[order][at])
        # The faces nearest the cell's other points lie between, on the
        # walks from its own face to the cell's centre and corners. Cells
        # side by side share corners, and mostly their face too: each walk
        # is taken once.
        if walk and rows_list[0].size:
            own_rows = rows_list[0]
            own_face = face_list[0]
            k, rest = np.divmod(nodes[own_rows], plane)
            iy, ix = np.divmod(rest, nx)
            corners = (nx + 1) * (ny + 1)
            span = corners + nx * ny  # corners, then centres, per slab
            ids = [k * span + (iy + ay) * (nx + 1) + ix + ax for ax, ay in ((0, 0), (1, 0), (0, 1), (1, 1))]
            ids.append(k * span + corners + iy * nx + ix)
            owner = np.tile(own_rows, 5)
            walks, which = _unique_rows(np.stack([np.concatenate(ids), np.tile(own_face, 5)], 1))
            pk, rest = np.divmod(walks[:, 0], span)
            centre = rest >= corners
            cy_, cx_ = np.divmod(np.where(centre, rest - corners, 0), nx)
            py, px = np.divmod(np.where(centre, 0, rest), nx + 1)
            point = np.stack(
                [
                    np.where(centre, x_min + (cx_ + 0.5) * cx, x_min + px * cx),
                    np.where(centre, y_min + (cy_ + 0.5) * cy, y_min + py * cy),
                    zc[pk],  # at the node's height: the heights placed later are near it
                ],
                1,
            )
            steps_taken: list[np.ndarray] = []
            climb(walks[:, 1], point, steps_taken)
            if steps_taken:
                taken = np.concatenate(steps_taken)  # (walk, face)
                # every row whose walk this was
                order = np.argsort(which, kind="stable")
                first = np.searchsorted(which[order], taken[:, 0], side="left")
                many = np.searchsorted(which[order], taken[:, 0], side="right") - first
                at = np.arange(many.sum()) - np.repeat(np.cumsum(many) - many, many) + np.repeat(first, many)
                rows_list.append(owner[order][at])
                face_list.append(np.repeat(taken[:, 1], many))
        count = faces.time.size
        code = _unique(np.concatenate(rows_list) * count + np.concatenate(face_list))
        return code // count, code % count

    # Plain cells near the front. Under a flat front -- the cell's face lies
    # level over the whole cell and each neighbour sideways is a wall or
    # came through a level face of the same height and time -- the front is
    # a plane at an exact height, the same for every fine row.
    band_keys = np.flatnonzero(band.reshape(-1))
    own = face_of[band_keys]
    bk, rest = np.divmod(band_keys, plane)
    biy, bix = np.divmod(rest, nx)

    def level(f, iy_, ix_):
        safe = np.maximum(f, 0)
        lo, hi = faces.lo[safe], faces.hi[safe]
        return (
            (f >= 0) & (hi[:, 2] == lo[:, 2])
            & (lo[:, 0] <= x_min + ix_ * cx + 1e-12) & (hi[:, 0] >= x_min + (ix_ + 1) * cx - 1e-12)
            & (lo[:, 1] <= y_min + iy_ * cy + 1e-12) & (hi[:, 1] >= y_min + (iy_ + 1) * cy - 1e-12)
        )

    flat = level(own, biy, bix)
    safe_own = np.maximum(own, 0)
    for step, dy_, dx_, ok in ((-1, 0, -1, bix > 0), (1, 0, 1, bix < nx - 1), (-nx, -1, 0, biy > 0), (nx, 1, 0, biy < ny - 1)):
        n = np.where(ok, band_keys + step, band_keys)
        kn = kind_flat[n]
        fn = face_of[n]
        same = (
            (kn == ETCHABLE) & level(fn, biy + dy_, bix + dx_)
            & (faces.lo[np.maximum(fn, 0), 2] == faces.lo[safe_own, 2])
            & (np.abs(faces.time[np.maximum(fn, 0)] - faces.time[safe_own]) <= 1e-12 * (1.0 + np.abs(faces.time[safe_own])))
        )
        flat &= ~ok | (kn == WALL) | same
    flat_keys = band_keys[flat]
    fk = bk[flat]
    f_own = own[flat]
    f_plane = faces.lo[f_own, 2]
    f_side = np.where(f_plane > zc[fk], 1.0, -1.0)  # +1: the face is above, the front goes down
    f_height = f_plane - f_side * (budget - faces.time[f_own]) / slowness[lab_flat[flat_keys]]
    inside = (f_height > zb[fk] + 1e-12) & (f_height < zb[fk + 1] - 1e-12)
    flat_heights = np.unique(np.round(f_height[inside], 9))
    band_keys = band_keys[~flat]
    if band_keys.size:
        band_rows, band_faces = candidates(band_keys)
    band_k, rest = np.divmod(band_keys, plane)
    band_iy, band_ix = np.divmod(rest, nx)

    # Pieces in refined cells: whole, none, or fine cell by fine cell.
    if pieces:
        ids = np.arange(size, total)
        t = T[ids]
        s_piece = slowness[piece_label]
        # a piece's centre can be anywhere in its cell: a whole diagonal away
        spread = np.sqrt((2.0 * reach_xy) ** 2 + tall[np.minimum(rk[piece_brick], L - 1)] ** 2)
        whole_piece = t + s_piece * spread <= budget
        maybe = np.isfinite(t) & ~whole_piece & (t - s_piece * spread <= budget)
        close = ids[maybe]
        piece_row = np.full(pieces, -1, dtype=np.int64)
        piece_row[close - size] = np.arange(close.size)
        if close.size:
            # a piece is a fine cell or so from its neighbours: no walk
            rows, fcs = candidates(close, walk=False)
            order = np.argsort(rows, kind="stable")
            piece_rows, piece_faces = rows[order], fcs[order]

    def place(heights, subset, skip_flat=False):
        """Where the front is at ``heights`` (one per slab), in the slabs
        ``subset``: (wholly taken plain cells (slabs, ny, nx), keys of the
        cells taken in part, their fine cells taken)."""
        out_whole = whole & subset[:, None, None]
        out_keys: list[np.ndarray] = []
        out_blocks: list[np.ndarray] = []
        if flat_keys.size and not skip_flat:
            go = subset[fk] & (f_side * (heights[fk] - f_height) >= 0.0)
            out_whole.reshape(-1)[flat_keys[go]] = True
        chosen = subset[band_k]
        if chosen.any():
            keys_here = band_keys[chosen]
            index = np.full(band_keys.size, -1, dtype=np.int64)
            index[chosen] = np.arange(int(chosen.sum()))
            pick = index[band_rows] >= 0
            rows, fcs = index[band_rows[pick]], band_faces[pick]
            k, iy, ix = band_k[chosen], band_iy[chosen], band_ix[chosen]
            counts = np.zeros((keys_here.size, B, B + 1), dtype=np.int32)
            s_rows = slowness[lab_flat[keys_here]][rows]
            radius = (budget - faces.time[fcs]) / s_rows
            good = radius >= 0.0
            rows, fcs, radius = rows[good], fcs[good], radius[good]
            by = np.arange(B)
            c0 = faces.centre[fcs]
            h0 = faces.half[fcs]
            yv = y_min + (iy[rows][:, None] * B + by[None, :] + 0.5) * fy
            dyv = np.maximum(np.abs(yv - c0[:, 1:2]) - h0[:, 1:2], 0.0)
            dz = np.maximum(np.abs(heights[k[rows]] - c0[:, 2]) - h0[:, 2], 0.0)[:, None]
            w2 = radius[:, None] ** 2 - dyv**2 - dz**2
            ok = w2 >= 0.0
            w = np.sqrt(np.maximum(w2, 0.0))
            lo = c0[:, 0:1] - h0[:, 0:1] - w
            hi = c0[:, 0:1] + h0[:, 0:1] + w
            base = ix[rows][:, None] * B
            first_col = np.clip(np.ceil((lo - x_min) / fx - 0.5 - 1e-9).astype(np.int64) - base, 0, B)
            last_col = np.clip(np.floor((hi - x_min) / fx - 0.5 + 1e-9).astype(np.int64) - base, -1, B - 1)
            ok &= last_col >= first_col
            m, r = np.nonzero(ok)
            at = (rows[m] * B + r) * (B + 1)
            length = keys_here.size * B * (B + 1)
            counts = np.bincount(at + first_col[m, r], minlength=length) - np.bincount(
                at + last_col[m, r] + 1, minlength=length
            )
            taken = np.cumsum(counts.reshape(keys_here.size, B, B + 1), axis=2)[:, :, :B] > 0
            out_keys.append(keys_here)
            out_blocks.append(taken)
        if pieces:
            bricks_here = np.flatnonzero(subset[np.minimum(rk, L - 1)] & in_stack)
            taken_fine = np.zeros((bricks_here.size, B, B), dtype=bool)
            step = max(1, 1_000_000 // (B * B))
            for first in range(0, bricks_here.size, step):
                part = bricks_here[first : first + step]
                block = fnode[part].reshape(-1)
                has = block >= 0
                piece = np.where(has, block - size, 0)
                taken = has & whole_piece[piece]
                if close.size:
                    cells = np.flatnonzero(has & (piece_row[piece] >= 0))
                    if cells.size:
                        # every fine cell of the close pieces against its piece's faces
                        r, rem = np.divmod(cells, B * B)
                        by, bx = np.divmod(rem, B)
                        r = part[r]
                        owner = piece_row[piece[cells]]
                        lo = np.searchsorted(piece_rows, owner, side="left")
                        many = np.searchsorted(piece_rows, owner, side="right") - lo
                        point = np.stack(
                            [x_min + (rix[r] * B + bx + 0.5) * fx, y_min + (riy[r] * B + by + 0.5) * fy, heights[rk[r]]], 1
                        )
                        which = np.repeat(np.arange(cells.size), many)
                        at = np.arange(which.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                        f = piece_faces[at]
                        s_cell = slowness[piece_label[close[owner] - size]]
                        tt = faces.time[f] + s_cell[which] * faces.distance(f, point[which])
                        best = np.full(cells.size, np.inf)
                        np.minimum.at(best, which, tt)
                        taken[cells[best <= budget]] = True
                taken_fine[first : first + part.size] = taken.reshape(-1, B, B)
            keep = taken_fine.reshape(bricks_here.size, -1).any(axis=1)
            out_keys.append(rkeys[bricks_here[keep]])
            out_blocks.append(taken_fine[keep])
        keys = np.concatenate(out_keys) if out_keys else np.zeros(0, np.int64)
        blocks = np.concatenate(out_blocks) if out_blocks else np.zeros((0, B, B), bool)
        return out_whole, keys, blocks

    if trace is not None:
        trace.update(band_keys=band_keys, flat_keys=flat_keys, whole=whole,
                     band_rows=band_rows if band_keys.size else None, band_faces=band_faces if band_keys.size else None,
                     zb=zb)
    return Front(place, flat_heights)


def _touching(record, rect_of_record, count):
    """Which entry rectangles share an edge (or are one face apart across
    a corner), as CSR (pointer, index).

    ``record`` holds unit lattice faces (orientation, plane, u0, u1, v0,
    v1); ``rect_of_record`` the rectangle each was merged into."""
    o, at, u, v = record[:, 0], record[:, 1], record[:, 2], record[:, 4]
    span = int(max(at.max(), u.max(), v.max())) + 2
    # Each unit face's four edges, as one number: (direction, x, y, z)
    # on the lattice.
    xf, yf, zf = o == 0, o == 1, o == 2
    keys = []
    owner = []
    for sel, a, b in (
        (xf, (1, at, u, v), (1, at, u, v + 1)),
        (xf, (2, at, u, v), (2, at, u + 1, v)),
        (yf, (0, u, at, v), (0, u, at, v + 1)),
        (yf, (2, u, at, v), (2, u + 1, at, v)),
        (zf, (0, u, v, at), (0, u, v + 1, at)),
        (zf, (1, u, v, at), (1, u + 1, v, at)),
    ):
        idx = np.flatnonzero(sel)
        for d, x, y, z in (a, b):
            keys.append(((d * span + x[idx]) * span + y[idx]) * span + z[idx])
            owner.append(rect_of_record[idx])
    key = np.concatenate(keys)
    owner = np.concatenate(owner)
    order = np.argsort(key, kind="stable")
    key, owner = key[order], owner[order]
    code = []
    # An edge is shared by at most four unit faces.
    for gap in (1, 2, 3):
        same = key[gap:] == key[:-gap]
        a, b = owner[:-gap][same], owner[gap:][same]
        differ = a != b
        a, b = a[differ], b[differ]
        code += [a * count + b, b * count + a]
    code = _unique(np.concatenate(code)) if code else np.zeros(0, np.int64)
    ptr, idx = _csr(code // count, code % count, count)
    # Two steps as well: on a staircase the faces either side of a corner
    # are equally far from a point beyond it (both nearest at the corner),
    # and the face that is closer lies one further on.
    src = code // count
    lo = ptr[idx]
    many = ptr[idx + 1] - lo
    at2 = np.arange(many.sum()) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
    a2, b2 = np.repeat(src, many), idx[at2]
    code = _unique(np.concatenate([code, (a2 * count + b2)[a2 != b2]]))
    return _csr(code // count, code % count, count)


def _csr(src, dst, count):
    """Sorted (src, dst) pairs as CSR."""
    ptr = np.zeros(count + 1, np.int64)
    np.add.at(ptr, src + 1, 1)
    return np.cumsum(ptr), dst.astype(np.int64)


def _unique_pairs(a, b):
    """Distinct (a, b) pairs of non-negative integers, sorted by a then b.
    Runs of one pair (a brick's worth of fine cells) are dropped first."""
    a = np.asarray(a, np.int64).reshape(-1)
    b = np.asarray(b, np.int64).reshape(-1)
    if a.size == 0:
        return a, b
    fresh = np.ones(a.size, dtype=bool)
    fresh[1:] = (a[1:] != a[:-1]) | (b[1:] != b[:-1])
    a, b = a[fresh], b[fresh]
    base = int(b.max()) + 1
    if (int(a.max()) + 1) * base < 2**62:
        code = _unique(a * base + b)
        return code // base, code % base
    pairs = np.unique(np.stack([a, b], 1), axis=0)
    return pairs[:, 0], pairs[:, 1]

