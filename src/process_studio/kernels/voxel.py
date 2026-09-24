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
import itertools
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
        self._pool_hash = np.zeros(0, dtype=np.uint64)
        self.brick_ref = np.zeros(0, dtype=np.int64)
        if bricks is not None and len(bricks):
            self.brick_ref = self._intern(np.asarray(bricks, dtype=np.uint8))
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

    def _intern(self, blocks: np.ndarray) -> np.ndarray:
        """Pool entries for ``blocks``, adding the ones not there yet."""
        n = blocks.shape[0]
        if n == 0:
            return np.zeros(0, dtype=np.int64)
        blocks = np.ascontiguousarray(blocks, dtype=np.uint8)
        hashes = _brick_hash(blocks)
        # among themselves: one entry per distinct brick
        unique_hash, first, inverse = _unique(hashes, return_index=True, return_inverse=True)
        inverse = inverse.reshape(-1)
        same = (blocks == blocks[first[inverse]]).all(axis=(1, 2))
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
            match[np.flatnonzero(found)[~ok]] = -1
        new = match < 0
        start = self.pool.shape[0]
        match[new] = start + np.arange(int(new.sum()))
        added = [blocks[first[new]]]
        added_hash = [unique_hash[new]]
        refs[same] = match[inverse[same]]
        # a hash shared by different bricks (never seen, but cheap to allow)
        odd = np.flatnonzero(~same)
        if odd.size:
            rest, back = np.unique(blocks[odd].reshape(odd.size, -1), axis=0, return_inverse=True)
            base = start + int(new.sum())
            refs[odd] = base + back.reshape(-1)
            added.append(rest.reshape(-1, self.refine, self.refine))
            added_hash.append(_brick_hash(added[-1]))
        self.pool = np.concatenate([self.pool] + added)
        self._pool_hash = np.concatenate([self._pool_hash] + added_hash)
        return refs

    def _compact(self) -> None:
        """Drop pool entries no cell refers to."""
        used, back = _unique(self.brick_ref, return_inverse=True)
        if used.size == self.pool.shape[0]:
            return
        self.pool = np.ascontiguousarray(self.pool[used])
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

    def store(self, keys: np.ndarray, blocks: np.ndarray) -> None:
        """Write fine labels for the cells ``keys``: a block of one label
        becomes a plain cell, any other becomes (or stays) a brick."""
        keys = np.asarray(keys, dtype=np.int64)
        if keys.size == 0:
            return
        blocks = np.asarray(blocks, dtype=np.uint8)
        first = blocks[:, :1, :1]
        uniform = (blocks == first).all(axis=(1, 2))
        flat = self.labels.reshape(-1)
        flat[keys[uniform]] = blocks[uniform, 0, 0]
        flat[keys[~uniform]] = MIXED
        keep = ~np.isin(self.brick_keys, keys)
        new_keys = np.concatenate([self.brick_keys[keep], keys[~uniform]])
        new_refs = np.concatenate([self.brick_ref[keep], self._intern(blocks[~uniform])])
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
        """The label at points ``(x, y)`` of slabs ``k`` (arrays, broadcast)."""
        x_min, y_min, _, _ = self.bounds
        k, x, y = np.broadcast_arrays(np.asarray(k), np.asarray(x, float), np.asarray(y, float))
        fx = np.clip(((x - x_min) / self.fine_x).astype(np.int64), 0, self.nx * self.refine - 1)
        fy = np.clip(((y - y_min) / self.fine_y).astype(np.int64), 0, self.ny * self.refine - 1)
        return self.sample_fine(k, fx, fy)

    def sample_fine(self, k: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
        """The label at fine cell ``(fx, fy)`` of slabs ``k``."""
        B = self.refine
        k, fx, fy = np.broadcast_arrays(np.asarray(k), np.asarray(fx), np.asarray(fy))
        shape = k.shape
        k, fx, fy = k.reshape(-1), fx.reshape(-1), fy.reshape(-1)
        ix, bx = np.divmod(fx, B)
        iy, by = np.divmod(fy, B)
        out = self.labels[k, iy, ix]
        mixed = out == MIXED
        if mixed.any():
            keys = k[mixed] * self.plane + iy[mixed] * self.nx + ix[mixed]
            at = np.searchsorted(self.brick_keys, keys)
            out[mixed] = self.pool[self.brick_ref[at], by[mixed], bx[mixed]]
        return out.reshape(shape)

    @classmethod
    def from_fine(
        cls, bounds, fine: np.ndarray, refine: int, z, materials, z_offset: float = 0.0
    ) -> "VoxelState":
        """A state holding the dense fine labels ``fine`` (slabs, ny*refine, nx*refine)."""
        fine = np.asarray(fine, dtype=np.uint8)
        n, fy, fx = fine.shape
        B = int(refine)
        ny, nx = fy // B, fx // B
        cells = fine.reshape(n, ny, B, nx, B).transpose(0, 1, 3, 2, 4).reshape(-1, B, B)
        state = cls(bounds, nx, ny, z, np.zeros((n, ny, nx), np.uint8), materials, z_offset, B)
        state.store(np.arange(cells.shape[0], dtype=np.int64), cells)
        return state

    def to_fine(self) -> np.ndarray:
        """The dense fine labels (slabs, ny*refine, nx*refine); for small grids and tests."""
        B = self.refine
        keys = np.arange(self.n * self.plane, dtype=np.int64)
        cells = self.blocks(keys).reshape(self.n, self.ny, self.nx, B, B)
        return cells.transpose(0, 1, 3, 2, 4).reshape(self.n, self.ny * B, self.nx * B)

    def check(self) -> None:
        """That bricks and MIXED marks agree and no brick is uniform (for tests)."""
        marked = np.flatnonzero(self.labels.reshape(-1) == MIXED)
        assert np.array_equal(marked, self.brick_keys), "MIXED cells and bricks disagree"
        if self.brick_ref.size:
            data = self.pool[_unique(self.brick_ref)]
            first = data[:, :1, :1]
            assert not (data == first).all(axis=(1, 2)).any(), "a brick is uniform"
            assert not (data == MIXED).any(), "a brick holds the MIXED mark"
            assert np.unique(self.pool.reshape(len(self.pool), -1), axis=0).shape[0] == len(self.pool), "a brick is pooled twice"

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
            _cells, data = self.slab_bricks(k)
            if data.size:
                found |= set(_labels_in(data).tolist())
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
            fine = (self.pool == label).sum(axis=(1, 2)) / float(self.refine**2)
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
                state._pool_hash = _brick_hash(state.pool)
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


def components(mask: np.ndarray, *, connect_layers: bool = True) -> np.ndarray:
    """Label the 6-connected pieces of a stack of grids (0 = not in the mask).

    Runs along x are found first, then runs that touch in the row above or
    in the slab above are joined (not across the first axis when
    ``connect_layers`` is false: a stack of separate grids). Labels are
    positive but not consecutive.
    """
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
    parent = _union_find(count, np.concatenate(heads), np.concatenate(tails))
    return parent[run3]


def components2(
    plain: np.ndarray, keys: np.ndarray, fine: np.ndarray, nx: int, ny: int
) -> tuple[np.ndarray, np.ndarray]:
    """The 6-connected pieces of a set given at the grid's two levels.

    ``plain`` marks whole bulk cells of a stack (slabs, ny, nx); ``keys``
    (slab * ny * nx + cell) are the refined cells and ``fine`` their fine
    cells in the set. Returns a piece id for every plain cell and every
    fine cell (0 outside the set); a piece may run through both levels.
    Fine cells of a refined cell join each other inside it, the plain
    cell or refined cell's fine cells beside them, and those above and
    below.
    """
    plane = ny * nx
    layers = plain.shape[0]
    coarse_id = components(plain)
    base = int(coarse_id.max()) if coarse_id.size else 0
    if keys.size == 0:
        return coarse_id, np.zeros(fine.shape, dtype=np.int64)
    fine_id = components(fine, connect_layers=False)
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
    covers: dict[tuple[int, int], Mask2] = {}
    near_of: dict[bytes, Mask2] = {}
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
        film = covers[window]
        void_here = Mask2.build(kind[k] == 0, *_fine_where(state, k, kind[k] == 2, lambda b: b == VOID))
        film = _and(state, film, void_here)
        if shadow is not None:
            film = _and(state, film, shadow.inverse())
        if opening is not None:
            film = _and(state, film, opening)
        films.append((k, film))
    keys_out, blocks_out = [], []
    for k, film in films:
        whole = film.coarse == 1
        state.labels[k][whole] = label
        if film.cells.size:
            keys = k * plane + film.cells
            blocks = state.blocks(keys)
            blocks[film.blocks] = label
            keys_out.append(keys)
            blocks_out.append(blocks)
    if keys_out:
        state.store(np.concatenate(keys_out), np.concatenate(blocks_out))
    state.consolidate()


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
    inside = _as_mask(state, opening) or Mask2.uniform(state.ny, state.nx, B)
    fine_columns = (state.labels == MIXED).any(axis=0) | (inside.coarse == 2)
    plain = (inside.coarse == 1) & ~fine_columns
    stop_plain = _walk(state.labels.reshape(n, -1), state.z, table, budget, plain.reshape(-1))
    cells = np.flatnonzero(fine_columns.reshape(-1))
    stops: list[np.ndarray] = []
    for part in _chunks(cells, n * B * B):
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
            size = part.size * B * B
            stop = stop_fine[at : at + size].reshape(part.size, B, B)
            at += size
            for k in range(state.n):
                keys = k * plane + part
                blocks = state.blocks(keys)
                gone = state.z[k] >= stop - Z_EPS
                if gone.any():
                    blocks[gone] = VOID
                    keys_out.append(keys)
                    blocks_out.append(blocks)
        if keys_out:
            state.store(np.concatenate(keys_out), np.concatenate(blocks_out))
    state.consolidate()


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
    # Which open space the ambient reaches is settled before the slabs are
    # cut: cutting changes no connection, and the cut stack is many times
    # taller.
    live = _live(state)
    step = max(float(dz or 0.0), state.cell)
    mask = _as_mask(state, opening)
    reach = float(budget) * float(table.max()) + step
    # One grid of planes for every bend, so the cuts near two boundaries
    # line up instead of leaving slivers between them.
    planes = set()
    for zb in _curving(state, table, live, mask):
        lo, hi = math.floor((zb - reach) / step), math.ceil((zb + reach) / step)
        planes.update(j * step for j in range(lo, hi + 1))
    lows, highs = state.z[:-1][holding], state.z[1:][holding]
    planes = [p for p in sorted(planes) if ((lows < p) & (p < highs)).any()]
    source = state.split(planes)
    live._reslab(np.concatenate([source, [live.n - 1]]))
    from . import voxel_wet

    front = voxel_wet.arrival(state, table, float(budget), mask, live, should_cancel, step=step)
    del live
    L = state.n
    z0, z1 = state.z[:-1].copy(), state.z[1:].copy()
    everywhere = np.ones(L, dtype=bool)
    # Where the front curves in a slab: it is not the same at the slab's
    # top as at its bottom (a level front is cut exactly instead).
    # each slab in as many layers as it takes to be no thicker than fine_z
    layers = np.ones(L, dtype=np.int64)
    if fine_z and fine_z < step:
        layers = np.maximum(1, np.ceil((z1 - z0) / float(fine_z) - 1e-9)).astype(np.int64)
        layers[(z1 - z0) > step * (1 + 1e-9)] = 1  # thicker slabs the front crosses upright
    cuts = [float(h) for h in front.flat_heights]
    if (layers > 1).any():
        half = 0.5 * (z1 - z0) / layers
        top = front.place(z1 - half, everywhere, skip_flat=True)
        bottom = front.place(z0 + half, everywhere, skip_flat=True)
        curved = (top[0] != bottom[0]).any(axis=(1, 2))
        curved[_unique(_differing(top[1:], bottom[1:], state.refine) // state.plane)] = True
        for k in np.flatnonzero(curved & (layers > 1)):
            cuts.extend(z0[k] + (z1[k] - z0[k]) * j / layers[k] for j in range(1, int(layers[k])))
    source = state.split(cuts) if cuts else np.arange(L)
    # Each new slab, placed at its middle, from the slab it was cut from.
    first = np.searchsorted(source, np.arange(L), side="left")
    count = np.bincount(source, minlength=L)
    middles = 0.5 * (state.z[:-1] + state.z[1:])
    B = state.refine
    for r in range(int(count.max()) if count.size else 0):
        subset = count > r
        heights = np.where(subset, middles[np.minimum(first + r, state.n - 1)], 0.0)
        whole, keys, fine = front.place(heights, subset)
        for k in np.flatnonzero(subset):
            target = first[k] + r
            state.labels[target][whole[k]] = fill
        if keys.size:
            k, cell = np.divmod(keys, state.plane)
            new_keys = (first[k] + r) * state.plane + cell
            order = np.argsort(new_keys, kind="stable")
            new_keys, fine = new_keys[order], fine[order]
            blocks = state.blocks(new_keys)
            blocks[fine] = fill
            state.store(new_keys, blocks)
    state.consolidate()


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


def _live(state: VoxelState) -> "VoxelState":
    """Open space the ambient reaches, as a two-level field over the stack
    plus one slab on top for the ambient itself (1 where reached); the
    rest of the open space is a sealed cavity."""
    plain = np.concatenate([state.labels == VOID, np.ones((1, state.ny, state.nx), dtype=bool)])
    fine = (state.pool == VOID)[state.brick_ref]
    coarse_id, fine_id = components2(plain, state.brick_keys, fine, state.nx, state.ny)
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
        if axis == "y":
            ix = state.nx - 1 - ix
            state.pool = np.ascontiguousarray(state.pool[:, :, ::-1])
        else:
            iy = state.ny - 1 - iy
            state.pool = np.ascontiguousarray(state.pool[:, ::-1, :])
        state._pool_hash = _brick_hash(state.pool)
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
        return Mask2.build(coarse, np.zeros(0, np.int64), np.zeros((0, 1, 1), bool))
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
    return Mask2.build(coarse, cells, blocks)


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


def top_view(
    state: VoxelState,
    colors: Mapping[str, str],
    rgb,
    *,
    hidden: Sequence[str] = (),
    steps: bool = True,
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    x_min, y_min, x_max, y_max = state.bounds
    top = max(z_max, state.top + state.z_offset)
    slabs = np.arange(state.n)[:, None]
    if line is not None:
        (ax, ay), (bx, by) = line
        length = float(math.hypot(bx - ax, by - ay))
        if length <= 0.0:
            raise VoxelError("a section line needs two distinct points")
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
    """Closed box meshes for every material, faces joined into rectangles.

    Each material's triangles face outward; a face against another
    material carries that material's place in the mesh order, and is left
    out unless ``buried`` asks for it.

    The mesh is conforming: joining cells into rectangles leaves corners
    of one rectangle part-way along the edge of the next (a T-junction),
    and a GPU draws the two sides of such an edge along slightly
    different lines, which shows as pixel cracks. So every corner that
    lies on another drawn face's edge -- of any material -- is put into
    that edge, and a face with such points is fanned from its centre.
    Corners are on the lattice of cell boundaries, so this is exact
    integer work.

    On two levels the lattice is the fine one: faces between bulk cells
    are found and joined on the bulk grid, and faces with a refined cell
    on either side fine square by fine square, joined into rectangles the
    same way.
    """
    state = shown(state)
    B = state.refine
    order = state.present()
    place = np.full(_OUTSIDE + 1, -1, dtype=np.int64)
    for index, name in enumerate(order):
        place[state.known_id(name)] = index
    n = state.n
    owners: list[np.ndarray] = []
    others: list[np.ndarray] = []
    lattice: list[np.ndarray] = []  # (Q, 4, 3) corners as (i, j, b), counter-clockwise from outside

    def add(owner, neighbour, corners, scale=B):
        keep = (neighbour == VOID) | (neighbour == _OUTSIDE) | buried
        corners = corners[keep].astype(np.int64)
        corners[:, :, :2] *= scale  # onto the fine lattice
        owners.append(owner[keep].astype(np.int64))
        others.append(neighbour[keep].astype(np.int64))
        lattice.append(corners)

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
            add(own, nb, corners)
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
                add(own, nb, corners)
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
                add(own, nb, corners)
    if state.brick_keys.size:
        for own, nb, corners in _fine_faces(state):
            add(own, nb, corners, scale=1)
    if not owners:
        return {}
    owner = np.concatenate(owners)
    neighbour = np.concatenate(others)
    corners = np.concatenate(lattice)
    x_min, y_min, _, _ = state.bounds

    def place_of(points):
        return np.stack(
            [
                x_min + points[:, 0] * state.fine_x,
                y_min + points[:, 1] * state.fine_y,
                state.z[points[:, 2]],
            ],
            axis=1,
        )

    coordinates, triangles, of_quad = _conforming(corners, place_of)
    coordinates = coordinates.astype(np.float32)
    meshes: dict[str, tuple[np.ndarray, ...]] = {}
    tri_owner = owner[of_quad]
    tri_other = neighbour[of_quad]
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


def _fine_faces(state: VoxelState):
    """Faces with a refined cell on either side, as rectangles on the fine
    lattice: (owner, neighbour, corners) per orientation and sense, the
    corners counter-clockwise seen from outside the owner."""
    B, n, nx, ny, plane = state.refine, state.n, state.nx, state.ny, state.plane
    labels = state.labels
    # Unit squares: (owner, neighbour, orientation, sense, plane, u, v).
    # Orientation 0: x = plane, u along y, v the slab; 1: y = plane, u
    # along x, v the slab; 2: z = slab boundary plane, u along x, v along y.
    units: list[np.ndarray] = []

    def blocks(k, cells, fill):
        if 0 <= k < n:
            return state.blocks(k * plane + cells).astype(np.int16)
        return np.full((cells.size, B, B), fill, dtype=np.int16)

    def emit(a, b, orient, line, u, v):
        # a sits on the negative side of the face, b on the positive one
        for owner, other, sense in ((a, b, 1), (b, a, 0)):
            face = (owner != other) & (owner != VOID) & (owner != _OUTSIDE)
            if face.any():
                count = int(face.sum())
                units.append(np.stack([
                    owner[face], other[face], np.full(count, orient), np.full(count, sense),
                    np.broadcast_to(line, face.shape)[face], np.broadcast_to(u, face.shape)[face],
                    np.broadcast_to(v, face.shape)[face],
                ], 1).astype(np.int64))

    fine = np.arange(B)
    for b in range(n + 1):
        below = labels[b - 1] if b > 0 else None
        above = labels[b] if b < n else None
        hit = np.zeros((ny, nx), dtype=bool)
        if below is not None:
            hit |= below == MIXED
        if above is not None:
            hit |= above == MIXED
        cells = np.flatnonzero(hit)
        if cells.size == 0:
            continue
        iy, ix = np.divmod(cells, nx)
        u = ix[:, None, None] * B + fine[None, None, :]
        v = iy[:, None, None] * B + fine[None, :, None]
        emit(blocks(b - 1, cells, _OUTSIDE), blocks(b, cells, VOID), 2, np.int64(b), u, v)
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
                pair = pad[:, :-1] | pad[:, 1:]  # (ny, nx + 1): wall line i between i-1 and i
            else:
                pad = np.pad(mixed, ((1, 1), (0, 0)))
                pair = pad[:-1, :] | pad[1:, :]
            r, c = np.nonzero(pair)
            if axis == 0:
                low_ok, high_ok = c > 0, c < nx
                low_cell = r * nx + np.clip(c - 1, 0, nx - 1)
                high_cell = r * nx + np.clip(c, 0, nx - 1)
            else:
                low_ok, high_ok = r > 0, r < ny
                low_cell = np.clip(r - 1, 0, ny - 1) * nx + c
                high_cell = np.clip(r, 0, ny - 1) * nx + c
            low = blocks(k, low_cell, _OUTSIDE)
            high = blocks(k, high_cell, _OUTSIDE)
            low[~low_ok] = _OUTSIDE
            high[~high_ok] = _OUTSIDE
            if axis == 0:
                a, bb = low[:, :, -1], high[:, :, 0]  # (m, B) along y
                line = (c * B)[:, None]
                u = r[:, None] * B + fine[None, :]
            else:
                a, bb = low[:, -1, :], high[:, 0, :]  # (m, B) along x
                line = (r * B)[:, None]
                u = c[:, None] * B + fine[None, :]
            emit(a, bb, axis, line, u, np.int64(k))
            # inside refined cells
            cells = np.flatnonzero(mixed.reshape(-1))
            iy, ix = np.divmod(cells, nx)
            data = blocks(k, cells, VOID)
            if axis == 0:
                a, bb = data[:, :, :-1], data[:, :, 1:]  # (m, B, B-1): row, wall j+1
                line = ix[:, None, None] * B + fine[None, None, 1:]
                u = iy[:, None, None] * B + fine[None, :, None]
            else:
                a, bb = data[:, :-1, :], data[:, 1:, :]  # (m, B-1, B)
                line = iy[:, None, None] * B + fine[None, 1:, None]
                u = ix[:, None, None] * B + fine[None, None, :]
            emit(a, bb, axis, line, u, np.int64(k))
    if not units:
        return []
    table = np.concatenate(units)
    # Join along u (same everything else and v), then along v.
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
    u0, u1, v0, v1 = runs[begin, 5], runs[begin, 6], runs[begin, 7], runs[end, 7] + 1

    def quad(*corners):
        return np.stack([np.stack(corner, -1) for corner in corners], axis=1)

    out = []
    for o, sn in itertools.product((0, 1, 2), (1, 0)):
        pick = (orient == o) & (sense == sn)
        if not pick.any():
            continue
        L, a0, a1, b0, b1 = line[pick], u0[pick], u1[pick], v0[pick], v1[pick]
        if o == 2:  # u = x, v = y, at slab boundary L
            corners = (quad((a0, b0, L), (a1, b0, L), (a1, b1, L), (a0, b1, L)) if sn
                       else quad((a0, b0, L), (a0, b1, L), (a1, b1, L), (a1, b0, L)))
        elif o == 0:  # x = L, u = y, v = slab
            corners = (quad((L, a0, b0), (L, a1, b0), (L, a1, b1), (L, a0, b1)) if sn
                       else quad((L, a0, b0), (L, a0, b1), (L, a1, b1), (L, a1, b0)))
        else:  # y = L, u = x, v = slab
            corners = (quad((a0, L, b0), (a0, L, b1), (a1, L, b1), (a1, L, b0)) if sn
                       else quad((a0, L, b0), (a1, L, b0), (a1, L, b1), (a0, L, b1)))
        out.append((own[pick], nb[pick], corners))
    return out


def _conforming(corners: np.ndarray, place_of) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Triangles for quads given by lattice corners, with every corner that
    lies inside another quad's edge added to that edge.

    ``place_of`` turns lattice points into coordinates. Returns the vertex
    coordinates, the triangles (indices into them) and the quad each
    triangle came from.
    """
    count = corners.shape[0]
    points, point_of = _unique_rows(corners.reshape(-1, 3))
    point_of = point_of.reshape(count, 4)
    # Every edge is parallel to one axis; along it only that coordinate
    # moves. Points are looked up by (axis, the other two coordinates,
    # position along the axis), sorted once per axis.
    scale = int(points.max()) + 2 if points.size else 2
    extras_at: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []  # (quad, edge, point) inserted
    for axis in range(3):
        other = [a for a in range(3) if a != axis]
        line_key = points[:, other[0]] * scale + points[:, other[1]]
        sort_key = line_key * scale + points[:, axis]
        order = np.argsort(sort_key, kind="stable")
        sorted_key = sort_key[order]
        for e in range(4):
            a = corners[:, e]
            b = corners[:, (e + 1) % 4]
            along = a[:, axis] != b[:, axis]
            if not along.any():
                continue
            q = np.flatnonzero(along)
            lo_pos = np.minimum(a[q, axis], b[q, axis])
            hi_pos = np.maximum(a[q, axis], b[q, axis])
            key = a[q, other[0]] * scale + a[q, other[1]]
            lo = np.searchsorted(sorted_key, key * scale + lo_pos, side="right")
            hi = np.searchsorted(sorted_key, key * scale + hi_pos, side="left")
            many = hi - lo
            has = many > 0
            if not has.any():
                continue
            q, lo, many = q[has], lo[has], many[has]
            forward = (b[q, axis] > a[q, axis])
            # Ragged ranges, flattened: quad, rank along the edge, point.
            repeat = np.repeat(np.arange(q.size), many)
            offset = np.arange(repeat.size) - np.repeat(np.cumsum(many) - many, many)
            rank = np.where(forward[repeat], offset, many[repeat] - 1 - offset)
            inserted = order[lo[repeat] + offset]
            extras_at.append((q[repeat], np.full(repeat.size, e) * 1_000_000 + 1 + rank, inserted))
    base = np.arange(count)
    if not extras_at:
        triangles = np.concatenate([point_of[:, [0, 1, 2]], point_of[:, [0, 2, 3]]])
        return place_of(points), triangles, np.concatenate([base, base])
    quad_of = np.concatenate([e[0] for e in extras_at])
    slot = np.concatenate([e[1] for e in extras_at])
    point = np.concatenate([e[2] for e in extras_at])
    split = np.zeros(count, dtype=bool)
    split[quad_of] = True
    plain = np.flatnonzero(~split)
    triangles = [point_of[plain][:, [0, 1, 2]], point_of[plain][:, [0, 2, 3]]]
    owners = [plain, plain]
    # The split quads: corners and inserted points in order round the
    # quad, fanned from a new vertex at the centre -- the midpoint of two
    # opposite corners, in real coordinates, since slabs are not evenly
    # spaced in z.
    fanned = np.flatnonzero(split)
    loop_quad = np.concatenate([np.repeat(fanned, 4), quad_of])
    loop_slot = np.concatenate([np.tile(np.arange(4) * 1_000_000, fanned.size), slot])
    loop_point = np.concatenate([point_of[fanned].ravel(), point])
    order = np.lexsort((loop_slot, loop_quad))
    loop_quad, loop_point = loop_quad[order], loop_point[order]
    first = np.flatnonzero(np.concatenate([[True], loop_quad[1:] != loop_quad[:-1]]))
    last = np.concatenate([first[1:], [loop_quad.size]]) - 1
    following = np.arange(loop_quad.size) + 1
    following[last] = first
    located = place_of(points)
    centres = 0.5 * (place_of(corners[fanned][:, 0]) + place_of(corners[fanned][:, 2]))
    lookup = np.full(count, -1, dtype=np.int64)
    lookup[fanned] = located.shape[0] + np.arange(fanned.size)
    all_points = np.concatenate([located, centres])
    triangles.append(np.stack([lookup[loop_quad], loop_point, loop_point[following]], axis=1))
    owners.append(loop_quad)
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
        total += part.labels.nbytes + part.pool.nbytes + part.brick_ref.nbytes + part.brick_keys.nbytes
    for meshes in state.meshes.values():
        for arrays in meshes.values():
            total += sum(array.nbytes for array in arrays)
    return total


# -- steps -----------------------------------------------------------------


def grid_size(project: ProjectDefinition) -> tuple[float, int]:
    """The bulk cell edge and the refinement for a project.

    The bulk is the window over :data:`TARGET_CELLS`. Boundaries are
    refined to the project's XY resolution when that is finer: ``refine``
    fine cells per bulk cell, the least power of two that makes a fine
    cell no larger than the resolution asked for, up to :data:`MAX_REFINE`."""
    from . import slab

    x_min, y_min, x_max, y_max = slab._window(project)
    span = max(x_max - x_min, y_max - y_min)
    cell = span / TARGET_CELLS
    resolution = slab.resolution_xy_um(project)
    refine = 1
    # powers of two, so a picture can take every second, fourth... fine cell
    while refine < MAX_REFINE and cell / refine > resolution * (1 + 1e-9):
        refine *= 2
    return cell, refine


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
    z_fine = slab.resolution_um(project)
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
                etch_vertical(new, rates, budget, opening)
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
