"""The voxel model's wet etch: the etchant's arrival time, on the two-level grid.

The solver settles cells in order of arrival (a group marching method) on
a graph of *nodes*:

* the plain bulk of an etchable material is laid out in graded squares, one
  node each: a square is no wider than its distance to anything else, so
  cells are one bulk cell wide next to a boundary and double in size, step
  by step, into the bulk;
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
does not). A square wider than a cell walks its face on, over the entry
faces that touch, to the one nearest itself. Where the front passes into
another etchable material it crosses their shared face in patches, each
opening when the etchant reached its middle; patches are a bulk cell wide
and are quartered, down to fine cells, where the time the etchant got
there differs across them by more than it takes to cross a fine cell.
The last step places the front at the fine level: squares the front's
edge crosses are halved down to cells, then to blocks of fine cells, and
each fine row cut exactly against the faces of the node, its neighbours,
the walks from its face across it and every level face over it; pieces
near the edge are settled fine cell by fine cell. At each halving only
the faces that can still be nearest somewhere in the part are kept (a
bound on how fast the lead of one face over another can change across
it), and a part lying wholly under level faces of one plane is taken
whole when the plane is near enough straight above or below.

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


def _graded_squares(code):
    """The bulk of each slab as squares of one code, each no wider than its
    distance (in cells) to the nearest cell of another code: next to a
    boundary one cell, twice that one further out, and so on. ``code`` is
    (slabs, ny, nx); -1 (a refined cell) is never part of a square. Returns
    (slab, x0, y0, side, code) in cells."""
    L, ny, nx = code.shape
    edge = code < 0
    edge[:, :, 1:] |= code[:, :, 1:] != code[:, :, :-1]
    edge[:, :, :-1] |= code[:, :, 1:] != code[:, :, :-1]
    edge[:, 1:, :] |= code[:, 1:, :] != code[:, :-1, :]
    edge[:, :-1, :] |= code[:, 1:, :] != code[:, :-1, :]
    count = np.zeros((L, ny + 1, nx + 1), np.int32)
    count[:, 1:, 1:] = edge.cumsum(1, dtype=np.int32).cumsum(2, dtype=np.int32)
    taken = np.zeros(code.shape, bool)
    out = []
    top = 0
    while (2 << top) <= min(ny, nx):
        top += 1
    for level in range(top, 0, -1):
        s = 1 << level
        if ny % s or nx % s:
            continue
        # no edge cell within s - 1 cells of the square: count the edge cells
        # in the square grown by s - 1 on each side
        by, bx = np.arange(0, ny, s), np.arange(0, nx, s)
        y0 = np.clip(by - (s - 1), 0, ny); y1 = np.clip(by + 2 * s - 1, 0, ny)
        x0 = np.clip(bx - (s - 1), 0, nx); x1 = np.clip(bx + 2 * s - 1, 0, nx)
        near = (count[:, y1][:, :, x1] - count[:, y0][:, :, x1] - count[:, y1][:, :, x0] + count[:, y0][:, :, x0]) > 0
        free = ~taken.reshape(L, ny // s, s, nx // s, s).any(axis=(2, 4))
        k, qy, qx = np.nonzero(~near & free)
        if k.size:
            out.append((k, qx * s, qy * s, np.full(k.size, s), code[k, qy * s, qx * s]))
            taken.reshape(L, ny // s, s, nx // s, s)[k, qy, :, qx, :] = True
    k, qy, qx = np.nonzero(~taken & (code >= 0))
    out.append((k, qx, qy, np.ones(k.size, np.int64), code[k, qy, qx]))
    return [np.concatenate([o[i] for o in out]).astype(np.int64) for i in range(5)]


def _merge_rectangles(key, u0, u1, v0, v1):
    """Unit lattice faces with the same ``key`` columns joined into the largest
    rectangles a sweep along u and then along v finds. Returns the
    rectangles (key columns, u0, u1, v0, v1) and which rectangle each face
    went into."""
    from .voxel import _row_order, _unique_rows

    record = np.concatenate([key, np.stack([u0, u1, v0, v1], 1)], 1)
    kc = key.shape[1]
    record, which = _unique_rows(record)
    U0, U1, V0, V1 = kc, kc + 1, kc + 2, kc + 3
    order = _row_order(record[:, list(range(kc)) + [V0, V1, U0]])
    rec = record[order]
    same = np.ones(rec.shape[0], dtype=bool)
    same[1:] = (rec[1:, :kc] == rec[:-1, :kc]).all(axis=1) & (rec[1:, V0] == rec[:-1, V0]) & (rec[1:, V1] == rec[:-1, V1])
    start = np.ones(rec.shape[0], dtype=bool)
    start[1:] = ~same[1:] | (rec[1:, U0] > rec[:-1, U1])
    group = np.cumsum(start) - 1
    begins = np.flatnonzero(start)
    runs = rec[begins].copy()
    runs[:, U1] = np.maximum.reduceat(rec[:, U1], begins)
    run_of = np.empty(rec.shape[0], np.int64)
    run_of[order] = group
    order2 = _row_order(runs[:, list(range(kc)) + [U0, U1, V0]])
    rr = runs[order2]
    same2 = np.ones(rr.shape[0], dtype=bool)
    same2[1:] = (rr[1:, :kc] == rr[:-1, :kc]).all(axis=1) & (rr[1:, U0] == rr[:-1, U0]) & (rr[1:, U1] == rr[:-1, U1])
    start2 = np.ones(rr.shape[0], dtype=bool)
    start2[1:] = ~same2[1:] | (rr[1:, V0] > rr[:-1, V1])
    group2 = np.cumsum(start2) - 1
    begins2 = np.flatnonzero(start2)
    rects = rr[begins2].copy()
    rects[:, V1] = np.maximum.reduceat(rr[:, V1], begins2)
    rect_of_run = np.empty(runs.shape[0], np.int64)
    rect_of_run[order2] = group2
    return rects, rect_of_run[run_of[which]], record, rect_of_run[run_of]


def arrival(
    state, table, budget, opening, live, should_cancel: Callable[[], bool] | None,
    trace: dict | None = None, step: float | None = None,
):
    """Where the etchant gets to within ``budget``.

    Returns a :class:`Front`.
    """
    from .voxel import MIXED, VOID, Mask2, _check, components

    def check():
        _check(should_cancel, "an isotropic etch")

    L, ny, nx, B = state.n, state.ny, state.nx, state.refine
    plane = ny * nx
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
    check()
    flive = live.blocks(rkeys) if R else np.zeros((0, B, B), np.uint8)
    fcol = columns.blocks_for(rcell, B) if R else np.zeros((0, B, B), bool)
    fkind = np.full((R, B, B), WALL, dtype=np.uint8)
    fkind[(fl != VOID) & (table[fl] > 0.0)] = ETCHABLE
    fkind[(fl == VOID) & (flive == 1) & fcol] = SOURCE

    # -- the bulk as graded squares: one node each --------------------------
    check()
    code = np.where(kind == ETCHABLE, 10 + lab.astype(np.int64), kind.astype(np.int64))
    code[kind == REFINED] = -1
    sq_k, sq_x, sq_y, sq_s, sq_code = _graded_squares(code)
    del code
    etch_sq = sq_code >= 10
    NL = int(etch_sq.sum())
    nk, nqx, nqy, ns = sq_k[etch_sq], sq_x[etch_sq], sq_y[etch_sq], sq_s[etch_sq]
    nlab = sq_code[etch_sq] - 10
    del sq_k, sq_x, sq_y, sq_s, sq_code, etch_sq
    check()
    owner = np.full(kind.shape, -1, dtype=np.int32)
    for s in np.unique(ns):
        pick = np.flatnonzero(ns == s)
        owner.reshape(L + 1, ny // s, s, nx // s, s)[nk[pick], nqy[pick] // s, :, nqx[pick] // s, :] = pick[:, None, None]
    owner_flat = owner.reshape(-1)
    leaf_lo = np.stack([x_min + nqx * cx, y_min + nqy * cy, zb[nk]], 1)
    leaf_hi = np.stack([x_min + (nqx + ns) * cx, y_min + (nqy + ns) * cy, zb[nk + 1]], 1)
    leaf_pos = 0.5 * (leaf_lo + leaf_hi)

    # -- pieces of etchable material inside refined cells ------------------
    check()
    fnode = np.full((R, B, B), -1, dtype=np.int32)
    found: list[tuple[int, np.ndarray, np.ndarray]] = []
    next_id = NL

    def add_pieces(m):
        """Number the pieces of material ``m``; their bricks and centres."""
        nonlocal next_id
        mask = (fkind == ETCHABLE) & (fl == m)
        ids = components(mask, connect_layers=False, check=check)[mask]
        check()
        # the pieces numbered 0, 1, ... (the labels are run numbers)
        used = np.zeros(int(ids.max()) + 1, dtype=bool)
        used[ids] = True
        inverse = (np.cumsum(used) - 1)[ids]
        del ids, used
        count = int(inverse.max()) + 1
        fnode[mask] = next_id + inverse
        check()
        r, by, bx = np.nonzero(mask)
        del mask
        check()
        weight = np.bincount(inverse, minlength=count).astype(float)
        brick = np.zeros(count, np.int64)
        brick[inverse] = r
        px = np.bincount(inverse, x_min + (rix[r] * B + bx + 0.5) * fx, count) / weight
        py = np.bincount(inverse, y_min + (riy[r] * B + by + 0.5) * fy, count) / weight
        found.append((int(m), brick, np.stack([px, py, zc[rk[brick]]], axis=1)))
        next_id += count

    for m in np.flatnonzero(np.bincount(fl[fkind == ETCHABLE], minlength=256)) if R else []:
        check()
        add_pieces(m)
    if next_id >= 2**31:
        raise MemoryError("too many pieces for this etch")
    piece_label = np.concatenate([np.full(b.size, m, np.int64) for m, b, _p in found]) if found else np.zeros(0, np.int64)
    piece_brick = np.concatenate([b for _m, b, _p in found]) if found else np.zeros(0, np.int64)
    piece_pos = np.concatenate([p for _m, _b, p in found]) if found else np.zeros((0, 3))
    del found
    pieces = next_id - NL
    total = next_id

    def node_label(ids):
        out = np.empty(ids.size, np.int64)
        leaf = ids < NL
        out[leaf] = nlab[ids[leaf]]
        out[~leaf] = piece_label[ids[~leaf] - NL]
        return out

    def node_pos(ids):
        out = np.empty((ids.size, 3))
        leaf = ids < NL
        out[leaf] = leaf_pos[ids[leaf]]
        out[~leaf] = piece_pos[ids[~leaf] - NL]
        return out

    def node_box(ids):
        """Leaves: their square prism; pieces: their bulk cell."""
        lo = np.empty((ids.size, 3)); hi = np.empty((ids.size, 3))
        leaf = ids < NL
        lo[leaf], hi[leaf] = leaf_lo[ids[leaf]], leaf_hi[ids[leaf]]
        r = piece_brick[ids[~leaf] - NL]
        lo[~leaf] = np.stack([x_min + rix[r] * cx, y_min + riy[r] * cy, zb[rk[r]]], 1)
        hi[~leaf] = np.stack([x_min + (rix[r] + 1) * cx, y_min + (riy[r] + 1) * cy, zb[rk[r] + 1]], 1)
        return lo, hi

    # -- links ---------------------------------------------------------------
    link_a: list[np.ndarray] = []
    link_b: list[np.ndarray] = []
    # square to square, sideways and up and down
    for a_, b_ in ((owner[:, :, :-1], owner[:, :, 1:]), (owner[:, :-1, :], owner[:, 1:, :]), (owner[:-1], owner[1:])):
        check()
        touch = (a_ >= 0) & (b_ >= 0) & (a_ != b_)
        if touch.any():
            a2, b2 = _unique_pairs(a_[touch], b_[touch])
            link_a.append(a2); link_b.append(b2)

    def refined_at(keys):
        if R == 0:
            return np.zeros(keys.size, np.int64), np.zeros(keys.size, bool)
        at = np.minimum(np.searchsorted(rkeys, keys), R - 1)
        return at, rkeys[at] == keys

    def facing(dx, dy, dk, part):
        """For the refined cells ``part`` with a neighbour that way: my fine
        cells on that side and the neighbour's fine cells facing them (a
        plain neighbour repeated)."""
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
        spread = (-1,) + (1,) * (len(shape) - 1)
        their_kind = np.broadcast_to(kind_flat[other].reshape(spread), shape).copy()
        their_node = np.broadcast_to(owner_flat[other].reshape(spread), shape).astype(np.int64)
        their_lab = np.broadcast_to(lab_flat[other].reshape(spread), shape).copy()
        if hit.any():
            their_kind[hit] = side(fkind, at[hit], True)
            their_node[hit] = side(fnode, at[hit], True)
            their_lab[hit] = side(fl, at[hit], True)
        return idx, other, hit, mine_kind, mine_node, mine_lab, their_kind, their_node, their_lab

    directions = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    # Faces on the fine lattice: (node, orientation, plane, u0, u1, v0, v1),
    # and for faces onto a sealed cavity also the cavity. An x-face lies on
    # fine line x = plane, spanning fine rows u and slabs v; a y-face
    # likewise across; a z-face lies on slab boundary ``plane``, spanning
    # fine columns u and fine rows v.
    lattice: list[np.ndarray] = []
    cav_lattice: list[np.ndarray] = []
    cavity_of = None
    if (kind == CAVITY).any():
        cavity_of = components(kind == CAVITY, check=check).reshape(-1)

    def entry(node, orient, plane_at, u0, u1, v0, v1, into=None):
        n = np.asarray(node).size
        row = [np.asarray(node, np.int64), np.full(n, orient, np.int64)]
        row += [np.broadcast_to(np.asarray(a, np.int64), (n,)) for a in (plane_at, u0, u1, v0, v1)]
        if into is None:
            lattice.append(np.stack(row, 1))
        else:
            cav_lattice.append(np.stack([np.broadcast_to(np.asarray(into, np.int64), (n,))] + row, 1))

    def record(node, where, idx, dx, dy, dk, into=None):
        r = idx[where[0]]
        k = rk[r]
        if dk:
            by, bx = where[1], where[2]
            u0 = rix[r] * B + bx
            v0 = riy[r] * B + by
            entry(node, 2, k + (1 if dk > 0 else 0), u0, u0 + 1, v0, v0 + 1, into)
        elif dx:
            by = where[1]
            u0 = riy[r] * B + by
            entry(node, 0, (rix[r] + (1 if dx > 0 else 0)) * B, u0, u0 + 1, k, k + 1, into)
        else:
            bx = where[1]
            u0 = rix[r] * B + bx
            entry(node, 1, (riy[r] + (1 if dy > 0 else 0)) * B, u0, u0 + 1, k, k + 1, into)

    def border(dx, dy, dk, part):
        idx, other, hit, mk, mn, ml, tk, tn, tl = facing(dx, dy, dk, part)
        link = (mn >= 0) & (tn >= 0)  # the same material, or a crossing into another
        if link.any():
            a2, b2 = _unique_pairs(mn[link], tn[link])
            link_a.append(a2); link_b.append(b2)
        # My etchable against their open space, and a plain etchable
        # neighbour against my open space (a refined one reports its own).
        plain_other = ~np.broadcast_to(hit.reshape((-1,) + (1,) * (mk.ndim - 1)), mk.shape)
        for mine, theirs, node, rows_from_mine in ((mk, tk, mn, True), (tk, mk, tn, False)):
            enter = (mine == ETCHABLE) & (theirs == SOURCE)
            if not rows_from_mine:
                enter &= plain_other
            if enter.any():
                record(node[enter], np.nonzero(enter), idx, dx, dy, dk)
        # my etchable against a plain sealed cavity
        onto = (mk == ETCHABLE) & (tk == CAVITY) & plain_other
        if onto.any():
            where = np.nonzero(onto)
            record(mn[onto], where, idx, dx, dy, dk, into=cavity_of[other[where[0]]])

    for dx, dy, dk in directions:
        for start in range(0, R, 4096):  # a few million fine cells at a time
            check()
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
            # different etchable materials side by side inside a brick
            cross = (a == ETCHABLE) & (b == ETCHABLE) & (na != nb)
            if cross.any():
                a2, b2 = _unique_pairs(na[cross], nb[cross])
                link_a.append(a2); link_b.append(b2)
    # Plain etchable against plain open space or a plain sealed cavity.
    for dx, dy, dk in directions:
        check()
        sl_me = [slice(None)] * 3
        sl_th = [slice(None)] * 3
        for axis, d in ((0, dk), (1, dy), (2, dx)):
            if d > 0:
                sl_me[axis], sl_th[axis] = slice(0, -1), slice(1, None)
            elif d < 0:
                sl_me[axis], sl_th[axis] = slice(1, None), slice(0, -1)
        me = kind[tuple(sl_me)] == ETCHABLE
        for target in (SOURCE, CAVITY):
            there = kind[tuple(sl_th)] == target
            k, iy, ix = np.nonzero(me & there)
            if k.size == 0:
                continue
            k = k + (1 if dk < 0 else 0); iy = iy + (1 if dy < 0 else 0); ix = ix + (1 if dx < 0 else 0)
            node = owner[k, iy, ix]
            into = None
            if target == CAVITY:
                into = cavity_of[(k + dk) * plane + (iy + dy) * nx + ix + dx]
            if dk:
                entry(node, 2, k + (1 if dk > 0 else 0), ix * B, ix * B + B, iy * B, iy * B + B, into)
            elif dx:
                entry(node, 0, (ix + (1 if dx > 0 else 0)) * B, iy * B, iy * B + B, k, k + 1, into)
            else:
                entry(node, 1, (iy + (1 if dy > 0 else 0)) * B, ix * B, ix * B + B, k, k + 1, into)

    def geometry(rects, o_col):
        o, at, u0, u1, v0, v1 = (rects[:, o_col + i] for i in range(6))
        centre = np.zeros((rects.shape[0], 3))
        half = np.zeros((rects.shape[0], 3))
        xf, yf, zf = o == 0, o == 1, o == 2
        centre[xf] = np.stack([x_min + at[xf] * fx, y_min + 0.5 * (u0[xf] + u1[xf]) * fy, 0.5 * (zb[v0[xf]] + zb[v1[xf]])], 1)
        half[xf] = np.stack([np.zeros(xf.sum()), 0.5 * (u1[xf] - u0[xf]) * fy, 0.5 * (zb[v1[xf]] - zb[v0[xf]])], 1)
        centre[yf] = np.stack([x_min + 0.5 * (u0[yf] + u1[yf]) * fx, y_min + at[yf] * fy, 0.5 * (zb[v0[yf]] + zb[v1[yf]])], 1)
        half[yf] = np.stack([0.5 * (u1[yf] - u0[yf]) * fx, np.zeros(yf.sum()), 0.5 * (zb[v1[yf]] - zb[v0[yf]])], 1)
        centre[zf] = np.stack([x_min + 0.5 * (u0[zf] + u1[zf]) * fx, y_min + 0.5 * (v0[zf] + v1[zf]) * fy, zb[at[zf]]], 1)
        half[zf] = np.stack([0.5 * (u1[zf] - u0[zf]) * fx, 0.5 * (v1[zf] - v0[zf]) * fy, np.zeros(zf.sum())], 1)
        return centre, half

    # One source surface, not thousands of fine squares: coplanar entry
    # faces that touch are joined into the largest rectangles. A node
    # inherits one face; were it one fine square, a point further on would
    # measure to that square instead of to the edge of the opening it
    # really is nearest to.
    faces = Faces()
    if lattice:
        table_e = np.concatenate(lattice)
        del lattice
        check()
        rects, rect_of_entry, _records, _rect_of_record = _merge_rectangles(
            table_e[:, 1:3], table_e[:, 3], table_e[:, 4], table_e[:, 5], table_e[:, 6]
        )
        centre, half = geometry(rects, 0)
        first_face = faces.add(centre, half, np.zeros(rects.shape[0]))
        entry_node, entry_face = _unique_pairs(table_e[:, 0], first_face[rect_of_entry])
        if B > 1 or (ns > 1).any():
            check()
            adj_ptr, adj_idx = _touching(rects[:, :6], first_face, rects.shape[0])
        else:  # nodes a fine cell apart need no walk
            adj_ptr, adj_idx = np.zeros(rects.shape[0] + 1, np.int64), np.zeros(0, np.int64)
        del table_e
    else:
        entry_node = np.zeros(0, np.int64)
        entry_face = np.zeros(0, np.int64)
        adj_ptr, adj_idx = np.zeros(1, np.int64), np.zeros(0, np.int64)
    n_entry = adj_ptr.size - 1
    # Faces onto sealed cavities, by cavity: they open when it floods.
    if cav_lattice:
        table_c = np.concatenate(cav_lattice)
        del cav_lattice
        crects, crect_of, _r, _o = _merge_rectangles(
            table_c[:, [0, 2, 3]], table_c[:, 4], table_c[:, 5], table_c[:, 6], table_c[:, 7]
        )
        cav_centre, cav_half = geometry(crects, 1)
        cav_comp = crects[:, 0]
        cav_node, cav_rect = _unique_pairs(table_c[:, 1], crect_of)
        by_node = np.argsort(cav_node, kind="stable")
        cav_node, cav_rect = cav_node[by_node], cav_rect[by_node]
        cav_lo, cav_hi = cav_centre - cav_half, cav_centre + cav_half
        flooded = np.zeros(int(cavity_of.max()) + 1, dtype=bool)
        del table_c
    else:
        cav_node = cav_rect = np.zeros(0, np.int64)
        flooded = np.zeros(1, dtype=bool)

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
            check()
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
        del a, b
    else:
        link_src = link_dst = np.zeros(0, np.int64)
    del link_a, link_b

    def neighbours(nodes):
        """(row, neighbour) for every link of ``nodes``."""
        lo = np.searchsorted(link_src, nodes, side="left")
        many = np.searchsorted(link_src, nodes, side="right") - lo
        rows = np.repeat(np.arange(nodes.size), many)
        at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
        return rows, link_dst[at]

    # -- the march ----------------------------------------------------------
    T = np.full(total, np.inf)
    face_of = np.full(total, -1, dtype=np.int64)
    known = np.zeros(total, dtype=bool)
    queued = np.zeros(total, dtype=bool)
    probe0 = 1.25 * min(cx, cy, float(np.min(thick)) if thick.size else cx)
    leaf_reach = 0.5 * np.hypot(ns * cx, ns * cy)

    def node_at(points):
        """The node holding each point (-1 for none) and whether it is open to the etchant."""
        ix = np.floor((points[:, 0] - x_min) / cx).astype(np.int64)
        iy = np.floor((points[:, 1] - y_min) / cy).astype(np.int64)
        iz = np.searchsorted(state.z, points[:, 2], side="right") - 1
        iz = np.where(points[:, 2] >= state.top, L, iz)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny) & (iz >= 0)
        key = np.where(ok, iz * plane + iy * nx + ix, 0)
        kc = np.where(ok, kind_flat[key], WALL)
        node = np.where(kc == ETCHABLE, owner_flat[key], -1).astype(np.int64)
        open_ = kc == SOURCE
        if cavity_of is not None:
            open_ |= (kc == CAVITY) & flooded[np.minimum(cavity_of[key], flooded.size - 1)]
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
        reach = np.where(cells < NL, leaf_reach[np.minimum(cells, max(NL - 1, 0))] if NL else 0.0, 0.0)

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
            """Whether the way from each node to face ``f`` starts, just past
            the node itself, through settled material of its own kind or
            open space."""
            gap = faces.nearest(f, p[rows]) - p[rows]
            length = np.sqrt((gap * gap).sum(axis=1))
            reach_here = probe0 + reach[rows]
            clear = np.ones(rows.size, dtype=bool)
            far = length > reach_here
            if far.any():
                ahead = p[rows[far]] + gap[far] * (reach_here[far] / length[far])[:, None]
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
            t = faces.time[f] + s[rows] * faces.distance(f, p[rows])
            clear = sight(rows, f)
            offer(rows[clear], t[clear], face=f[clear])
            bend = ~clear
            if bend.any():
                pn = node_pos(nbrs[bend])
                d = pn - p[rows[bend]]
                t = T[nbrs[bend]] + s[rows[bend]] * np.sqrt((d * d).sum(axis=1))
                offer(rows[bend], t, centre=pn, half=np.zeros_like(pn), time=T[nbrs[bend]])

        if link_src.size:
            rows, nbrs = neighbours(cells)
            settled = known[nbrs]
            rows, nbrs = rows[settled], nbrs[settled]
            same = node_label(nbrs) == mylab[rows]
            inherit(rows[same], nbrs[same])
            if (~same).any():
                # into another etchable material: through the face the two
                # share, in patches no wider than a bulk cell, each opening at
                # the time the etchant reached its middle
                r2, n2 = rows[~same], nbrs[~same]
                t_patch, f_patch, row_patch = crossing(cells[r2], n2)
                if f_patch.size:
                    rr = r2[row_patch]
                    t = t_patch + s[rr] * faces.distance(f_patch, p[rr])
                    offer(rr, t, face=f_patch)
        # A square wider than a cell takes its neighbour's face, which is
        # nearest to the neighbour: walk it on to the one nearest the square.
        if NL:
            wide = (cells < NL) & ~fresh & (how >= 0) & (how < n_entry)
            wide[wide] = ns[cells[wide]] > 1
            if wide.any():
                rows = np.flatnonzero(wide)
                f = climb(how[rows], p[rows])
                t = faces.time[f] + s[rows] * faces.distance(f, p[rows])
                better = t < best[rows]
                best[rows[better]] = t[better]
                how[rows[better]] = f[better]
        return best, how, fresh, new_centre, new_half, new_time

    cross_made: dict[int, tuple[int, int]] = {}
    cross_node: list[np.ndarray] = []
    cross_face: list[np.ndarray] = []

    def crossing(down, up):
        """Faces where the etchant crosses from settled node ``up`` into
        ``down`` (another material): their shared face in patches no wider
        than a bulk cell, each opening when the etchant reached its middle.
        Made once per pair. Returns (time, face, which pair) per patch."""
        code_ = down.astype(np.int64) * total + up
        new = np.array([c not in cross_made for c in code_.tolist()], dtype=bool)
        if new.any():
            d, u, c_new = down[new], up[new], code_[new]
            lo_a, hi_a = node_box(d)
            lo_b, hi_b = node_box(u)
            lo, hi = np.maximum(lo_a, lo_b), np.minimum(hi_a, hi_b)
            # two pieces of one refined cell share no face of their boxes:
            # cross halfway between them
            inner = ((hi - lo) > 1e-12).all(axis=1)
            if inner.any():
                mid = 0.5 * (node_pos(d[inner]) + node_pos(u[inner]))
                lo[inner], hi[inner] = mid, mid
            unit = np.array([cx, cy, max(cx, cy)])
            parts = np.maximum(1, np.ceil((hi - lo) / unit - 1e-9)).astype(np.int64)
            many = parts.prod(axis=1)
            which = np.repeat(np.arange(d.size), many)
            local = np.arange(which.size) - np.repeat(np.cumsum(many) - many, many)
            pz = local % parts[which, 2]
            py = (local // parts[which, 2]) % parts[which, 1]
            px = local // (parts[which, 2] * parts[which, 1])
            step_ = (hi - lo)[which] / parts[which]
            plo = lo[which] + np.stack([px, py, pz], 1) * step_
            phi = plo + step_
            fu_all = face_of[u]
            s_up_all = slowness[node_label(u)]
            pos_u = node_pos(u)

            def opens(which_, points):
                fu = fu_all[which_]
                has = fu >= 0
                safe = np.maximum(fu, 0)
                s_up = s_up_all[which_]
                return np.where(has, faces.time[safe] + s_up * faces.distance(safe, points),
                                T[u][which_] + s_up * np.sqrt(((points - pos_u[which_]) ** 2).sum(axis=1)))

            # Where the time the etchant gets there differs across a patch
            # by more than it takes to cross a fine cell, quarter it, down
            # to fine cells: the crossing is then placed to a fine cell.
            smallest = np.array([fx, fy, max(fx, fy)])
            done_lo, done_hi, done_w = [], [], []
            while which.size:
                ext = phi - plo
                corners = []
                for ax, ay, az in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1)):
                    corners.append(opens(which, plo + ext * np.array([ax, ay, az])))
                corners = np.stack(corners, 1)
                spread = corners.max(axis=1) - corners.min(axis=1)
                split_ax = ext > smallest * (1 + 1e-9)
                split = (spread > s_up_all[which] * min(fx, fy)) & split_ax.any(axis=1)
                keep = ~split
                done_lo.append(plo[keep]); done_hi.append(phi[keep]); done_w.append(which[keep])
                if not split.any():
                    break
                plo, phi, which = plo[split], phi[split], which[split]
                halves = np.where(split_ax[split], 2, 1)
                count_ = halves.prod(axis=1)
                rep = np.repeat(np.arange(which.size), count_)
                loc = np.arange(rep.size) - np.repeat(np.cumsum(count_) - count_, count_)
                h = halves[rep]
                ix_ = np.stack([loc // (h[:, 1] * h[:, 2]), (loc // h[:, 2]) % h[:, 1], loc % h[:, 2]], 1)
                sub = (phi - plo)[rep] / h
                plo = plo[rep] + ix_ * sub
                phi = plo + sub
                which = which[rep]
            plo = np.concatenate(done_lo); phi = np.concatenate(done_hi); which = np.concatenate(done_w)
            order_ = np.argsort(which, kind="stable")
            plo, phi, which = plo[order_], phi[order_], which[order_]
            many = np.bincount(which, minlength=d.size)
            pc = 0.5 * (plo + phi)
            t_open = opens(which, pc)
            made = faces.add(pc, 0.5 * (phi - plo), t_open)
            starts = made[0] + np.cumsum(many) - many
            for c, st, m in zip(c_new.tolist(), starts.tolist(), many.tolist()):
                cross_made[c] = (st, m)
            cross_node.append(d[which]); cross_face.append(made)
        spans = [cross_made[c] for c in code_.tolist()]
        start = np.array([a for a, _ in spans], np.int64)
        count = np.array([b for _, b in spans], np.int64)
        which = np.repeat(np.arange(down.size), count)
        f = start[which] + np.arange(which.size) - np.repeat(np.cumsum(count) - count, count)
        return faces.time[f], f, which

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
        if not link_src.size or fronts.size == 0:
            return np.zeros(0, np.int64)
        _rows, found = neighbours(fronts)
        found = found[~known[found]]
        places = np.arange(found.size, dtype=np.int64)
        stamp[found] = places
        return found[stamp[found] == places]

    def offer_faces(nodes, fcs):
        """Nodes reached straight from faces ``fcs`` (one each)."""
        nonlocal trial
        t = faces.time[fcs] + slowness[node_label(nodes)] * faces.distance(fcs, node_pos(nodes))
        order = np.lexsort((t, nodes))
        first = np.ones(order.size, dtype=bool)
        first[1:] = nodes[order][1:] != nodes[order][:-1]
        chosen = order[first]
        nodes, t, fcs = nodes[chosen], t[chosen], fcs[chosen]
        better = (t < T[nodes]) & ~known[nodes]
        nodes, t, fcs = nodes[better], t[better], fcs[better]
        T[nodes] = t
        face_of[nodes] = fcs
        new = nodes[~queued[nodes]]
        queued[new] = True
        trial = np.concatenate([trial, new])

    # Seed: every node an entry face enters, at its distance from the face.
    if entry_node.size:
        offer_faces(entry_node, entry_face)
    rates = slowness[slowness > 0.0]
    delta = 0.5 * min(cx, cy) * (rates.min() if rates.size else 1.0)
    # On past the budget by a node's reach: a node whose centre the front
    # misses can still have a corner, or the top of its slab, within it.
    # Slabs no thicker than ``step`` may be placed at other heights than
    # their middle (cut where the front curves or lies level); thicker ones
    # the front crosses upright, and their middle is where the node is.
    step = float(step) if step else float(np.max(thick[:L], initial=cx))
    tall = np.where(thick[:L] <= step * (1 + 1e-6), 0.5 * thick[:L], 0.0)
    # a square's reach: half its diagonal, and half its slab where placed in z
    leaf_reach3 = np.sqrt(leaf_reach**2 + tall[np.minimum(nk, L - 1)] ** 2) if NL else np.zeros(0)
    past = (rates.max() if rates.size else 1.0) * (
        math.hypot(cx, cy) + float(np.max(tall, initial=0.0)) + 2.0 * float(np.max(leaf_reach3, initial=0.0))
    )
    while trial.size:
        check()
        times = T[trial]
        first_time = float(times.min())
        if first_time > budget + past:
            break
        take = times <= first_time + delta
        group = trial[take]
        trial = trial[~take]
        queued[group] = False
        known[group] = True
        # Breaching a sealed cavity floods it; its walls open at that time.
        if cav_node.size:
            lo = np.searchsorted(cav_node, group, side="left")
            many = np.searchsorted(cav_node, group, side="right") - lo
            if many.any():
                rows = np.repeat(np.arange(group.size), many)
                at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                nodes, rect = group[rows], cav_rect[at]
                wet = ~flooded[cav_comp[rect]]
                nodes, rect = nodes[wet], rect[wet]
                f = face_of[nodes]
                ok = f >= 0
                nodes, rect, f = nodes[ok], rect[ok], f[ok]
                if nodes.size:
                    reach_t = faces.time[f] + slowness[node_label(nodes)] * _box_gap(faces.lo[f], faces.hi[f], cav_lo[rect], cav_hi[rect])
                    comps = cav_comp[rect]
                    for comp in _unique(comps):
                        t_open = float(reach_t[comps == comp].min())
                        flooded[comp] = True
                        walls = np.flatnonzero(cav_comp == comp)
                        made = faces.add(cav_centre[walls], cav_half[walls], np.full(walls.size, t_open))
                        face_of_rect = np.full(cav_comp.size, -1, np.int64)
                        face_of_rect[walls] = made
                        use = np.isin(cav_rect, walls)
                        offer_faces(cav_node[use], face_of_rect[cav_rect[use]])
        settle(reachable(group))

    if trace is not None:
        trace.update(T=T, face_of=face_of, faces=faces, fnode=fnode, rkeys=rkeys, NL=NL,
                     piece_pos=piece_pos, entry_node=entry_node, entry_face=entry_face, known=known,
                     owner=owner, ns=ns, nk=nk, nqx=nqx, nqy=nqy)
    # -- place the front at the fine level ---------------------------------
    # The march settles nodes; where the front is is placed from the faces,
    # at whatever height the caller asks, so a slab can be cut finely in z
    # where the front curves and exactly where it lies level.
    reach_xy = 0.5 * math.hypot(cx, cy)
    leaf_T = T[:NL]
    leaf_s = slowness[nlab]
    in_slabs = nk < L
    whole_leaf = in_slabs & (leaf_T + leaf_s * leaf_reach3 <= budget)
    band_leaf = in_slabs & ~whole_leaf & np.isfinite(leaf_T) & (leaf_T - leaf_s * leaf_reach3 <= budget)

    def candidates(nodes, walk=True):
        """(row, face) pairs: each node's own face, its settled same-material
        neighbours' faces, its entry faces, and the walks from its own face
        to the corners and middle of its square."""
        check()
        rows_list, face_list = [], []
        own = face_of[nodes]
        ok = own >= 0
        rows_list.append(np.flatnonzero(ok))
        face_list.append(own[ok])
        labels_here = node_label(nodes)
        if link_src.size:
            rows, nb = neighbours(nodes)
            use = known[nb] & (node_label(nb) == labels_here[rows]) & (face_of[nb] >= 0)
            rows_list.append(rows[use])
            face_list.append(face_of[nb[use]])
        if entry_node.size:
            order = np.argsort(entry_node, kind="stable")
            en = entry_node[order]
            lo = np.searchsorted(en, nodes, side="left")
            many = np.searchsorted(en, nodes, side="right") - lo
            if many.any():
                rows = np.repeat(np.arange(nodes.size), many)
                at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                rows_list.append(rows)
                face_list.append(entry_face[order][at])
        if cross_node:
            cn = np.concatenate(cross_node); cf = np.concatenate(cross_face)
            order = np.argsort(cn, kind="stable")
            cn, cf = cn[order], cf[order]
            lo = np.searchsorted(cn, nodes, side="left")
            many = np.searchsorted(cn, nodes, side="right") - lo
            if many.any():
                rows = np.repeat(np.arange(nodes.size), many)
                at = np.arange(rows.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                rows_list.append(rows)
                face_list.append(cf[at])
        if walk and rows_list[0].size:
            own_rows = rows_list[0]
            own_face = face_list[0]
            leaf = nodes[own_rows] < NL
            own_rows, own_face = own_rows[leaf], own_face[leaf]
            ids = nodes[own_rows]
            s_ = ns[ids]
            # the middle and corners of each square, with every face the
            # walk passes; wide squares also on a lattice of up to 5 x 5
            # points, as the nearest face turns along a curved rim -- there
            # only where the walk ends
            corners = [(0.5, 0.5), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]

            def at(i_, s_i, ax, ay):
                return np.stack([x_min + (nqx[i_] + ax * s_i) * cx, y_min + (nqy[i_] + ay * s_i) * cy, zc[nk[i_]]], 1)

            point = np.concatenate([at(ids, s_, ax, ay) for ax, ay in corners])
            owner_row = np.tile(own_rows, len(corners))
            steps_taken: list[np.ndarray] = []
            climb(np.tile(own_face, len(corners)), point, steps_taken)
            for st in steps_taken:
                rows_list.append(owner_row[st[:, 0]])
                face_list.append(st[:, 1])
            points, owners, starts = [], [], []
            for n_ in (2, 4):
                pick = (s_ == 2) if n_ == 2 else (s_ >= 4)
                if not pick.any():
                    continue
                i_ = ids[pick]
                for a_ in range(n_ + 1):
                    for b_ in range(n_ + 1):
                        if (a_ / n_, b_ / n_) in corners:
                            continue
                        points.append(at(i_, s_[pick], a_ / n_, b_ / n_))
                        owners.append(own_rows[pick])
                        starts.append(own_face[pick])
            if points:
                ends = climb(np.concatenate(starts), np.concatenate(points))
                rows_list.append(np.concatenate(owners))
                face_list.append(ends)
        count = faces.time.size
        code_ = _unique(np.concatenate(rows_list) * count + np.concatenate(face_list))
        return code_ // count, code_ % count

    def covered(group, f, lo_b, hi_b, sl, groups):
        """Per group, the latest arrival anywhere in its box from level
        entry faces of one plane that cover the box's plan between them
        (straight up or down to the plane); inf where no plane does.
        ``lo_b``/``hi_b`` are per group."""
        out = np.full(groups, np.inf)
        use = (f < n_entry) & (faces.half[f, 2] == 0.0)
        if not use.any():
            return out
        g, f = group[use], f[use]
        wx = np.minimum(faces.hi[f, 0], hi_b[g, 0]) - np.maximum(faces.lo[f, 0], lo_b[g, 0])
        wy = np.minimum(faces.hi[f, 1], hi_b[g, 1]) - np.maximum(faces.lo[f, 1], lo_b[g, 1])
        area = np.maximum(wx, 0.0) * np.maximum(wy, 0.0)
        plane_z = faces.lo[f, 2]
        order = np.lexsort((plane_z, g))
        g, plane_z, area, f = g[order], plane_z[order], area[order], f[order]
        start = np.flatnonzero(np.concatenate([[True], (g[1:] != g[:-1]) | (plane_z[1:] != plane_z[:-1])]))
        total = np.add.reduceat(area, start)
        gs, zs = g[start], plane_z[start]
        need = (hi_b[gs, 0] - lo_b[gs, 0]) * (hi_b[gs, 1] - lo_b[gs, 1])
        full = total >= need * (1 - 1e-9)
        t0 = np.maximum.reduceat(faces.time[f], start)
        far = t0 + sl[gs] * np.maximum(np.abs(zs - lo_b[gs, 2]), np.abs(zs - hi_b[gs, 2]))
        np.minimum.at(out, gs[full], far[full])
        return out

    def rivals(group, f, lo_b, hi_b, sl):
        """Which (group, face) pairs can still be nearest somewhere in their
        group's box: the distance to a face (a convex box) has a gradient
        that turns at most 1/d per unit moved, so from the box's middle
        the lead of the best face over another changes by no more than
        r|u_f - u_g| + r^2/2 (1/d_f + 1/d_g) across it (r its half-diagonal)."""
        mid = 0.5 * (lo_b + hi_b)
        r = 0.5 * np.sqrt(((hi_b - lo_b) ** 2).sum(axis=1))
        vec = mid - faces.nearest(f, mid)
        d = np.sqrt((vec * vec).sum(axis=1))
        u = vec / np.maximum(d, 1e-300)[:, None]
        t = faces.time[f] + sl * d
        order = np.lexsort((t, group))
        first = np.ones(order.size, dtype=bool)
        first[1:] = group[order][1:] != group[order][:-1]
        best_of = np.full(int(group.max()) + 1 if group.size else 0, -1, np.int64)
        best_of[group[order[first]]] = order[first]
        g = best_of[group]
        near_f, near_g = d - r, (d - r)[g]
        slack = np.where(
            (near_f > 0) & (near_g > 0),
            r * np.sqrt(((u - u[g]) ** 2).sum(axis=1)) + 0.5 * r * r * (1.0 / np.maximum(near_f, 1e-300) + 1.0 / np.maximum(near_g, 1e-300)),
            2.0 * r,
        )
        slack = np.minimum(slack, 2.0 * r)
        return (t - t[g] <= sl * slack * (1 + 1e-9) + 1e-12) | (np.arange(f.size) == g)

    # Squares near the front, and the faces each is measured against.
    band_ids = np.flatnonzero(band_leaf)
    if band_ids.size:
        band_rows, band_faces = candidates(band_ids)
        order = np.argsort(band_rows, kind="stable")
        band_rows, band_faces = band_rows[order], band_faces[order]
        # Only the faces that can be nearest somewhere in the square's slab.
        # A wide square under a level surface lies under many of its faces
        # (the surface is cut into rectangles round its holes), and a walk
        # to a few points does not meet them all: the level faces over it
        # are gathered from those touching, plane by plane.
        sq_lo = np.stack([x_min + nqx[band_ids] * cx, y_min + nqy[band_ids] * cy, zb[nk[band_ids]]], 1)
        sq_hi = np.stack([x_min + (nqx[band_ids] + ns[band_ids]) * cx, y_min + (nqy[band_ids] + ns[band_ids]) * cy, zb[nk[band_ids] + 1]], 1)
        sq_sl = slowness[nlab[band_ids]]
        count_f = faces.time.size
        seen = _unique(band_rows * count_f + band_faces)
        is_level = np.zeros(count_f, dtype=bool)
        is_level[:n_entry] = faces.half[:n_entry, 2] == 0.0

        def over(rows, f):
            return (
                (faces.lo[f, 0] <= sq_hi[rows, 0]) & (faces.hi[f, 0] >= sq_lo[rows, 0])
                & (faces.lo[f, 1] <= sq_hi[rows, 1]) & (faces.hi[f, 1] >= sq_lo[rows, 1])
            )

        r0, f0 = seen // count_f, seen % count_f
        grow = is_level[f0] & over(r0, f0)
        g_rows, g_f = r0[grow], f0[grow]
        while g_rows.size:
            check()
            lo_a = adj_ptr[g_f]
            many = adj_ptr[g_f + 1] - lo_a
            r2 = np.repeat(g_rows, many)
            f2 = adj_idx[np.arange(r2.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo_a, many)]
            same_plane = is_level[f2] & (faces.lo[f2, 2] == np.repeat(faces.lo[g_f, 2], many))
            r2, f2 = r2[same_plane], f2[same_plane]
            ok = over(r2, f2)
            code_new = _unique(r2[ok] * count_f + f2[ok])
            at = np.searchsorted(seen, code_new)
            known_ = (at < seen.size) & (seen[np.minimum(at, seen.size - 1)] == code_new)
            code_new = code_new[~known_]
            if code_new.size == 0:
                break
            seen = np.union1d(seen, code_new)
            g_rows, g_f = code_new // count_f, code_new % count_f
        rows_, fcs_ = seen // count_f, seen % count_f

        def prune(rows_, fcs_):
            low = faces.time[fcs_] + sq_sl[rows_] * _box_gap(faces.lo[fcs_], faces.hi[fcs_], sq_lo[rows_], sq_hi[rows_])
            high = faces.time[fcs_] + sq_sl[rows_] * _box_far(faces.lo[fcs_], faces.hi[fcs_], sq_lo[rows_], sq_hi[rows_])
            upper = np.full(band_ids.size, np.inf)
            np.minimum.at(upper, rows_, high)
            upper = np.minimum(upper, covered(rows_, fcs_, sq_lo, sq_hi, sq_sl, band_ids.size))
            keep = low <= upper[rows_]
            keep[keep] = rivals(rows_[keep], fcs_[keep], sq_lo[rows_[keep]], sq_hi[rows_[keep]], sq_sl[rows_[keep]])
            return rows_[keep], fcs_[keep]

        rows_, fcs_ = prune(rows_, fcs_)
        band_rows, band_faces = rows_, fcs_
        del sq_lo, sq_hi, sq_sl, seen, rows_, fcs_
    else:
        band_rows = band_faces = np.zeros(0, np.int64)

    if trace is not None:
        trace.update(band_ids=band_ids.copy(), band_rows=band_rows.copy(), band_faces=band_faces.copy(), adj_ptr=adj_ptr, adj_idx=adj_idx)

    # Flat fronts: where the arrival is the same at a square's middle and
    # corners, at its top and at its bottom, and differs between the two by
    # the slab's thickness at the material's rate, the front is a plane at
    # an exact height -- the slab is to be cut there. Squares that are not
    # flat as a whole are looked at in quarters, down to one cell.
    heights_found: list[np.ndarray] = []
    if band_ids.size:
        # a level front stands over or under a level face: squares with no
        # level face over or under any of them cannot be flat
        b_ = band_ids[band_rows]
        level = (
            (faces.half[band_faces, 2] == 0.0)
            & (faces.lo[band_faces, 0] < x_min + (nqx[b_] + ns[b_]) * cx)
            & (faces.hi[band_faces, 0] > x_min + nqx[b_] * cx)
            & (faces.lo[band_faces, 1] < y_min + (nqy[b_] + ns[b_]) * cy)
            & (faces.hi[band_faces, 1] > y_min + nqy[b_] * cy)
        )
        sq_row = _unique(band_rows[level])
        sq_x, sq_y, sq_s = nqx[band_ids[sq_row]], nqy[band_ids[sq_row]], ns[band_ids[sq_row]]
        # (part, face) pairs go down with the parts, as in placing: only the
        # faces that can be nearest somewhere in the part are kept, and a
        # part the front cannot cross inside its slab (reached nowhere, or
        # everywhere) is dropped -- there is no level front in it to find.
        lo_c = np.searchsorted(band_rows, sq_row, side="left")
        many = np.searchsorted(band_rows, sq_row, side="right") - lo_c
        p_sq = np.repeat(np.arange(sq_row.size), many)
        p_f = band_faces[np.arange(p_sq.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo_c, many)]
        while sq_row.size:
            check()
            ids = band_ids[sq_row]
            k_ = nk[ids]
            sl = slowness[nlab[ids]]
            box_lo = np.stack([x_min + sq_x * cx, y_min + sq_y * cy, zb[k_]], 1)
            box_hi = np.stack([x_min + (sq_x + sq_s) * cx, y_min + (sq_y + sq_s) * cy, zb[k_ + 1]], 1)
            low = faces.time[p_f] + sl[p_sq] * _box_gap(faces.lo[p_f], faces.hi[p_f], box_lo[p_sq], box_hi[p_sq])
            high = faces.time[p_f] + sl[p_sq] * _box_far(faces.lo[p_f], faces.hi[p_f], box_lo[p_sq], box_hi[p_sq])
            lower = np.full(sq_row.size, np.inf); upper = np.full(sq_row.size, np.inf)
            np.minimum.at(lower, p_sq, low); np.minimum.at(upper, p_sq, high)
            upper = np.minimum(upper, covered(p_sq, p_f, box_lo, box_hi, sl, sq_row.size))
            keep = low <= upper[p_sq]
            keep[keep] = rivals(p_sq[keep], p_f[keep], box_lo[p_sq[keep]], box_hi[p_sq[keep]], sl[p_sq[keep]])
            p_sq, p_f = p_sq[keep], p_f[keep]
            live_sq = (lower <= budget) & (upper > budget)
            # arrival at the middle and corners of each part, top and bottom
            values = []
            for zz in (zb[k_ + 1], zb[k_]):
                at_h = []
                for ax, ay in ((0.5, 0.5), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)):
                    pts = np.stack([x_min + (sq_x + ax * sq_s) * cx, y_min + (sq_y + ay * sq_s) * cy, zz], 1)
                    t = faces.time[p_f] + sl[p_sq] * faces.distance(p_f, pts[p_sq])
                    out = np.full(sq_row.size, np.inf)
                    np.minimum.at(out, p_sq, t)
                    at_h.append(out)
                values.append(np.stack(at_h, 1))
            top_t, bottom_t = values
            tol = 1e-9 * (1.0 + np.abs(top_t[:, 0]))
            flat = (
                (np.abs(top_t - top_t[:, :1]) <= tol[:, None]).all(axis=1)
                & (np.abs(bottom_t - bottom_t[:, :1]) <= tol[:, None]).all(axis=1)
                & (np.abs(np.abs(top_t[:, 0] - bottom_t[:, 0]) - sl * (zb[k_ + 1] - zb[k_])) <= tol)
                & np.isfinite(top_t[:, 0])
            )
            down = top_t[:, 0] < bottom_t[:, 0]  # the etchant came from above
            h = np.where(down, zb[k_ + 1] - (budget - top_t[:, 0]) / sl, zb[k_] + (budget - bottom_t[:, 0]) / sl)
            inside = live_sq & flat & (h > zb[k_] + 1e-12) & (h < zb[k_ + 1] - 1e-12)
            heights_found.append(h[inside])
            more = live_sq & ~flat & (sq_s > 1)
            idx = np.flatnonzero(more)
            renum = np.full(sq_row.size, -1, np.int64)
            renum[idx] = np.arange(idx.size)
            half_ = sq_s[idx] // 2
            sq_row = np.repeat(sq_row[idx], 4)
            sq_x = np.repeat(sq_x[idx], 4) + np.tile([0, 1, 0, 1], idx.size) * np.repeat(half_, 4)
            sq_y = np.repeat(sq_y[idx], 4) + np.tile([0, 0, 1, 1], idx.size) * np.repeat(half_, 4)
            sq_s = np.repeat(half_, 4)
            mine = more[p_sq]
            parent = renum[p_sq[mine]]
            p_sq = 4 * np.repeat(parent, 4) + np.tile(np.arange(4), parent.size)
            p_f = np.repeat(p_f[mine], 4)
    flat_heights = np.unique(np.round(np.concatenate(heights_found), 9)) if heights_found else np.zeros(0)

    # Pieces in refined cells: whole, none, or fine cell by fine cell.
    if pieces:
        ids = np.arange(NL, total)
        t = T[ids]
        s_piece = slowness[piece_label]
        # a piece's centre can be anywhere in its cell: a whole diagonal away
        spread = np.sqrt((2.0 * reach_xy) ** 2 + tall[np.minimum(rk[piece_brick], L - 1)] ** 2)
        whole_piece = t + s_piece * spread <= budget
        maybe = np.isfinite(t) & ~whole_piece & (t - s_piece * spread <= budget)
        close = ids[maybe]
        piece_row = np.full(pieces, -1, dtype=np.int64)
        piece_row[close - NL] = np.arange(close.size)
        if close.size:
            rows, fcs = candidates(close, walk=False)
            order = np.argsort(rows, kind="stable")
            piece_rows, piece_faces = rows[order], fcs[order]

    def paint(target, k, x0, y0, s):
        """Set whole squares of a (slabs, ny, nx) grid."""
        for size_ in _unique(s):
            pick = s == size_
            view = target.reshape(target.shape[0], ny // size_, size_, nx // size_, size_)
            view[k[pick], y0[pick] // size_, :, x0[pick] // size_, :] = True

    def place(heights, subset):
        """Where the front is at ``heights`` (one per slab), in the slabs
        ``subset``: (wholly taken plain cells (slabs, ny, nx), keys of the
        cells taken in part, their fine cells taken)."""
        out_whole = np.zeros((L, ny, nx), dtype=bool)
        out_keys: list[np.ndarray] = []
        out_blocks: list[np.ndarray] = []
        sub = np.concatenate([subset, [False]])
        chosen = whole_leaf & sub[nk]
        pick = np.flatnonzero(chosen)
        if pick.size:
            paint(out_whole, nk[pick], nqx[pick], nqy[pick], ns[pick])
        # Squares the front's edge crosses: halved until each part is
        # wholly in, wholly out, or one cell -- whose fine rows are cut exactly.
        rows_here = np.flatnonzero(sub[nk[band_ids]])
        cells_row = np.zeros(0, np.int64)
        cells_x = cells_y = cells_row
        pair_cell = pair_face = cells_row
        if rows_here.size:
            # (part, face) pairs go down with the parts; a face that is
            # farther from all of a part than another face is at most, or
            # than the budget allows, is dropped -- it is nearest nowhere there
            sq_row = rows_here  # index into band_ids
            sq_x, sq_y, sq_s = nqx[band_ids[sq_row]], nqy[band_ids[sq_row]], ns[band_ids[sq_row]]
            lo_c = np.searchsorted(band_rows, sq_row, side="left")
            many = np.searchsorted(band_rows, sq_row, side="right") - lo_c
            p_sq = np.repeat(np.arange(sq_row.size), many)
            p_f = band_faces[np.arange(p_sq.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo_c, many)]
            done_rows, done_x, done_y, done_pc, done_pf = [], [], [], [], []
            n_done = 0
            while sq_row.size:
                check()
                node = band_ids[sq_row]
                k_ = nk[node]
                w_ = p_sq
                lo_b = np.stack([x_min + sq_x[w_] * cx, y_min + sq_y[w_] * cy, heights[k_[w_]]], 1)
                hi_b = np.stack([x_min + (sq_x[w_] + sq_s[w_]) * cx, y_min + (sq_y[w_] + sq_s[w_]) * cy, heights[k_[w_]]], 1)
                sl = slowness[nlab[node]][w_]
                low = faces.time[p_f] + sl * _box_gap(faces.lo[p_f], faces.hi[p_f], lo_b, hi_b)
                high = faces.time[p_f] + sl * _box_far(faces.lo[p_f], faces.hi[p_f], lo_b, hi_b)
                lower = np.full(sq_row.size, np.inf); upper = np.full(sq_row.size, np.inf)
                np.minimum.at(lower, w_, low); np.minimum.at(upper, w_, high)
                box_lo = np.stack([x_min + sq_x * cx, y_min + sq_y * cy, heights[k_]], 1)
                box_hi = np.stack([x_min + (sq_x + sq_s) * cx, y_min + (sq_y + sq_s) * cy, heights[k_]], 1)
                upper = np.minimum(upper, covered(w_, p_f, box_lo, box_hi, slowness[nlab[node]], sq_row.size))
                keep_pair = low <= np.minimum(upper[w_], budget)
                keep_pair[keep_pair] = rivals(w_[keep_pair], p_f[keep_pair], lo_b[keep_pair], hi_b[keep_pair], sl[keep_pair])
                p_sq, p_f = p_sq[keep_pair], p_f[keep_pair]
                inside_all = upper <= budget
                big = sq_s > 1
                fill = inside_all & big
                if fill.any():
                    paint(out_whole, k_[fill], sq_x[fill], sq_y[fill], sq_s[fill])
                one = ~big & (lower <= budget)
                if one.any():
                    # cells: kept with their faces for the fine rows
                    idx = np.flatnonzero(one)
                    renum = np.full(sq_row.size, -1, np.int64)
                    renum[idx] = n_done + np.arange(idx.size)
                    mine = one[p_sq]
                    done_rows.append(sq_row[idx]); done_x.append(sq_x[idx]); done_y.append(sq_y[idx])
                    done_pc.append(renum[p_sq[mine]]); done_pf.append(p_f[mine])
                    n_done += idx.size
                split = big & ~inside_all & (lower <= budget)
                if not split.any():
                    break
                idx = np.flatnonzero(split)
                renum = np.full(sq_row.size, -1, np.int64)
                renum[idx] = np.arange(idx.size)
                h = sq_s[idx] // 2
                ox = np.array([0, 1, 0, 1]); oy = np.array([0, 0, 1, 1])
                sq_row = np.repeat(sq_row[idx], 4)
                sq_x = np.repeat(sq_x[idx], 4) + np.tile(ox, idx.size) * np.repeat(h, 4)
                sq_y = np.repeat(sq_y[idx], 4) + np.tile(oy, idx.size) * np.repeat(h, 4)
                sq_s = np.repeat(h, 4)
                mine = split[p_sq]
                parent = renum[p_sq[mine]]
                p_sq = (4 * np.repeat(parent, 4) + np.tile(np.arange(4), parent.size))
                p_f = np.repeat(p_f[mine], 4)
            if n_done:
                cells_row = np.concatenate(done_rows); cells_x = np.concatenate(done_x); cells_y = np.concatenate(done_y)
                pair_cell = np.concatenate(done_pc); pair_face = np.concatenate(done_pf)
        if cells_row.size:
            # each cell's fine rows against the faces that can be nearest in it
            node = band_ids[cells_row]
            k = nk[node]
            keys_here = k * plane + cells_y * nx + cells_x
            # and below a cell, on square blocks of its fine cells quartered
            # down to four across: the faces still in the running, and the
            # blocks that are wholly in (every fine-cell centre counts)
            n_cells = keys_here.size
            b_cell = np.arange(n_cells)
            b_x0 = np.zeros(n_cells, np.int64)
            b_y0 = np.zeros(n_cells, np.int64)
            R = B
            p_b, p_f = pair_cell, pair_face
            full_cell, full_x0, full_y0, full_R = [], [], [], []
            while True:
                check()
                s_b = slowness[nlab[node[b_cell]]]
                w_ = p_b
                cxl = x_min + (cells_x[b_cell] * B + b_x0 + 0.5) * fx
                cyl = y_min + (cells_y[b_cell] * B + b_y0 + 0.5) * fy
                zb_ = heights[k[b_cell]]
                lo_b = np.stack([cxl[w_], cyl[w_], zb_[w_]], 1)
                hi_b = np.stack([cxl[w_] + (R - 1) * fx, cyl[w_] + (R - 1) * fy, zb_[w_]], 1)
                low = faces.time[p_f] + s_b[w_] * _box_gap(faces.lo[p_f], faces.hi[p_f], lo_b, hi_b)
                high = faces.time[p_f] + s_b[w_] * _box_far(faces.lo[p_f], faces.hi[p_f], lo_b, hi_b)
                lower = np.full(b_cell.size, np.inf); upper = np.full(b_cell.size, np.inf)
                np.minimum.at(lower, w_, low); np.minimum.at(upper, w_, high)
                box_lo = np.stack([cxl - 0.5 * fx, cyl - 0.5 * fy, zb_], 1)
                box_hi = np.stack([cxl + (R - 0.5) * fx, cyl + (R - 0.5) * fy, zb_], 1)
                upper = np.minimum(upper, covered(w_, p_f, box_lo, box_hi, s_b, b_cell.size))
                whole_b = upper <= budget
                if whole_b.any():
                    full_cell.append(b_cell[whole_b]); full_x0.append(b_x0[whole_b]); full_y0.append(b_y0[whole_b])
                    full_R.append(np.full(int(whole_b.sum()), R))
                live_b = ~whole_b & (lower <= budget)
                keep_pair = live_b[w_] & (low <= np.minimum(upper[w_], budget))
                keep_pair[keep_pair] = rivals(w_[keep_pair], p_f[keep_pair], lo_b[keep_pair], hi_b[keep_pair], s_b[w_][keep_pair])
                p_b, p_f = p_b[keep_pair], p_f[keep_pair]
                idx = np.flatnonzero(live_b)
                renum = np.full(b_cell.size, -1, np.int64)
                renum[idx] = np.arange(idx.size)
                if R <= 4:
                    b_cell, b_x0, b_y0, p_b = b_cell[idx], b_x0[idx], b_y0[idx], renum[p_b]
                    break
                R //= 2
                b_cell = np.repeat(b_cell[idx], 4)
                b_x0 = np.repeat(b_x0[idx], 4) + np.tile([0, R, 0, R], idx.size)
                b_y0 = np.repeat(b_y0[idx], 4) + np.tile([0, 0, R, R], idx.size)
                parent = renum[p_b]
                p_b = 4 * np.repeat(parent, 4) + np.tile(np.arange(4), parent.size)
                p_f = np.repeat(p_f, 4)
            # fine rows of the blocks left, against their faces
            rows_b, fcs = p_b, p_f
            s_rows = slowness[nlab[node[b_cell]]][rows_b]
            radius = (budget - faces.time[fcs]) / s_rows
            good = radius >= 0.0
            rows_b, fcs, radius = rows_b[good], fcs[good], radius[good]
            by = np.arange(R)
            c0 = faces.centre[fcs]
            h0 = faces.half[fcs]
            cell_r = b_cell[rows_b]
            row_in = b_y0[rows_b][:, None] + by[None, :]
            yv = y_min + (cells_y[cell_r][:, None] * B + row_in + 0.5) * fy
            dyv = np.maximum(np.abs(yv - c0[:, 1:2]) - h0[:, 1:2], 0.0)
            dz = np.maximum(np.abs(heights[k[cell_r]] - c0[:, 2]) - h0[:, 2], 0.0)[:, None]
            w2 = radius[:, None] ** 2 - dyv**2 - dz**2
            ok = w2 >= 0.0
            w = np.sqrt(np.maximum(w2, 0.0))
            lo = c0[:, 0:1] - h0[:, 0:1] - w
            hi = c0[:, 0:1] + h0[:, 0:1] + w
            base = cells_x[cell_r][:, None] * B
            x0_ = b_x0[rows_b][:, None]
            first_col = np.clip(np.ceil((lo - x_min) / fx - 0.5 - 1e-9).astype(np.int64) - base, x0_, x0_ + R)
            last_col = np.clip(np.floor((hi - x_min) / fx - 0.5 + 1e-9).astype(np.int64) - base, x0_ - 1, x0_ + R - 1)
            ok &= last_col >= first_col
            m, r = np.nonzero(ok)
            row_id = cell_r[m] * B + row_in[m, r]
            starts_, ends_ = first_col[m, r], last_col[m, r]
            if full_cell:
                fc = np.concatenate(full_cell); fx0 = np.concatenate(full_x0); fy0 = np.concatenate(full_y0); fR = np.concatenate(full_R)
                rep = np.repeat(np.arange(fc.size), fR)
                off = np.arange(rep.size) - np.repeat(np.cumsum(fR) - fR, fR)
                row_id = np.concatenate([row_id, fc[rep] * B + fy0[rep] + off])
                starts_ = np.concatenate([starts_, fx0[rep]])
                ends_ = np.concatenate([ends_, fx0[rep] + fR[rep] - 1])
            taken = np.zeros((n_cells, B, B), dtype=bool)
            if row_id.size:
                # union of the intervals on each fine row: as bit masks (a
                # row is at most 64 fine cells), or-ed together per row
                one = np.uint64(1)
                upto = np.where(ends_ + 1 >= 64, ~np.uint64(0), (one << np.minimum(ends_ + 1, 63).astype(np.uint64)) - one)
                below = (one << starts_.astype(np.uint64)) - one
                bits = upto & ~below
                order_r = np.argsort(row_id, kind="stable")
                row_id, bits = row_id[order_r], bits[order_r]
                first_r = np.flatnonzero(np.concatenate([[True], row_id[1:] != row_id[:-1]]))
                used = row_id[first_r]
                merged = np.bitwise_or.reduceat(bits, first_r)
                cover = np.unpackbits(merged.astype('<u8').view(np.uint8).reshape(-1, 8), axis=1, bitorder='little')[:, :B].view(bool)
                taken.reshape(n_cells * B, B)[used] = cover
            out_keys.append(keys_here)
            out_blocks.append(taken)
        if pieces:
            bricks_here = np.flatnonzero(subset[np.minimum(rk, L - 1)] & in_stack)
            taken_fine = np.zeros((bricks_here.size, B, B), dtype=bool)
            step_ = max(1, 1_000_000 // (B * B))
            for first in range(0, bricks_here.size, step_):
                check()
                part = bricks_here[first : first + step_]
                block = fnode[part].reshape(-1)
                has = block >= 0
                piece = np.where(has, block - NL, 0)
                taken = has & whole_piece[piece]
                if close.size:
                    cells = np.flatnonzero(has & (piece_row[piece] >= 0))
                    if cells.size:
                        # every fine cell of the close pieces against its piece's faces
                        r, rem = np.divmod(cells, B * B)
                        by, bx = np.divmod(rem, B)
                        r = part[r]
                        owner_ = piece_row[piece[cells]]
                        lo = np.searchsorted(piece_rows, owner_, side="left")
                        many = np.searchsorted(piece_rows, owner_, side="right") - lo
                        point = np.stack(
                            [x_min + (rix[r] * B + bx + 0.5) * fx, y_min + (riy[r] * B + by + 0.5) * fy, heights[rk[r]]], 1
                        )
                        which = np.repeat(np.arange(cells.size), many)
                        at = np.arange(which.size) - np.repeat(np.cumsum(many) - many, many) + np.repeat(lo, many)
                        f = piece_faces[at]
                        s_cell = slowness[piece_label[close[owner_] - NL]]
                        tt = faces.time[f] + s_cell[which] * faces.distance(f, point[which])
                        best = np.full(cells.size, np.inf)
                        np.minimum.at(best, which, tt)
                        taken[cells[best <= budget]] = True
                taken_fine[first : first + part.size] = taken.reshape(-1, B, B)
            keep = taken_fine.reshape(bricks_here.size, B * B).any(axis=1)
            out_keys.append(rkeys[bricks_here[keep]])
            out_blocks.append(taken_fine[keep])
        keys = np.concatenate(out_keys) if out_keys else np.zeros(0, np.int64)
        blocks = np.concatenate(out_blocks) if out_blocks else np.zeros((0, B, B), bool)
        # a cell painted whole and also cut in rows: the rows are the answer
        return out_whole, keys, blocks

    return Front(place, flat_heights)


def _box_gap(lo_a, hi_a, lo_b, hi_b):
    """The distance between two axis-aligned boxes (0 where they meet)."""
    gap = np.maximum(np.maximum(lo_a - hi_b, lo_b - hi_a), 0.0)
    return np.sqrt((gap * gap).sum(axis=-1))


def _box_far(lo_a, hi_a, lo_b, hi_b):
    """The farthest any point of box b is from box a: per axis the farther
    end of b's span from a's span (distance to an interval is convex)."""
    def off(v):
        return np.maximum(np.maximum(lo_a - v, v - hi_a), 0.0)
    span = np.maximum(off(lo_b), off(hi_b))
    return np.sqrt((span * span).sum(axis=-1))


def _touching(record, rect_of_record, count):
    """Which entry rectangles share an edge (or are one face apart across
    a corner), as CSR (pointer, index).

    ``record`` holds the rectangles on the lattice (orientation, plane, u0,
    u1, v0, v1) and ``rect_of_record`` the face of each. Their edges are
    listed fine step by fine step, so rectangles of any size meet where
    they touch."""
    o, at, u0, u1, v0, v1 = (record[:, i] for i in range(6))
    span = int(max(at.max(), u1.max(), v1.max())) + 2
    keys, owner = [], []

    def edges(sel, along_u):
        idx = np.flatnonzero(sel)
        if idx.size == 0:
            return
        if along_u:  # edges along u at v0 and at v1, one per fine step
            n = u1[idx] - u0[idx]
        else:
            n = v1[idx] - v0[idx]
        rep = np.repeat(idx, n)
        step_ = np.arange(rep.size) - np.repeat(np.cumsum(n) - n, n)
        for side in (0, 1):
            if along_u:
                uu, vv = u0[rep] + step_, (v0 if side == 0 else v1)[rep]
            else:
                uu, vv = (u0 if side == 0 else u1)[rep], v0[rep] + step_
            oo, aa = o[rep], at[rep]
            # (direction, x, y, z) on the lattice
            d = np.where(oo == 0, np.where(along_u, 1, 2), np.where(oo == 1, np.where(along_u, 0, 2), np.where(along_u, 0, 1)))
            x = np.where(oo == 0, aa, uu)
            y = np.where(oo == 0, uu, np.where(oo == 1, aa, vv))
            z = np.where(oo == 2, aa, vv)
            keys.append(((d * span + x) * span + y) * span + z)
            owner.append(rect_of_record[rep])

    for along_u in (True, False):
        edges(np.ones(o.size, bool), along_u)
    key = np.concatenate(keys)
    owner = np.concatenate(owner)
    order = np.argsort(key, kind="stable")
    key, owner = key[order], owner[order]
    fresh = np.concatenate([[True], key[1:] != key[:-1]])
    group_start = np.flatnonzero(fresh)
    size_ = np.diff(np.concatenate([group_start, [key.size]]))
    code = []
    # an edge is shared by a few faces: pair each with the others of its group
    for gap in range(1, int(size_.max()) if size_.size else 1):
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

