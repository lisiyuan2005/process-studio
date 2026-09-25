"""The voxel film model: slabs in z, a grid of material labels in x and y.

The slab kernel's other two film models keep every slab as exact polygons.
That is what makes them exact, and it is also what makes them slow and
fragile on a long flow: boolean cuts leave seams that have to be snapped
away, an isotropic etch can only say "everything within d of the void" by
buffering the front over and over -- 200 times for a 0.5 um etch next to a
5 nm liner -- and every buffer adds arcs, so each step costs more than the
one before.

Here the stack is still a list of slabs, each with an exact bottom and top,
because films are what a process flow is made of and a 5 nm film must stay
5 nm thick. But inside a slab, x and y are a fixed grid of cells, each
holding one material or void (``labels[k, iy, ix]``, 0 = void):

* one cell holds one material, so two materials in one place cannot even be
  written down -- there are no seams, no slivers, nothing to validate;
* "within d of the void" is a distance test on the grid, done once per
  step, so a wet etch has no sub-steps and its cost does not depend on
  its depth or on the thinnest barrier nearby;
* nothing compounds: a grid has no vertices to accumulate.

What it gives up is accuracy sideways below the grid. An edge lands on
the grid, within half a fine cell of where it should be, and a round hole
is drawn in cells. Heights are exact. The grid has two levels: the bulk
cell is the project window over :data:`TARGET_CELLS` (a 1.6 um window is
3.1 nm cells), and a cell that holds more than one material is refined
into a brick of fine cells no larger than the project's XY resolution --
the boundary gets the resolution, the bulk does not pay for it. A
sidewall film thinner than a fine cell is kept one fine cell thick rather
than dropped, because a liner that disappears is a worse lie than one
drawn a little thick.

Semantics follow the simplified (square) film model of the polygon slabs:

* **Deposition.** At every height the film is the void within ``t`` of the
  solid sideways, with the solid taken over the heights within ``t`` above
  and below (square corners in z, round in x and y). Planar deposition only
  looks down -- the solid at that height and below -- and forms nothing in a
  column with solid anywhere above it. A masked deposition keeps the film
  inside the mask's columns (the lift-off result).
* **Vertical etch.** Each column inside the mask is walked from the top: void
  costs nothing, a material with rate ``r`` costs ``h / r`` of the budget,
  and a material with rate 0 stops the column. Everything the walk passed
  is removed, and the slab where it ran out is split at that height.
* **Isotropic etch and oxidation: the etchant's arrival time.** The
  etchant is a liquid: it fills open space at once, and inside a material
  with rate ``r`` it moves at ``r`` in every direction; a material with
  rate 0 stops it. A cell goes when the etchant reaches its centre within
  the etch time. It starts in the open space the ambient reaches inside
  the mask's columns; open space the ambient reaches outside them is under
  resist (spun on, it fills those holes) and stays shut; a sealed cavity
  fills the moment the front breaks into it and etches on from there.
  Several materials etch together, each at its own rate, so a slow one
  under a fast one only starts once the fast one has exposed it, and a
  front goes round a barrier rather than through it. Round in z: slabs of
  an etchable material are cut at the project's z step (never finer than
  a bulk cell) near the slab boundaries where the front can bend, so it
  can curve there. An oxidation relabels the cells instead
  of emptying them.
* **CMP** removes everything above the plane; **flip** turns the stack over
  and mirrors it, exactly as the polygon slabs do.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import shapely
from PIL import Image
from shapely.geometry import box

from ..layout.quick_sketch import QuickSketch
from ..models import MaterialDefinition, ProcessStep, ProcessType, ProjectDefinition, Recipe
from ..worker.errors import Cancelled
from . import voxel_cut

#: Label of an empty cell.
VOID = 0

#: Label of a bulk cell that holds a brick of fine labels instead.
MIXED = 255

#: How a stored voxel state starts, so the kernel can tell it from a
#: polygon state stored under the same suffix.
MAGIC = b"PSVOXEL1"

#: Cells across the longer side of the window, unless the project's XY
#: resolution asks for finer ones.
TARGET_CELLS = 512

#: At most this many fine cells across a bulk cell along a boundary: a
#: brick is ``MAX_REFINE ** 2`` labels.
MAX_REFINE = 64

#: Heights are rounded to this many decimals (1e-9 um), like the polygon
#: slabs, so two planes computed two ways are one plane.
Z_DECIMALS = 9
Z_EPS = 1e-9

#: The 3D view and the pictures are drawn at about this many pixels across.
BASE_PIXELS = 900
BACKGROUND_RGB = (247, 249, 252)
STEP_LINE_SHADE = 0.45
SECTION_POSITIONS = 201


class VoxelError(ValueError):
    """A step the voxel model cannot carry out as given."""


def _z(value: float) -> float:
    return round(float(value), Z_DECIMALS)


def _unique(values, return_index=False, return_inverse=False):
    """``np.unique`` for 1-D integers, by sorting: numpy 2's hashing unique
    is some ninety times slower on the tens of millions of keys a fine
    grid makes."""
    values = np.asarray(values).reshape(-1)
    order = np.argsort(values, kind="stable") if (return_index or return_inverse) else None
    ordered = values[order] if order is not None else np.sort(values)
    fresh = np.ones(ordered.size, dtype=bool)
    fresh[1:] = ordered[1:] != ordered[:-1]
    out = ordered[fresh]
    if not (return_index or return_inverse):
        return out
    result = [out]
    if return_index:
        result.append(order[fresh])
    if return_inverse:
        inverse = np.empty(values.size, dtype=np.int64)
        inverse[order] = np.cumsum(fresh) - 1
        result.append(inverse)
    return tuple(result)


def _labels_in(values) -> np.ndarray:
    """The distinct labels in a uint8 array."""
    return np.flatnonzero(np.bincount(np.asarray(values, dtype=np.uint8).reshape(-1), minlength=256)).astype(np.uint8)


_HASH_MULT = np.random.default_rng(20250923).integers(1, 2**63, size=8192, dtype=np.uint64) | np.uint64(1)


def _brick_hash(blocks: np.ndarray) -> np.ndarray:
    """A 64-bit hash of each brick (for pooling; equality is checked)."""
    n = blocks.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.uint64)
    flat = np.ascontiguousarray(blocks, dtype=np.uint8).reshape(n, -1)
    pad = (-flat.shape[1]) % 8
    if pad:
        flat = np.concatenate([flat, np.zeros((n, pad), np.uint8)], axis=1)
    words = flat.view(np.uint64)
    mult = _HASH_MULT[: words.shape[1]] if words.shape[1] <= _HASH_MULT.size else np.resize(_HASH_MULT, words.shape[1])
    with np.errstate(over="ignore"):
        h = (words * mult).sum(axis=1, dtype=np.uint64)
        h ^= h >> np.uint64(31)
        h *= np.uint64(0x9E3779B97F4A7C15)
    return h


def _pair_hash(blocks: np.ndarray, cuts: np.ndarray | None) -> np.ndarray:
    """A hash of each brick's labels and cuts; a brick without cuts hashes
    as its labels alone."""
    h = _brick_hash(blocks)
    if cuts is None or not cuts.any():
        return h
    n = cuts.shape[0]
    extra = _brick_hash(np.ascontiguousarray(cuts, dtype=np.uint16).view(np.uint8).reshape(n, -1))
    has = cuts.reshape(n, -1).any(axis=1)
    with np.errstate(over="ignore"):
        h = np.where(has, h ^ (extra * np.uint64(0xC2B2AE3D27D4EB4F)), h)
    return h


# -- the state -------------------------------------------------------------


class VoxelState:
    """A stack of slabs, each a grid of material labels, refined along boundaries.

    ``z`` holds the slab boundaries from the floor (0) to the top, so slab
    ``k`` spans ``z[k]`` to ``z[k + 1]`` and holds ``labels[k]``. Label
    ``i > 0`` is ``materials[i - 1]``. Row ``iy`` of a grid is at
    ``y_min + (iy + 0.5) * cell_y``.

    The grid has two levels. A cell of ``labels`` is the bulk resolution;
    where a cell is not all one material it holds :data:`MIXED` and a
    ``refine`` x ``refine`` brick of fine labels, so the boundary has the
    fine resolution and the bulk does not pay for it. Bricks are keyed by
    ``k * ny * nx + iy * nx + ix`` in ``brick_keys`` (sorted), with the
    fine labels in ``bricks``; a brick is only ever kept if it really is
    mixed, so two slabs are the same exactly when their arrays are. With
    ``refine`` 1 there are no bricks and the grid is a plain one.

    A fine cell may be *cut* (see :mod:`voxel_cut`): one straight line
    between two of its eight anchors, its label on one side and another
    material on the other. ``pool_cut`` holds each brick's cuts (0 where a
    fine cell is not cut) beside ``pool``'s labels; a fine cell's label is
    still the material at its centre.

    Bricks are stored once each: ``pool`` holds the distinct bricks and
    ``brick_ref`` which one each refined cell has. Thin slabs along a
    front curving in z repeat every upright wall's bricks, and those cost
    a reference each, not a copy. Equal bricks always share one entry, so
    two cells hold the same fine labels exactly when their references are
    equal.
    """

    def __init__(
        self,
        bounds: tuple[float, float, float, float],
        nx: int,
        ny: int,
        z: np.ndarray,
        labels: np.ndarray,
        materials: Sequence[str],
        z_offset: float,
        refine: int = 1,
        brick_keys: np.ndarray | None = None,
        bricks: np.ndarray | None = None,
        cuts: np.ndarray | None = None,
    ) -> None:
        self.bounds = tuple(float(value) for value in bounds)
        self.nx = int(nx)
        self.ny = int(ny)
        self.z = np.asarray(z, dtype=np.float64)
        self.labels = np.ascontiguousarray(labels, dtype=np.uint8)
        self.materials = list(materials)
        self.z_offset = float(z_offset)
        self.refine = int(refine)
        B = self.refine
        self.brick_keys = (
            np.zeros(0, dtype=np.int64) if brick_keys is None else np.asarray(brick_keys, dtype=np.int64)
        )
        self.pool = np.zeros((0, B, B), dtype=np.uint8)
        self.pool_cut = np.zeros((0, B, B), dtype=np.uint16)
        self._pool_hash = np.zeros(0, dtype=np.uint64)
        self.brick_ref = np.zeros(0, dtype=np.int64)
        if bricks is not None and len(bricks):
            self.brick_ref = self._intern(np.asarray(bricks, dtype=np.uint8), cuts)
        #: Where this state was stored, if it was.
        self.path: Path | None = None
        #: Display meshes by ``buried``, once built.
        self.meshes: dict[bool, dict[str, tuple[np.ndarray, ...]]] = {}
        #: Copies with coarser bricks for pictures, by refinement.
        self.shown: dict[int, "VoxelState"] = {}
        self.mesh_lock = threading.RLock()

    # geometry of the grid

    @property
    def cell_x(self) -> float:
        return (self.bounds[2] - self.bounds[0]) / self.nx

    @property
    def cell_y(self) -> float:
        return (self.bounds[3] - self.bounds[1]) / self.ny

    @property
    def cell(self) -> float:
        return min(self.cell_x, self.cell_y)

    @property
    def fine_x(self) -> float:
        return self.cell_x / self.refine

    @property
    def fine_y(self) -> float:
        return self.cell_y / self.refine

    @property
    def fine(self) -> float:
        return min(self.fine_x, self.fine_y)

    @property
    def plane(self) -> int:
        return self.ny * self.nx

    @property
    def n(self) -> int:
        return int(self.labels.shape[0])

    @property
    def top(self) -> float:
        return float(self.z[-1]) if self.n else 0.0

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        x_min, y_min, _, _ = self.bounds
        xs = x_min + (np.arange(self.nx) + 0.5) * self.cell_x
        ys = y_min + (np.arange(self.ny) + 0.5) * self.cell_y
        return xs, ys

    # bricks

    @property
    def bricks(self) -> np.ndarray:
        """Every refined cell's fine labels, one copy each (for tests and
        small states: a stack of thin slabs is mostly shared bricks)."""
        return self.pool[self.brick_ref]

    def _intern(self, blocks: np.ndarray, cuts: np.ndarray | None = None) -> np.ndarray:
        """Pool entries for ``blocks`` (with their ``cuts``), adding the ones not there yet."""
        n = blocks.shape[0]
        if n == 0:
            return np.zeros(0, dtype=np.int64)
        blocks = np.ascontiguousarray(blocks, dtype=np.uint8)
        cuts = np.zeros(blocks.shape, dtype=np.uint16) if cuts is None else np.ascontiguousarray(cuts, dtype=np.uint16)
        hashes = _pair_hash(blocks, cuts)
        # among themselves: one entry per distinct brick
        unique_hash, first, inverse = _unique(hashes, return_index=True, return_inverse=True)
        inverse = inverse.reshape(-1)
        same = (blocks == blocks[first[inverse]]).all(axis=(1, 2)) & (cuts == cuts[first[inverse]]).all(axis=(1, 2))
        refs = np.empty(n, dtype=np.int64)
        # against the pool
        order = np.argsort(self._pool_hash, kind="stable")
        sorted_hash = self._pool_hash[order]
        at = np.searchsorted(sorted_hash, unique_hash)
        found = at < sorted_hash.size
        found[found] &= sorted_hash[at[found]] == unique_hash[found]
        match = np.full(unique_hash.size, -1, dtype=np.int64)
        match[found] = order[at[found]]
        if found.any():
            ok = (self.pool[match[found]] == blocks[first[found]]).all(axis=(1, 2))
            ok &= (self.pool_cut[match[found]] == cuts[first[found]]).all(axis=(1, 2))
            match[np.flatnonzero(found)[~ok]] = -1
        new = match < 0
        start = self.pool.shape[0]
        match[new] = start + np.arange(int(new.sum()))
        added = [blocks[first[new]]]
        added_cut = [cuts[first[new]]]
        added_hash = [unique_hash[new]]
        refs[same] = match[inverse[same]]
        # a hash shared by different bricks (never seen, but cheap to allow)
        for i in np.flatnonzero(~same):
            refs[i] = start + sum(len(a) for a in added)
            added.append(blocks[i : i + 1])
            added_cut.append(cuts[i : i + 1])
            added_hash.append(_pair_hash(blocks[i : i + 1], cuts[i : i + 1]))
        self.pool = np.concatenate([self.pool] + added)
        self.pool_cut = np.concatenate([self.pool_cut] + added_cut)
        self._pool_hash = np.concatenate([self._pool_hash] + added_hash)
        return refs

    def _compact(self) -> None:
        """Drop pool entries no cell refers to."""
        used, back = _unique(self.brick_ref, return_inverse=True)
        if used.size == self.pool.shape[0]:
            return
        self.pool = np.ascontiguousarray(self.pool[used])
        self.pool_cut = np.ascontiguousarray(self.pool_cut[used])
        self._pool_hash = self._pool_hash[used]
        self.brick_ref = back.reshape(-1).astype(np.int64)

    def blocks(self, keys: np.ndarray) -> np.ndarray:
        """Fine labels of the cells ``keys`` (slab * plane + cell), a brick
        where there is one and the cell's label repeated where there is not."""
        keys = np.asarray(keys, dtype=np.int64)
        B = self.refine
        out = np.empty((keys.size, B, B), dtype=np.uint8)
        out[...] = self.labels.reshape(-1)[keys][:, None, None]
        if self.brick_keys.size and keys.size:
            at = np.searchsorted(self.brick_keys, keys)
            at = np.minimum(at, self.brick_keys.size - 1)
            hit = self.brick_keys[at] == keys
            out[hit] = self.pool[self.brick_ref[at[hit]]]
        return out

    def cuts(self, keys: np.ndarray) -> np.ndarray:
        """Cuts of the fine cells of cells ``keys`` (0 where not cut)."""
        keys = np.asarray(keys, dtype=np.int64)
        B = self.refine
        out = np.zeros((keys.size, B, B), dtype=np.uint16)
        if self.brick_keys.size and keys.size:
            at = np.searchsorted(self.brick_keys, keys)
            at = np.minimum(at, self.brick_keys.size - 1)
            hit = self.brick_keys[at] == keys
            out[hit] = self.pool_cut[self.brick_ref[at[hit]]]
        return out

    @property
    def has_cuts(self) -> bool:
        return bool(self.pool_cut.size) and bool(self.pool_cut.any())

    def store(self, keys: np.ndarray, blocks: np.ndarray, cuts: np.ndarray | None = None) -> None:
        """Write fine labels (and cuts) for the cells ``keys``: a block of
        one label and no cut becomes a plain cell, any other becomes (or
        stays) a brick.

        Without ``cuts``, a fine cell keeps the cut it had where its label
        is unchanged and loses it where the label changed: a process that
        does not draw cuts leaves the ones it did not touch alone."""
        keys = np.asarray(keys, dtype=np.int64)
        if keys.size == 0:
            return
        blocks = np.asarray(blocks, dtype=np.uint8)
        if cuts is None:
            cuts = np.zeros(blocks.shape, dtype=np.uint16)
            if self.has_cuts and self.brick_keys.size:
                # only bricks with cuts have any to keep
                at = np.minimum(np.searchsorted(self.brick_keys, keys), self.brick_keys.size - 1)
                hit = np.flatnonzero(self.brick_keys[at] == keys)
                refs = self.brick_ref[at[hit]]
                cut_any = self.pool_cut.reshape(self.pool_cut.shape[0], -1).any(axis=1)
                keep = cut_any[refs]
                hit, refs = hit[keep], refs[keep]
                if hit.size:
                    kept = self.pool_cut[refs]
                    kept[blocks[hit] != self.pool[refs]] = 0
                    cuts[hit] = kept
        cuts = np.asarray(cuts, dtype=np.uint16)
        if cuts.any():
            # a cut with the same material both sides is no cut
            cuts = np.where(((cuts >> 8) == blocks) | ((cuts & 0xFF) == 0), np.uint16(0), cuts)
        first = blocks[:, :1, :1]
        uniform = (blocks == first).all(axis=(1, 2)) & ~cuts.any(axis=(1, 2))
        flat = self.labels.reshape(-1)
        flat[keys[uniform]] = blocks[uniform, 0, 0]
        flat[keys[~uniform]] = MIXED
        keep = ~np.isin(self.brick_keys, keys)
        new_keys = np.concatenate([self.brick_keys[keep], keys[~uniform]])
        new_refs = np.concatenate([self.brick_ref[keep], self._intern(blocks[~uniform], cuts[~uniform])])
        order = np.argsort(new_keys, kind="stable")
        self.brick_keys = new_keys[order]
        self.brick_ref = new_refs[order]

    def slab_bricks(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        """The bricks of slab ``k``: their cells (flat in the slab) and labels."""
        lo = np.searchsorted(self.brick_keys, k * self.plane)
        hi = np.searchsorted(self.brick_keys, (k + 1) * self.plane)
        return self.brick_keys[lo:hi] - k * self.plane, self.pool[self.brick_ref[lo:hi]]

    def slab_refs(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        """The bricks of slab ``k`` as (cells, pool entries): no copies."""
        lo = np.searchsorted(self.brick_keys, k * self.plane)
        hi = np.searchsorted(self.brick_keys, (k + 1) * self.plane)
        return self.brick_keys[lo:hi] - k * self.plane, self.brick_ref[lo:hi]

    def sample(self, k: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """The material at points ``(x, y)`` of slabs ``k`` (arrays,
        broadcast), on the side of a cut the point is on (on the line: the
        cell's own label)."""
        x_min, y_min, _, _ = self.bounds
        # the points' own arithmetic before broadcasting over the slabs
        gx = (np.asarray(x, float) - x_min) / self.fine_x
        gy = (np.asarray(y, float) - y_min) / self.fine_y
        fx = np.clip(np.floor(gx).astype(np.int64), 0, self.nx * self.refine - 1)
        fy = np.clip(np.floor(gy).astype(np.int64), 0, self.ny * self.refine - 1)
        out = self.sample_fine(k, fx, fy)
        if self.has_cuts:
            shape = out.shape
            out = out.reshape(-1).copy()
            cut = self.sample_cut(k, fx, fy).reshape(-1)
            has = cut > 0
            if has.any():
                u = np.broadcast_to(gx - fx, shape).reshape(-1)[has]
                v = np.broadcast_to(gy - fy, shape).reshape(-1)[has]
                right = ~voxel_cut.left_of(voxel_cut.code_of(cut[has]), u, v)
                where = np.flatnonzero(has)[right]
                out[where] = voxel_cut.other_of(cut[where])
            out = out.reshape(shape)
        return out

    def sample_cut(self, k: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
        """The cut of fine cell ``(fx, fy)`` of slabs ``k`` (0 for none)."""
        B = self.refine
        if not self.has_cuts:
            return np.zeros(np.broadcast_shapes(np.shape(k), np.shape(fx), np.shape(fy)), dtype=np.uint16)
        ix, bx = np.divmod(np.asarray(fx), B)
        iy, by = np.divmod(np.asarray(fy), B)
        k, ix, bx, iy, by = np.broadcast_arrays(np.asarray(k), ix, bx, iy, by)
        shape = k.shape
        k, ix, bx, iy, by = (a.reshape(-1) for a in (k, ix, bx, iy, by))
        out = np.zeros(k.size, dtype=np.uint16)
        mixed = self.labels[k, iy, ix] == MIXED
        if mixed.any():
            keys = k[mixed] * self.plane + iy[mixed] * self.nx + ix[mixed]
            at = np.searchsorted(self.brick_keys, keys)
            out[mixed] = self.pool_cut[self.brick_ref[at], by[mixed], bx[mixed]]
        return out.reshape(shape)

    def sample_fine(self, k: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
        """The label at fine cell ``(fx, fy)`` of slabs ``k``."""
        B = self.refine
        ix, bx = np.divmod(np.asarray(fx), B)
        iy, by = np.divmod(np.asarray(fy), B)
        k, ix, bx, iy, by = np.broadcast_arrays(np.asarray(k), ix, bx, iy, by)
        shape = k.shape
        k, ix, bx, iy, by = (a.reshape(-1) for a in (k, ix, bx, iy, by))
        out = self.labels[k, iy, ix]
        mixed = out == MIXED
        if mixed.any():
            keys = k[mixed] * self.plane + iy[mixed] * self.nx + ix[mixed]
            at = np.searchsorted(self.brick_keys, keys)
            out[mixed] = self.pool[self.brick_ref[at], by[mixed], bx[mixed]]
        return out.reshape(shape)

    @classmethod
    def from_fine(
        cls, bounds, fine: np.ndarray, refine: int, z, materials, z_offset: float = 0.0, cuts: np.ndarray | None = None
    ) -> "VoxelState":
        """A state holding the dense fine labels ``fine`` (slabs, ny*refine,
        nx*refine), and their ``cuts`` if given."""
        fine = np.asarray(fine, dtype=np.uint8)
        n, fy, fx = fine.shape
        B = int(refine)
        ny, nx = fy // B, fx // B

        def blocks(a):
            return a.reshape(n, ny, B, nx, B).transpose(0, 1, 3, 2, 4).reshape(-1, B, B)

        state = cls(bounds, nx, ny, z, np.zeros((n, ny, nx), np.uint8), materials, z_offset, B)
        state.store(
            np.arange(n * ny * nx, dtype=np.int64), blocks(fine),
            None if cuts is None else blocks(np.asarray(cuts, dtype=np.uint16)),
        )
        return state

    def to_fine(self) -> np.ndarray:
        """The dense fine labels (slabs, ny*refine, nx*refine); for small grids and tests."""
        B = self.refine
        keys = np.arange(self.n * self.plane, dtype=np.int64)
        cells = self.blocks(keys).reshape(self.n, self.ny, self.nx, B, B)
        return cells.transpose(0, 1, 3, 2, 4).reshape(self.n, self.ny * B, self.nx * B)

    def to_fine_cuts(self) -> np.ndarray:
        """The dense fine cuts, laid out as :meth:`to_fine`; for tests."""
        B = self.refine
        keys = np.arange(self.n * self.plane, dtype=np.int64)
        cells = self.cuts(keys).reshape(self.n, self.ny, self.nx, B, B)
        return cells.transpose(0, 1, 3, 2, 4).reshape(self.n, self.ny * B, self.nx * B)

    def check(self) -> None:
        """That bricks and MIXED marks agree and no brick is uniform (for tests)."""
        marked = np.flatnonzero(self.labels.reshape(-1) == MIXED)
        assert np.array_equal(marked, self.brick_keys), "MIXED cells and bricks disagree"
        if self.brick_ref.size:
            used = _unique(self.brick_ref)
            data = self.pool[used]
            cut = self.pool_cut[used]
            first = data[:, :1, :1]
            assert not ((data == first).all(axis=(1, 2)) & ~cut.any(axis=(1, 2))).any(), "a brick is uniform"
            assert not (data == MIXED).any(), "a brick holds the MIXED mark"
            both = np.concatenate([self.pool.reshape(len(self.pool), -1), self.pool_cut.reshape(len(self.pool), -1)], axis=1)
            assert np.unique(both, axis=0).shape[0] == len(self.pool), "a brick is pooled twice"
            code = voxel_cut.code_of(cut)
            assert (code <= 32).all(), "a cut code is out of range"
            assert not ((code > 0) & (voxel_cut.other_of(cut) == data)).any(), "a cut with one material both sides"
            assert not ((code == 0) & (cut != 0)).any(), "a material across no cut"

    # materials

    def material_id(self, name: str) -> int:
        """The label of ``name``, adding it to the table when it is new."""
        if name in self.materials:
            return self.materials.index(name) + 1
        if len(self.materials) >= MIXED - 1:
            raise VoxelError(f"the voxel model holds at most {MIXED - 1} materials")
        self.materials.append(name)
        return len(self.materials)

    def known_id(self, name: str) -> int | None:
        return self.materials.index(name) + 1 if name in self.materials else None

    def present(self) -> list[str]:
        """Materials that own cells, in the order they first appear from the floor up."""
        order: list[str] = []
        seen: set[int] = {VOID, MIXED}
        for k in range(self.n):
            found = set(_labels_in(self.labels[k]).tolist())
            _cells, refs = self.slab_refs(k)
            if refs.size:
                used = _unique(refs)
                found |= set(_labels_in(self.pool[used]).tolist())
                cut = self.pool_cut[used]
                if cut.any():
                    found |= set(_labels_in(voxel_cut.other_of(cut[cut > 0])).tolist())
            for label in sorted(found - seen):
                seen.add(label)
                order.append(self.materials[label - 1])
        return order

    def volume(self, name: str) -> float:
        label = self.known_id(name)
        if label is None:
            return 0.0
        counts = (self.labels == label).sum(axis=(1, 2)).astype(np.float64)
        if self.brick_ref.size:
            # a cut cell: its label has the left part, the other material the rest
            left = voxel_cut.LEFT_AREA[voxel_cut.code_of(self.pool_cut)]
            share = np.where(self.pool == label, left, 0.0) + np.where(
                (self.pool_cut > 0) & (voxel_cut.other_of(self.pool_cut) == label), 1.0 - left, 0.0
            )
            fine = share.sum(axis=(1, 2)) / float(self.refine**2)
            np.add.at(counts, self.brick_keys // self.plane, fine[self.brick_ref])
        return float(np.sum(counts * np.diff(self.z))) * self.cell_x * self.cell_y

    # structure

    def copy(self) -> "VoxelState":
        out = VoxelState(
            self.bounds, self.nx, self.ny, self.z.copy(), self.labels.copy(),
            self.materials, self.z_offset, self.refine, self.brick_keys.copy(),
        )
        out.brick_ref = self.brick_ref.copy()
        out.pool = self.pool  # never written in place
        out.pool_cut = self.pool_cut
        out._pool_hash = self._pool_hash
        out._compact()
        return out

    def _reslab(self, source: np.ndarray) -> None:
        """Slabs become ``source``'s old slabs, bricks following them."""
        self.labels = np.ascontiguousarray(self.labels[source])
        if not self.brick_keys.size:
            return
        old_slab, cell = np.divmod(self.brick_keys, self.plane)
        order = np.argsort(source, kind="stable")
        firsts = np.searchsorted(source[order], old_slab, side="left")
        lasts = np.searchsorted(source[order], old_slab, side="right")
        copies = lasts - firsts
        repeat = np.repeat(np.arange(old_slab.size), copies)
        within = np.arange(repeat.size) - np.repeat(np.cumsum(copies) - copies, copies)
        new_slab = order[firsts[repeat] + within]
        keys = new_slab * self.plane + cell[repeat]
        sort = np.argsort(keys, kind="stable")
        self.brick_keys = keys[sort]
        self.brick_ref = self.brick_ref[repeat][sort]

    def split(self, planes) -> np.ndarray:
        """Cut the slabs at every height in ``planes``; returns, for each
        new slab, the index of the slab it was cut from."""
        cuts = sorted(
            {_z(p) for p in planes if self.z[0] + Z_EPS < p < self.z[-1] - Z_EPS}
            - {_z(b) for b in self.z}
        )
        if not cuts:
            return np.arange(self.n)
        z = np.array(sorted({_z(b) for b in self.z} | set(cuts)), dtype=np.float64)
        middles = 0.5 * (z[:-1] + z[1:])
        source = np.searchsorted(self.z, middles, side="right") - 1
        source = np.clip(source, 0, self.n - 1)
        self.z = z
        self._reslab(source)
        return source

    def extend(self, height: float) -> None:
        """Add empty room above the top, up to ``height``."""
        height = _z(height)
        if height <= self.top + Z_EPS:
            return
        self.z = np.append(self.z, height)
        void = np.zeros((1, self.ny, self.nx), dtype=np.uint8)
        self.labels = np.concatenate([self.labels, void], axis=0)

    def _same_slabs(self, k: int) -> bool:
        if not np.array_equal(self.labels[k], self.labels[k + 1]):
            return False
        cells_a, refs_a = self.slab_refs(k)
        cells_b, refs_b = self.slab_refs(k + 1)
        return np.array_equal(cells_a, cells_b) and np.array_equal(refs_a, refs_b)

    def consolidate(self) -> None:
        """Merge neighbouring slabs that hold the same grid; drop empty ones on top."""
        if self.n == 0:
            return
        count = self.n
        # A slab with a brick is not empty: a brick is never all void.
        while count > 0 and not self.labels[count - 1].any():
            count -= 1
        keep = [0] if count else []
        for k in range(1, count):
            if not self._same_slabs(k - 1):
                keep.append(k)
        keep = np.array(keep, dtype=np.int64)
        z = np.concatenate([self.z[keep], [self.z[count]]]) if count else self.z[:1]
        if self.brick_keys.size:
            slab = self.brick_keys // self.plane
            renumber = np.full(self.n, -1, dtype=np.int64)
            renumber[keep] = np.arange(keep.size)
            wanted = renumber[slab] >= 0
            self.brick_keys = renumber[slab[wanted]] * self.plane + self.brick_keys[wanted] % self.plane
            self.brick_ref = self.brick_ref[wanted]
        self.labels = np.ascontiguousarray(self.labels[keep])
        self.z = np.asarray(z, dtype=np.float64)

    # storage

    def save(self, path: Path) -> None:
        buffer = io.BytesIO()
        np.savez_compressed(
            buffer,
            z=self.z,
            labels=self.labels,
            bounds=np.array(self.bounds, dtype=np.float64),
            grid=np.array([self.nx, self.ny, self.refine], dtype=np.int64),
            materials=np.array(self.materials, dtype=str),
            z_offset=np.array(self.z_offset, dtype=np.float64),
            brick_keys=self.brick_keys,
            brick_ref=self.brick_ref,
            pool=self.pool,
            pool_cut=self.pool_cut,
        )
        Path(path).write_bytes(MAGIC + buffer.getvalue())
        self.path = Path(path)

    @classmethod
    def load(cls, path: Path) -> "VoxelState":
        raw = Path(path).read_bytes()
        if not raw.startswith(MAGIC):
            raise VoxelError(f"{Path(path).name} is not a stored voxel state")
        with np.load(io.BytesIO(raw[len(MAGIC):]), allow_pickle=False) as stored:
            grid = [int(value) for value in stored["grid"]]
            nx, ny = grid[0], grid[1]
            refine = grid[2] if len(grid) > 2 else 1
            state = cls(
                tuple(float(value) for value in stored["bounds"]),
                nx,
                ny,
                stored["z"],
                stored["labels"],
                [str(name) for name in stored["materials"]],
                float(stored["z_offset"]),
                refine,
                stored["brick_keys"] if "brick_keys" in stored.files else None,
                stored["bricks"] if "bricks" in stored.files else None,
            )
            if "pool" in stored.files:
                state.pool = np.ascontiguousarray(stored["pool"], dtype=np.uint8)
                state.pool_cut = (
                    np.ascontiguousarray(stored["pool_cut"], dtype=np.uint16)
                    if "pool_cut" in stored.files
                    else np.zeros(state.pool.shape, dtype=np.uint16)
                )
                state._pool_hash = _pair_hash(state.pool, state.pool_cut)
                state.brick_ref = np.asarray(stored["brick_ref"], dtype=np.int64)
        state.path = Path(path)
        return state


def is_voxel_file(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


# -- grid tools ------------------------------------------------------------


def _row_distance(seed: np.ndarray, spacing: float) -> np.ndarray:
    """Distance along the last axis from every cell centre to the nearest
    seed centre in the same row, ``inf`` where the row has none."""
    size = seed.shape[-1]
    index = np.arange(size, dtype=np.int32)
    left = np.where(seed, index, np.int32(-1))
    np.maximum.accumulate(left, axis=-1, out=left)
    right = np.where(seed, index, np.int32(size))
    right = np.minimum.accumulate(right[..., ::-1], axis=-1)[..., ::-1]
    distance = np.minimum(
        np.where(left >= 0, index - left, size), np.where(right < size, right - index, size)
    ).astype(np.float32)
    distance *= np.float32(spacing)
    distance[distance >= np.float32(size * spacing)] = np.inf
    return distance


#: Past this many rows of reach, ``within`` takes the lower envelope of
#: parabolas (a fixed cost per cell) instead of one pass per row of reach.
#: Measured, not derived: a pass per row is one contiguous array operation
#: and the envelope is a Python loop over the rows with scattered indexing,
#: so on a 1600 x 1600 grid the passes win up to about 600 rows of reach
#: (1.6 s against 3.1 at 320) and lose beyond it (4.8 s against 3.1 at 1200).
ENVELOPE_ROWS = 640


def within(seed: np.ndarray, radius: float, cell_x: float, cell_y: float) -> np.ndarray:
    """Cells whose centre is within ``radius`` of a seed centre in the same
    grid, for a stack of grids ``(layers, ny, nx)``.

    Exact Euclidean distance, as a test. The distance along x to the
    nearest seed in each row is found in one pass. For a short reach, a
    cell is within it when some row ``m`` away has a seed within
    ``sqrt(radius^2 - (m * cell_y)^2)`` along x of it -- one pass per row.
    For a long one that is too many passes, and the squared distance down
    each column is taken exactly as the lower envelope of the parabolas
    the rows put there (Felzenszwalb and Huttenlocher), at a fixed cost per
    cell whatever the reach.
    """
    out = np.zeros(seed.shape, dtype=bool)
    if radius < 0 or not seed.any():
        return out
    steps = int(radius / cell_y + 1e-9)
    # A few layers at a time: the row distances are four bytes a cell.
    chunk = max(1, int(4_000_000 // max(1, seed.shape[1] * seed.shape[2])))
    for start in range(0, seed.shape[0], chunk):
        part = seed[start : start + chunk]
        if not part.any():
            continue
        distance = _row_distance(part, cell_x)
        reach = out[start : start + chunk]
        if steps > ENVELOPE_ROWS:
            layers, rows, cols = distance.shape
            columns = distance.transpose(0, 2, 1).reshape(-1, rows).astype(np.float64)
            squared = _envelope(columns * columns, cell_y)
            reach[...] = (squared <= radius * radius * (1 + 1e-12)).reshape(layers, cols, rows).transpose(0, 2, 1)
            continue
        np.less_equal(distance, np.float32(radius), out=reach)
        for m in range(1, steps + 1):
            half = math.sqrt(max(radius * radius - (m * cell_y) ** 2, 0.0))
            near = distance <= np.float32(half)
            reach[:, m:, :] |= near[:, :-m, :]
            reach[:, :-m, :] |= near[:, m:, :]
    return out


def _envelope(f: np.ndarray, spacing: float) -> np.ndarray:
    """``min over q of f[:, q] + ((i - q) * spacing)^2`` for every line and ``i``.

    The one-dimensional squared distance transform, for many lines at
    once: each finite ``f[q]`` is a parabola, their lower envelope is
    built left to right (a parabola hides the ones before it where it
    starts lower), and then read off. The loops run along the line; every
    step handles all the lines together.
    """
    lines, n = f.shape
    s2 = spacing * spacing
    q2 = s2 * np.arange(n, dtype=np.float64) ** 2
    v = np.zeros((lines, n), dtype=np.int64)  # where the envelope's parabolas sit
    z = np.full((lines, n + 1), np.inf)  # where each takes over
    k = np.full(lines, -1, dtype=np.int64)
    rows = np.arange(lines)
    for q in range(n):
        fq = f[:, q]
        active = np.isfinite(fq)
        if not active.any():
            continue
        start = active & (k < 0)
        r = rows[start]
        v[r, 0] = q
        z[r, 0] = -np.inf
        z[r, 1] = np.inf
        k[r] = 0
        r = rows[active & ~start]
        while r.size:
            kk = k[r]
            p = v[r, kk]
            cross = ((fq[r] + q2[q]) - (f[r, p] + q2[p])) / (2.0 * s2 * (q - p))
            hidden = cross <= z[r, kk]
            placed = r[~hidden]
            if placed.size:
                kp = kk[~hidden] + 1
                v[placed, kp] = q
                z[placed, kp] = cross[~hidden]
                z[placed, kp + 1] = np.inf
                k[placed] = kp
            r = r[hidden]
            k[r] -= 1
    out = np.full((lines, n), np.inf)
    have = rows[k >= 0]
    if have.size == 0:
        return out
    at = np.zeros(lines, dtype=np.int64)
    for q in range(n):
        r = have
        while r.size:
            move = z[r, at[r] + 1] < q
            r = r[move]
            at[r] += 1
        p = v[have, at[have]]
        out[have, q] = s2 * (q - p) ** 2 + f[have, p]
    return out


def _union_find(count: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Roots for ids 0..count joined by the pairs (a, b): hooking roots to
    the smaller root and jumping pointers until nothing moves, on arrays."""
    parent = np.arange(count + 1, dtype=np.int64)
    a = np.asarray(a, dtype=np.int64)
    b = np.asarray(b, dtype=np.int64)
    while a.size:
        ra = parent[a]
        rb = parent[b]
        low = np.minimum(ra, rb)
        high = np.maximum(ra, rb)
        differ = low != high
        if not differ.any():
            break
        a, b = a[differ], b[differ]
        np.minimum.at(parent, high[differ], low[differ])
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    return parent


def components(mask: np.ndarray, *, connect_layers: bool = True, check: Callable[[], None] | None = None) -> np.ndarray:
    """Label the 6-connected pieces of a stack of grids (0 = not in the mask).

    Runs along x are found first, then runs that touch in the row above or
    in the slab above are joined (not across the first axis when
    ``connect_layers`` is false: a stack of separate grids). Labels are
    positive but not consecutive. ``check`` is called between passes (a
    stop can be raised from it).
    """
    check = check or (lambda: None)
    check()
    layers, rows, cols = mask.shape
    flat = mask.reshape(-1, cols)
    starts = flat.copy()
    starts[:, 1:] &= ~flat[:, :-1]
    run = np.cumsum(starts.ravel(), dtype=np.int64).reshape(flat.shape)
    run[~flat] = 0
    count = int(run.max()) if run.size else 0
    if count == 0:
        return np.zeros(mask.shape, dtype=np.int64)
    run3 = run.reshape(layers, rows, cols)

    heads: list[np.ndarray] = []
    tails: list[np.ndarray] = []
    pairs = [(run3[:, :-1, :], run3[:, 1:, :])]
    if connect_layers:
        pairs.append((run3[:-1], run3[1:]))
    for below, above in pairs:
        check()
        if below.size == 0:
            continue
        both = (below > 0) & (above > 0)
        # One edge per stretch where the same two runs face each other.
        repeat = np.zeros_like(both)
        repeat[..., 1:] = (
            both[..., 1:]
            & both[..., :-1]
            & (below[..., 1:] == below[..., :-1])
            & (above[..., 1:] == above[..., :-1])
        )
        keep = both & ~repeat
        heads.append(below[keep])
        tails.append(above[keep])
    if not heads:
        return run3
    check()
    parent = _union_find(count, np.concatenate(heads), np.concatenate(tails))
    return parent[run3]


def components2(
    plain: np.ndarray, keys: np.ndarray, fine: np.ndarray, nx: int, ny: int,
    check: Callable[[], None] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """The 6-connected pieces of a set given at the grid's two levels.

    ``plain`` marks whole bulk cells of a stack (slabs, ny, nx); ``keys``
    (slab * ny * nx + cell) are the refined cells and ``fine`` their fine
    cells in the set. Returns a piece id for every plain cell and every
    fine cell (0 outside the set); a piece may run through both levels.
    Fine cells of a refined cell join each other inside it, the plain
    cell or refined cell's fine cells beside them, and those above and
    below. ``check`` is called between passes.
    """
    check = check or (lambda: None)
    plane = ny * nx
    layers = plain.shape[0]
    coarse_id = components(plain, check=check)
    base = int(coarse_id.max()) if coarse_id.size else 0
    if keys.size == 0:
        return coarse_id, np.zeros(fine.shape, dtype=np.int64)
    fine_id = components(fine, connect_layers=False, check=check)
    fine_id[fine_id > 0] += base
    count = int(fine_id.max()) if fine_id.size else base
    count = max(count, base)
    B = fine.shape[1]
    k, rest = np.divmod(keys, plane)
    iy, ix = np.divmod(rest, nx)
    flat_plain = plain.reshape(-1)
    flat_coarse = coarse_id.reshape(-1)
    heads: list[np.ndarray] = []
    tails: list[np.ndarray] = []

    def refined(at_keys):
        at = np.minimum(np.searchsorted(keys, at_keys), keys.size - 1)
        return at, keys[at] == at_keys

    def per_brick(bricks, ids):
        """One (id) per piece per brick: the distinct ids of each brick's cells."""
        rows = np.repeat(bricks, ids.shape[1])
        values = ids.reshape(-1)
        keep = values > 0
        combined = _unique(rows[keep] * (count + 1) + values[keep])
        return combined // (count + 1), combined % (count + 1)

    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        check()
        ok = (ix + dx >= 0) & (ix + dx < nx) & (iy + dy >= 0) & (iy + dy < ny)
        idx = np.flatnonzero(ok)
        other = k[idx] * plane + (iy[idx] + dy) * nx + ix[idx] + dx
        if dx == 1:
            mine, side = fine_id[idx, :, -1], 0
        elif dx == -1:
            mine, side = fine_id[idx, :, 0], -1
        elif dy == 1:
            mine, side = fine_id[idx, -1, :], 0
        else:
            mine, side = fine_id[idx, 0, :], -1
        # A plain neighbour in the set.
        member = flat_plain[other]
        if member.any():
            bricks, ids = per_brick(np.arange(int(member.sum())), mine[member])
            heads.append(ids)
            tails.append(flat_coarse[other[member]][bricks])
        # A refined neighbour: fine cells face to face (each pair of bricks once).
        if dx == 1 or dy == 1:
            at, hit = refined(other)
            if hit.any():
                theirs = fine_id[at[hit]]
                theirs = theirs[:, :, side] if dx else theirs[:, side, :]
                both = (mine[hit] > 0) & (theirs > 0)
                pairs = _unique(mine[hit][both] * (count + 1) + theirs[both])
                heads.append(pairs // (count + 1))
                tails.append(pairs % (count + 1))
    for dk in (1, -1):
        check()
        ok = (k + dk >= 0) & (k + dk < layers)
        idx = np.flatnonzero(ok)
        other = (k[idx] + dk) * plane + rest[idx]
        member = flat_plain[other]
        if member.any():
            chosen = idx[member]
            bricks, ids = per_brick(np.arange(chosen.size), fine_id[chosen].reshape(chosen.size, -1))
            heads.append(ids)
            tails.append(flat_coarse[other[member]][bricks])
        if dk == 1:
            at, hit = refined(other)
            if hit.any():
                mine = fine_id[idx[hit]]
                theirs = fine_id[at[hit]]
                both = (mine > 0) & (theirs > 0)
                pairs = _unique(mine[both] * (count + 1) + theirs[both])
                heads.append(pairs // (count + 1))
                tails.append(pairs % (count + 1))
    check()
    parent = _union_find(count, np.concatenate(heads) if heads else np.zeros(0), np.concatenate(tails) if tails else np.zeros(0))
    return parent[coarse_id], parent[fine_id]


def _neighbours(mask: np.ndarray) -> np.ndarray:
    """Cells with a 6-neighbour in ``mask`` (the window edges have none)."""
    out = np.zeros_like(mask)
    out[:, :, 1:] |= mask[:, :, :-1]
    out[:, :, :-1] |= mask[:, :, 1:]
    out[:, 1:, :] |= mask[:, :-1, :]
    out[:, :-1, :] |= mask[:, 1:, :]
    out[1:] |= mask[:-1]
    out[:-1] |= mask[1:]
    return out


def _window(z: np.ndarray, k: int, reach: float, *, below_only: bool = False) -> tuple[int, int]:
    """Slabs ``j`` (first, last inclusive) whose gap to slab ``k`` is under ``reach``."""
    first = int(np.searchsorted(z[1:], z[k] - reach + Z_EPS, side="right"))
    if below_only:
        return first, k
    last = int(np.searchsorted(z[:-1], z[k + 1] + reach - Z_EPS, side="left")) - 1
    return first, last


def _check(should_cancel: Callable[[], bool] | None, what: str) -> None:
    if should_cancel is not None and should_cancel():
        raise Cancelled(f"Stopped during {what}; it did not finish.")


# -- two-level yes/no fields -------------------------------------------------


class Mask2:
    """A yes/no field over one slab's grid, at the grid's two levels.

    ``coarse`` holds 0 (no), 1 (yes) or 2 (part: see the fine block) per
    bulk cell; ``cells`` lists the part cells (flat, sorted) and
    ``blocks`` their fine values.
    """

    def __init__(self, coarse: np.ndarray, cells: np.ndarray, blocks: np.ndarray) -> None:
        self.coarse = np.ascontiguousarray(coarse, dtype=np.uint8)
        self.cells = np.asarray(cells, dtype=np.int64)
        self.blocks = np.asarray(blocks, dtype=bool)
        #: The outline it was drawn from, if any: asked at any point where
        #: a boundary is fitted (see :func:`_refit`).
        self.geometry = None

    def at(self, state: "VoxelState", x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Yes or no at points: from the outline where there is one, else
        from the fine cell the point is in."""
        x, y = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float))
        if self.geometry is not None:
            return shapely.intersects_xy(self.geometry, x.ravel(), y.ravel()).reshape(x.shape)
        B = state.refine
        x_min, y_min, _, _ = state.bounds
        fx = np.clip(np.floor((x - x_min) / state.fine_x).astype(np.int64), 0, state.nx * B - 1)
        fy = np.clip(np.floor((y - y_min) / state.fine_y).astype(np.int64), 0, state.ny * B - 1)
        cell = (fy // B) * state.nx + fx // B
        coarse = self.coarse.reshape(-1)[cell]
        out = coarse == 1
        part = coarse == 2
        if part.any():
            at = np.searchsorted(self.cells, cell[part])
            out[part] = self.blocks[at, fy[part] % B, fx[part] % B]
        return out

    @classmethod
    def uniform(cls, ny: int, nx: int, refine: int, value: bool = True) -> "Mask2":
        return cls(
            np.full((ny, nx), 1 if value else 0, np.uint8),
            np.zeros(0, np.int64),
            np.zeros((0, refine, refine), bool),
        )

    @classmethod
    def build(cls, coarse: np.ndarray, cells: np.ndarray, blocks: np.ndarray) -> "Mask2":
        """From yes/no bulk cells and fine blocks for some of them, which
        take precedence; blocks that are all one value become plain cells."""
        coarse = np.asarray(coarse).astype(np.uint8).copy()
        cells = np.asarray(cells, dtype=np.int64)
        blocks = np.asarray(blocks, dtype=bool)
        if cells.size:
            every = blocks.all(axis=(1, 2))
            none = ~blocks.any(axis=(1, 2))
            flat = coarse.reshape(-1)
            flat[cells[every]] = 1
            flat[cells[none]] = 0
            part = ~every & ~none
            flat[cells[part]] = 2
            order = np.argsort(cells[part], kind="stable")
            return cls(coarse, cells[part][order], blocks[part][order])
        return cls(coarse, cells, blocks.reshape(0, *blocks.shape[1:]) if blocks.size == 0 else blocks)

    def any(self) -> bool:
        return bool(self.coarse.any())

    def blocks_for(self, cells: np.ndarray, refine: int) -> np.ndarray:
        cells = np.asarray(cells, dtype=np.int64)
        out = np.empty((cells.size, refine, refine), dtype=bool)
        out[...] = (self.coarse.reshape(-1)[cells] == 1)[:, None, None]
        if self.cells.size and cells.size:
            at = np.minimum(np.searchsorted(self.cells, cells), self.cells.size - 1)
            hit = self.cells[at] == cells
            out[hit] = self.blocks[at[hit]]
        return out

    def to_fine(self, refine: int) -> np.ndarray:
        ny, nx = self.coarse.shape
        cells = np.arange(ny * nx)
        blocks = self.blocks_for(cells, refine).reshape(ny, nx, refine, refine)
        return blocks.transpose(0, 2, 1, 3).reshape(ny * refine, nx * refine)


def _as_mask(state: "VoxelState", opening) -> Mask2 | None:
    """A mask as the processes take it: a :class:`Mask2`, a plain yes/no
    array over the bulk cells, or None for the whole window."""
    if opening is None or isinstance(opening, Mask2):
        return opening
    return Mask2.build(
        np.asarray(opening, dtype=bool), np.zeros(0, np.int64), np.zeros((0, state.refine, state.refine), bool)
    )


# -- cut cells ---------------------------------------------------------------


def _padded(state: VoxelState, k: int, cells: np.ndarray) -> np.ndarray:
    """Fine labels of cells ``cells`` of slab ``k`` with a ring of their
    neighbours' fine cells round them: (cells, B + 2, B + 2). Past the
    window's edge the ring repeats the cell's own edge (the wafer goes on)."""
    B, nx, ny, plane = state.refine, state.nx, state.ny, state.plane
    iy, ix = np.divmod(np.asarray(cells, dtype=np.int64), nx)
    out = np.empty((cells.size, B + 2, B + 2), dtype=np.uint8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            jy = np.clip(iy + dy, 0, ny - 1)
            jx = np.clip(ix + dx, 0, nx - 1)
            data = state.blocks(k * plane + jy * nx + jx)
            # a neighbour past the edge: the cell's own edge row again
            if dy == -1:
                data = np.where((iy == 0)[:, None, None], data[:, :1, :].repeat(B, 1), data)
            if dy == 1:
                data = np.where((iy == ny - 1)[:, None, None], data[:, -1:, :].repeat(B, 1), data)
            if dx == -1:
                data = np.where((ix == 0)[:, None, None], data[:, :, :1].repeat(B, 2), data)
            if dx == 1:
                data = np.where((ix == nx - 1)[:, None, None], data[:, :, -1:].repeat(B, 2), data)
            rows = slice(0, 1) if dy == -1 else (slice(B + 1, B + 2) if dy == 1 else slice(1, B + 1))
            cols = slice(0, 1) if dx == -1 else (slice(B + 1, B + 2) if dx == 1 else slice(1, B + 1))
            src_rows = slice(B - 1, B) if dy == -1 else (slice(0, 1) if dy == 1 else slice(0, B))
            src_cols = slice(B - 1, B) if dx == -1 else (slice(0, 1) if dx == 1 else slice(0, B))
            out[:, rows, cols] = data[:, src_rows, src_cols]
    return out


def _fill_whole(state: VoxelState, k: int, whole: np.ndarray, label: int) -> None:
    """Give the cells ``whole`` of slab ``k`` all to ``label``. A cell with
    a brick there (open fine cells with a cut, say) gives it up."""
    grid = state.labels[k]
    bricked = whole & (grid == MIXED)
    grid[whole & ~bricked] = label
    if bricked.any():
        cells = np.flatnonzero(bricked.reshape(-1))
        shape = (cells.size, state.refine, state.refine)
        state.store(k * state.plane + cells, np.full(shape, label, np.uint8), np.zeros(shape, np.uint16))


def _refit(state: VoxelState, k: int, cells: np.ndarray, decide, memo: dict | None = None, context: bytes = b"") -> None:
    """Draw the cuts along the boundaries in cells ``cells`` of slab ``k``.

    ``decide(x, y)`` says which material the step just run leaves at any
    point of the slab (for the fine cells' centres it is what was written).
    Every fine cell with a neighbour (of eight) of another material is
    fitted from what ``decide`` says at its anchors and, where the
    boundary crosses half a side, at that half side's middle (see
    :func:`voxel_cut.fit`); the cells in between are left whole.

    With ``memo``, a slab whose cells (and their ring of neighbours) hold
    what an earlier slab's held, under the same ``context`` -- everything
    else ``decide`` reads -- takes that slab's answer instead of asking
    again: a thick layer is many slabs with one content.
    """
    cells = np.asarray(cells, dtype=np.int64)
    if cells.size == 0:
        return
    B, nx = state.refine, state.nx
    x_min, y_min, _, _ = state.bounds
    fx, fy = state.fine_x, state.fine_y
    keys = k * state.plane + cells
    labels = state.blocks(keys)
    cuts = np.zeros(labels.shape, dtype=np.uint16)
    pad = _padded(state, k, cells)
    if memo is not None:
        key = hashlib.blake2b(context + cells.tobytes() + pad.tobytes(), digest_size=16).digest()
        hit = memo.get(key)
        if hit is not None:
            state.store(keys, hit[0].copy(), hit[1].copy())
            return
    centre = pad[:, 1:-1, 1:-1]
    edge = np.zeros(labels.shape, dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                edge |= pad[:, 1 + dy : B + 1 + dy, 1 + dx : B + 1 + dx] != centre
    c, by, bx = np.nonzero(edge)
    if c.size:
        iy, ix = np.divmod(cells[c], nx)
        x0 = x_min + (ix * B + bx) * fx
        y0 = y_min + (iy * B + by) * fy
        # A hair inside the cell: a point on a side belongs to both cells,
        # and a field given cell by cell (a mask with no outline, the state
        # itself) has its boundary exactly there -- read from inside, a
        # cell sees its own material all round and is not cut.
        inward = 0.5 + (voxel_cut.ANCHORS - 0.5) * (1.0 - 2e-3)
        ax = x0[:, None] + inward[None, :, 0] * fx
        ay = y0[:, None] + inward[None, :, 1] * fy
        anchor = np.asarray(decide(ax.ravel(), ay.ravel()), dtype=np.uint8).reshape(-1, 8)
        crossing = anchor != np.roll(anchor, -1, axis=1)
        snap = np.broadcast_to(voxel_cut.TO_MIDDLE, anchor.shape).copy()
        r, h = np.nonzero(crossing)
        if r.size:
            middle = 0.5 + (voxel_cut.half_middles() - 0.5) * (1.0 - 2e-3)
            mx = x0[r] + middle[h, 0] * fx
            my = y0[r] + middle[h, 1] * fy
            got = np.asarray(decide(mx, my), dtype=np.uint8)
            # the middle of the half side holds the far end's material: the
            # crossing is in the near half, so it snaps to the near anchor
            snap[r, h] = np.where(got == anchor[r, (h + 1) % 8], 0, 1)
        label, cut = voxel_cut.fit(anchor, labels[c, by, bx], snap)
        labels[c, by, bx] = label
        cuts[c, by, bx] = cut
    if memo is not None:
        # identical slabs come together (one layer split up): a few will do
        while len(memo) >= 4:
            memo.pop(next(iter(memo)))
        memo[key] = (labels.copy(), cuts.copy())
    state.store(keys, labels, cuts)


# -- processes -------------------------------------------------------------


def deposit(
    state: VoxelState,
    material: str,
    thickness: float,
    *,
    planar: bool,
    opening=None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Grow ``thickness`` of ``material`` over every surface (see the module notes).

    A fine cell of open space gets film when its centre is within
    ``thickness`` of the solid, the solid being taken over the heights
    within ``thickness`` above and below it (or only below, planar). That
    distance is measured to the solid's edge, the real surface. Where the
    film's edge falls inside a bulk cell, the cell is refined and each of
    its fine rows is cut exactly against the solid's boundary.
    """
    t = float(thickness)
    label = state.material_id(material)
    opening = _as_mask(state, opening)
    old_top = state.top
    planes = {b + t for b in state.z} | {b - t for b in state.z}
    state.extend(old_top + t)
    state.split(planes)
    n, B, plane = state.n, state.refine, state.plane
    kind = np.where(state.labels == VOID, 0, np.where(state.labels == MIXED, 2, 1)).astype(np.uint8)
    full = np.zeros((n + 1, state.ny, state.nx), dtype=np.uint16)
    np.cumsum(kind == 1, axis=0, dtype=np.uint16, out=full[1:])
    part = np.zeros((n + 1, state.ny, state.nx), dtype=np.uint16)
    np.cumsum(kind == 2, axis=0, dtype=np.uint16, out=part[1:])
    # Centre to centre, so half a fine cell more than the thickness; and at
    # least one fine cell sideways: thinner, a film would vanish from every
    # wall and leave the floor and the top as its only trace.
    reach = max(t + 0.5 * state.fine, state.fine * 1.0001)

    # Everything is read from the state as it was, then written at once.
    before = state.copy()
    # Inside a mask the film forms where the gas gets to: the open space
    # the opening leads to, under the resist too where it is buried, and
    # never the space the resist fills (see _wet_field).
    reached = None
    if opening is not None:
        reached = _wet_field(state, opening, check=lambda: _check(should_cancel, "a deposition"))
    covers: dict[tuple[int, int], Mask2] = {}
    near_of: dict[bytes, Mask2] = {}
    solid_of: dict[tuple[int, int], tuple[bytes, Mask2]] = {}
    films: list[tuple[int, Mask2]] = []
    shadow = _Stack(state) if planar else None
    for k in range(n - 1, -1, -1):
        if shadow is not None and k < n - 1:
            shadow.add(k + 1)
        if not (kind[k] != 1).any():
            continue
        _check(should_cancel, "a deposition")
        first, last = _window(state.z, k, t, below_only=planar)
        window = (first, last)
        if window not in covers:
            solid = _union(state, kind, full, part, first, last)
            # different windows often hold the same solid (every slab of
            # one thick layer): measure it once
            digest = hashlib.blake2b(
                solid.coarse.tobytes() + solid.cells.tobytes() + np.packbits(solid.blocks).tobytes(),
                digest_size=16,
            ).digest()
            if digest not in near_of:
                near_of[digest] = _near(state, solid, reach)
            covers[window] = near_of[digest]
            solid_of[window] = (digest, solid)
        film = covers[window]
        void_here = Mask2.build(kind[k] == 0, *_fine_where(state, k, kind[k] == 2, lambda b: b == VOID))
        film = _and(state, film, void_here)
        if shadow is not None:
            film = _and(state, film, shadow.inverse())
        if reached is not None:
            film = _and(state, film, _field_mask(reached, k))
        films.append((k, film, window))
    keys_out, blocks_out = [], []
    for k, film, _window_k in films:
        _fill_whole(state, k, film.coarse == 1, label)
        if film.cells.size:
            keys = k * plane + film.cells
            blocks = state.blocks(keys)
            blocks[film.blocks] = label
            keys_out.append(keys)
            blocks_out.append(blocks)
    if keys_out:
        state.store(np.concatenate(keys_out), np.concatenate(blocks_out))
    # Boundaries: the film's edge at any point -- open space within reach
    # of the solid's surface (the same surface lines, the same reach), not
    # under solid (planar), inside the mask.
    trees: dict[bytes, tuple[Any, np.ndarray] | None] = {}
    memo: dict = {}
    x_min, y_min, _, _ = state.bounds
    half_cell = 0.5 * math.hypot(state.cell_x, state.cell_y)
    for k, _film, window in films:
        _check(should_cancel, "a deposition")
        region = _changed_cells(state, k, before, k)
        if region.size == 0:
            continue
        digest, solid = solid_of[window]
        if digest not in trees:
            segments = _surface(before, solid)
            trees[digest] = (shapely.STRtree(shapely.linestrings(segments.reshape(-1, 2, 2))), segments) if len(segments) else None
        found = trees[digest]
        # Per cell: the surface lines that can be the nearest to some point
        # of it -- those within the nearest one's distance from its centre
        # plus its diagonal -- or, where even the farthest point of the cell
        # is within reach or the nearest is past it, the answer for all of it.
        whole_in = np.zeros(region.size, dtype=bool)
        whole_out = np.ones(region.size, dtype=bool)
        if found is not None:
            tree, segments = found
            ry, rx = np.divmod(region, state.nx)
            centres = shapely.points(x_min + (rx + 0.5) * state.cell_x, y_min + (ry + 0.5) * state.cell_y)
            (first, line_hit), distance = tree.query_nearest(centres, return_distance=True, all_matches=False)
            nearest = np.full(region.size, np.inf)
            np.minimum.at(nearest, first, distance)
            nearest_line = np.full(region.size, -1, dtype=np.int64)
            nearest_line[first] = line_hit
            whole_in = nearest + half_cell <= reach
            whole_out = nearest - half_cell > reach
        else:
            segments = np.zeros((0, 4))
            nearest_line = np.full(region.size, -1, dtype=np.int64)
            nearest = np.full(region.size, np.inf)

        def decide(x, y, k=k, region=region, segments=segments, found=found,
                   whole_in=whole_in, whole_out=whole_out, nearest_line=nearest_line, nearest=nearest):
            old = before.sample(k, x, y)
            out = old.copy()
            film = old == VOID
            ask = np.flatnonzero(film)
            if ask.size:
                gx = np.clip(np.floor((x[ask] - x_min) / state.cell_x).astype(np.int64), 0, state.nx - 1)
                gy = np.clip(np.floor((y[ask] - y_min) / state.cell_y).astype(np.int64), 0, state.ny - 1)
                row = np.searchsorted(region, gy * state.nx + gx)
                sure = whole_in[row]
                none = whole_out[row]
                film[ask[none]] = False
                keep = ~sure & ~none
                ask, row = ask[keep], row[keep]
            if ask.size:
                # most points are well inside the film: the line nearest to
                # their bulk cell's centre is already within reach of them;
                # and a point farther from its cell's centre than the centre
                # is from reach cannot be within it
                g = segments[np.maximum(nearest_line[row], 0)]
                quick = (nearest_line[row] >= 0) & (_line_distance2(x[ask], y[ask], g) <= reach * reach * (1 + 1e-12))
                cy_, cx_ = np.divmod(region[row], state.nx)
                away = np.hypot(x[ask] - (x_min + (cx_ + 0.5) * state.cell_x), y[ask] - (y_min + (cy_ + 0.5) * state.cell_y))
                far = ~quick & (nearest[row] - away > reach * (1 + 1e-9))
                film[ask[far]] = False
                keep = ~quick & ~far
                ask, row = ask[keep], row[keep]
            if ask.size:
                film[ask] = _within_lines(
                    x[ask], y[ask], found[0], segments, reach,
                    x_min, y_min, state.fine_x, state.fine_y, state.nx * state.refine, state.refine,
                )
            if planar and k + 1 < before.n:
                above = before.sample(np.arange(k + 1, before.n)[:, None], x[None, :], y[None, :])
                film &= ~(above != VOID).any(axis=0)
            if reached is not None:
                film &= reached.sample(k, x, y) == 1
            out[film] = label
            return out

        # what decide reads besides the slab itself: the surface lines and
        # the slab as it was; above it too when planar (not shared then)
        context = None
        if not (planar and k + 1 < before.n):
            before_keys = k * before.plane + region
            context = digest + before.blocks(before_keys).tobytes() + before.cuts(before_keys).tobytes()
        _refit(state, k, region, decide, memo if context is not None else None, context or b"")
    state.consolidate()


def _line_distance2(px, py, g):
    """Squared distance from points to axis-aligned segments (x0, y0, x1, y1)."""
    gx0, gx1 = np.minimum(g[:, 0], g[:, 2]), np.maximum(g[:, 0], g[:, 2])
    gy0, gy1 = np.minimum(g[:, 1], g[:, 3]), np.maximum(g[:, 1], g[:, 3])
    dx = np.maximum(np.maximum(gx0 - px, px - gx1), 0.0)
    dy = np.maximum(np.maximum(gy0 - py, py - gy1), 0.0)
    return dx * dx + dy * dy


def _pairs(group, lo_of, many_of):
    """(row, index) for ragged ranges: row i owns ``many_of[i]`` indices from ``lo_of[i]``."""
    which = np.repeat(np.arange(group), many_of)
    at = np.arange(which.size) - np.repeat(np.cumsum(many_of) - many_of, many_of) + np.repeat(lo_of, many_of)
    return which, at


def _narrow(parent_of, lo, many, seg_of, segments, cx, cy, half, budget=4_000_000):
    """For groups (with centres ``cx``, ``cy`` and half-diagonal ``half``)
    whose candidate lines are ``seg_of[lo : lo + many]``, keep the lines that
    can be the nearest somewhere in the group: within the nearest one's
    distance from the centre plus twice ``half``. Returns (group, line)
    sorted by group, and each group's nearest distance from its centre."""
    kept_g: list[np.ndarray] = []
    nearest_all = np.full(cx.size, np.inf)
    kept_s: list[np.ndarray] = []
    total = cx.size
    start = 0
    cum = np.concatenate([[0], np.cumsum(many)])
    while start < total:
        stop = int(np.searchsorted(cum, cum[start] + budget, side="right")) - 1
        stop = max(stop, start + 1)
        stop = min(stop, total)
        which, at = _pairs(stop - start, lo[start:stop], many[start:stop])
        seg = seg_of[at]
        d2 = _line_distance2(cx[start + which], cy[start + which], segments[seg])
        nearest = np.full(stop - start, np.inf)
        np.minimum.at(nearest, which, d2)
        nearest_all[start:stop] = np.sqrt(nearest)
        limit = (np.sqrt(nearest) + 2.0 * half) ** 2
        keep = d2 <= limit[which] * (1 + 1e-12)
        kept_g.append(start + which[keep])
        kept_s.append(seg[keep])
        start = stop
    g = np.concatenate(kept_g) if kept_g else np.zeros(0, np.int64)
    sg = np.concatenate(kept_s) if kept_s else np.zeros(0, np.int64)
    order = np.argsort(g, kind="stable")
    return g[order], sg[order], nearest_all


def _within_lines(x, y, tree, segments, reach, x_min, y_min, fx, fy, fine_cols, refine):
    """Whether points are within ``reach`` of any surface line (``segments``,
    indexed by ``tree``).

    Narrowed level by level, so a wall drawn in a great many short lines is
    not measured line by line from every point: for each block of 4 x 4
    fine cells the points are in, the lines within the nearest one's
    distance from its centre plus its diagonal (the only ones that can be
    nearest anywhere in it); from those, each fine cell's the same way;
    then each point against its fine cell's."""
    out = np.zeros(x.size, dtype=bool)
    if x.size == 0:
        return out
    gx = np.floor((x - x_min) / fx).astype(np.int64)
    gy = np.floor((y - y_min) / fy).astype(np.int64)
    q = min(4, refine)
    block_id = (gy // q) * (fine_cols // q + 1) + gx // q
    blocks, b_first, b_back = _unique(block_id, return_index=True, return_inverse=True)
    bcx = x_min + ((gx[b_first] // q) * q + 0.5 * q) * fx
    bcy = y_min + ((gy[b_first] // q) * q + 0.5 * q) * fy
    centres = shapely.points(bcx, bcy)
    (first, _line), distance = tree.query_nearest(centres, return_distance=True, all_matches=False)
    nearest = np.full(blocks.size, np.inf)
    np.minimum.at(nearest, first, distance)
    bg, bs = tree.query(centres, predicate="dwithin", distance=nearest + q * math.hypot(fx, fy) + 1e-12)
    order = np.argsort(bg, kind="stable")
    bg, bs = bg[order], bs[order]
    # fine cells, from their block's lines
    fine_id = gy * fine_cols + gx
    cells, first_c, back = _unique(fine_id, return_index=True, return_inverse=True)
    c_block = b_back[first_c]
    ccx = x_min + (cells % fine_cols + 0.5) * fx
    ccy = y_min + (cells // fine_cols + 0.5) * fy
    lo = np.searchsorted(bg, c_block, side="left")
    many = np.searchsorted(bg, c_block, side="right") - lo
    kc, ks, centre_d = _narrow(None, lo, many, bs, segments, ccx, ccy, 0.5 * math.hypot(fx, fy))
    # a point is no farther from the lines than its cell's centre plus the
    # way to it, and no nearer than that less the way: most are settled so
    away = np.hypot(x - ccx[back], y - ccy[back])
    d = centre_d[back]
    out[d + away <= reach] = True
    open_ = np.abs(d - reach) < away * (1 + 1e-9) + 1e-12 * reach
    open_ &= ~out
    ask = np.flatnonzero(open_)
    back = back[ask]
    # points, from their fine cell's lines
    lo2 = np.searchsorted(kc, back, side="left")
    many2 = np.searchsorted(kc, back, side="right") - lo2
    which, at = _pairs(ask.size, lo2, many2)
    d2 = _line_distance2(x[ask[which]], y[ask[which]], segments[ks[at]])
    out[ask[which[d2 <= reach * reach * (1 + 1e-12)]]] = True
    return out


def _fine_where(state: VoxelState, k: int, cells_mask: np.ndarray, test) -> tuple[np.ndarray, np.ndarray]:
    """Fine blocks of ``test(labels)`` for the cells of slab ``k`` in ``cells_mask``."""
    cells = np.flatnonzero(cells_mask.reshape(-1))
    if cells.size == 0:
        return cells, np.zeros((0, state.refine, state.refine), bool)
    return cells, test(state.blocks(k * state.plane + cells))


def _and(state: VoxelState, a: Mask2, b: Mask2) -> Mask2:
    """Where both masks are yes."""
    coarse = (a.coarse == 1) & (b.coarse == 1)
    fine = ((a.coarse == 2) & (b.coarse >= 1)) | ((b.coarse == 2) & (a.coarse >= 1))
    cells = np.flatnonzero(fine.reshape(-1))
    if cells.size == 0:
        return Mask2.build(coarse, cells, np.zeros((0, state.refine, state.refine), bool))
    blocks = a.blocks_for(cells, state.refine) & b.blocks_for(cells, state.refine)
    return Mask2.build(coarse, cells, blocks)


class _Stack:
    """The union of the solid in the slabs added so far, kept as slabs are
    added from the top down: what a planar film's column is shadowed by."""

    def __init__(self, state: VoxelState) -> None:
        self.state = state
        self.full = np.zeros((state.ny, state.nx), dtype=bool)
        self.cells = np.zeros(0, dtype=np.int64)
        self.blocks = np.zeros((0, state.refine, state.refine), dtype=bool)

    def add(self, k: int) -> None:
        state = self.state
        grid = state.labels[k]
        self.full |= (grid != VOID) & (grid != MIXED)
        cells, data = state.slab_bricks(k)
        solid = data != VOID
        merged = np.union1d(self.cells, cells)
        blocks = np.zeros((merged.size, state.refine, state.refine), dtype=bool)
        if self.cells.size:
            blocks[np.searchsorted(merged, self.cells)] |= self.blocks
        if cells.size:
            blocks[np.searchsorted(merged, cells)] |= solid
        keep = ~self.full.reshape(-1)[merged]
        self.cells, self.blocks = merged[keep], blocks[keep]

    def inverse(self) -> Mask2:
        """Where nothing above is solid."""
        coarse = ~self.full
        return Mask2.build(coarse, self.cells, ~self.blocks)


def _union(state: VoxelState, kind, full, part, first: int, last: int) -> Mask2:
    """The solid in any slab from ``first`` to ``last``."""
    whole = full[last + 1] > full[first]
    some = (part[last + 1] > part[first]) & ~whole
    cells = np.flatnonzero(some.reshape(-1))
    blocks = np.zeros((cells.size, state.refine, state.refine), dtype=bool)
    for j in range(first, last + 1):
        here = kind[j].reshape(-1)[cells] == 2
        if here.any():
            blocks[here] |= state.blocks(j * state.plane + cells[here]) != VOID
    return Mask2.build(whole, cells, blocks)


def _surface(state: VoxelState, mask: Mask2) -> np.ndarray:
    """The solid's surface cells -- fine cells of ``mask`` with an open
    4-neighbour -- as lines through their centres, (x0, y0, x1, y1),
    merged along rows (and, for cells alone in their row, along columns).

    A cell at the window's edge is not on the surface on that side: the
    wafer goes on past the window.
    """
    B, nx, ny = state.refine, state.nx, state.ny
    coarse = mask.coarse
    xs: list[np.ndarray] = []  # fine column of each surface cell
    ys: list[np.ndarray] = []  # fine row
    # A plain solid cell next to a plain open one: its whole side row.
    offsets = np.arange(B)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        here = coarse[max(0, -dy) : ny - max(0, dy), max(0, -dx) : nx - max(0, dx)]
        there = coarse[max(0, dy) : ny - max(0, -dy) or None, max(0, dx) : nx - max(0, -dx) or None]
        iy, ix = np.nonzero((here == 1) & (there == 0))
        iy, ix = iy + max(0, -dy), ix + max(0, -dx)
        if dx:
            col = ix * B + (B - 1 if dx > 0 else 0)
            xs.append(np.repeat(col, B))
            ys.append((iy[:, None] * B + offsets[None, :]).reshape(-1))
        else:
            row = iy * B + (B - 1 if dy > 0 else 0)
            ys.append(np.repeat(row, B))
            xs.append((ix[:, None] * B + offsets[None, :]).reshape(-1))
    # Refined cells, and plain solid cells next to them: fine neighbours.
    part = mask.cells
    if part.size:
        is_part = np.zeros(ny * nx, dtype=bool)
        is_part[part] = True
        py, px = np.divmod(part, nx)
        # The cells whose fine surface must be looked at: the refined ones
        # and their plain solid neighbours.
        grown = is_part.reshape(ny, nx).copy()
        near = np.zeros_like(grown)
        near[1:, :] |= grown[:-1, :]
        near[:-1, :] |= grown[1:, :]
        near[:, 1:] |= grown[:, :-1]
        near[:, :-1] |= grown[:, 1:]
        look = np.flatnonzero((grown | (near & (coarse == 1))).reshape(-1))
        ly, lx = np.divmod(look, nx)
        mine = mask.blocks_for(look, B)
        open_side = np.zeros_like(mine)
        # within the block
        open_side[:, :, :-1] |= ~mine[:, :, 1:]
        open_side[:, :, 1:] |= ~mine[:, :, :-1]
        open_side[:, :-1, :] |= ~mine[:, 1:, :]
        open_side[:, 1:, :] |= ~mine[:, :-1, :]
        # across each side, where there is a neighbour
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ok = (lx + dx >= 0) & (lx + dx < nx) & (ly + dy >= 0) & (ly + dy < ny)
            idx = np.flatnonzero(ok)
            theirs = mask.blocks_for((ly[idx] + dy) * nx + lx[idx] + dx, B)
            if dx == 1:
                open_side[idx, :, -1] |= ~theirs[:, :, 0]
            elif dx == -1:
                open_side[idx, :, 0] |= ~theirs[:, :, -1]
            elif dy == 1:
                open_side[idx, -1, :] |= ~theirs[:, 0, :]
            else:
                open_side[idx, 0, :] |= ~theirs[:, -1, :]
        c, r, q = np.nonzero(mine & open_side)
        xs.append(lx[c] * B + q)
        ys.append(ly[c] * B + r)
    fx_all = np.concatenate(xs).astype(np.int64) if xs else np.zeros(0, np.int64)
    fy_all = np.concatenate(ys).astype(np.int64) if ys else np.zeros(0, np.int64)
    if fx_all.size == 0:
        return np.zeros((0, 4))
    key = _unique(fy_all * (nx * B + 1) + fx_all)
    fy_all, fx_all = np.divmod(key, nx * B + 1)
    # Runs along rows.
    new = np.ones(key.size, dtype=bool)
    new[1:] = (fy_all[1:] != fy_all[:-1]) | (fx_all[1:] != fx_all[:-1] + 1)
    group = np.cumsum(new) - 1
    length = np.bincount(group)
    starts = np.flatnonzero(new)
    row_y, row_x0 = fy_all[starts], fx_all[starts]
    row_x1 = row_x0 + length - 1
    alone = length == 1
    segments = [np.stack([row_x0[~alone], row_y[~alone], row_x1[~alone], row_y[~alone]], 1)]
    # Cells alone in their row: runs along columns.
    ax, ay = row_x0[alone], row_y[alone]
    order = np.lexsort((ay, ax))
    ax, ay = ax[order], ay[order]
    new = np.ones(ax.size, dtype=bool)
    new[1:] = (ax[1:] != ax[:-1]) | (ay[1:] != ay[:-1] + 1)
    group = np.cumsum(new) - 1
    length = np.bincount(group) if ax.size else np.zeros(0, np.int64)
    starts = np.flatnonzero(new)
    segments.append(np.stack([ax[starts], ay[starts], ax[starts], ay[starts] + length - 1], 1))
    lattice = np.concatenate(segments).astype(np.float64)
    x_min, y_min, _, _ = state.bounds
    return np.stack(
        [
            x_min + (lattice[:, 0] + 0.5) * state.fine_x,
            y_min + (lattice[:, 1] + 0.5) * state.fine_y,
            x_min + (lattice[:, 2] + 0.5) * state.fine_x,
            y_min + (lattice[:, 3] + 0.5) * state.fine_y,
        ],
        axis=1,
    )


def _near(state: VoxelState, solid: Mask2, reach: float) -> Mask2:
    """Fine cells whose centre is within ``reach`` of the centre of a fine
    cell of ``solid``.

    Measured between centres, as the cells are what the grid knows of the
    surface: on a flat wall that is the edge distance plus half a fine
    cell, and on a curve drawn in cells it rounds the staircase back
    towards the curve instead of letting its corners reach out.
    """
    B = state.refine
    cx, cy = state.cell_x, state.cell_y
    if B == 1:  # the cells are the fine cells
        return Mask2.build(within((solid.coarse >= 1)[None], reach, cx, cy)[0], np.zeros(0, np.int64), np.zeros((0, 1, 1), bool))
    half = 0.5 * math.hypot(cx, cy)
    fine = math.hypot(state.fine_x, state.fine_y)
    whole = solid.coarse == 1
    some = solid.coarse >= 1
    # Bounds from the bulk grid: every fine cell of a cell is within reach
    # when the cell's centre is within reach - half - (a fine diagonal) of
    # a whole solid cell's centre; none is when it is further than
    # reach + 2 half from any cell with solid in it.
    inner = reach - half - fine
    sure = (within(whole[None], inner, cx, cy)[0] if inner >= 0.0 else np.zeros_like(whole)) | whole
    maybe = within(some[None], reach + 2.0 * half, cx, cy)[0]
    band = np.flatnonzero((maybe & ~sure).reshape(-1))
    if band.size == 0:
        return Mask2.build(sure, band, np.zeros((0, B, B), bool))
    segments = _surface(state, solid)
    if segments.shape[0] == 0:
        return Mask2.build(sure | some, np.zeros(0, np.int64), np.zeros((0, B, B), bool))
    lines = shapely.linestrings(segments.reshape(-1, 2, 2))
    tree = shapely.STRtree(lines)
    x_min, y_min, _, _ = state.bounds
    by, bx = np.divmod(band, state.nx)
    centres = shapely.points(x_min + (bx + 0.5) * cx, y_min + (by + 0.5) * cy)
    (first, _nearest), distance = tree.query_nearest(centres, return_distance=True, all_matches=False)
    nearest = np.full(band.size, np.inf)
    np.minimum.at(nearest, first, distance)
    # Every segment that can be the nearest to some fine cell of the cell.
    pairs = tree.query(centres, predicate="dwithin", distance=nearest + 2.0 * half + 1e-12)
    cell_of, seg_of = pairs
    order = np.argsort(cell_of, kind="stable")
    cell_of, seg_of = cell_of[order], seg_of[order]
    covered = np.zeros((band.size, B, B), dtype=bool)
    fx, fy = state.fine_x, state.fine_y
    rows = np.arange(B)
    size = max(1, 8_000_000 // B)
    for start in range(0, cell_of.size, size):
        c = cell_of[start : start + size]
        g = segments[seg_of[start : start + size]]
        y = y_min + (by[c][:, None] * B + rows[None, :] + 0.5) * fy  # (m, B)
        x0, y0, x1, y1 = (g[:, i][:, None] for i in range(4))
        vertical = x0 == x1
        ya, yb = np.minimum(y0, y1), np.maximum(y0, y1)
        dy = np.where(vertical, np.maximum(np.maximum(ya - y, y - yb), 0.0), np.abs(y - y0))
        w = np.sqrt(np.maximum(reach * reach - dy * dy, 0.0))
        lo = np.where(vertical, x0, np.minimum(x0, x1)) - w
        hi = np.where(vertical, x0, np.maximum(x0, x1)) + w
        base = bx[c][:, None] * B
        first_col = np.ceil((lo - x_min) / fx - 0.5 - 1e-9).astype(np.int64) - base
        last_col = np.floor((hi - x_min) / fx - 0.5 + 1e-9).astype(np.int64) - base
        first_col = np.clip(first_col, 0, B)
        last_col = np.clip(last_col, -1, B - 1)
        ok = (dy <= reach) & (last_col >= first_col)
        # each segment's span on each fine row: +1 where it starts, -1 past
        # its end, counted per cell of this chunk; covered where the sum is
        fresh = np.concatenate([[True], c[1:] != c[:-1]])
        local = np.cumsum(fresh) - 1
        cells_here = c[fresh]
        m, r = np.nonzero(ok)
        at = (local[m] * B + r) * (B + 1)
        length = cells_here.size * B * (B + 1)
        counts = np.bincount(at + first_col[m, r], minlength=length) - np.bincount(
            at + last_col[m, r] + 1, minlength=length
        )
        spans = np.cumsum(counts.reshape(cells_here.size, B, B + 1), axis=2)[:, :, :B] > 0
        covered[cells_here] |= spans
    # Refined cells of the solid itself: distance 0 there.
    covered |= solid.blocks_for(band, B)
    return Mask2.build(sure, band, covered)


def _walk(
    columns: np.ndarray, z: np.ndarray, table: np.ndarray, budget: float, inside: np.ndarray
) -> np.ndarray:
    """Where a vertical etch stops in each column: ``columns`` is (slabs,
    columns) of labels; ``inf`` for columns not ``inside``."""
    count = columns.shape[1]
    alive = inside.copy()
    remaining = np.full(count, float(budget))
    stop = np.full(count, np.inf)
    stop[inside] = z[-1]
    for k in range(columns.shape[0] - 1, -1, -1):
        if not alive.any():
            break
        grid = columns[k]
        z0, z1 = float(z[k]), float(z[k + 1])
        void = grid == VOID
        stop[alive & void] = z0
        rate = table[grid]
        alive &= ~(~void & (rate <= 0.0))
        etching = alive & ~void
        if not etching.any():
            continue
        cost = np.zeros(count)
        cost[etching] = (z1 - z0) / rate[etching]
        through = etching & (remaining >= cost - 1e-12)
        stop[through] = z0
        remaining[through] -= cost[through]
        partial = etching & ~through
        stop[partial] = z1 - remaining[partial] * rate[partial]
        alive &= ~partial
    return stop


def _chunks(cells: np.ndarray, per_cell: int, budget: int = 24_000_000):
    """Pieces of ``cells`` small enough that ``per_cell`` values each fit ``budget``."""
    size = max(1, budget // max(1, per_cell))
    for start in range(0, cells.size, size):
        yield cells[start : start + size]


def etch_vertical(
    state: VoxelState,
    rates: Mapping[str, float],
    budget: float,
    opening: Mask2 | None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Etch straight down inside ``opening`` for ``budget`` (see the module notes).

    A column is a bulk cell where every slab and the mask are plain there,
    and a fine cell elsewhere.
    """
    table = np.zeros(256, dtype=np.float64)
    for name, rate in rates.items():
        label = state.known_id(name)
        if label is not None:
            table[label] = max(float(rate), 0.0)
    B, plane, n = state.refine, state.plane, state.n
    before = state.copy()
    inside = _as_mask(state, opening) or Mask2.uniform(state.ny, state.nx, B)
    fine_columns = (state.labels == MIXED).any(axis=0) | (inside.coarse == 2)
    plain = (inside.coarse == 1) & ~fine_columns
    stop_plain = _walk(state.labels.reshape(n, -1), state.z, table, budget, plain.reshape(-1))
    cells = np.flatnonzero(fine_columns.reshape(-1))
    stops: list[np.ndarray] = []
    for part in _chunks(cells, n * B * B):
        _check(should_cancel, "a vertical etch")
        keys = (np.arange(n)[:, None] * plane + part[None, :]).reshape(-1)
        columns = state.blocks(keys).reshape(n, -1)
        ins = inside.blocks_for(part, B).reshape(-1)
        stops.append(_walk(columns, state.z, table, budget, ins))
    stop_fine = np.concatenate(stops) if stops else np.zeros(0)
    found = np.concatenate([stop_plain, stop_fine])
    state.split(np.unique(np.round(found[np.isfinite(found)], Z_DECIMALS)))
    for k in range(state.n):
        gone = plain.reshape(-1) & (state.z[k] >= stop_plain - Z_EPS)
        state.labels[k].reshape(-1)[gone] = VOID
    if cells.size:
        keys_out, blocks_out = [], []
        at = 0
        for part in _chunks(cells, n * B * B):
            _check(should_cancel, "a vertical etch")
            size = part.size * B * B
            stop = stop_fine[at : at + size].reshape(part.size, B, B)
            at += size
            for k in range(state.n):
                keys = k * plane + part
                blocks = state.blocks(keys)
                # only what was there to take: most of the columns above
                # the surface are open already
                gone = (state.z[k] >= stop - Z_EPS) & (blocks != VOID)
                changed = gone.any(axis=(1, 2))
                if changed.any():
                    blocks = blocks[changed]
                    blocks[gone[changed]] = VOID
                    keys_out.append(keys[changed])
                    blocks_out.append(blocks)
        if keys_out:
            state.store(np.concatenate(keys_out), np.concatenate(blocks_out))
    # Boundaries: the columns at any point, walked as they were before.
    edges = _boundary_cells(state, inside)
    if edges.size:
        slabs = np.arange(before.n)[:, None]
        x_min, y_min, _, _ = state.bounds
        wide = state.nx * B * 4000 + 1
        # where the etch stops at a point is the same for every slab, and
        # the slabs' boundaries mostly lie over one another: walked once
        known = [np.zeros(0, np.int64), np.zeros(0)]

        def stop_at(x, y):
            # the fitting's points sit at quarter steps a thousandth in
            # from the sides: 4000 to a fine cell tells them apart exactly
            qx = (x - x_min) / state.fine_x * 4000
            qy = (y - y_min) / state.fine_y * 4000
            if np.abs(qx - np.rint(qx)).max(initial=0.0) > 0.01 or np.abs(qy - np.rint(qy)).max(initial=0.0) > 0.01:
                return _walk(before.sample(slabs, x[None, :], y[None, :]), before.z, table, budget, inside.at(state, x, y))
            ids = np.rint(qy).astype(np.int64) * wide + np.rint(qx).astype(np.int64)
            ids, first, back = _unique(ids, return_index=True, return_inverse=True)
            keys, values = known
            at = np.minimum(np.searchsorted(keys, ids), max(keys.size - 1, 0))
            hit = (keys[at] == ids) if keys.size else np.zeros(ids.size, bool)
            stop = np.empty(ids.size)
            stop[hit] = values[at[hit]]
            miss = np.flatnonzero(~hit)
            if miss.size:
                px, py = x[first[miss]], y[first[miss]]
                columns = before.sample(slabs, px[None, :], py[None, :])
                stop[miss] = _walk(columns, before.z, table, budget, inside.at(state, px, py))
                merged = np.concatenate([keys, ids[miss]])
                order = np.argsort(merged, kind="stable")
                known[0] = merged[order]
                known[1] = np.concatenate([values, stop[miss]])[order]
            return stop[back]

        def left_at(k):
            bottom = state.z[k]
            source = int(np.searchsorted(before.z, 0.5 * (state.z[k] + state.z[k + 1]), side="right")) - 1

            def decide(x, y):
                out = before.sample(source, x, y)
                out[bottom >= stop_at(x, y) - Z_EPS] = VOID
                return out

            return decide

        for k in range(state.n):
            _check(should_cancel, "a vertical etch")
            j = int(np.searchsorted(before.z, 0.5 * (state.z[k] + state.z[k + 1]), side="right")) - 1
            here = np.intersect1d(edges, _changed_cells(state, k, before, j))
            _refit(state, k, here, left_at(k))
    state.consolidate()


def _boundary_cells(state: VoxelState, mask: Mask2 | None) -> np.ndarray:
    """Bulk cells where a boundary can be fitted: every refined cell of
    any slab, the cells a mask's outline crosses, and -- one fine cell a
    cell -- every cell with a neighbour of another label."""
    ny, nx = state.ny, state.nx
    near = (state.labels == MIXED).any(axis=0)
    if mask is not None:
        near |= mask.coarse == 2
    if state.refine == 1:
        for k in range(state.n):
            grid = state.labels[k]
            differs = np.zeros((ny, nx), dtype=bool)
            differs[1:, :] |= grid[1:, :] != grid[:-1, :]
            differs[:-1, :] |= grid[1:, :] != grid[:-1, :]
            differs[:, 1:] |= grid[:, 1:] != grid[:, :-1]
            differs[:, :-1] |= grid[:, 1:] != grid[:, :-1]
            near |= differs
        if mask is not None:
            m = mask.coarse
            differs = np.zeros((ny, nx), dtype=bool)
            differs[1:, :] |= m[1:, :] != m[:-1, :]
            differs[:-1, :] |= m[1:, :] != m[:-1, :]
            differs[:, 1:] |= m[:, 1:] != m[:, :-1]
            differs[:, :-1] |= m[:, 1:] != m[:, :-1]
            near |= differs
    return np.flatnonzero(near.reshape(-1))


def etch_isotropic(
    state: VoxelState,
    rates: Mapping[str, float],
    budget: float,
    opening: np.ndarray | None,
    *,
    product: str | None = None,
    dz: float | None = None,
    fine_z: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Etch by the etchant's arrival time (see the module notes).

    ``rates`` are per material and ``budget`` is the time they run for, in
    the same units: a depth of the reference material with relative rates,
    or minutes with rates per minute. With ``product`` the cells taken
    become that material (an oxidation) instead of void.

    In z: near the slab boundaries where the front can bend, slabs are
    cut every ``dz`` (never finer than a bulk cell) for the march. The
    front is then placed exactly: a front that lies level becomes a slab
    boundary at its exact height, and where it curves the slab is cut
    into thin slabs no thicker than ``fine_z`` (when finer than ``dz``),
    each placed at its own height. Slabs the front crosses upright stay
    whole.
    """
    table = np.zeros(256)
    for name, rate in rates.items():
        label = state.known_id(name)
        if label is not None and float(rate) > 0.0:
            table[label] = float(rate)
    etchable = np.flatnonzero(table > 0.0)
    if etchable.size == 0 or not budget > 0.0:
        return
    holding = np.isin(state.labels, etchable).any(axis=(1, 2))
    if state.brick_keys.size:
        # a thin film may live wholly in refined cells
        in_bricks = np.isin(state.pool, etchable).any(axis=(1, 2))[state.brick_ref]
        holding[_unique(state.brick_keys[in_bricks] // state.plane)] = True
    if not holding.any():
        return
    product_label = state.material_id(product) if product is not None else None
    fill = VOID if product_label is None else product_label
    # Where the etchant is -- which open space the ambient reaches, less
    # what the resist fills -- is settled before the slabs are cut: cutting
    # changes no connection, and the cut stack is many times taller. The
    # mask is in it; nothing after this reads the mask.
    mask = _as_mask(state, opening)
    live = _wet_field(state, mask, check=lambda: _check(should_cancel, "an isotropic etch"))
    _check(should_cancel, "an isotropic etch")
    step = max(float(dz or 0.0), state.cell)
    reach = float(budget) * float(table.max()) + step
    # One grid of planes for every bend, so the cuts near two boundaries
    # line up instead of leaving slivers between them.
    planes = set()
    for zb in _curving(state, table, live, None):
        lo, hi = math.floor((zb - reach) / step), math.ceil((zb + reach) / step)
        planes.update(j * step for j in range(lo, hi + 1))
    lows, highs = state.z[:-1][holding], state.z[1:][holding]
    planes = [p for p in sorted(planes) if ((lows < p) & (p < highs)).any()]
    _check(should_cancel, "an isotropic etch")
    source = state.split(planes)
    live._reslab(np.concatenate([source, [live.n - 1]]))
    from . import voxel_wet

    front = voxel_wet.arrival(state, table, float(budget), None, live, should_cancel, step=step)
    del live
    L = state.n
    # A level front is a slab boundary at its exact height.
    source = state.split([float(h) for h in front.flat_heights]) if front.flat_heights.size else np.arange(L)
    # Where the front curves inside a slab -- not the same at the slab's top
    # as at its bottom -- the slab is cut into layers no thicker than
    # fine_z; slabs thicker than the march's step the front crosses upright.
    if fine_z and fine_z < step:
        z0, z1 = state.z[:-1].copy(), state.z[1:].copy()
        layers = np.maximum(1, np.ceil((z1 - z0) / float(fine_z) - 1e-9)).astype(np.int64)
        layers[(z1 - z0) > step * (1 + 1e-6)] = 1
        maybe = np.flatnonzero(layers > 1)
        if maybe.size:
            half = 0.5 * (z1 - z0) / layers
            curved = np.zeros(state.n, dtype=bool)
            tops = dict(_placed(front, source[maybe], (z1 - half)[maybe], L))
            for slab_index, (whole_b, keys_b, fine_b) in _placed(front, source[maybe], (z0 + half)[maybe], L):
                _check(should_cancel, "an isotropic etch")
                whole_a, keys_a, fine_a = tops[slab_index]
                k = maybe[slab_index]
                curved[k] = (whole_a != whole_b).any() or _differing((keys_a, fine_a), (keys_b, fine_b), state.refine).size > 0
            cuts = [z0[k] + (z1[k] - z0[k]) * j / layers[k] for k in np.flatnonzero(curved) for j in range(1, int(layers[k]))]
            if cuts:
                source = source[state.split(cuts)]
    # Each slab placed at its middle, from the slab the march had.
    middles = 0.5 * (state.z[:-1] + state.z[1:])
    before = state.copy()
    changed = np.zeros(state.n, dtype=bool)
    for target, (whole, keys, fine) in _placed(front, source, middles, L):
        _check(should_cancel, "an isotropic etch")
        _fill_whole(state, target, whole, fill)
        changed[target] |= bool(whole.any()) or bool(keys.size)
        if keys.size:
            new_keys = target * state.plane + keys
            blocks = state.blocks(new_keys)
            blocks[fine] = fill
            state.store(new_keys, blocks)
    # Boundaries: what the etchant left at any point of each slab.
    if front.at is not None:
        for target in np.flatnonzero(changed):
            _check(should_cancel, "an isotropic etch")

            def decide(x, y, target=int(target)):
                old = before.sample(target, x, y)
                out = old.copy()
                said = front.at(middles[target], int(source[target]), x, y)
                # where placing settled the whole fine cell, what it wrote there
                settled = said < 0
                if settled.any():
                    written = state.sample_fine(
                        target,
                        np.floor((x[settled] - state.bounds[0]) / state.fine_x).astype(np.int64),
                        np.floor((y[settled] - state.bounds[1]) / state.fine_y).astype(np.int64),
                    )
                    said[settled] = (written == fill).astype(np.int8)
                gone = (table[old] > 0.0) & (said > 0)
                out[gone] = fill
                return out

            _refit(state, int(target), _changed_cells(state, int(target), before, int(target)), decide)
    state.consolidate()


def _changed_cells(state: VoxelState, k: int, before: VoxelState, j: int) -> np.ndarray:
    """Cells of slab ``k`` a step changed from slab ``j`` of ``before``,
    with the ring of cells round them, where a boundary can be fitted: the
    only places a boundary can have moved."""
    a, b = state.labels[k], before.labels[j]
    diff = a != b
    both = np.flatnonzero(((a == MIXED) & (b == MIXED)).reshape(-1))
    if both.size:
        for part in _chunks(both, state.refine * state.refine * 3):
            ka, kb = k * state.plane + part, j * before.plane + part
            differs = (state.blocks(ka) != before.blocks(kb)).any(axis=(1, 2))
            differs |= (state.cuts(ka) != before.cuts(kb)).any(axis=(1, 2))
            diff.reshape(-1)[part[differs]] = True
    if not diff.any():
        return np.zeros(0, dtype=np.int64)
    ring = diff.copy()
    ring[1:, :] |= diff[:-1, :]
    ring[:-1, :] |= diff[1:, :]
    ring[:, 1:] |= ring[:, :-1].copy()
    ring[:, :-1] |= ring[:, 1:].copy()
    wanted = np.zeros(a.size, dtype=bool)
    wanted[_edge_cells(state, k)] = True
    return np.flatnonzero(ring.reshape(-1) & wanted)


def _edge_cells(state: VoxelState, k: int) -> np.ndarray:
    """Cells of slab ``k`` a boundary can be fitted in: its refined cells,
    and -- one fine cell a cell -- every cell beside another label."""
    grid = state.labels[k]
    near = grid == MIXED
    if state.refine == 1:
        differs = np.zeros(grid.shape, dtype=bool)
        step = grid[1:, :] != grid[:-1, :]
        differs[1:, :] |= step
        differs[:-1, :] |= step
        step = grid[:, 1:] != grid[:, :-1]
        differs[:, 1:] |= step
        differs[:, :-1] |= step
        near |= differs
    return np.flatnonzero(near.reshape(-1))


def _placed(front, source, heights, L):
    """The front placed for many slabs: slab i is placed at ``heights[i]``
    with the march's slab ``source[i]``. Yields (i, (whole cells (ny, nx),
    cells taken in part, their fine cells)), a few slabs of one march slab
    at a time."""
    order = np.argsort(source, kind="stable")
    first = np.searchsorted(source[order], np.arange(L), side="left")
    count = np.bincount(source, minlength=L)
    for r in range(int(count.max()) if count.size else 0):
        subset = count > r
        pick = order[np.minimum(first + r, source.size - 1)]
        at = np.where(subset, heights[pick], 0.0)
        whole, keys, fine = front.place(at, subset)
        k, cell = np.divmod(keys, whole.shape[1] * whole.shape[2])
        order_k = np.argsort(k, kind="stable")
        k, cell, fine = k[order_k], cell[order_k], fine[order_k]
        for m in np.flatnonzero(subset):
            lo, hi = np.searchsorted(k, m), np.searchsorted(k, m, side="right")
            yield int(pick[m]), (whole[m], cell[lo:hi], fine[lo:hi])


def _differing(a, b, refine):
    """Keys whose fine cells differ between two (keys, fine) placements."""
    keys = np.union1d(a[0], b[0])
    blocks = []
    for part_keys, part_fine in (a, b):
        full = np.zeros((keys.size, refine, refine), dtype=bool)
        if part_keys.size:
            full[np.searchsorted(keys, part_keys)] = part_fine
        blocks.append(full)
    return keys[(blocks[0] != blocks[1]).any(axis=(1, 2))]


def _curving(state: VoxelState, table: np.ndarray, live: "VoxelState", opening: "Mask2 | None") -> list[float]:
    """Slab boundaries near which the front can curve in z.

    Within a slab the only way to curve is through its top or bottom. A
    boundary is quiet when paths cannot cross it at all (every etchable
    cell on either side faces a wall) or cross it into the same thing
    (etchable faces the same rate, and no etchable faces a wall). It is not
    when an etchable cell faces open space the etchant can be in, or a
    material of another rate, or when some etchable cells face etchable and
    others a wall -- the front goes round the wall through the other slab.
    A front off a wall that spans the slab is itself upright, so only the
    heights near these boundaries need cutting.
    """
    B = state.refine
    n = state.n
    columns = opening if opening is not None else Mask2.uniform(state.ny, state.nx, B)

    def code(lab, liv, col):
        # -1 wall, 0 open space the etchant fills (or a cavity it may), else the rate
        out = np.where(table[lab] > 0.0, table[lab], -1.0)
        void = lab == VOID
        out[void & ((liv == 0) | ((liv == 1) & (col > 0)))] = 0.0
        return out

    def coarse(k):
        lab = state.labels[k] if k < n else np.zeros((state.ny, state.nx), np.uint8)
        out = code(lab, live.labels[k], columns.coarse)
        out[(lab == MIXED) | (live.labels[k] == MIXED)] = np.nan
        return out

    def fine(k, cells):
        keys = k * state.plane + cells
        lab = state.blocks(keys) if k < n else np.zeros((cells.size, B, B), np.uint8)
        return code(lab, live.blocks(keys), columns.blocks_for(cells, B))

    events = []
    below = coarse(0)
    for b in range(1, n + 1):
        above = coarse(b)
        mixed = np.flatnonzero((np.isnan(below) | np.isnan(above)).reshape(-1))
        p = [below.reshape(-1), fine(b - 1, mixed).reshape(-1)]
        q = [above.reshape(-1), fine(b, mixed).reshape(-1)]
        p, q = np.concatenate(p), np.concatenate(q)
        keep = ~(np.isnan(p) | np.isnan(q))
        p, q = p[keep], q[keep]
        etch_p, etch_q = p > 0.0, q > 0.0
        crossing = etch_p & etch_q
        if (
            (etch_p & (q == 0.0)).any()
            or ((p == 0.0) & etch_q).any()
            or (crossing & (p != q)).any()
            or (crossing.any() and ((etch_p & (q < 0.0)) | ((p < 0.0) & etch_q)).any())
        ):
            events.append(float(state.z[b]))
        below = above
    return events


def _live(state: VoxelState, check: Callable[[], None] | None = None) -> "VoxelState":
    """Open space the ambient reaches, as a two-level field over the stack
    plus one slab on top for the ambient itself (1 where reached); the
    rest of the open space is a sealed cavity."""
    plain = np.concatenate([state.labels == VOID, np.ones((1, state.ny, state.nx), dtype=bool)])
    fine = (state.pool == VOID)[state.brick_ref]
    coarse_id, fine_id = components2(plain, state.brick_keys, fine, state.nx, state.ny, check=check)
    ambient = _unique(coarse_id[-1])
    ambient = ambient[ambient > 0]
    labels = np.isin(coarse_id, ambient).astype(np.uint8)
    reached = np.isin(fine_id, ambient) & fine
    # Refined cells keep their bricks: 1 where reached, 0 elsewhere.
    labels.reshape(-1)[state.brick_keys] = MIXED
    field = VoxelState(
        state.bounds, state.nx, state.ny, np.concatenate([state.z, [state.top + state.cell]]),
        labels, ["live"], 0.0, state.refine,
    )
    field.store(state.brick_keys, reached.astype(np.uint8))
    return field


#: In the field :func:`_wet_field` returns: open space under resist.
RESIST = 2


def _field_mask(field: VoxelState, k: int, value: int = 1) -> "Mask2":
    """Where slab ``k`` of a two-level field holds ``value``, as a mask."""
    lab = field.labels[k]
    cells = np.flatnonzero((lab == MIXED).reshape(-1))
    return Mask2.build(lab == value, cells, field.blocks(k * field.plane + cells) == value)


def _wet_field(state: VoxelState, opening: "Mask2 | None", check: Callable[[], None] | None = None) -> "VoxelState":
    """Where the etchant is when a wet etch starts, as :func:`_live` lays it
    out: 1 open space it fills, :data:`RESIST` open space under resist, 0
    the rest -- sealed cavities, and pockets the etchant can get to only
    through resist, which fill once the etch breaks into them.

    Resist is spun on from above: outside the mask's opening it fills the
    space over the wafer and every hole that is open straight up to it.
    Space under solid is not reached by it, however it is joined to the
    rest -- a channel running under the covered part from a hole in the
    opening fills with etchant from that hole.
    """
    live = _live(state, check)
    if opening is None:
        return live
    check = check or (lambda: None)
    B, n, ny, nx, plane = state.refine, state.n, state.ny, state.nx, state.plane
    col = opening.coarse
    lab = np.concatenate([state.labels, np.zeros((1, ny, nx), np.uint8)])  # the ambient on top
    # columns looked at fine cell by fine cell: refined somewhere, or the
    # mask's edge crosses them
    fine_col = col == 2
    if state.brick_keys.size:
        fine_col.reshape(-1)[_unique(state.brick_keys % plane)] = True
    # open straight up to the ambient, from the top down
    exposed = np.zeros(lab.shape, dtype=bool)
    e = np.ones((ny, nx), dtype=bool)
    for k in range(n, -1, -1):
        e = e & (lab[k] == VOID)
        exposed[k] = e
    resist_c = exposed & ((col == 0) & ~fine_col)[None]
    resist_keys: list[np.ndarray] = []
    resist_blocks: list[np.ndarray] = []
    cells = np.flatnonzero((fine_col & (col != 1)).reshape(-1))
    if cells.size:
        e = np.ones((cells.size, B, B), dtype=bool)
        inside = opening.blocks_for(cells, B)
        for k in range(n, -1, -1):
            check()
            if k < n:
                e &= state.blocks(k * plane + cells) == VOID
            r = e & ~inside
            has = r.any(axis=(1, 2))
            resist_keys.append(k * plane + cells[has])
            resist_blocks.append(r[has])
            going = e.any(axis=(1, 2))
            if not going.any():
                break
            cells, e, inside = cells[going], e[going], inside[going]
    r_keys = np.concatenate(resist_keys) if resist_keys else np.zeros(0, np.int64)
    r_blocks = np.concatenate(resist_blocks) if resist_blocks else np.zeros((0, B, B), bool)
    order = np.argsort(r_keys, kind="stable")
    r_keys, r_blocks = r_keys[order], r_blocks[order]
    if not resist_c.any() and r_keys.size == 0:
        return live
    # what the ambient reaches, less the resist: the pieces of it that touch
    # the opening hold etchant
    keys = np.union1d(live.brick_keys, r_keys)
    fine_live = live.blocks(keys) == 1
    fine_resist = np.zeros(fine_live.shape, dtype=bool)
    if r_keys.size:
        fine_resist[np.searchsorted(keys, r_keys)] = r_blocks
    fine_open = fine_live & ~fine_resist
    plain_open = (live.labels == 1) & ~resist_c
    plain_open.reshape(-1)[keys] = False
    check()
    coarse_id, fine_id = components2(plain_open, keys, fine_open, nx, ny, check=check)
    k_of, cell_of = np.divmod(keys, plane)
    seeds = [coarse_id[plain_open & (col >= 1)[None]]]
    if keys.size:
        seeds.append(fine_id[fine_open & opening.blocks_for(cell_of, B)])
    wet = _unique(np.concatenate(seeds))
    wet = wet[wet > 0]
    labels = np.where(np.isin(coarse_id, wet), 1, 0).astype(np.uint8)
    labels[resist_c] = RESIST
    labels.reshape(-1)[keys] = MIXED
    blocks = np.where(np.isin(fine_id, wet), 1, 0).astype(np.uint8)
    blocks[fine_resist] = RESIST
    field = VoxelState(state.bounds, nx, ny, live.z.copy(), labels, ["wet"], 0.0, B)
    field.store(keys, blocks)
    return field


def cmp(state: VoxelState, height: float) -> None:
    """Remove everything above ``height`` (from the floor)."""
    state.split([height])
    keep = int(np.searchsorted(state.z, _z(height) - Z_EPS, side="left"))
    keep = max(1, min(keep, state.n))
    state.z = state.z[: keep + 1]
    state.labels = np.ascontiguousarray(state.labels[:keep])
    wanted = state.brick_keys < keep * state.plane
    state.brick_keys = state.brick_keys[wanted]
    state.brick_ref = state.brick_ref[wanted]
    state.consolidate()


def flip(state: VoxelState, axis: str) -> None:
    """Turn the stack over about the x or the y axis (see ``slab._flip``)."""
    top = state.top
    if not state.labels.any():
        raise VoxelError("there is nothing to flip: this state has no material")
    labels = state.labels[::-1]
    labels = labels[:, :, ::-1] if axis == "y" else labels[:, ::-1, :]
    if state.brick_keys.size:
        k, rest = np.divmod(state.brick_keys, state.plane)
        iy, ix = np.divmod(rest, state.nx)
        k = state.n - 1 - k
        # a turn over is a mirror: a cut's line is mirrored with its cell
        code = voxel_cut.code_of(state.pool_cut)
        if axis == "y":
            ix = state.nx - 1 - ix
            state.pool = np.ascontiguousarray(state.pool[:, :, ::-1])
            code = voxel_cut.MIRROR_X[code][:, :, ::-1]
            other = voxel_cut.other_of(state.pool_cut)[:, :, ::-1]
        else:
            iy = state.ny - 1 - iy
            state.pool = np.ascontiguousarray(state.pool[:, ::-1, :])
            code = voxel_cut.MIRROR_Y[code][:, ::-1, :]
            other = voxel_cut.other_of(state.pool_cut)[:, ::-1, :]
        state.pool_cut = np.ascontiguousarray(voxel_cut.pack(code, other))
        state._pool_hash = _pair_hash(state.pool, state.pool_cut)
        keys = k * state.plane + iy * state.nx + ix
        order = np.argsort(keys, kind="stable")
        state.brick_keys = keys[order]
        state.brick_ref = state.brick_ref[order]
    state.labels = np.ascontiguousarray(labels)
    state.z = np.array([_z(top - value) for value in state.z[::-1]], dtype=np.float64)
    state.consolidate()


# -- masks -----------------------------------------------------------------


def rasterize(state: VoxelState, geometry) -> Mask2:
    """Where ``geometry`` is, at both levels: a bulk cell by its centre,
    and each fine cell by its own centre in the bulk cells the outline
    crosses."""
    xs, ys = state.centres()
    grid_x, grid_y = np.meshgrid(xs, ys)
    prepared = shapely.make_valid(geometry)
    shapely.prepare(prepared)
    coarse = shapely.intersects_xy(prepared, grid_x.ravel(), grid_y.ravel()).reshape(state.ny, state.nx)
    B = state.refine
    if B == 1:
        out = Mask2.build(coarse, np.zeros(0, np.int64), np.zeros((0, 1, 1), bool))
        out.geometry = prepared
        return out
    # The bulk cells the outline passes through: points along it a quarter
    # of a cell apart, with their neighbours for the corners a line clips.
    outline = shapely.segmentize(shapely.boundary(prepared), 0.25 * state.cell)
    points = shapely.get_coordinates(outline)
    x_min, y_min, _, _ = state.bounds
    ix = np.clip(((points[:, 0] - x_min) / state.cell_x).astype(np.int64), 0, state.nx - 1)
    iy = np.clip(((points[:, 1] - y_min) / state.cell_y).astype(np.int64), 0, state.ny - 1)
    touched = np.zeros((state.ny, state.nx), dtype=bool)
    touched[iy, ix] = True
    grown = touched.copy()
    grown[1:, :] |= touched[:-1, :]
    grown[:-1, :] |= touched[1:, :]
    grown[:, 1:] |= touched[:, :-1]
    grown[:, :-1] |= touched[:, 1:]
    grown[1:, 1:] |= touched[:-1, :-1]
    grown[:-1, :-1] |= touched[1:, 1:]
    grown[1:, :-1] |= touched[:-1, 1:]
    grown[:-1, 1:] |= touched[1:, :-1]
    cells = np.flatnonzero(grown.reshape(-1))
    blocks = np.zeros((cells.size, B, B), dtype=bool)
    offsets = (np.arange(B) + 0.5) / B
    for part_start in range(0, cells.size, max(1, 4_000_000 // (B * B))):
        part = cells[part_start : part_start + max(1, 4_000_000 // (B * B))]
        cy, cx = np.divmod(part, state.nx)
        fx = x_min + (cx[:, None, None] + offsets[None, None, :]) * state.cell_x
        fy = y_min + (cy[:, None, None] + offsets[None, :, None]) * state.cell_y
        fx, fy = np.broadcast_arrays(fx, fy)
        inside = shapely.intersects_xy(prepared, fx.ravel(), fy.ravel())
        blocks[part_start : part_start + part.size] = inside.reshape(part.size, B, B)
    out = Mask2.build(coarse, cells, blocks)
    out.geometry = prepared
    return out


def _row_order(rows):
    """Lexicographic order of rows of non-negative integers (first column
    most significant): as one number per row when that fits."""
    rows = np.asarray(rows, np.int64)
    key = np.zeros(rows.shape[0], np.int64)
    room = 1
    for c in range(rows.shape[1]):
        base = int(rows[:, c].max()) + 1 if rows.shape[0] else 1
        room *= base
        if room >= 2**62:
            return np.lexsort(rows.T[::-1])
        key = key * base + rows[:, c]
    return np.argsort(key, kind="stable")


def _unique_rows(rows):
    """The distinct rows, in order, and which of them each row is."""
    order = _row_order(rows)
    ordered = rows[order]
    fresh = np.ones(order.size, dtype=bool)
    fresh[1:] = (ordered[1:] != ordered[:-1]).any(axis=1)
    rank = np.cumsum(fresh) - 1
    which = np.empty(order.size, np.int64)
    which[order] = rank
    return ordered[fresh], which


# -- pictures --------------------------------------------------------------


def _palette(state: VoxelState, colors: Mapping[str, str], rgb) -> np.ndarray:
    table = np.zeros((256, 3), dtype=np.uint8)
    table[:] = BACKGROUND_RGB
    for index, name in enumerate(state.materials, start=1):
        table[index] = rgb(colors.get(name, "#7c83a0"))
    return table


def _png(rgb: np.ndarray) -> str:
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _scale(count: int) -> int:
    """Pixels per cell for a picture ``count`` cells across."""
    return max(1, round(BASE_PIXELS / max(count, 1)))


#: Pictures and the 3D view draw at most about this many fine cells across
#: the window: bricks are shown at a coarser power of two past that.
SHOWN_CELLS = 2048


def shown(state: VoxelState, limit: int = SHOWN_CELLS) -> VoxelState:
    """``state`` with bricks of at most ``limit`` fine cells across the
    window, each shown cell the material most of its fine cells hold (open
    space losing a tie). The same state when it is fine enough already."""
    refine = state.refine
    while refine > 1 and max(state.nx, state.ny) * refine > limit:
        refine //= 2
    if refine == state.refine:
        return state
    with state.mesh_lock:
        cached = state.shown.get(refine)
        if cached is not None:
            return cached
    step = state.refine // refine
    out = VoxelState(
        state.bounds, state.nx, state.ny, state.z.copy(), state.labels.copy(),
        state.materials, state.z_offset, refine,
    )
    out.labels.reshape(-1)[state.brick_keys] = VOID  # rewritten by store
    used, back = _unique(state.brick_ref, return_inverse=True)
    present = _labels_in(state.pool[used]) if used.size else np.zeros(0, np.uint8)
    # solid first, so a tie goes to the solid
    present = np.concatenate([present[present != VOID], present[present == VOID]])
    reduced = np.zeros((used.size, refine, refine), dtype=np.uint8)
    chunk = max(1, 16_000_000 // max(1, state.refine**2))
    for start in range(0, used.size, chunk):
        blocks = state.pool[used[start : start + chunk]].reshape(-1, refine, step, refine, step)
        best = np.full(blocks.shape[:1] + (refine, refine), -1, dtype=np.int32)
        label = np.zeros(best.shape, dtype=np.uint8)
        for value in present:
            count = (blocks == value).sum(axis=(2, 4), dtype=np.int32)
            more = count > best
            best[more] = count[more]
            label[more] = value
        reduced[start : start + chunk] = label
    out.store(state.brick_keys, reduced[back.reshape(-1)])
    with state.mesh_lock:
        state.shown[refine] = out
    return out


def _slab_fine(state: VoxelState, k: int) -> np.ndarray:
    """Slab ``k`` as dense fine labels (ny * refine, nx * refine)."""
    B = state.refine
    out = np.repeat(np.repeat(state.labels[k], B, axis=0), B, axis=1)
    cells, data = state.slab_bricks(k)
    if cells.size:
        iy, ix = np.divmod(cells, state.nx)
        out.reshape(state.ny, B, state.nx, B)[iy, :, ix, :] = data
    return out


def _frame(extent: tuple[float, float, float, float]) -> tuple[int, int]:
    """A picture's size for outlines: the true shape, BASE_PIXELS on its longer side."""
    h0, h1, v0, v1 = extent
    span = max(h1 - h0, v1 - v0, 1e-12)
    return max(2, round(BASE_PIXELS * (h1 - h0) / span)), max(2, round(BASE_PIXELS * (v1 - v0) / span))


def _darker_hex(rgb):
    def darker(color: str) -> str:
        r, g, b = rgb(color)
        return "#%02x%02x%02x" % (int(r * STEP_LINE_SHADE), int(g * STEP_LINE_SHADE), int(b * STEP_LINE_SHADE))

    return darker


def top_view(
    state: VoxelState,
    colors: Mapping[str, str],
    rgb,
    *,
    hidden: Sequence[str] = (),
    steps: bool = True,
    vector: bool = False,
) -> dict[str, Any]:
    if vector:
        x_min, y_min, x_max, y_max = state.bounds
        extent = (x_min, x_max, y_min, y_max)
        width, height = _frame(extent)
        hidden_ids = {state.known_id(name) for name in hidden} - {None}
        return {
            "image": "",
            "vector": _top_vector(state, colors, extent, width, height, hidden_ids, steps, _darker_hex(rgb)),
            "exact": False,
            "width": width,
            "height": height,
            "extent": {"horizontalMin": x_min, "horizontalMax": x_max, "verticalMin": y_min, "verticalMax": y_max},
        }
    state = shown(state)
    hidden_ids = {state.known_id(name) for name in hidden} - {None}
    visible = np.ones(256, dtype=bool)
    visible[VOID] = False
    visible[MIXED] = False
    for label in hidden_ids:
        visible[label] = False
    rows, cols = state.ny * state.refine, state.nx * state.refine
    colour = np.zeros((rows, cols), dtype=np.uint8)
    height = np.full((rows, cols), -np.inf)
    found = np.zeros((rows, cols), dtype=bool)
    for k in range(state.n - 1, -1, -1):
        grid = _slab_fine(state, k)
        seen = visible[grid] & ~found
        colour[seen] = grid[seen]
        height[seen] = state.z[k + 1]
        found |= seen
        if found.all():
            break
    table = _palette(state, colors, rgb)
    image = table[colour]
    if steps:
        # Where one material meets itself at another height: colour alone
        # cannot show that the surface is not flat there. Drawn on the
        # higher side.
        edge = np.zeros(colour.shape, dtype=bool)
        same = (colour[:, 1:] == colour[:, :-1]) & (colour[:, 1:] > 0)
        step = same & (np.abs(height[:, 1:] - height[:, :-1]) > Z_EPS)
        higher = height[:, 1:] > height[:, :-1]
        edge[:, 1:] |= step & higher
        edge[:, :-1] |= step & ~higher
        same = (colour[1:, :] == colour[:-1, :]) & (colour[1:, :] > 0)
        step = same & (np.abs(height[1:, :] - height[:-1, :]) > Z_EPS)
        higher = height[1:, :] > height[:-1, :]
        edge[1:, :] |= step & higher
        edge[:-1, :] |= step & ~higher
        image[edge] = (image[edge].astype(np.float64) * STEP_LINE_SHADE).astype(np.uint8)
    # Row 0 of the picture is the far (y max) edge.
    image = image[::-1]
    factor = _scale(max(rows, cols))
    image = np.repeat(np.repeat(image, factor, axis=0), factor, axis=1)
    x_min, y_min, x_max, y_max = state.bounds
    return {
        "image": _png(np.ascontiguousarray(image)),
        "exact": False,
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "extent": {
            "horizontalMin": x_min,
            "horizontalMax": x_max,
            "verticalMin": y_min,
            "verticalMax": y_max,
        },
    }


def _section_image(
    state: VoxelState,
    columns: np.ndarray,
    length: float,
    colors: Mapping[str, str],
    rgb,
    z_max: float,
) -> tuple[np.ndarray, float]:
    """Paint slabs of a line of cells (``columns[k, i]``) into a picture."""
    width = max(2, min(2600, columns.shape[1] * _scale(columns.shape[1])))
    pixels_per_um = width / max(length, 1e-12)
    bottom, top = state.z_offset, z_max
    height = max(2, min(2600, round((top - bottom) * pixels_per_um)))
    scale_y = height / max(top - bottom, 1e-12)
    table = _palette(state, colors, rgb)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[...] = BACKGROUND_RGB
    stretch = np.minimum(
        (np.arange(width) * columns.shape[1] // width), columns.shape[1] - 1
    )
    for k in range(state.n):
        z0 = state.z[k] + state.z_offset
        z1 = state.z[k + 1] + state.z_offset
        row0 = int(round((top - z1) * scale_y))
        row1 = int(round((top - z0) * scale_y))
        if row1 <= row0:
            row1 = row0 + 1  # a film thinner than a pixel still shows
        row0, row1 = max(row0, 0), min(row1, height)
        if row1 <= row0:
            continue
        line = columns[k][stretch]
        painted = line != VOID
        if painted.any():
            canvas[row0:row1, painted] = table[line[painted]]
    return canvas, pixels_per_um


def section(
    state: VoxelState,
    colors: Mapping[str, str],
    rgb,
    *,
    z_max: float,
    axis: str = "y",
    position: float | None = None,
    line: tuple[tuple[float, float], tuple[float, float]] | None = None,
    interpolation: int = 1,
    vector: bool = False,
) -> dict[str, Any]:
    x_min, y_min, x_max, y_max = state.bounds
    top = max(z_max, state.top + state.z_offset)
    slabs = np.arange(state.n)[:, None]
    if line is not None:
        (ax, ay), (bx, by) = line
        length = float(math.hypot(bx - ax, by - ay))
        if length <= 0.0:
            raise VoxelError("a section line needs two distinct points")
        if vector:
            # a quarter of a fine cell apart: the runs' ends are that close
            samples = max(2, min(200_000, int(math.ceil(length / (0.25 * state.fine)))))
            extent = (0.0, length, state.z_offset, top)
            width, height = _frame(extent)
            return {
                "image": "",
                "vector": _section_vector(state, colors, extent, width, height, line=line, samples=samples),
                "axis": "line",
                "position": 0.0,
                "index": 0,
                "interpolation": interpolation,
                "sampledSpacingUm": length / samples,
                "exact": False,
                "width": width,
                "height": height,
                "horizontalAxis": "s",
                "extent": {"horizontalMin": 0.0, "horizontalMax": length, "verticalMin": state.z_offset, "verticalMax": top},
                "positions": [],
                "line": {"start": [float(ax), float(ay)], "end": [float(bx), float(by)]},
            }
        samples = max(2, min(4 * SHOWN_CELLS, int(math.ceil(length / (0.5 * state.fine)))))
        t = (np.arange(samples) + 0.5) / samples
        px, py = ax + t * (bx - ax), ay + t * (by - ay)
        image, pixels_per_um = _section_image(
            state, state.sample(slabs, px[None, :], py[None, :]), length, colors, rgb, top
        )
        return {
            "image": _png(image),
            "axis": "line",
            "position": 0.0,
            "index": 0,
            "interpolation": interpolation,
            "sampledSpacingUm": length / samples,
            "exact": False,
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "horizontalAxis": "s",
            "extent": {
                "horizontalMin": 0.0,
                "horizontalMax": length,
                "verticalMin": state.z_offset,
                "verticalMax": top,
            },
            "positions": [],
            "line": {"start": [float(ax), float(ay)], "end": [float(bx), float(by)]},
        }
    if axis == "y":
        coordinates = np.linspace(y_min, y_max, SECTION_POSITIONS)
    elif axis == "x":
        coordinates = np.linspace(x_min, x_max, SECTION_POSITIONS)
    else:
        raise VoxelError("section axis must be 'x' or 'y'")
    default = 0.5 * (coordinates[0] + coordinates[-1])
    index = int(np.argmin(np.abs(coordinates - (default if position is None else position))))
    cut = float(coordinates[index])
    if vector:
        horizontal = (x_min, x_max) if axis == "y" else (y_min, y_max)
        extent = (horizontal[0], horizontal[1], state.z_offset, top)
        width, height = _frame(extent)
        return {
            "image": "",
            "vector": _section_vector(state, colors, extent, width, height, axis=axis, cut=cut),
            "axis": axis,
            "position": cut,
            "index": index,
            "interpolation": interpolation,
            "sampledSpacingUm": state.fine,
            "exact": False,
            "width": width,
            "height": height,
            "horizontalAxis": "x" if axis == "y" else "y",
            "extent": {"horizontalMin": horizontal[0], "horizontalMax": horizontal[1], "verticalMin": state.z_offset, "verticalMax": top},
            "positions": [float(value) for value in coordinates],
        }
    shown_state = shown(state)
    B = shown_state.refine
    if axis == "y":
        row = int(np.clip((cut - y_min) / shown_state.fine_y, 0, state.ny * B - 1))
        columns = shown_state.sample_fine(slabs, np.arange(state.nx * B)[None, :], row)
        horizontal = (x_min, x_max)
        spacing = shown_state.fine_x
    else:
        col = int(np.clip((cut - x_min) / shown_state.fine_x, 0, state.nx * B - 1))
        columns = shown_state.sample_fine(slabs, col, np.arange(state.ny * B)[None, :])
        horizontal = (y_min, y_max)
        spacing = shown_state.fine_y
    image, pixels_per_um = _section_image(
        state, columns, horizontal[1] - horizontal[0], colors, rgb, top
    )
    return {
        "image": _png(image),
        "axis": axis,
        "position": cut,
        "index": index,
        "interpolation": interpolation,
        "sampledSpacingUm": spacing,
        "exact": False,
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "horizontalAxis": "x" if axis == "y" else "y",
        "extent": {
            "horizontalMin": horizontal[0],
            "horizontalMax": horizontal[1],
            "verticalMin": state.z_offset,
            "verticalMax": top,
        },
        "positions": [float(value) for value in coordinates],
    }


def _row_runs(labels: np.ndarray, cuts: np.ndarray, v: float, origin: float, step: float):
    """Runs along a row of fine cells (slabs, cells), read at ``v`` (0..1)
    across them: where the row crosses a cell's cut the cell is split at the
    crossing. Returns, per slab, run boundaries (from ``origin``, fine cells
    ``step`` wide) and materials."""
    n, count = labels.shape
    code = voxel_cut.code_of(cuts)
    other = voxel_cut.other_of(cuts).astype(np.int64)
    lab = labels.astype(np.int64)
    a, b = voxel_cut.START[code], voxel_cut.END[code]
    ax, ay = voxel_cut.ANCHORS[a, 0], voxel_cut.ANCHORS[a, 1]
    bx, by = voxel_cut.ANCHORS[b, 0], voxel_cut.ANCHORS[b, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (v - ay) / (by - ay)
        u = ax + t * (bx - ax)
    split = (code > 0) & (by != ay) & (t >= 0) & (t <= 1) & (u > 1e-9) & (u < 1 - 1e-9)
    u = np.where(split, u, 1.0)
    first = np.where(voxel_cut.left_of(code, np.where(split, 0.5 * u, 0.5), v), lab, other)
    second = np.where(voxel_cut.left_of(code, np.where(split, 0.5 * (1 + u), 0.5), v), lab, other)
    col = np.arange(count)[None, :]
    starts = np.stack([np.broadcast_to(col, (n, count)).astype(np.float64), col + u], -1).reshape(n, -1)
    mats = np.stack([first, second], -1).reshape(n, -1)
    lengths = np.stack([u, 1.0 - u], -1).reshape(n, -1)
    s_list, m_list = [], []
    for k in range(n):
        keep = lengths[k] > 1e-12
        st, mt = starts[k][keep], mats[k][keep]
        fresh = np.concatenate([[True], mt[1:] != mt[:-1]])
        s_list.append(origin + np.concatenate([st[fresh], [count]]) * step)
        m_list.append(mt[fresh])
    return s_list, m_list


def _section_vector(state: VoxelState, colors, extent, width, height, *, axis=None, cut=None, line=None, samples=None):
    """A section as outlines (see :mod:`voxel_vector`)."""
    from . import voxel_vector

    x_min, y_min, x_max, y_max = state.bounds
    slabs = np.arange(state.n)[:, None]
    B = state.refine
    if line is None:
        if axis == "y":
            g = (cut - y_min) / state.fine_y
            row = int(np.clip(np.floor(g), 0, state.ny * B - 1))
            v = float(np.clip(g - row, 0.0, 1.0))
            cols = np.arange(state.nx * B)[None, :]
            labels = state.sample_fine(slabs, cols, row)
            cuts = state.sample_cut(slabs, cols, row)
            s_list, m_list = _row_runs(labels, cuts, v, x_min, state.fine_x)
            s_min, s_max = x_min, x_max
        else:
            g = (cut - x_min) / state.fine_x
            col = int(np.clip(np.floor(g), 0, state.nx * B - 1))
            u = float(np.clip(g - col, 0.0, 1.0))
            rows = np.arange(state.ny * B)[None, :]
            labels = state.sample_fine(slabs, col, rows)
            cuts = state.sample_cut(slabs, col, rows)
            # a column read as a row: swap x and y, which mirrors the cut
            code = voxel_cut.code_of(cuts)
            swapped = voxel_cut.pack(voxel_cut.SWAP_XY[code], voxel_cut.other_of(cuts))
            s_list, m_list = _row_runs(labels, swapped, u, y_min, state.fine_y)
            s_min, s_max = y_min, y_max
    else:
        (ax, ay), (bx, by) = line
        t = (np.arange(samples) + 0.5) / samples
        labels = state.sample(slabs, (ax + t * (bx - ax))[None, :], (ay + t * (by - ay))[None, :]).astype(np.int64)
        length = float(math.hypot(bx - ax, by - ay))
        s_list, m_list = [], []
        for k in range(state.n):
            fresh = np.concatenate([[True], labels[k][1:] != labels[k][:-1]])
            s_list.append(np.concatenate([np.flatnonzero(fresh), [samples]]) * (length / samples))
            m_list.append(labels[k][fresh])
        s_min, s_max = 0.0, length
    z = state.z + state.z_offset
    found = voxel_vector.runs_loops(s_list, m_list, z, s_min, s_max)
    fills = [
        (state.materials[m - 1], *found[m])
        for m in sorted(found)
        if 0 < m <= len(state.materials)
    ]
    return voxel_vector.payload(fills, [], colors, extent, width, height)


def _top_field(state: VoxelState, hidden_ids: set[int]):
    """What is seen from above, as a flat two-level map: the topmost
    material of every bulk column and its height, and fine cell by fine
    cell in the columns that meet a refined cell first -- where the topmost
    thing is a cut, the line, the material and height on its left, and on
    its right whatever lies under an open side (the same material at
    another height is kept: that is a step to draw).

    Returns (coarse, coarse height, cells, labels, codes, others, left
    heights, right heights)."""
    n, ny, nx, B = state.n, state.ny, state.nx, state.refine
    visible = np.ones(256, dtype=bool)
    visible[VOID] = False
    for label in hidden_ids:
        visible[label] = False
    coarse = np.zeros((ny, nx), dtype=np.int64)
    height = np.full((ny, nx), -np.inf)
    start = np.full((ny, nx), -1, dtype=np.int64)
    found = np.zeros((ny, nx), dtype=bool)
    for k in range(n - 1, -1, -1):
        grid = state.labels[k]
        refined = (grid == MIXED) & ~found
        start[refined] = k
        found |= refined
        seen = visible[grid] & (grid != MIXED) & ~found
        coarse[seen] = grid[seen]
        height[seen] = state.z[k + 1]
        found |= seen
        if found.all():
            break
    cells = np.flatnonzero((start >= 0).reshape(-1))
    coarse.reshape(-1)[cells] = MIXED
    m = cells.size
    lab = np.zeros((m, B, B), dtype=np.int64)
    code = np.zeros((m, B, B), dtype=np.int64)
    other = np.zeros((m, B, B), dtype=np.int64)
    h_left = np.full((m, B, B), -np.inf)
    h_right = np.full((m, B, B), -np.inf)
    if m == 0:
        return coarse, height, cells, lab, code, other, h_left, h_right
    first = start.reshape(-1)[cells]
    done = np.zeros((m, B, B), dtype=bool)
    pending: list[tuple[np.ndarray, int]] = []  # fine cells whose right side is open, and the slab
    for k in range(int(first.max()), -1, -1):
        active = np.flatnonzero((first >= k) & ~done.all(axis=(1, 2)))
        if active.size == 0:
            break
        keys = k * state.plane + cells[active]
        L = state.blocks(keys).astype(np.int64)
        C = state.cuts(keys)
        K = voxel_cut.code_of(C)
        O = voxel_cut.other_of(C).astype(np.int64)
        todo = ~done[active]
        vis_l = visible[L] & todo
        vis_o = visible[O] & (K > 0) & todo
        whole = vis_l & (K == 0)
        both = vis_l & vis_o
        only_l = vis_l & ~vis_o & (K > 0)
        only_o = vis_o & ~vis_l
        settle = whole | both | only_l | only_o
        a, r, c = np.nonzero(settle)
        rows = active[a]
        flip = only_o[a, r, c]
        here = K[a, r, c]
        # only the far side seen: it becomes the left one, the line turns round
        turned = voxel_cut.CODE_OF[voxel_cut.END[here], voxel_cut.START[here]]
        lab[rows, r, c] = np.where(flip, O[a, r, c], L[a, r, c])
        other[rows, r, c] = np.where(flip, L[a, r, c], O[a, r, c])
        code[rows, r, c] = np.where(flip, turned, here)
        top = state.z[k + 1]
        h_left[rows, r, c] = top
        h_right[rows, r, c] = np.where(both[a, r, c], top, -np.inf)
        done[rows, r, c] = True
        half = np.flatnonzero((only_l | only_o)[a, r, c])
        if half.size:
            pending.append(((rows[half] * B + r[half]) * B + c[half], k))
    # an open right side: what is under it, at a point well inside it
    x_min, y_min, _, _ = state.bounds
    for flat, k in pending:
        row, rem = np.divmod(flat, B * B)
        r, c = np.divmod(rem, B)
        k_code = code.reshape(-1)[flat]
        centroid = np.array([voxel_cut.polygon(int(cc), False).mean(axis=0) for cc in k_code]).reshape(-1, 2)
        iy, ix = np.divmod(cells[row], nx)
        px = x_min + (ix * B + c + centroid[:, 0]) * state.fine_x
        py = y_min + (iy * B + r + centroid[:, 1]) * state.fine_y
        under = np.zeros(flat.size, dtype=np.int64)
        under_h = np.full(flat.size, -np.inf)
        missing = np.ones(flat.size, dtype=bool)
        for j in range(k - 1, -1, -1):
            if not missing.any():
                break
            got = state.sample(j, px, py).astype(np.int64)
            hit = missing & visible[got]
            under[hit] = got[hit]
            under_h[hit] = state.z[j + 1]
            missing &= ~hit
        other.reshape(-1)[flat] = under
        h_right.reshape(-1)[flat] = under_h
    # no cut left where nothing differs across it
    # (open to the bottom on both sides is the same height too: -inf)
    with np.errstate(invalid="ignore"):
        level = (h_left == h_right) | (np.abs(h_left - h_right) <= Z_EPS)
    none = (code > 0) & (other == lab) & level
    code[none] = 0
    return coarse, height, cells, lab, code, other, h_left, h_right


def _top_vector(state: VoxelState, colors, extent, width, height, hidden_ids, steps: bool, darker):
    """The view from above as outlines, with the lines where a material
    meets itself at another height."""
    from . import voxel_vector

    coarse, top_h, cells, lab, code, other, h_left, h_right = _top_field(state, hidden_ids)
    x_min, y_min, _, _ = state.bounds
    # the fills see a cut only where the material changes across it
    fill_code = np.where(other != lab, code, 0)
    cut = voxel_cut.pack(fill_code, other)
    found = voxel_vector.field_loops(
        coarse, cells, lab.astype(np.uint8), cut, x_min, y_min, 0.5 * state.fine_x, 0.5 * state.fine_y
    )
    fills = [(state.materials[m - 1], *found[m]) for m in sorted(found) if 0 < m <= len(state.materials)]
    strokes = []
    if steps:
        mat, x0, y0, x1, y1 = voxel_vector.step_edges(coarse, top_h, cells, lab, code, other, h_left, h_right)
        for m in np.unique(mat):
            pick = mat == m
            count = int(pick.sum())
            points = np.stack([
                np.stack([x_min + x0[pick] * 0.5 * state.fine_x, y_min + y0[pick] * 0.5 * state.fine_y], 1),
                np.stack([x_min + x1[pick] * 0.5 * state.fine_x, y_min + y1[pick] * 0.5 * state.fine_y], 1),
            ], 1).reshape(-1, 2)
            strokes.append((state.materials[int(m) - 1], points, np.arange(count) * 2))
    return voxel_vector.payload(fills, strokes, colors, extent, width, height, darker)


# -- the 3D view -----------------------------------------------------------

#: A face against the floor or the window edge: nothing is there.
_OUTSIDE = 256


def _rectangles(keys: np.ndarray) -> tuple[np.ndarray, ...]:
    """Cover the nonzero cells of a 2D key grid with rectangles of one key.

    Runs of one key along a row are found first, then runs with the same
    key and the same span in consecutive rows are joined. Returns
    ``(key, row0, row1, col0, col1)`` with exclusive ends.
    """
    rows, cols = keys.shape
    if rows == 0 or cols == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, empty, empty, empty
    change = np.ones((rows, cols + 1), dtype=bool)
    change[:, 1:-1] = keys[:, 1:] != keys[:, :-1]
    begin = change[:, :-1] & (keys != 0)
    finish = change[:, 1:] & (keys != 0)
    row_b, col_b = np.nonzero(begin)
    _row_f, col_f = np.nonzero(finish)
    key = keys[row_b, col_b].astype(np.int64)
    if key.size == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, empty, empty, empty
    col_f = col_f + 1
    order = np.lexsort((row_b, col_f, col_b, key))
    key, row_b, col_b, col_f = key[order], row_b[order], col_b[order], col_f[order]
    fresh = np.ones(key.size, dtype=bool)
    fresh[1:] = (
        (key[1:] != key[:-1])
        | (col_b[1:] != col_b[:-1])
        | (col_f[1:] != col_f[:-1])
        | (row_b[1:] != row_b[:-1] + 1)
    )
    first = np.flatnonzero(fresh)
    last = np.concatenate([first[1:], [key.size]]) - 1
    return key[first], row_b[first], row_b[last] + 1, col_b[first], col_f[first]


def build_meshes(state: VoxelState, *, buried: bool) -> dict[str, tuple[np.ndarray, ...]]:
    """Closed meshes for every material, faces joined into rectangles.

    Each material's triangles face outward; a face against another
    material carries that material's place in the mesh order, and is left
    out unless ``buried`` asks for it.

    The mesh is conforming: joining cells into rectangles leaves corners
    of one rectangle part-way along the edge of the next (a T-junction),
    and a GPU draws the two sides of such an edge along slightly
    different lines, which shows as pixel cracks. So every corner that
    lies on another drawn face's edge -- of any material -- is put into
    that edge, and a face with such points is fanned from its centre.

    Everything is on an integer lattice of :data:`voxel_cut.LATTICE`
    points per fine cell and one point per slab boundary, so this is exact
    integer work: faces between bulk cells are found and joined on the
    bulk grid, faces with a refined cell on either side half a fine side
    at a time (a cut meets a side at its middle), and a cut cell adds its
    slanted wall and the parts its cuts split its floor and ceiling into
    -- including the point where a cut below crosses a cut above.
    """
    state = shown(state)
    B = state.refine
    M = voxel_cut.LATTICE
    order = state.present()
    place = np.full(_OUTSIDE + 1, -1, dtype=np.int64)
    for index, name in enumerate(order):
        place[state.known_id(name)] = index
    n = state.n
    owners: list[np.ndarray] = []
    others: list[np.ndarray] = []
    shapes: list[np.ndarray] = []  # (P, W, 3) lattice points, counter-clockwise from outside
    sizes: list[np.ndarray] = []

    def add_polygons(owner, neighbour, points, size):
        keep = (neighbour == VOID) | (neighbour == _OUTSIDE) | buried
        keep &= (owner != VOID) & (owner != _OUTSIDE)
        if not keep.any():
            return
        owners.append(owner[keep].astype(np.int64))
        others.append(neighbour[keep].astype(np.int64))
        points = points[keep].astype(np.int64)
        padded = np.zeros((points.shape[0], _WIDEST, 3), dtype=np.int64)
        padded[:, : points.shape[1]] = points
        shapes.append(padded)
        sizes.append(np.broadcast_to(np.asarray(size, dtype=np.int64), keep.shape)[keep].copy())

    def add(owner, neighbour, corners, scale):
        corners = corners.astype(np.int64).copy()
        corners[:, :, :2] *= scale  # onto the lattice
        add_polygons(owner, neighbour, corners, 4)

    def faces_between(owner, neighbour, tag=None):
        # Keys only where a face is: most of the grid has none, and whole-
        # grid arithmetic was nearly all of the build. A refined cell on
        # either side is left to the fine pass.
        mask = (owner != neighbour) & (owner != VOID) & (owner != _OUTSIDE)
        mask &= (owner != MIXED) & (neighbour != MIXED)
        keys = np.zeros(owner.shape, dtype=np.int64)
        values = owner[mask].astype(np.int64) * 512 + neighbour[mask].astype(np.int64) + 1
        if tag is not None:
            values = values * tag[0] + np.broadcast_to(tag[1], owner.shape)[mask].astype(np.int64)
        keys[mask] = values
        return keys

    def split(keys):
        keys = keys - 1
        return keys // 512, keys % 512

    def quad(*corners):
        return np.stack([np.stack(corner, -1) for corner in corners], axis=1)

    labels = state.labels.astype(np.int16)
    coarse = B * M
    # Horizontal faces, at every slab boundary.
    for b in range(n + 1):
        below = labels[b - 1] if b > 0 else np.full((state.ny, state.nx), _OUTSIDE, dtype=np.int16)
        above = labels[b] if b < n else np.full((state.ny, state.nx), VOID, dtype=np.int16)
        for owner, neighbour, up in ((below, above, True), (above, below, False)):
            key, r0, r1, c0, c1 = _rectangles(faces_between(owner, neighbour))
            if key.size == 0:
                continue
            own, nb = split(key)
            bb = np.full(key.size, b)
            if up:
                corners = quad((c0, r0, bb), (c1, r0, bb), (c1, r1, bb), (c0, r1, bb))
            else:
                corners = quad((c0, r0, bb), (c0, r1, bb), (c1, r1, bb), (c1, r0, bb))
            add(own, nb, corners, coarse)
    if n:
        # Walls facing x, between column i-1 and i, joined along y and up
        # through the slabs. The wall line is folded into the key so runs
        # never join across lines.
        edge = np.full((n, state.ny, 1), _OUTSIDE, dtype=np.int16)
        left = np.concatenate([edge, labels], axis=2).transpose(2, 0, 1)
        right = np.concatenate([labels, edge], axis=2).transpose(2, 0, 1)
        walls = state.nx + 1
        line_of = np.arange(walls, dtype=np.int32)[:, None, None]
        for owner, neighbour, positive in ((left, right, True), (right, left, False)):
            tagged = faces_between(owner, neighbour, (walls, line_of))
            key, r0, r1, c0, c1 = _rectangles(tagged.reshape(-1, state.ny))
            if key.size:
                line = key % walls
                own, nb = split(key // walls)
                s0, s1 = r0 - line * n, r1 - line * n
                if positive:
                    corners = quad((line, c0, s0), (line, c1, s0), (line, c1, s1), (line, c0, s1))
                else:
                    corners = quad((line, c0, s0), (line, c0, s1), (line, c1, s1), (line, c1, s0))
                add(own, nb, corners, coarse)
        # Walls facing y, between row j-1 and j.
        edge = np.full((n, 1, state.nx), _OUTSIDE, dtype=np.int16)
        low = np.concatenate([edge, labels], axis=1).transpose(1, 0, 2)
        high = np.concatenate([labels, edge], axis=1).transpose(1, 0, 2)
        walls = state.ny + 1
        line_of = np.arange(walls, dtype=np.int32)[:, None, None]
        for owner, neighbour, positive in ((low, high, True), (high, low, False)):
            tagged = faces_between(owner, neighbour, (walls, line_of))
            key, r0, r1, c0, c1 = _rectangles(tagged.reshape(-1, state.nx))
            if key.size:
                line = key % walls
                own, nb = split(key // walls)
                s0, s1 = r0 - line * n, r1 - line * n
                if positive:
                    corners = quad((c0, line, s0), (c0, line, s1), (c1, line, s1), (c1, line, s0))
                else:
                    corners = quad((c0, line, s0), (c1, line, s0), (c1, line, s1), (c0, line, s1))
                add(own, nb, corners, coarse)
    if state.brick_keys.size:
        for own, nb, points, size in _fine_faces(state):
            add_polygons(own, nb, points, size)
    if not owners:
        return {}
    owner = np.concatenate(owners)
    neighbour = np.concatenate(others)
    points = np.concatenate(shapes)
    size = np.concatenate(sizes)
    x_min, y_min, _, _ = state.bounds

    def place_of(lattice):
        return np.stack(
            [
                x_min + lattice[:, 0] * (state.fine_x / M),
                y_min + lattice[:, 1] * (state.fine_y / M),
                state.z[lattice[:, 2]],
            ],
            axis=1,
        )

    coordinates, triangles, of_face = _conforming(points, size, place_of)
    coordinates = coordinates.astype(np.float32)
    meshes: dict[str, tuple[np.ndarray, ...]] = {}
    tri_owner = owner[of_face]
    tri_other = neighbour[of_face]
    for name in order:
        label = state.known_id(name)
        chosen = tri_owner == label
        if not chosen.any():
            continue
        tris = triangles[chosen]
        used, inverse = _unique(tris.ravel(), return_inverse=True)
        faces = inverse.reshape(-1, 3).astype(np.uint32)
        other = tri_other[chosen]
        interface = ((other != VOID) & (other != _OUTSIDE)).astype(np.uint8)
        index = np.where(interface == 1, place[np.minimum(other, _OUTSIDE)], -1).astype(np.int16)
        meshes[name] = (coordinates[used], faces, interface, index)
    return meshes


#: The most corners a face has: a cut wall with the crossings of the cuts
#: below and above put into its bottom and top edges.
_WIDEST = 6


def _join_units(table: np.ndarray) -> tuple[np.ndarray, ...]:
    """Unit faces (owner, other, orient, sense, line, u, v) joined into
    rectangles: along u, then along v. Returns (owner, other, orient,
    sense, line, u0, u1, v0, v1), ends exclusive."""
    order = _row_order(table[:, [0, 1, 2, 3, 4, 6, 5]])
    rec = table[order]
    start = np.ones(rec.shape[0], dtype=bool)
    start[1:] = (rec[1:, [0, 1, 2, 3, 4, 6]] != rec[:-1, [0, 1, 2, 3, 4, 6]]).any(axis=1) | (rec[1:, 5] != rec[:-1, 5] + 1)
    begin = np.flatnonzero(start)
    end = np.concatenate([begin[1:], [rec.shape[0]]]) - 1
    runs = np.stack([rec[begin, 0], rec[begin, 1], rec[begin, 2], rec[begin, 3], rec[begin, 4],
                     rec[begin, 5], rec[end, 5] + 1, rec[begin, 6]], 1)  # ..., u0, u1, v
    order = _row_order(runs[:, [0, 1, 2, 3, 4, 5, 6, 7]])
    runs = runs[order]
    start = np.ones(runs.shape[0], dtype=bool)
    start[1:] = (runs[1:, :7] != runs[:-1, :7]).any(axis=1) | (runs[1:, 7] != runs[:-1, 7] + 1)
    begin = np.flatnonzero(start)
    end = np.concatenate([begin[1:], [runs.shape[0]]]) - 1
    own, nb, orient, sense, line = (runs[begin, i] for i in range(5))
    return own, nb, orient, sense, line, runs[begin, 5], runs[begin, 6], runs[begin, 7], runs[end, 7] + 1


def _fine_faces(state: VoxelState):
    """Faces with a refined cell on either side, on the lattice: (owner,
    neighbour, points (P, W, 3), corner count) per group, the points
    counter-clockwise seen from outside the owner.

    Floors and ceilings between fine cells that are not cut, and walls
    between fine cells half a side at a time, are unit faces joined into
    rectangles; a floor or ceiling a cut crosses is split into the parts
    the cuts below and above make, and a cut cell has a wall along its
    cut."""
    B, n, nx, ny, plane = state.refine, state.n, state.nx, state.ny, state.plane
    M = voxel_cut.LATTICE
    H = M // 2
    labels = state.labels
    code_of, other_of = voxel_cut.code_of, voxel_cut.other_of
    left_on = voxel_cut.EDGE_LEFT
    floors: list[np.ndarray] = []  # unit faces, a fine cell each
    sides: list[np.ndarray] = []  # unit faces, half a fine side each
    out: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []

    def blocks(k, cells, fill):
        if 0 <= k < n:
            keys = k * plane + cells
            return state.blocks(keys).astype(np.int16), state.cuts(keys)
        return np.full((cells.size, B, B), fill, dtype=np.int16), np.zeros((cells.size, B, B), dtype=np.uint16)

    def emit(into, a, b, orient, line, u, v, where=None):
        # a sits on the negative side of the face, b on the positive one
        for owner, other, sense in ((a, b, 1), (b, a, 0)):
            face = (owner != other) & (owner != VOID) & (owner != _OUTSIDE)
            if where is not None:
                face &= where
            if face.any():
                count = int(face.sum())
                into.append(np.stack([
                    owner[face], other[face], np.full(count, orient), np.full(count, sense),
                    np.broadcast_to(line, face.shape)[face], np.broadcast_to(u, face.shape)[face],
                    np.broadcast_to(v, face.shape)[face],
                ], 1).astype(np.int64))

    def material(lab, cut, side, half):
        """The material of each fine cell along half ``half`` of its side ``side``."""
        code = code_of(cut)
        return np.where(left_on[code, side, half], lab, other_of(cut).astype(np.int16))

    fine = np.arange(B)
    # Floors and ceilings.
    for b in range(n + 1):
        hit = np.zeros((ny, nx), dtype=bool)
        if b > 0:
            hit |= labels[b - 1] == MIXED
        if b < n:
            hit |= labels[b] == MIXED
        cells = np.flatnonzero(hit)
        if cells.size == 0:
            continue
        iy, ix = np.divmod(cells, nx)
        lb, cb = blocks(b - 1, cells, _OUTSIDE)
        la, ca = blocks(b, cells, VOID)
        u = ix[:, None, None] * B + fine[None, None, :]
        v = iy[:, None, None] * B + fine[None, :, None]
        plain = (cb == 0) & (ca == 0)
        emit(floors, lb, la, 2, np.int64(b), u, v, plain)
        c, ry, rx = np.nonzero(~plain)
        if c.size == 0:
            continue
        code_b, code_a = code_of(cb[c, ry, rx]), code_of(ca[c, ry, rx])
        low, high = lb[c, ry, rx], la[c, ry, rx]
        low_other = other_of(cb[c, ry, rx]).astype(np.int16)
        high_other = other_of(ca[c, ry, rx]).astype(np.int16)
        ox = (ix[c] * B + rx) * M
        oy = (iy[c] * B + ry) * M
        pair = code_b * 33 + code_a
        for key in _unique(pair):
            sel = np.flatnonzero(pair == key)
            for poly, left_b, left_a in voxel_cut.regions(int(key) // 33, int(key) % 33):
                mb = low[sel] if left_b else low_other[sel]
                ma = high[sel] if left_a else high_other[sel]
                face = mb != ma
                if not face.any():
                    continue
                pick = sel[face]
                pts = np.empty((pick.size, len(poly), 3), dtype=np.int64)
                pts[:, :, 0] = ox[pick][:, None] + poly[None, :, 0]
                pts[:, :, 1] = oy[pick][:, None] + poly[None, :, 1]
                pts[:, :, 2] = b
                # up: the material below owns it; down: the one above, reversed
                out.append((mb[face], ma[face], pts, np.full(pick.size, len(poly))))
                out.append((ma[face], mb[face], pts[:, ::-1], np.full(pick.size, len(poly))))
    halves = np.arange(2 * B)
    for k in range(n):
        grid = labels[k]
        mixed = grid == MIXED
        if not mixed.any():
            continue
        for axis in (0, 1):  # walls facing x, then y
            # pairs of cells side by side along the axis, either refined;
            # the window edge is a neighbour outside
            if axis == 0:
                pad = np.pad(mixed, ((0, 0), (1, 1)))
                pair_ = pad[:, :-1] | pad[:, 1:]  # (ny, nx + 1): wall line i between i-1 and i
            else:
                pad = np.pad(mixed, ((1, 1), (0, 0)))
                pair_ = pad[:-1, :] | pad[1:, :]
            r, cc = np.nonzero(pair_)
            if axis == 0:
                low_ok, high_ok = cc > 0, cc < nx
                low_cell = r * nx + np.clip(cc - 1, 0, nx - 1)
                high_cell = r * nx + np.clip(cc, 0, nx - 1)
            else:
                low_ok, high_ok = r > 0, r < ny
                low_cell = np.clip(r - 1, 0, ny - 1) * nx + cc
                high_cell = np.clip(r, 0, ny - 1) * nx + cc
            low_l, low_c = blocks(k, low_cell, _OUTSIDE)
            high_l, high_c = blocks(k, high_cell, _OUTSIDE)
            low_l[~low_ok] = _OUTSIDE
            high_l[~high_ok] = _OUTSIDE
            low_c[~low_ok] = 0
            high_c[~high_ok] = 0
            if axis == 0:
                # the low cell's right side, the high cell's left side, along y
                a = np.stack([material(low_l[:, :, -1], low_c[:, :, -1], 1, h) for h in (0, 1)], -1).reshape(-1, 2 * B)
                bb = np.stack([material(high_l[:, :, 0], high_c[:, :, 0], 3, h) for h in (0, 1)], -1).reshape(-1, 2 * B)
                line = (cc * B)[:, None] * M
                u = r[:, None] * 2 * B + halves[None, :]
            else:
                a = np.stack([material(low_l[:, -1, :], low_c[:, -1, :], 0, h) for h in (0, 1)], -1).reshape(-1, 2 * B)
                bb = np.stack([material(high_l[:, 0, :], high_c[:, 0, :], 2, h) for h in (0, 1)], -1).reshape(-1, 2 * B)
                line = (r * B)[:, None] * M
                u = cc[:, None] * 2 * B + halves[None, :]
            emit(sides, a, bb, axis, line, u, np.int64(k))
            # inside refined cells
            cells = np.flatnonzero(mixed.reshape(-1))
            iy, ix = np.divmod(cells, nx)
            data, cut = blocks(k, cells, VOID)
            if axis == 0:
                a = np.stack([material(data[:, :, :-1], cut[:, :, :-1], 1, h) for h in (0, 1)], -1)  # (m, B, B-1, 2)
                bb = np.stack([material(data[:, :, 1:], cut[:, :, 1:], 3, h) for h in (0, 1)], -1)
                a = a.transpose(0, 2, 1, 3).reshape(cells.size, B - 1, 2 * B)
                bb = bb.transpose(0, 2, 1, 3).reshape(cells.size, B - 1, 2 * B)
                line = ((ix[:, None, None] * B + fine[None, 1:, None]) * M)
                u = iy[:, None, None] * 2 * B + halves[None, None, :]
            else:
                a = np.stack([material(data[:, :-1, :], cut[:, :-1, :], 0, h) for h in (0, 1)], -1)  # (m, B-1, B, 2)
                bb = np.stack([material(data[:, 1:, :], cut[:, 1:, :], 2, h) for h in (0, 1)], -1)
                a = a.reshape(cells.size, B - 1, 2 * B)
                bb = bb.reshape(cells.size, B - 1, 2 * B)
                line = ((iy[:, None, None] * B + fine[None, 1:, None]) * M)
                u = ix[:, None, None] * 2 * B + halves[None, None, :]
            emit(sides, a, bb, axis, line, u, np.int64(k))
        # the walls along the cuts
        cells = np.flatnonzero(mixed.reshape(-1))
        iy, ix = np.divmod(cells, nx)
        data, cut = blocks(k, cells, VOID)
        c, ry, rx = np.nonzero(cut > 0)
        if c.size:
            code = code_of(cut[c, ry, rx])
            mine, theirs = data[c, ry, rx], other_of(cut[c, ry, rx]).astype(np.int16)
            _unused, below = blocks(k - 1, cells, VOID)
            _unused, above = blocks(k + 1, cells, VOID)
            code_below = code_of(below[c, ry, rx])
            code_above = code_of(above[c, ry, rx])
            ox = (ix[c] * B + rx) * M
            oy = (iy[c] * B + ry) * M
            start, end = voxel_cut.START[code], voxel_cut.END[code]
            ax_ = ox + (voxel_cut.ANCHORS[start, 0] * M).astype(np.int64)
            ay_ = oy + (voxel_cut.ANCHORS[start, 1] * M).astype(np.int64)
            bx_ = ox + (voxel_cut.ANCHORS[end, 0] * M).astype(np.int64)
            by_ = oy + (voxel_cut.ANCHORS[end, 1] * M).astype(np.int64)
            cross_b = _crossings(code, code_below)
            cross_t = _crossings(code, code_above)
            count = code.size
            # the label's face looks across to the right: A0, (Xb), B0, B1, (Xt), A1
            pts = np.zeros((count, _WIDEST, 3), dtype=np.int64)
            size = np.zeros(count, dtype=np.int64)

            def put(where, x, y, z):
                pts[np.arange(count)[where], size[where]] = np.stack([x[where], y[where], np.full(int(where.sum()), z)], 1)
                size[where] += 1

            every = np.ones(count, dtype=bool)
            put(every, ax_, ay_, k)
            hb = cross_b[:, 0] >= 0
            put(hb, ox + cross_b[:, 0], oy + cross_b[:, 1], k)
            put(every, bx_, by_, k)
            put(every, bx_, by_, k + 1)
            ht = cross_t[:, 0] >= 0
            put(ht, ox + cross_t[:, 0], oy + cross_t[:, 1], k + 1)
            put(every, ax_, ay_, k + 1)
            out.append((mine, theirs, pts, size.copy()))
            # the other material's face looks back: the same corners reversed
            idx = size[:, None] - 1 - np.arange(_WIDEST)[None, :]
            idx = np.where(idx >= 0, idx, 0)
            reverse = np.take_along_axis(pts, idx[:, :, None].repeat(3, axis=2), axis=1)
            out.append((theirs, mine, reverse, size.copy()))
    # Unit faces, joined.
    if floors:
        own, nb, orient, sense, line, u0, u1, v0, v1 = _join_units(np.concatenate(floors))
        L = line
        a0, a1, b0, b1 = u0 * M, u1 * M, v0 * M, v1 * M
        up = sense == 1
        corners = np.where(
            up[:, None, None],
            np.stack([np.stack([a0, b0, L], -1), np.stack([a1, b0, L], -1), np.stack([a1, b1, L], -1), np.stack([a0, b1, L], -1)], 1),
            np.stack([np.stack([a0, b0, L], -1), np.stack([a0, b1, L], -1), np.stack([a1, b1, L], -1), np.stack([a1, b0, L], -1)], 1),
        )
        out.append((own, nb, corners, np.full(own.size, 4)))
    if sides:
        own, nb, orient, sense, line, u0, u1, v0, v1 = _join_units(np.concatenate(sides))
        L, a0, a1, b0, b1 = line, u0 * H, u1 * H, v0, v1
        for o in (0, 1):
            for sn in (1, 0):
                pick = (orient == o) & (sense == sn)
                if not pick.any():
                    continue
                Lp, p0, p1, q0, q1 = L[pick], a0[pick], a1[pick], b0[pick], b1[pick]
                if o == 0:  # x = L, u = y, v = slab
                    corners = (np.stack([np.stack([Lp, p0, q0], -1), np.stack([Lp, p1, q0], -1), np.stack([Lp, p1, q1], -1), np.stack([Lp, p0, q1], -1)], 1) if sn
                               else np.stack([np.stack([Lp, p0, q0], -1), np.stack([Lp, p0, q1], -1), np.stack([Lp, p1, q1], -1), np.stack([Lp, p1, q0], -1)], 1))
                else:  # y = L, u = x, v = slab
                    corners = (np.stack([np.stack([p0, Lp, q0], -1), np.stack([p0, Lp, q1], -1), np.stack([p1, Lp, q1], -1), np.stack([p1, Lp, q0], -1)], 1) if sn
                               else np.stack([np.stack([p0, Lp, q0], -1), np.stack([p1, Lp, q0], -1), np.stack([p1, Lp, q1], -1), np.stack([p0, Lp, q1], -1)], 1))
                out.append((own[pick], nb[pick], corners, np.full(int(pick.sum()), 4)))
    return out


def _crossings(code: np.ndarray, other: np.ndarray) -> np.ndarray:
    """Where cut ``other`` crosses cut ``code`` strictly inside it, on the
    lattice within the cell: (count, 2), -1 where it does not."""
    out = np.full((code.size, 2), -1, dtype=np.int64)
    pair = code.astype(np.int64) * 33 + other.astype(np.int64)
    for key in _unique(pair):
        point = voxel_cut.crossing(int(key) // 33, int(key) % 33)
        if point is not None:
            out[pair == key] = point
    return out


def _conforming(points: np.ndarray, size: np.ndarray, place_of) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Triangles for faces given by lattice points ((F, W, 3), the first
    ``size`` of each), with every face corner that lies inside another
    face's edge along an axis added to that edge.

    ``place_of`` turns lattice points into coordinates. Returns the vertex
    coordinates, the triangles (indices into them) and the face each
    triangle came from. A face with no points added is fanned from its
    first corner (faces are convex); one with points added, from its
    centre."""
    count = points.shape[0]
    width = points.shape[1]
    valid = np.arange(width)[None, :] < size[:, None]
    flat = points[valid]
    unique_points, which = _unique_rows(flat)
    point_of = np.full((count, width), -1, dtype=np.int64)
    point_of[valid] = which
    # Every face edge that runs along an axis: only that coordinate moves.
    scale = int(unique_points.max()) + 2 if unique_points.size else 2
    extras: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []  # (face, slot, point)
    nxt = (np.arange(width)[None, :] + 1) % np.maximum(size[:, None], 1)
    for e in range(width):
        has = size > e
        f = np.flatnonzero(has)
        if f.size == 0:
            continue
        a = points[f, e]
        b = points[f, nxt[f, e]]
        for axis in range(3):
            other = [x for x in range(3) if x != axis]
            along = (a[:, axis] != b[:, axis]) & (a[:, other[0]] == b[:, other[0]]) & (a[:, other[1]] == b[:, other[1]])
            if not along.any():
                continue
            line_key = unique_points[:, other[0]] * scale + unique_points[:, other[1]]
            sort_key = line_key * scale + unique_points[:, axis]
            order = np.argsort(sort_key, kind="stable")
            sorted_key = sort_key[order]
            q = np.flatnonzero(along)
            lo_pos = np.minimum(a[q, axis], b[q, axis])
            hi_pos = np.maximum(a[q, axis], b[q, axis])
            key = a[q, other[0]] * scale + a[q, other[1]]
            lo = np.searchsorted(sorted_key, key * scale + lo_pos, side="right")
            hi = np.searchsorted(sorted_key, key * scale + hi_pos, side="left")
            many = hi - lo
            more = many > 0
            if not more.any():
                continue
            q, lo, many = q[more], lo[more], many[more]
            forward = b[q, axis] > a[q, axis]
            repeat = np.repeat(np.arange(q.size), many)
            offset = np.arange(repeat.size) - np.repeat(np.cumsum(many) - many, many)
            rank = np.where(forward[repeat], offset, many[repeat] - 1 - offset)
            inserted = order[lo[repeat] + offset]
            extras.append((f[q[repeat]], e * 1_000_000 + 1 + rank, inserted))
    located = place_of(unique_points)
    fanned_mask = np.zeros(count, dtype=bool)
    if extras:
        face_of = np.concatenate([x[0] for x in extras])
        fanned_mask[face_of] = True
    triangles: list[np.ndarray] = []
    owners: list[np.ndarray] = []
    # plain faces: a fan from the first corner
    plain = np.flatnonzero(~fanned_mask)
    for m in range(3, width + 1):
        pick = plain[size[plain] == m]
        if pick.size == 0:
            continue
        for i in range(1, m - 1):
            triangles.append(np.stack([point_of[pick, 0], point_of[pick, i], point_of[pick, i + 1]], 1))
            owners.append(pick)
    if not extras:
        return located, np.concatenate(triangles), np.concatenate(owners)
    # faces with points put in: their corners and the added points in order
    # round the face, fanned from a new vertex at the centre of the corners
    fanned = np.flatnonzero(fanned_mask)
    corner_face = np.repeat(fanned, size[fanned])
    corner_slot = (np.arange(corner_face.size) - np.repeat(np.cumsum(size[fanned]) - size[fanned], size[fanned])) * 1_000_000
    corner_point = point_of[fanned][np.arange(width)[None, :] < size[fanned][:, None]]
    loop_face = np.concatenate([corner_face, np.concatenate([x[0] for x in extras])])
    loop_slot = np.concatenate([corner_slot, np.concatenate([x[1] for x in extras])])
    loop_point = np.concatenate([corner_point, np.concatenate([x[2] for x in extras])])
    order = np.lexsort((loop_slot, loop_face))
    loop_face, loop_point = loop_face[order], loop_point[order]
    first = np.flatnonzero(np.concatenate([[True], loop_face[1:] != loop_face[:-1]]))
    last = np.concatenate([first[1:], [loop_face.size]]) - 1
    following = np.arange(loop_face.size) + 1
    following[last] = first
    corner_xyz = located[corner_point]
    centre_sum = np.zeros((count, 3))
    np.add.at(centre_sum, corner_face, corner_xyz)
    centres = centre_sum[fanned] / size[fanned][:, None]
    lookup = np.full(count, -1, dtype=np.int64)
    lookup[fanned] = located.shape[0] + np.arange(fanned.size)
    all_points = np.concatenate([located, centres])
    triangles.append(np.stack([lookup[loop_face], loop_point, loop_point[following]], axis=1))
    owners.append(loop_face)
    return all_points, np.concatenate(triangles), np.concatenate(owners)


def meshes_for(state: VoxelState, buried: bool) -> dict[str, tuple[np.ndarray, ...]]:
    with state.mesh_lock:
        built = state.meshes.get(bool(buried))
        if built is None:
            built = build_meshes(state, buried=bool(buried))
            state.meshes[bool(buried)] = built
        return built


def encode(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def surfaces(
    state: VoxelState,
    *,
    z_max: float,
    materials: Sequence[str] | None,
    buried: bool,
) -> dict[str, Any]:
    meshes = meshes_for(state, buried)
    order = list(meshes)
    payload = []
    for name in state.present():
        if name not in meshes or (materials is not None and name not in materials):
            continue
        vertices, faces, interface, neighbour = meshes[name]
        positions = vertices.copy()
        positions[:, 2] += np.float32(state.z_offset)
        against_index = np.where(neighbour < 0, 255, neighbour).astype(np.uint8)
        payload.append(
            {
                "material": name,
                "positions": encode(positions),
                "normals": "",
                "shading": "flat",
                "indices": encode(faces),
                "interfaceFaces": encode(interface),
                "neighbourFaces": encode(against_index),
                "neighbourMaterials": order,
                "vertexCount": int(len(positions)),
                "triangleCount": int(len(faces)),
            }
        )
    x_min, y_min, x_max, y_max = state.bounds
    return {
        "interpolation": 1,
        "exact": True,
        "triangulation": "voxel",
        "buried": bool(buried),
        "bounds": {
            "xMin": x_min,
            "xMax": x_max,
            "yMin": y_min,
            "yMax": y_max,
            "zMin": state.z_offset,
            "zMax": max(z_max, state.top + state.z_offset),
        },
        "surfaces": payload,
    }


def state_bytes(state: VoxelState) -> int:
    total = 64 * 1024
    for part in [state] + list(state.shown.values()):
        total += part.labels.nbytes + part.pool.nbytes + part.pool_cut.nbytes + part.brick_ref.nbytes + part.brick_keys.nbytes
    for meshes in state.meshes.values():
        for arrays in meshes.values():
            total += sum(array.nbytes for array in arrays)
    return total


# -- steps -----------------------------------------------------------------


def grid_size(project: ProjectDefinition, resolution: float | None = None) -> tuple[float, int]:
    """The bulk cell edge and the refinement for a project (at
    ``resolution`` instead of the project's XY resolution, for a step that
    has one of its own).

    The bulk is the window over :data:`TARGET_CELLS`. Boundaries are
    refined to the XY resolution when that is finer: ``refine`` fine cells
    per bulk cell, the least power of two that makes a fine cell no larger
    than the resolution asked for, up to :data:`MAX_REFINE`."""
    from . import slab

    x_min, y_min, x_max, y_max = slab._window(project)
    span = max(x_max - x_min, y_max - y_min)
    cell = span / TARGET_CELLS
    if resolution is None:
        resolution = slab.resolution_xy_um(project)
    refine = 1
    # powers of two, so a picture can take every second, fourth... fine cell
    while refine < MAX_REFINE and cell / refine > resolution * (1 + 1e-9):
        refine *= 2
    return cell, refine


def resample(state: VoxelState, refine: int, should_cancel: Callable[[], bool] | None = None) -> VoxelState:
    """``state`` with ``refine`` fine cells a side in each refined cell.

    The bulk cells, the slabs and the plain cells stay as they are. Each
    refined cell's new fine cells take the material at their centres, cut
    lines included, and their boundaries are fitted again against the old
    geometry -- the same fitting a process does -- so going finer draws the
    old lines exactly (every line between two old anchors passes through
    anchors of the finer cells) and going coarser keeps them as closely as
    the coarser cells allow.
    """
    if refine == state.refine:
        return state
    new = VoxelState(
        state.bounds, state.nx, state.ny, state.z.copy(), state.labels.copy(),
        list(state.materials), state.z_offset, refine,
    )
    if refine % state.refine == 0 and state.brick_keys.size:
        # Finer by a whole factor: a brick's finer drawing is its own
        # business, so each distinct brick is drawn once.
        used, back = _unique(state.brick_ref, return_inverse=True)
        blocks, cuts = _refined_bricks(state.pool[used], state.pool_cut[used], refine // state.refine)
        back = back.reshape(-1)
        for part in _chunks(np.arange(state.brick_keys.size), refine * refine * 3):
            _check(should_cancel, "changing the grid")
            new.store(state.brick_keys[part], blocks[back[part]], cuts[back[part]])
        return new
    x_min, y_min, _, _ = state.bounds
    B, plane, nx = refine, state.plane, state.nx
    rows = np.arange(B)
    for k in range(state.n):
        _check(should_cancel, "changing the grid")
        cells, _refs = state.slab_refs(k)
        if cells.size == 0:
            continue
        for part in _chunks(cells, B * B):
            iy, ix = np.divmod(part, nx)
            xs = x_min + (ix[:, None] * B + rows[None, :] + 0.5) * new.fine_x
            ys = y_min + (iy[:, None] * B + rows[None, :] + 0.5) * new.fine_y
            X = np.broadcast_to(xs[:, None, :], (part.size, B, B))
            Y = np.broadcast_to(ys[:, :, None], (part.size, B, B))
            blocks = state.sample(k, X.reshape(-1), Y.reshape(-1)).reshape(part.size, B, B)
            new.store(k * plane + part, blocks)
        _refit(new, k, cells, lambda x, y, k=k: state.sample(k, x, y))
    return new


def _refined_bricks(blocks: np.ndarray, cuts: np.ndarray, f: int) -> tuple[np.ndarray, np.ndarray]:
    """Bricks (P, b, b) with their cuts, each fine cell split into f x f.

    A child takes its parent's material, or the side of the parent's cut
    its centre is on; a child the cut crosses is fitted from its parent's
    line exactly as :func:`_refit` fits a cell -- anchors a hair inside,
    half sides snapped at their middles -- and every line between two
    parent anchors runs through child anchors, so nothing is lost.
    """
    P, b, _ = blocks.shape
    B = b * f
    j = np.arange(B)
    parent = j // f
    offset = j % f
    labels = blocks[:, parent[:, None], parent[None, :]]
    parent_cut = cuts[:, parent[:, None], parent[None, :]]
    out_cut = np.zeros((P, B, B), dtype=np.uint16)
    p, r, c = np.nonzero(parent_cut)
    if p.size == 0:
        return labels, out_cut
    packed = parent_cut[p, r, c]
    code = voxel_cut.code_of(packed)
    other = voxel_cut.other_of(packed)
    mine = labels[p, r, c]

    def side(cc, u, v, a, o):
        return np.where(voxel_cut.left_of(cc, u, v), a, o).astype(np.uint8)

    # child points in the parent's unit square
    centre = side(code, (offset[c] + 0.5) / f, (offset[r] + 0.5) / f, mine, other)
    inward = 0.5 + (voxel_cut.ANCHORS - 0.5) * (1.0 - 2e-3)
    au = (offset[c][:, None] + inward[None, :, 0]) / f
    av = (offset[r][:, None] + inward[None, :, 1]) / f
    anchor = side(code[:, None], au, av, mine[:, None], other[:, None])
    crossing = anchor != np.roll(anchor, -1, axis=1)
    snap = np.broadcast_to(voxel_cut.TO_MIDDLE, anchor.shape).copy()
    rr, h = np.nonzero(crossing)
    if rr.size:
        middle = 0.5 + (voxel_cut.half_middles() - 0.5) * (1.0 - 2e-3)
        mu = (offset[c[rr]] + middle[h, 0]) / f
        mv = (offset[r[rr]] + middle[h, 1]) / f
        got = side(code[rr], mu, mv, mine[rr], other[rr])
        snap[rr, h] = np.where(got == anchor[rr, (h + 1) % 8], 0, 1)
    label, cut = voxel_cut.fit(anchor, centre, snap)
    labels[p, r, c] = label
    out_cut[p, r, c] = cut
    return labels, out_cut


def initial_state(project: ProjectDefinition) -> VoxelState:
    """The bare wafer: one substrate slab, its top face at z = 0."""
    from . import slab

    x_min, y_min, x_max, y_max = slab._window(project)
    z_offset = float(project.grid["z_min"])
    if -z_offset <= 0.0:
        raise slab.SlabError("the project window needs room below z = 0 for the substrate")
    cell, refine = grid_size(project)
    nx = max(1, round((x_max - x_min) / cell))
    ny = max(1, round((y_max - y_min) / cell))
    labels = np.ones((1, ny, nx), dtype=np.uint8)
    return VoxelState(
        (x_min, y_min, x_max, y_max), nx, ny, np.array([0.0, _z(-z_offset)]), labels,
        [slab.SUBSTRATE_MATERIAL], z_offset, refine,
    )


def _opening(
    state: VoxelState,
    step: ProcessStep,
    project: ProjectDefinition,
    parameters: Mapping[str, Any],
    sketches: Mapping[str, QuickSketch],
) -> np.ndarray | None:
    """The cells a step's mask leaves open, or None for the whole window."""
    from . import slab

    if step.mask_source == "none":
        return None
    window = box(*state.bounds)
    if step.mask_source == "quick_sketch":
        sketch_id = str(parameters.get("sketch_id", "default"))
        if sketch_id not in sketches:
            raise slab.SlabError(f"quick sketch {sketch_id!r} was not found")
        geometry = slab.sketch_geometry(sketches[sketch_id], window)
    else:
        if not project.gds_path:
            raise slab.SlabError("the project has no GDS file")
        if step.layer is None or step.datatype is None:
            raise slab.SlabError("GDS steps require a layer and a datatype")
        from deviceflow import Layout

        layout = Layout.from_gds(project.gds_path, window=None, grid=slab.GEOMETRY_GRID_UM)
        geometry = layout.mask(int(step.layer), int(step.datatype))._geom
    geometry = shapely.make_valid(geometry)
    if step.keep == "outside":
        geometry = window.difference(geometry)
    geometry = geometry.intersection(window)
    if geometry.is_empty:
        raise slab.SlabError("this mask does not overlap the device window")
    cells = rasterize(state, geometry)
    if not cells.any():
        raise slab.SlabError(
            f"this mask's openings are narrower than one voxel cell ({state.fine * 1000:.3g} nm), "
            "so the voxel model sees none of them; use the simplified or detailed film model"
        )
    return cells


def _deposit_step(state, step, recipe, parameters, logger, project, opening, should_cancel):
    from . import slab

    material = str(parameters.get("material") or recipe.output_material or "")
    if not material:
        raise slab.SlabError("a deposition step needs an output material")
    thickness = slab._deposit_thickness(parameters)
    if thickness <= 0.0:
        raise slab.SlabError("deposition thickness must be greater than zero")
    slab._check_length(thickness, "A film", project)
    mode = str(parameters.get("mode", "conformal")).strip().lower()
    if mode in {"directional", "evaporation", "fill", "directional prism"}:
        raise slab.SlabError(
            f"this kernel cannot deposit in {mode!r} mode; it offers 'conformal' "
            "(equal thickness on every surface) and 'planar' (from straight above, "
            "sidewalls covered, nothing under an overhang)."
        )
    if mode not in {"conformal", "planar"}:
        raise slab.SlabError(f"unknown deposition mode {mode!r}; expected 'conformal' or 'planar'")
    where = " inside the mask" if opening is not None else ""
    logger(f"VOXEL deposit {material} {thickness:g} um {mode}{where}")
    deposit(
        state, material, thickness, planar=mode == "planar", opening=opening,
        should_cancel=should_cancel,
    )


def _budget(recipe, parameters, project, what: str) -> tuple[dict[str, float], float, str]:
    """Rates and the budget they run for, the way the polygon slabs read them:
    a depth of the fastest material (rates relative to it), or a time."""
    from . import slab

    rates = slab._etch_rates(recipe, parameters)
    active = {name: rate for name, rate in rates.items() if rate > 0.0}
    if not active:
        raise slab.SlabError(f"every material in this {what} has rate zero; nothing would be removed")
    if parameters.get("target") is not None:
        depth = float(parameters["target"])
        if depth <= 0.0:
            raise slab.SlabError(f"{what} depth must be greater than zero")
        slab._check_length(depth, f"An {what} depth", project)
        reference = max(active, key=lambda name: active[name])
        return {name: rates[name] / rates[reference] for name in rates}, depth, (
            f"{depth:g} um of {reference}"
        )
    if parameters.get("time_min") is None:
        raise slab.SlabError(f"an {what} step needs a target depth or a time")
    minutes = float(parameters["time_min"])
    if minutes <= 0.0:
        raise slab.SlabError(f"{what} time must be greater than zero")
    return dict(rates), minutes, f"for {minutes:g} min"


def run_step(
    state: VoxelState,
    step: ProcessStep,
    *,
    project: ProjectDefinition,
    recipes: Mapping[str, Recipe],
    sketches: Mapping[str, QuickSketch],
    logger: Callable[[str], None],
    materials: Sequence[MaterialDefinition] = (),
    should_cancel: Callable[[], bool] | None = None,
) -> VoxelState:
    """Advance one step on a copy of ``state``."""
    from . import slab

    new = state.copy()
    if not step.enabled:
        logger(f"SKIP {step.name}: disabled")
        return new
    recipe = step.effective_recipe(recipes)
    parameters = dict(recipe.parameters)
    kind = recipe.process_type
    logger(f"RUN {step.name} [{kind.value}]")
    # A step with a resolution of its own runs on its own fine grid: the
    # state is drawn again at it first (see resample), and the next step
    # draws it at its own. A CMP or a flip keeps whatever grid it is given.
    own = slab.step_resolution_um(parameters, kind)
    if own is not None or kind in (ProcessType.DEPOSIT, ProcessType.ETCH, ProcessType.OXIDATION):
        _, refine = grid_size(project, own)
        if refine != new.refine:
            logger(f"VOXEL boundaries redrawn from {new.refine} to {refine} fine cells per cell")
            new = resample(new, refine, should_cancel)
    boundary = (
        f", boundaries in {new.fine_x * 1000:.4g} x {new.fine_y * 1000:.4g} nm "
        f"({new.refine} x {new.refine} per cell)" if new.refine > 1 else ""
    )
    logger(
        f"VOXEL grid {new.nx} x {new.ny} cells of {new.cell_x * 1000:.4g} x "
        f"{new.cell_y * 1000:.4g} nm{boundary}; heights exact"
    )
    # The march cuts slabs near a bending front at the project's z step,
    # never finer than a bulk cell; the front is then placed at the z step
    # itself, in thin slabs only where it curves.
    z_fine = own if own is not None else slab.resolution_um(project)
    z_step = max(z_fine, new.cell)
    try:
        if kind is ProcessType.DEPOSIT:
            opening = _opening(new, step, project, parameters, sketches)
            _deposit_step(new, step, recipe, parameters, logger, project, opening, should_cancel)
        elif kind is ProcessType.ETCH:
            opening = _opening(new, step, project, parameters, sketches)
            fraction = float(parameters.get("directional_fraction", 1.0))
            if fraction not in (0.0, 1.0):
                raise slab.SlabError(
                    f"this kernel etches either straight down (directional_fraction 1) or "
                    f"isotropically (0); this step asks for {fraction:g}. A mixed profile "
                    "has to be built as the two steps it is made of."
                )
            rates, budget, text = _budget(recipe, parameters, project, "etch")
            if fraction == 1.0:
                logger(f"VOXEL etch vertical {text}")
                etch_vertical(new, rates, budget, opening, should_cancel=should_cancel)
            else:
                logger(f"VOXEL etch isotropic {text}, curved in z every {z_fine * 1000:g} nm")
                etch_isotropic(
                    new, rates, budget, opening, dz=z_step, fine_z=z_fine, should_cancel=should_cancel
                )
        elif kind is ProcessType.OXIDATION:
            opening = _opening(new, step, project, parameters, sketches)
            rates, budget, text = _budget(recipe, parameters, project, "oxidation")
            product = str(parameters.get("material") or recipe.output_material or "SiO2")
            if product in {name for name, rate in rates.items() if rate > 0.0}:
                raise slab.SlabError(f"{product} cannot be both oxidised and the oxide it becomes")
            logger(f"VOXEL oxidize {text} into {product}, curved in z every {z_fine * 1000:g} nm")
            etch_isotropic(
                new, rates, budget, opening, product=product, dz=z_step, fine_z=z_fine,
                should_cancel=should_cancel,
            )
        elif kind is ProcessType.CMP:
            if parameters.get("target_z") is not None:
                height = float(parameters["target_z"]) - new.z_offset
            else:
                height = new.top - float(parameters.get("removal_amount", 0.0))
            if height <= 0.0:
                raise slab.SlabError("the polish plane is at or below the wafer floor")
            logger(f"VOXEL cmp to z={height + new.z_offset:g} um")
            cmp(new, height)
        elif kind is ProcessType.FLIP:
            axis = str(parameters.get("axis", "y")).strip().lower()
            if axis not in ("x", "y"):
                raise slab.SlabError(f"a flip turns the wafer about the x or the y axis, not {axis!r}")
            logger(f"VOXEL flip about the {axis} axis")
            flip(new, axis)
    except VoxelError as error:
        raise slab.SlabError(str(error)) from error
    new.path = None
    new.meshes = {}
    new.shown = {}
    return new
