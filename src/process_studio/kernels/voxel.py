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

What it gives up is sub-cell accuracy sideways. An edge lands on the grid,
within half a cell of where it should be, and a round hole is drawn in
cells. Heights are exact. The cell is the project window over
:data:`TARGET_CELLS` (or the project's XY resolution when that is finer),
so a 1.6 um window is 3.1 nm cells; a sidewall film thinner than a cell is
kept one cell thick rather than dropped, because a liner that disappears
is a worse lie than one drawn a little thick.

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
* **Isotropic etch and oxidation.** The etchant is in the void connected to
  the ambient (with a mask, pre-existing void outside the mask's columns is
  covered; void the etch makes is open). Each connected piece of a target
  loses every cell within ``d`` of the void touching *that piece*, with the
  same square-in-z rule as a film. Barriers are respected because only
  surfaces of the piece itself start its etch: a nitride layer is not
  etched through the oxide above it from a cavity on the other side. The
  approximation is inside one piece: the distance is a straight line, so a
  piece that folds back around a barrier can be etched across the fold. For
  layers etched from a trench or a hole -- the usual case -- the straight
  line is the path. Several targets are etched from the deepest down, each
  from the void the ones before it opened. An oxidation relabels the cells
  instead of emptying them.
* **CMP** removes everything above the plane; **flip** turns the stack over
  and mirrors it, exactly as the polygon slabs do.
"""

from __future__ import annotations

import base64
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

#: Label of an empty cell.
VOID = 0

#: How a stored voxel state starts, so the kernel can tell it from a
#: polygon state stored under the same suffix.
MAGIC = b"PSVOXEL1"

#: Cells across the longer side of the window, unless the project's XY
#: resolution asks for finer ones.
TARGET_CELLS = 512

#: Never finer than this many cells across: memory is cells x slabs.
MAX_CELLS = 1024

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


# -- the state -------------------------------------------------------------


class VoxelState:
    """A stack of slabs, each a grid of material labels.

    ``z`` holds the slab boundaries from the floor (0) to the top, so slab
    ``k`` spans ``z[k]`` to ``z[k + 1]`` and holds ``labels[k]``. Label
    ``i > 0`` is ``materials[i - 1]``. Row ``iy`` of a grid is at
    ``y_min + (iy + 0.5) * cell_y``.
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
    ) -> None:
        self.bounds = tuple(float(value) for value in bounds)
        self.nx = int(nx)
        self.ny = int(ny)
        self.z = np.asarray(z, dtype=np.float64)
        self.labels = np.ascontiguousarray(labels, dtype=np.uint8)
        self.materials = list(materials)
        self.z_offset = float(z_offset)
        #: Where this state was stored, if it was.
        self.path: Path | None = None
        #: Display meshes by ``buried``, once built.
        self.meshes: dict[bool, dict[str, tuple[np.ndarray, ...]]] = {}
        self.mesh_lock = threading.Lock()

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

    # materials

    def material_id(self, name: str) -> int:
        """The label of ``name``, adding it to the table when it is new."""
        if name in self.materials:
            return self.materials.index(name) + 1
        if len(self.materials) >= 255:
            raise VoxelError("the voxel model holds at most 255 materials")
        self.materials.append(name)
        return len(self.materials)

    def known_id(self, name: str) -> int | None:
        return self.materials.index(name) + 1 if name in self.materials else None

    def present(self) -> list[str]:
        """Materials that own cells, in the order they first appear from the floor up."""
        order: list[str] = []
        seen: set[int] = set()
        for k in range(self.n):
            for label in np.unique(self.labels[k]):
                label = int(label)
                if label != VOID and label not in seen:
                    seen.add(label)
                    order.append(self.materials[label - 1])
        return order

    def volume(self, name: str) -> float:
        label = self.known_id(name)
        if label is None:
            return 0.0
        counts = (self.labels == label).sum(axis=(1, 2))
        return float(np.sum(counts * np.diff(self.z))) * self.cell_x * self.cell_y

    # structure

    def copy(self) -> "VoxelState":
        return VoxelState(
            self.bounds, self.nx, self.ny, self.z.copy(), self.labels.copy(),
            self.materials, self.z_offset,
        )

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
        self.labels = np.ascontiguousarray(self.labels[source])
        return source

    def extend(self, height: float) -> None:
        """Add empty room above the top, up to ``height``."""
        height = _z(height)
        if height <= self.top + Z_EPS:
            return
        self.z = np.append(self.z, height)
        void = np.zeros((1, self.ny, self.nx), dtype=np.uint8)
        self.labels = np.concatenate([self.labels, void], axis=0)

    def consolidate(self) -> None:
        """Merge neighbouring slabs that hold the same grid; drop empty ones on top."""
        if self.n == 0:
            return
        count = self.n
        while count > 0 and not self.labels[count - 1].any():
            count -= 1
        labels = self.labels[:count]
        z = self.z[: count + 1]
        if count > 1:
            same = np.all(labels[1:] == labels[:-1], axis=(1, 2))
            keep = np.concatenate([[True], ~same])
            boundaries = np.concatenate([z[:-1][keep], z[-1:]])
            labels = labels[keep]
            z = boundaries
        self.labels = np.ascontiguousarray(labels)
        self.z = np.asarray(z, dtype=np.float64)

    # storage

    def save(self, path: Path) -> None:
        buffer = io.BytesIO()
        np.savez_compressed(
            buffer,
            z=self.z,
            labels=self.labels,
            bounds=np.array(self.bounds, dtype=np.float64),
            grid=np.array([self.nx, self.ny], dtype=np.int64),
            materials=np.array(self.materials, dtype=str),
            z_offset=np.array(self.z_offset, dtype=np.float64),
        )
        Path(path).write_bytes(MAGIC + buffer.getvalue())
        self.path = Path(path)

    @classmethod
    def load(cls, path: Path) -> "VoxelState":
        raw = Path(path).read_bytes()
        if not raw.startswith(MAGIC):
            raise VoxelError(f"{Path(path).name} is not a stored voxel state")
        with np.load(io.BytesIO(raw[len(MAGIC):]), allow_pickle=False) as stored:
            nx, ny = (int(value) for value in stored["grid"])
            state = cls(
                tuple(float(value) for value in stored["bounds"]),
                nx,
                ny,
                stored["z"],
                stored["labels"],
                [str(name) for name in stored["materials"]],
                float(stored["z_offset"]),
            )
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


def within(seed: np.ndarray, radius: float, cell_x: float, cell_y: float) -> np.ndarray:
    """Cells whose centre is within ``radius`` of a seed centre in the same
    grid, for a stack of grids ``(layers, ny, nx)``.

    Exact Euclidean distance, as a test: the distance along x to the
    nearest seed in each row is found in one pass, and a cell is within
    reach when some row ``m`` away has a seed within
    ``sqrt(radius^2 - (m * cell_y)^2)`` along x of it.
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
        np.less_equal(distance, np.float32(radius), out=reach)
        for m in range(1, steps + 1):
            half = math.sqrt(max(radius * radius - (m * cell_y) ** 2, 0.0))
            near = distance <= np.float32(half)
            reach[:, m:, :] |= near[:, :-m, :]
            reach[:, :-m, :] |= near[:, m:, :]
    return out


def components(mask: np.ndarray) -> np.ndarray:
    """Label the 6-connected pieces of a stack of grids (0 = not in the mask).

    Runs along x are found first, then runs that touch in the row above or
    in the slab above are joined; the join is a union-find done on arrays,
    hooking roots to the smaller root and jumping pointers until nothing
    moves. Labels are positive but not consecutive.
    """
    layers, rows, cols = mask.shape
    flat = mask.reshape(-1, cols)
    starts = flat.copy()
    starts[:, 1:] &= ~flat[:, :-1]
    run = np.cumsum(starts.ravel(), dtype=np.int32).reshape(flat.shape)
    run[~flat] = 0
    count = int(run.max()) if run.size else 0
    if count == 0:
        return np.zeros(mask.shape, dtype=np.int32)
    run3 = run.reshape(layers, rows, cols)

    heads: list[np.ndarray] = []
    tails: list[np.ndarray] = []
    for below, above in ((run3[:, :-1, :], run3[:, 1:, :]), (run3[:-1], run3[1:])):
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
    parent = np.arange(count + 1, dtype=np.int32)
    if heads:
        a = np.concatenate(heads)
        b = np.concatenate(tails)
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
    return parent[run3]


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


# -- processes -------------------------------------------------------------


def deposit(
    state: VoxelState,
    material: str,
    thickness: float,
    *,
    planar: bool,
    opening: np.ndarray | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Grow ``thickness`` of ``material`` over every surface (see the module notes)."""
    t = float(thickness)
    label = state.material_id(material)
    old_top = state.top
    planes = {b + t for b in state.z} | {b - t for b in state.z}
    state.extend(old_top + t)
    state.split(planes)
    solid = state.labels != VOID
    counts = np.zeros((state.n + 1, state.ny, state.nx), dtype=np.uint16)
    np.cumsum(solid, axis=0, dtype=np.uint16, out=counts[1:])
    shadow = None
    if planar:
        # Solid anywhere above a slab: the sky does not see that column.
        above = np.logical_or.accumulate(solid[::-1], axis=0)[::-1]
        shadow = np.zeros_like(solid)
        shadow[:-1] = above[1:]
    # A film is at least one cell thick sideways: thinner, it would vanish
    # from every wall and leave the floor and the top as its only trace.
    reach = max(t + 0.5 * state.cell, state.cell * 1.0001)
    targets = [k for k in range(state.n) if not solid[k].all()]
    batch = 8
    for start in range(0, len(targets), batch):
        _check(should_cancel, "a deposition")
        chosen = targets[start : start + batch]
        seeds = np.empty((len(chosen), state.ny, state.nx), dtype=bool)
        for index, k in enumerate(chosen):
            first, last = _window(state.z, k, t, below_only=planar)
            seeds[index] = counts[last + 1] > counts[first]
        near = within(seeds, reach, state.cell_x, state.cell_y)
        for index, k in enumerate(chosen):
            film = near[index] & ~solid[k]
            if shadow is not None:
                film &= ~shadow[k]
            if opening is not None:
                film &= opening
            state.labels[k][film] = label
    state.consolidate()


def etch_vertical(
    state: VoxelState,
    rates: Mapping[str, float],
    budget: float,
    opening: np.ndarray | None,
) -> None:
    """Etch straight down inside ``opening`` for ``budget`` (see the module notes)."""
    table = np.zeros(256, dtype=np.float64)
    for name, rate in rates.items():
        label = state.known_id(name)
        if label is not None:
            table[label] = max(float(rate), 0.0)
    shape = (state.ny, state.nx)
    inside = np.ones(shape, dtype=bool) if opening is None else opening.copy()
    alive = inside.copy()
    remaining = np.full(shape, float(budget))
    stop = np.full(shape, np.inf)
    stop[inside] = state.top
    for k in range(state.n - 1, -1, -1):
        if not alive.any():
            break
        grid = state.labels[k]
        z0, z1 = float(state.z[k]), float(state.z[k + 1])
        void = grid == VOID
        passing = alive & void
        stop[passing] = z0
        rate = table[grid]
        alive &= ~(~void & (rate <= 0.0))
        etching = alive & ~void
        if not etching.any():
            continue
        cost = np.zeros(shape)
        cost[etching] = (z1 - z0) / rate[etching]
        through = etching & (remaining >= cost - 1e-12)
        stop[through] = z0
        remaining[through] -= cost[through]
        partial = etching & ~through
        stop[partial] = z1 - remaining[partial] * rate[partial]
        alive &= ~partial
    heights = np.unique(np.round(stop[np.isfinite(stop)], Z_DECIMALS))
    state.split(heights)
    for k in range(state.n):
        gone = inside & (state.z[k] >= stop - Z_EPS)
        state.labels[k][gone] = VOID
    state.consolidate()


def etch_isotropic(
    state: VoxelState,
    depths: Mapping[str, float],
    opening: np.ndarray | None,
    *,
    product: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Take ``depth`` of each target from its exposed surface (see the module notes).

    With ``product`` the cells taken become that material (an oxidation)
    instead of void.
    """
    targets = [
        (label, float(depth))
        for name, depth in depths.items()
        if float(depth) > 0.0 and (label := state.known_id(name)) is not None
    ]
    # The deepest first: the void it opens is where the shallower ones start.
    targets.sort(key=lambda item: -item[1])
    product_label = state.material_id(product) if product is not None else None
    columns = np.ones((state.ny, state.nx), dtype=bool) if opening is None else opening
    # Void the etch itself makes is open to the etchant even under the mask.
    made = np.zeros(state.labels.shape, dtype=bool)
    for label, depth in targets:
        _check(should_cancel, "an isotropic etch")
        if not (state.labels == label).any():
            continue
        live = _live_void(state, columns, made)
        target = state.labels == label
        pieces = components(target)
        seeds = live & _neighbours(np.concatenate([target, np.zeros_like(target[:1])]))
        # Split the target's heights where a seed slab's reach ends, so a
        # slab is either wholly within reach in z or wholly out of it.
        seed_slabs = np.flatnonzero(seeds.any(axis=(1, 2)))
        planes = []
        for j in seed_slabs:
            z0 = float(state.z[j]) if j < state.n else state.top
            planes.append(z0 - depth)
            if j < state.n:
                planes.append(float(state.z[j + 1]) + depth)
        source = state.split(planes)
        made = made[source]
        live = np.concatenate([live[:-1][source], live[-1:]])
        pieces = pieces[source]
        target = state.labels == label
        seeds = live & _neighbours(np.concatenate([target, np.zeros_like(target[:1])]))
        taken = _isotropic_reach(state, pieces, seeds, depth, should_cancel)
        state.labels[taken] = VOID if product_label is None else product_label
        if product_label is None:
            made |= taken
    state.consolidate()


def _live_void(state: VoxelState, columns: np.ndarray, made: np.ndarray) -> np.ndarray:
    """Void connected to the ambient, as a stack with one extra slab on top
    standing for the space above the device."""
    void = state.labels == VOID
    open_void = void & (columns[None] | made)
    stack = np.concatenate([open_void, columns[None]], axis=0)
    labels = components(stack)
    sources = np.unique(labels[-1][labels[-1] > 0])
    if sources.size == 0:
        return np.zeros(stack.shape, dtype=bool)
    return np.isin(labels, sources)


def _isotropic_reach(
    state: VoxelState,
    pieces: np.ndarray,
    seeds: np.ndarray,
    depth: float,
    should_cancel: Callable[[], bool] | None,
) -> np.ndarray:
    """Cells of each piece within ``depth`` of the seeds touching that piece."""
    taken = np.zeros(state.labels.shape, dtype=bool)
    reach = depth + 0.5 * state.cell
    top_gap = np.concatenate([state.z, [np.inf]])
    ids = np.flatnonzero(pieces.ravel())
    if ids.size == 0:
        return taken
    values = pieces.ravel()[ids]
    order = np.argsort(values, kind="stable")
    ids, values = ids[order], values[order]
    starts = np.flatnonzero(np.concatenate([[True], values[1:] != values[:-1]]))
    ends = np.concatenate([starts[1:], [values.size]])
    layers_total = state.n
    for begin, end in zip(starts, ends):
        _check(should_cancel, "an isotropic etch")
        piece = int(values[begin])
        k_idx, y_idx, x_idx = np.unravel_index(ids[begin:end], pieces.shape)
        k0, k1 = int(k_idx.min()), int(k_idx.max())
        y0, y1 = max(int(y_idx.min()) - 1, 0), min(int(y_idx.max()) + 2, state.ny)
        x0, x1 = max(int(x_idx.min()) - 1, 0), min(int(x_idx.max()) + 2, state.nx)
        s0, s1 = max(k0 - 1, 0), min(k1 + 2, layers_total + 1)
        own = pieces[k0 : k1 + 1, y0:y1, x0:x1] == piece
        # Seeds of this piece: open void next to one of its own cells.
        padded = np.zeros((s1 - s0, y1 - y0, x1 - x0), dtype=bool)
        padded[k0 - s0 : k0 - s0 + own.shape[0]] = own
        mine = seeds[s0:s1, y0:y1, x0:x1] & _neighbours(padded)
        if not mine.any():
            continue
        seed_layers = [j for j in range(s0, s1) if mine[j - s0].any()]
        stacked = np.zeros(own.shape, dtype=bool)
        for k in range(k0, k1 + 1):
            for j in seed_layers:
                # Gap between slab j (the top slab stands for everything
                # above the device) and slab k; strictly under the depth.
                if j < k:
                    gap = state.z[k] - top_gap[j + 1]
                elif j > k:
                    gap = top_gap[j] - state.z[k + 1]
                else:
                    gap = 0.0
                if gap < depth - Z_EPS:
                    stacked[k - k0] |= mine[j - s0]
        near = within(stacked, reach, state.cell_x, state.cell_y)
        taken[k0 : k1 + 1, y0:y1, x0:x1] |= near & own
    return taken


def cmp(state: VoxelState, height: float) -> None:
    """Remove everything above ``height`` (from the floor)."""
    state.split([height])
    keep = int(np.searchsorted(state.z, _z(height) - Z_EPS, side="left"))
    keep = max(1, min(keep, state.n))
    state.z = state.z[: keep + 1]
    state.labels = np.ascontiguousarray(state.labels[:keep])
    state.consolidate()


def flip(state: VoxelState, axis: str) -> None:
    """Turn the stack over about the x or the y axis (see ``slab._flip``)."""
    top = state.top
    if not state.labels.any():
        raise VoxelError("there is nothing to flip: this state has no material")
    labels = state.labels[::-1]
    labels = labels[:, :, ::-1] if axis == "y" else labels[:, ::-1, :]
    state.labels = np.ascontiguousarray(labels)
    state.z = np.array([_z(top - value) for value in state.z[::-1]], dtype=np.float64)
    state.consolidate()


# -- masks -----------------------------------------------------------------


def rasterize(state: VoxelState, geometry) -> np.ndarray:
    """Cells whose centre lies in ``geometry``."""
    xs, ys = state.centres()
    grid_x, grid_y = np.meshgrid(xs, ys)
    prepared = shapely.make_valid(geometry)
    shapely.prepare(prepared)
    inside = shapely.intersects_xy(prepared, grid_x.ravel(), grid_y.ravel())
    return inside.reshape(state.ny, state.nx)


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


def _scale(state: VoxelState) -> int:
    return max(1, round(BASE_PIXELS / max(state.nx, state.ny)))


def top_view(
    state: VoxelState,
    colors: Mapping[str, str],
    rgb,
    *,
    hidden: Sequence[str] = (),
    steps: bool = True,
) -> dict[str, Any]:
    hidden_ids = {state.known_id(name) for name in hidden} - {None}
    visible = np.ones(256, dtype=bool)
    visible[VOID] = False
    for label in hidden_ids:
        visible[label] = False
    colour = np.zeros((state.ny, state.nx), dtype=np.uint8)
    height = np.full((state.ny, state.nx), -np.inf)
    found = np.zeros((state.ny, state.nx), dtype=bool)
    for k in range(state.n - 1, -1, -1):
        grid = state.labels[k]
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
    factor = _scale(state)
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
    width = max(2, min(2600, columns.shape[1] * _scale(state)))
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
    if line is not None:
        (ax, ay), (bx, by) = line
        length = float(math.hypot(bx - ax, by - ay))
        if length <= 0.0:
            raise VoxelError("a section line needs two distinct points")
        samples = max(2, int(math.ceil(length / (0.5 * state.cell))))
        t = (np.arange(samples) + 0.5) / samples
        px, py = ax + t * (bx - ax), ay + t * (by - ay)
        ix = np.clip(((px - x_min) / state.cell_x).astype(int), 0, state.nx - 1)
        iy = np.clip(((py - y_min) / state.cell_y).astype(int), 0, state.ny - 1)
        image, pixels_per_um = _section_image(
            state, state.labels[:, iy, ix], length, colors, rgb, top
        )
        return {
            "image": _png(image),
            "axis": "line",
            "position": 0.0,
            "index": 0,
            "interpolation": interpolation,
            "sampledSpacingUm": state.cell,
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
    if axis == "y":
        row = int(np.clip((cut - y_min) / state.cell_y, 0, state.ny - 1))
        columns = state.labels[:, row, :]
        horizontal = (x_min, x_max)
    else:
        col = int(np.clip((cut - x_min) / state.cell_x, 0, state.nx - 1))
        columns = state.labels[:, :, col]
        horizontal = (y_min, y_max)
    image, pixels_per_um = _section_image(
        state, columns, horizontal[1] - horizontal[0], colors, rgb, top
    )
    return {
        "image": _png(image),
        "axis": axis,
        "position": cut,
        "index": index,
        "interpolation": interpolation,
        "sampledSpacingUm": state.cell,
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
    """
    order = state.present()
    place = np.full(_OUTSIDE + 1, -1, dtype=np.int64)
    for index, name in enumerate(order):
        place[state.known_id(name)] = index
    x_min, y_min, _, _ = state.bounds
    cx, cy = state.cell_x, state.cell_y
    quads: dict[int, list[np.ndarray]] = {}
    against: dict[int, list[np.ndarray]] = {}

    def add(owner, neighbour, corners):
        # owner, neighbour: (Q,) labels; corners: (Q, 4, 3)
        keep = (neighbour == VOID) | (neighbour == _OUTSIDE) | buried
        owner, neighbour, corners = owner[keep], neighbour[keep], corners[keep]
        for label in np.unique(owner):
            chosen = owner == label
            quads.setdefault(int(label), []).append(corners[chosen])
            against.setdefault(int(label), []).append(neighbour[chosen])

    def faces_between(owner, neighbour, tag=None):
        # Keys only where a face is: most of the grid has none, and whole-
        # grid arithmetic was nearly all of the build.
        mask = (owner != neighbour) & (owner != VOID) & (owner != _OUTSIDE)
        keys = np.zeros(owner.shape, dtype=np.int32)
        values = owner[mask].astype(np.int32) * 512 + neighbour[mask].astype(np.int32) + 1
        if tag is not None:
            values = values * tag[0] + np.broadcast_to(tag[1], owner.shape)[mask].astype(np.int32)
        keys[mask] = values
        return keys

    def split(keys):
        keys = keys - 1
        return keys // 512, keys % 512

    n = state.n
    labels = state.labels.astype(np.int16)
    # Horizontal faces, at every slab boundary.
    for b in range(n + 1):
        below = labels[b - 1] if b > 0 else np.full((state.ny, state.nx), _OUTSIDE, dtype=np.int16)
        above = labels[b] if b < n else np.full((state.ny, state.nx), VOID, dtype=np.int16)
        z = state.z[b]
        for owner, neighbour, up in ((below, above, True), (above, below, False)):
            key, r0, r1, c0, c1 = _rectangles(faces_between(owner, neighbour))
            if key.size == 0:
                continue
            own, nb = split(key)
            xa, xb = x_min + c0 * cx, x_min + c1 * cx
            ya, yb = y_min + r0 * cy, y_min + r1 * cy
            zz = np.full(key.size, z)
            if up:
                corners = np.stack(
                    [np.stack(v, -1) for v in ((xa, ya, zz), (xb, ya, zz), (xb, yb, zz), (xa, yb, zz))],
                    axis=1,
                )
            else:
                corners = np.stack(
                    [np.stack(v, -1) for v in ((xa, ya, zz), (xa, yb, zz), (xb, yb, zz), (xb, ya, zz))],
                    axis=1,
                )
            add(own, nb, corners)
    # Walls facing x: between column i-1 and i, on every slab, joined along
    # y and up through the slabs.
    if n:
        edge = np.full((n, state.ny, 1), _OUTSIDE, dtype=np.int16)
        # One grid per wall line: rows are slabs, columns are y. The wall
        # line is folded into the key so that runs never join across lines.
        left = np.concatenate([edge, labels], axis=2).transpose(2, 0, 1)  # column i-1 for wall i
        right = np.concatenate([labels, edge], axis=2).transpose(2, 0, 1)  # column i for wall i
        walls = state.nx + 1
        line_of = np.arange(walls, dtype=np.int32)[:, None, None]
        for owner, neighbour, positive in ((left, right, True), (right, left, False)):
            tagged = faces_between(owner, neighbour, (walls, line_of))  # (nx+1, n, ny)
            key, r0, r1, c0, c1 = _rectangles(tagged.reshape(-1, state.ny))
            if key.size:
                line = key % walls
                own, nb = split(key // walls)
                slab0 = r0 - line * n
                slab1 = r1 - line * n
                xx = x_min + line * cx
                ya, yb = y_min + c0 * cy, y_min + c1 * cy
                za, zb = state.z[slab0], state.z[slab1]
                if positive:
                    corners = [(xx, ya, za), (xx, yb, za), (xx, yb, zb), (xx, ya, zb)]
                else:
                    corners = [(xx, ya, za), (xx, ya, zb), (xx, yb, zb), (xx, yb, za)]
                add(own, nb, np.stack([np.stack(v, -1) for v in corners], axis=1))
        # Walls facing y: between row j-1 and j.
        edge = np.full((n, 1, state.nx), _OUTSIDE, dtype=np.int16)
        low = np.concatenate([edge, labels], axis=1).transpose(1, 0, 2)
        high = np.concatenate([labels, edge], axis=1).transpose(1, 0, 2)
        walls = state.ny + 1
        line_of = np.arange(walls, dtype=np.int32)[:, None, None]
        for owner, neighbour, positive in ((low, high, True), (high, low, False)):
            tagged = faces_between(owner, neighbour, (walls, line_of))  # (ny+1, n, nx)
            key, r0, r1, c0, c1 = _rectangles(tagged.reshape(-1, state.nx))
            if key.size:
                line = key % walls
                own, nb = split(key // walls)
                slab0 = r0 - line * n
                slab1 = r1 - line * n
                yy = y_min + line * cy
                xa, xb = x_min + c0 * cx, x_min + c1 * cx
                za, zb = state.z[slab0], state.z[slab1]
                if positive:
                    corners = [(xa, yy, za), (xa, yy, zb), (xb, yy, zb), (xb, yy, za)]
                else:
                    corners = [(xa, yy, za), (xb, yy, za), (xb, yy, zb), (xa, yy, zb)]
                add(own, nb, np.stack([np.stack(v, -1) for v in corners], axis=1))

    meshes: dict[str, tuple[np.ndarray, ...]] = {}
    for name in order:
        label = state.known_id(name)
        if label not in quads:
            continue
        corners = np.concatenate(quads[label], axis=0)
        neighbour = np.concatenate(against[label], axis=0)
        count = corners.shape[0]
        vertices = corners.reshape(-1, 3).astype(np.float32)
        base = np.arange(count, dtype=np.uint32) * 4
        faces = np.empty((count * 2, 3), dtype=np.uint32)
        faces[0::2] = np.stack([base, base + 1, base + 2], axis=1)
        faces[1::2] = np.stack([base, base + 2, base + 3], axis=1)
        other = np.repeat(neighbour, 2)
        interface = ((other != VOID) & (other != _OUTSIDE)).astype(np.uint8)
        index = np.where(interface == 1, place[np.minimum(other, _OUTSIDE)], -1).astype(np.int16)
        meshes[name] = (vertices, faces, interface, index)
    return meshes


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
    total = 64 * 1024 + state.labels.nbytes
    for meshes in state.meshes.values():
        for arrays in meshes.values():
            total += sum(array.nbytes for array in arrays)
    return total


# -- steps -----------------------------------------------------------------


def cell_size(project: ProjectDefinition) -> float:
    """The cell edge for a project: the window over :data:`TARGET_CELLS`,
    or the project's XY resolution when that is finer, never finer than
    the window over :data:`MAX_CELLS`."""
    from . import slab

    x_min, y_min, x_max, y_max = slab._window(project)
    span = max(x_max - x_min, y_max - y_min)
    cell = min(span / TARGET_CELLS, slab.resolution_xy_um(project))
    return max(cell, span / MAX_CELLS)


def initial_state(project: ProjectDefinition) -> VoxelState:
    """The bare wafer: one substrate slab, its top face at z = 0."""
    from . import slab

    x_min, y_min, x_max, y_max = slab._window(project)
    z_offset = float(project.grid["z_min"])
    if -z_offset <= 0.0:
        raise slab.SlabError("the project window needs room below z = 0 for the substrate")
    cell = cell_size(project)
    nx = max(1, round((x_max - x_min) / cell))
    ny = max(1, round((y_max - y_min) / cell))
    labels = np.ones((1, ny, nx), dtype=np.uint8)
    return VoxelState(
        (x_min, y_min, x_max, y_max), nx, ny, np.array([0.0, _z(-z_offset)]), labels,
        [slab.SUBSTRATE_MATERIAL], z_offset,
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
            f"this mask's openings are narrower than one voxel cell ({state.cell * 1000:.3g} nm), "
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
                logger(f"VOXEL etch isotropic {text}")
                depths = {name: rate * budget for name, rate in rates.items() if rate > 0.0}
                etch_isotropic(new, depths, opening, should_cancel=should_cancel)
        elif kind is ProcessType.OXIDATION:
            opening = _opening(new, step, project, parameters, sketches)
            rates, budget, text = _budget(recipe, parameters, project, "oxidation")
            product = str(parameters.get("material") or recipe.output_material or "SiO2")
            if product in {name for name, rate in rates.items() if rate > 0.0}:
                raise slab.SlabError(f"{product} cannot be both oxidised and the oxide it becomes")
            logger(f"VOXEL oxidize {text} into {product}")
            depths = {name: rate * budget for name, rate in rates.items() if rate > 0.0}
            etch_isotropic(new, depths, opening, product=product, should_cancel=should_cancel)
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
    return new
