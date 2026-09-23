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
  a cell) so the front can curve. An oxidation relabels the cells instead
  of emptying them.
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
    rates: Mapping[str, float],
    budget: float,
    opening: np.ndarray | None,
    *,
    product: str | None = None,
    dz: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Etch by the etchant's arrival time (see the module notes).

    ``rates`` are per material and ``budget`` is the time they run for, in
    the same units: a depth of the reference material with relative rates,
    or minutes with rates per minute. With ``product`` the cells taken
    become that material (an oxidation) instead of void. ``dz`` is how
    finely a slab holding an etchable material is cut in z, so the front
    can curve there; never finer than a cell.
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
    if not holding.any():
        return
    product_label = state.material_id(product) if product is not None else None
    # Which open space the ambient reaches is settled before the slabs are
    # cut: cutting changes no connection, and the cut stack is many times
    # taller.
    live = _live(state)
    step = max(float(dz or 0.0), state.cell)
    planes = []
    for k in np.flatnonzero(holding):
        z0, z1 = float(state.z[k]), float(state.z[k + 1])
        parts = max(1, math.ceil((z1 - z0) / step - 1e-9))
        planes.extend(z0 + (z1 - z0) * j / parts for j in range(1, parts))
    source = state.split(planes)
    live = live[np.concatenate([source, [live.shape[0] - 1]])]
    taken = _arrival(state, table, float(budget), opening, live, should_cancel)
    state.labels[taken] = VOID if product_label is None else product_label
    state.consolidate()


#: What a cell is to the etchant.
_WALL, _ETCHABLE, _SOURCE, _CAVITY = 0, 1, 2, 3


def _live(state: VoxelState) -> np.ndarray:
    """Open space the ambient reaches, as the stack plus one slab on top for
    the ambient itself; the rest of the open space is a sealed cavity."""
    void = np.concatenate(
        [state.labels == VOID, np.ones((1, state.ny, state.nx), dtype=bool)]
    )
    pieces = components(void)
    ambient = np.unique(pieces[-1])
    return np.isin(pieces, ambient[ambient > 0])


class _Faces:
    """Where fronts entered: axis-aligned rectangles (a point when flat in
    every direction), each with the time the etchant stood on it.

    A cell's arrival time is the time on its face plus the straight-line
    distance to the face's nearest point over the rate of the cell's own
    material. Within one material that is the exact distance to the
    surface the etch started from, not a sum of grid steps.
    """

    def __init__(self) -> None:
        self.centre = np.zeros((0, 3))
        self.half = np.zeros((0, 3))
        self.time = np.zeros(0)

    def add(self, centre: np.ndarray, half: np.ndarray, time: np.ndarray) -> np.ndarray:
        first = self.time.size
        self.centre = np.concatenate([self.centre, centre])
        self.half = np.concatenate([self.half, half])
        self.time = np.concatenate([self.time, time])
        return np.arange(first, first + time.size, dtype=np.int64)

    def nearest(self, face: np.ndarray, point: np.ndarray) -> np.ndarray:
        """The point of each face nearest to each point."""
        low = self.centre[face] - self.half[face]
        high = self.centre[face] + self.half[face]
        return np.clip(point, low, high)


def _arrival(
    state: VoxelState,
    table: np.ndarray,
    budget: float,
    opening: np.ndarray | None,
    live: np.ndarray,
    should_cancel: Callable[[], bool] | None,
) -> np.ndarray:
    """Cells of an etchable material the etchant reaches within ``budget``.

    Cells are settled in order of arrival, a small time band at a time
    (a group marching method). A cell next to open space enters through
    the face it shares with it; a cell next to a settled cell of its own
    material inherits that cell's entry face, as long as the straight line
    to the face starts off through settled cells of the same material or
    open space -- the line of sight. When it does not, the path bends at
    the neighbour, which becomes a corner the front turns around. Crossing
    into another material starts a new face on the interface, at the time
    the front reached it. So the front is exact within a material in line
    of sight of where it entered, and goes round barriers rather than
    through them.
    """
    L, ny, nx = state.n, state.ny, state.nx
    plane = ny * nx
    labels = np.concatenate([state.labels, np.zeros((1, ny, nx), dtype=np.uint8)])
    void = labels == VOID
    columns = np.ones((ny, nx), dtype=bool) if opening is None else opening
    kind = np.full(labels.shape, _WALL, dtype=np.uint8)
    kind[(table[labels] > 0.0) & ~void] = _ETCHABLE
    # Open space outside the mask is under resist and stays a wall; a
    # sealed cavity fills with etchant the moment the front breaks in.
    kind[live & columns[None]] = _SOURCE
    kind[void & ~live] = _CAVITY
    kind = kind.ravel()
    flat_labels = labels.ravel()
    slowness = np.zeros(256)
    slowness[table > 0.0] = 1.0 / table[table > 0.0]
    x_min, y_min = state.bounds[0], state.bounds[1]
    hx, hy = state.cell_x, state.cell_y
    thick = np.concatenate([np.diff(state.z), [hx]])
    centre_z = np.concatenate([0.5 * (state.z[:-1] + state.z[1:]), [state.top + 0.5 * hx]])
    probe = 1.25 * min(hx, hy, float(np.min(thick)))

    size = labels.size
    T = np.full(size, np.inf)
    face_of = np.full(size, -1, dtype=np.int64)
    known = np.zeros(size, dtype=bool)
    queued = np.zeros(size, dtype=bool)
    faces = _Faces()
    sources = np.flatnonzero(kind == _SOURCE)
    T[sources] = 0.0
    known[sources] = True

    cavity_cells = cavity_ids = cavity_of = None
    if (kind == _CAVITY).any():
        cavity_of = components(void & ~live).ravel()
        cavity_cells = np.flatnonzero(kind == _CAVITY)
        ids = cavity_of[cavity_cells]
        order = np.argsort(ids, kind="stable")
        cavity_cells, cavity_ids = cavity_cells[order], ids[order]

    steps = ((-1, 0, -1), (1, 0, 1), (-nx, 1, -1), (nx, 1, 1), (-plane, 2, -1), (plane, 2, 1))

    def where(cells):
        k, rest = np.divmod(cells, plane)
        y, x = np.divmod(rest, nx)
        centre = np.stack(
            [x_min + (x + 0.5) * hx, y_min + (y + 0.5) * hy, centre_z[k]], axis=1
        )
        half = np.stack([np.full(cells.size, 0.5 * hx), np.full(cells.size, 0.5 * hy), 0.5 * thick[k]], axis=1)
        inside = (x > 0, x < nx - 1, y > 0, y < ny - 1, k > 0, k < L)
        return k, centre, half, inside

    def cell_at(points):
        """The cell holding each point, or -1 outside the stack."""
        ix = np.floor((points[:, 0] - x_min) / hx).astype(np.int64)
        iy = np.floor((points[:, 1] - y_min) / hy).astype(np.int64)
        iz = np.searchsorted(state.z, points[:, 2], side="right") - 1
        iz = np.where(points[:, 2] >= state.top, L, iz)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny) & (iz >= 0)
        return np.where(ok, iz * plane + iy * nx + ix, -1)

    def update(cells):
        """Best arrival for each cell from its settled neighbours, and how."""
        _k, p, half, inside = where(cells)
        s = slowness[flat_labels[cells]]
        best = np.full(cells.size, np.inf)
        # How the best candidate enters: an existing face, or a new face
        # (centre, half extents, time) that is created if it wins.
        how = np.full(cells.size, -1, dtype=np.int64)
        new_centre = np.zeros((cells.size, 3))
        new_half = np.zeros((cells.size, 3))
        new_time = np.zeros(cells.size)
        fresh = np.zeros(cells.size, dtype=bool)
        for (step, axis, sign), ok in zip(steps, inside):
            n = np.where(ok, cells + step, cells)
            settled = ok & known[n]
            if not settled.any():
                continue
            kn = kind[n]
            # Through the face shared with open space, or with another
            # material the front has reached (a new face on the interface).
            shared_centre = p.copy()
            shared_centre[:, axis] += sign * half[:, axis]
            shared_half = half.copy()
            shared_half[:, axis] = 0.0
            open_side = settled & ((kn == _SOURCE) | (kn == _CAVITY))
            other = settled & (kn == _ETCHABLE) & (flat_labels[n] != flat_labels[cells])
            enter = np.where(open_side, T[n], np.inf)
            if other.any():
                _kk, pn, halfn, _in = where(n[other])
                enter[other] = T[n[other]] + slowness[flat_labels[n[other]]] * halfn[:, axis]
            t = enter + s * half[:, axis]
            better = t < best
            if better.any():
                best[better] = t[better]
                how[better] = -1
                fresh[better] = True
                new_centre[better] = shared_centre[better]
                new_half[better] = shared_half[better]
                new_time[better] = enter[better]
            # Through a settled cell of the same material: its entry face,
            # if the line to it starts off through open or settled ground.
            same = settled & (kn == _ETCHABLE) & (flat_labels[n] == flat_labels[cells]) & (face_of[n] >= 0)
            if not same.any():
                continue
            idx = np.flatnonzero(same)
            f = face_of[n[idx]]
            target = faces.nearest(f, p[idx])
            gap = target - p[idx]
            length = np.sqrt((gap * gap).sum(axis=1))
            t = faces.time[f] + s[idx] * length
            look = np.ones(idx.size, dtype=bool)
            far = length > probe
            if far.any():
                ahead = p[idx[far]] + gap[far] * (probe / length[far])[:, None]
                c = cell_at(ahead)
                good = c >= 0
                cc = np.where(good, c, 0)
                clear = good & (
                    (known[cc] & (kind[cc] == _ETCHABLE) & (flat_labels[cc] == flat_labels[cells[idx[far]]]))
                    | (known[cc] & ((kind[cc] == _SOURCE) | (kind[cc] == _CAVITY)))
                    | (cc == cells[idx[far]])
                )
                look[far] = clear
            # Out of sight: the path bends at the neighbour.
            _kk, pn, _halfn, _in = where(n[idx[~look]])
            if (~look).any():
                corner_gap = pn - p[idx[~look]]
                t[~look] = T[n[idx[~look]]] + s[idx[~look]] * np.sqrt((corner_gap * corner_gap).sum(axis=1))
            better = t < best[idx]
            if better.any():
                chosen = idx[better]
                best[chosen] = t[better]
                sight = look[better]
                how[chosen[sight]] = f[better][sight]
                fresh[chosen[sight]] = False
                bend = chosen[~sight]
                if bend.size:
                    corners = np.flatnonzero(~look)
                    lookup = np.full(idx.size, -1)
                    lookup[corners] = np.arange(corners.size)
                    at = lookup[np.flatnonzero(better)[~sight]]
                    fresh[bend] = True
                    new_centre[bend] = pn[at]
                    new_half[bend] = 0.0
                    new_time[bend] = T[n[idx[~look]]][at]
        return best, how, fresh, new_centre, new_half, new_time

    def settle_candidates(cells):
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

    stamp = np.zeros(size, dtype=np.int32)

    def reachable(fronts):
        """Etchable cells next to ``fronts`` that are not settled, once each."""
        k, rest = np.divmod(fronts, plane)
        y, x = np.divmod(rest, nx)
        inside = (x > 0, x < nx - 1, y > 0, y < ny - 1, k > 0, k < L)
        found = np.concatenate([fronts[ok] + step for (step, _a, _s), ok in zip(steps, inside)])
        found = found[(kind[found] == _ETCHABLE) & ~known[found]]
        places = np.arange(found.size, dtype=np.int32)
        stamp[found] = places
        return found[stamp[found] == places]

    trial = np.zeros(0, dtype=np.int64)
    settle_candidates(reachable(sources))
    fastest = slowness[slowness > 0.0].min()
    delta = 0.5 * min(hx, hy) * fastest
    while trial.size:
        _check(should_cancel, "an isotropic etch")
        times = T[trial]
        first = float(times.min())
        if first > budget:
            break
        take = times <= first + delta
        group = trial[take]
        trial = trial[~take]
        queued[group] = False
        known[group] = True
        fronts = [group]
        if cavity_cells is not None:
            k, rest = np.divmod(group, plane)
            y, x = np.divmod(rest, nx)
            inside = (x > 0, x < nx - 1, y > 0, y < ny - 1, k > 0, k < L)
            wall = np.concatenate([group[ok] for ok in inside])
            hit = np.concatenate([group[ok] + step for (step, _a, _s), ok in zip(steps, inside)])
            breach = (kind[hit] == _CAVITY) & ~known[hit]
            wall, hit = wall[breach], hit[breach]
            for piece in np.unique(cavity_of[hit]):
                lo = np.searchsorted(cavity_ids, piece, side="left")
                hi = np.searchsorted(cavity_ids, piece, side="right")
                cells = cavity_cells[lo:hi]
                # It fills when the front reaches its wall: half a cell past
                # the centre of the cell that broke through.
                through = wall[cavity_of[hit] == piece]
                T[cells] = float(np.min(T[through] + 0.5 * min(hx, hy) * slowness[flat_labels[through]]))
                known[cells] = True
                fronts.append(cells)
        settle_candidates(reachable(np.concatenate(fronts)))
    taken = (kind == _ETCHABLE) & known & (T <= budget)
    return taken.reshape(labels.shape)[:L]


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

    The mesh is conforming: joining cells into rectangles leaves corners
    of one rectangle part-way along the edge of the next (a T-junction),
    and a GPU draws the two sides of such an edge along slightly
    different lines, which shows as pixel cracks. So every corner that
    lies on another drawn face's edge -- of any material -- is put into
    that edge, and a face with such points is fanned from its centre.
    Corners are on the lattice of cell boundaries, so this is exact
    integer work.
    """
    order = state.present()
    place = np.full(_OUTSIDE + 1, -1, dtype=np.int64)
    for index, name in enumerate(order):
        place[state.known_id(name)] = index
    n = state.n
    owners: list[np.ndarray] = []
    others: list[np.ndarray] = []
    lattice: list[np.ndarray] = []  # (Q, 4, 3) corners as (i, j, b), counter-clockwise from outside

    def add(owner, neighbour, corners):
        keep = (neighbour == VOID) | (neighbour == _OUTSIDE) | buried
        owners.append(owner[keep].astype(np.int64))
        others.append(neighbour[keep].astype(np.int64))
        lattice.append(corners[keep].astype(np.int64))

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
    if not owners:
        return {}
    owner = np.concatenate(owners)
    neighbour = np.concatenate(others)
    corners = np.concatenate(lattice)
    x_min, y_min, _, _ = state.bounds

    def place_of(points):
        return np.stack(
            [
                x_min + points[:, 0] * state.cell_x,
                y_min + points[:, 1] * state.cell_y,
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
        used, inverse = np.unique(tris.ravel(), return_inverse=True)
        faces = inverse.reshape(-1, 3).astype(np.uint32)
        other = tri_other[chosen]
        interface = ((other != VOID) & (other != _OUTSIDE)).astype(np.uint8)
        index = np.where(interface == 1, place[np.minimum(other, _OUTSIDE)], -1).astype(np.int16)
        meshes[name] = (coordinates[used], faces, interface, index)
    return meshes


def _conforming(corners: np.ndarray, place_of) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Triangles for quads given by lattice corners, with every corner that
    lies inside another quad's edge added to that edge.

    ``place_of`` turns lattice points into coordinates. Returns the vertex
    coordinates, the triangles (indices into them) and the quad each
    triangle came from.
    """
    count = corners.shape[0]
    points, point_of = np.unique(corners.reshape(-1, 3), axis=0, return_inverse=True)
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
    logger(
        f"VOXEL grid {new.nx} x {new.ny} cells of {new.cell_x * 1000:.4g} x "
        f"{new.cell_y * 1000:.4g} nm; heights exact"
    )
    # A curved front is cut in z at the project's z step, as the detailed
    # films are, and never finer than a cell.
    z_step = max(slab.resolution_um(project), new.cell)
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
                logger(f"VOXEL etch isotropic {text}, curved in z every {z_step * 1000:g} nm")
                etch_isotropic(
                    new, rates, budget, opening, dz=z_step, should_cancel=should_cancel
                )
        elif kind is ProcessType.OXIDATION:
            opening = _opening(new, step, project, parameters, sketches)
            rates, budget, text = _budget(recipe, parameters, project, "oxidation")
            product = str(parameters.get("material") or recipe.output_material or "SiO2")
            if product in {name for name, rate in rates.items() if rate > 0.0}:
                raise slab.SlabError(f"{product} cannot be both oxidised and the oxide it becomes")
            logger(f"VOXEL oxidize {text} into {product}, curved in z every {z_step * 1000:g} nm")
            etch_isotropic(
                new, rates, budget, opening, product=product, dz=z_step,
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
    return new
