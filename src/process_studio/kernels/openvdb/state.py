"""The OpenVDB kernel's state: one narrow-band level set per material.

Every material is a signed distance field in its own OpenVDB grid, negative
inside, sharing one lattice: voxel (i, j, k) is centred at the window's
corner plus ``(i + 1/2 + TIE, j + 1/2 + TIE, k + 1/2 + TIE)`` voxels, with
``k = 0`` the first layer above the project's floor (``z_min``). Materials
never overlap. The grids hold the window and ``BELOW`` layers under the
floor, where the wafer continues as the lowest layer does -- the floor is
not a surface, and a hole etched through the whole stack stays open.

TIE moves every centre off the lattice of round numbers, so an interface at
a whole number of voxels never lands on a centre where the field would be
exactly zero.
"""

from __future__ import annotations

import io
import json
import math
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from . import _vdbcore as vc
from .field import HALF_WIDTH, pad_xy, solid_field

TIE = 0.0137
#: Layers kept under the floor.
BELOW = int(HALF_WIDTH) + 3
FORMAT = "process-studio-openvdb/1"


@dataclass(frozen=True)
class Lattice:
    """The voxel lattice a project's window is sampled on."""

    g: float
    x0: float
    y0: float
    x1: float
    y1: float
    z_floor: float
    nx: int
    ny: int
    periodic: bool = False

    @classmethod
    def for_window(cls, bounds, z_floor: float, g: float, periodic: bool = False) -> "Lattice":
        x0, y0, x1, y1 = (float(value) for value in bounds)
        nx = max(1, math.ceil((x1 - x0) / g - 1e-6))
        ny = max(1, math.ceil((y1 - y0) / g - 1e-6))
        if periodic and (abs(nx * g - (x1 - x0)) > 1e-6 * g or abs(ny * g - (y1 - y0)) > 1e-6 * g):
            raise ValueError(
                "a periodic window must be a whole number of voxels wide in x and in y"
            )
        return cls(float(g), x0, y0, x1, y1, float(z_floor), nx, ny, bool(periodic))

    @property
    def origin(self) -> tuple[float, float, float]:
        shift = (0.5 + TIE) * self.g
        return (self.x0 + shift, self.y0 + shift, self.z_floor + shift)

    def z_of(self, k: float) -> float:
        return self.origin[2] + k * self.g

    def k_of(self, z: float) -> float:
        return (z - self.origin[2]) / self.g

    def x_of(self, i):
        return self.origin[0] + np.asarray(i) * self.g

    def y_of(self, j):
        return self.origin[1] + np.asarray(j) * self.g

    def to_json(self) -> dict:
        return {
            "g": self.g, "x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1,
            "zFloor": self.z_floor, "nx": self.nx, "ny": self.ny, "periodic": self.periodic,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> "Lattice":
        return cls(
            float(data["g"]), float(data["x0"]), float(data["y0"]), float(data["x1"]),
            float(data["y1"]), float(data["zFloor"]), int(data["nx"]), int(data["ny"]),
            bool(data.get("periodic", False)),
        )


@dataclass(frozen=True)
class Block:
    """A dense working copy's frame: the window padded sideways by
    ``margin`` voxels, layers ``k0`` to ``k0 + nz - 1``."""

    lattice: Lattice
    k0: int
    nz: int
    margin: int

    # what the field operations read
    @property
    def g(self) -> float:
        return self.lattice.g

    @property
    def origin(self):
        return self.lattice.origin

    @property
    def periodic(self) -> bool:
        return self.lattice.periodic

    @property
    def x0(self) -> float:
        return self.lattice.x0

    @property
    def x1(self) -> float:
        return self.lattice.x1

    @property
    def y0(self) -> float:
        return self.lattice.y0

    @property
    def y1(self) -> float:
        return self.lattice.y1

    def z_of(self, k: float) -> float:
        return self.lattice.z_of(k)

    @property
    def shape(self) -> tuple[int, int, int]:
        return (self.lattice.nx + 2 * self.margin, self.lattice.ny + 2 * self.margin, self.nz)

    def axes(self):
        """World x, y, z of the block's voxel centres (open grids)."""
        lat = self.lattice
        i = np.arange(-self.margin, lat.nx + self.margin)
        j = np.arange(-self.margin, lat.ny + self.margin)
        k = np.arange(self.k0, self.k0 + self.nz)
        return (
            lat.x_of(i)[:, None, None],
            lat.y_of(j)[None, :, None],
            (lat.origin[2] + k * lat.g)[None, None, :],
        )

    def interior(self, array: np.ndarray) -> np.ndarray:
        m = self.margin
        return array[m: m + self.lattice.nx, m: m + self.lattice.ny, :]


class VdbState:
    """Per-material level sets on one lattice, in the order materials arrived."""

    def __init__(self, lattice: Lattice, grids: Mapping[str, "vc.SDF"], order: Iterable[str]) -> None:
        self.lattice = lattice
        self.grids = dict(grids)
        self.order = [name for name in order if name in self.grids]
        self._dense: dict[str, np.ndarray] | None = None
        self._solid: np.ndarray | None = None
        self._dense_k0 = -BELOW
        self.meshes: dict = {}

    # -- extent -------------------------------------------------------------

    def top_k(self) -> int | None:
        """The highest layer any material has a surface voxel in."""
        best = None
        for sdf in self.grids.values():
            lo, hi, ok = vc.active_bbox(sdf)
            if ok:
                best = hi[2] if best is None else max(best, hi[2])
        return best

    def top(self) -> float | None:
        """Height of the highest solid point, or None for an empty state."""
        dense = self.dense()
        if not dense:
            return None
        solid = np.minimum.reduce(list(dense.values()))
        inside = (solid < 0).any(axis=(0, 1))
        if not inside.any():
            return None
        k = int(np.nonzero(inside)[0].max())
        if k + 1 >= solid.shape[2]:
            return self.lattice.z_of(self._dense_k0 + k)
        a0, a1 = solid[:, :, k], solid[:, :, k + 1]
        below = a0 < 0
        frac = float(np.max(-a0[below] / np.maximum(a1[below] - a0[below], 1e-12)))
        return self.lattice.z_of(self._dense_k0 + k + min(max(frac, 0.0), 1.0))

    # -- dense copies ---------------------------------------------------------

    def block(self, headroom: float = 0.0, margin: int = 0) -> Block:
        """A frame from under the floor to ``headroom`` µm over the top."""
        top = self.top_k()
        extra = int(math.ceil(headroom / self.lattice.g)) + int(HALF_WIDTH) + 3
        k1 = (0 if top is None else top) + extra
        return Block(self.lattice, -BELOW, k1 + BELOW + 1, int(margin))

    def load(self, block: Block, names: Iterable[str] | None = None) -> dict[str, np.ndarray]:
        lat = self.lattice
        out = {}
        for name in self.order if names is None else names:
            if name not in self.grids:
                continue
            raw = np.asarray(vc.to_dense(self.grids[name], (0, 0, block.k0), (lat.nx, lat.ny, block.nz)))
            out[name] = pad_xy(raw, block.margin, lat.periodic)
        return out

    def dense(self) -> dict[str, np.ndarray]:
        """Every material over the window and the whole stack, for the views."""
        if self._dense is None:
            block = self.block()
            self._dense = self.load(block)
            self._dense_k0 = block.k0
        return self._dense

    def solid(self) -> np.ndarray | None:
        """Every material together, over the same block as :meth:`dense`."""
        if self._solid is None:
            dense = self.dense()
            if not dense:
                return None
            self._solid = solid_field(dense.values(), self.lattice.g)
        return self._solid

    def with_fields(self, block: Block, fields: Mapping[str, np.ndarray], order: Iterable[str] | None = None) -> "VdbState":
        """A new state with ``fields`` (dense, in ``block``) replacing those
        materials' grids; a field with nothing inside drops its material."""
        lat = self.lattice
        grids = dict(self.grids)
        bg = np.float32(HALF_WIDTH * lat.g)
        for name, field in fields.items():
            inner = np.ascontiguousarray(block.interior(field), dtype=np.float32)
            # the wafer continues under the floor as its lowest layer is
            floor = -block.k0
            if 0 < floor < inner.shape[2]:
                inner[:, :, :floor] = inner[:, :, floor: floor + 1]
            if not (inner < 0).any():
                grids.pop(name, None)
                continue
            np.clip(inner, -bg, bg, out=inner)
            grids[name] = vc.from_dense(inner, (0, 0, block.k0), lat.g, lat.origin, HALF_WIDTH)
        names = list(self.order if order is None else order)
        for name in fields:
            if name not in names:
                names.append(name)
        return VdbState(lat, grids, names)

    def materials(self) -> list[str]:
        return [name for name in self.order if name in self.grids]

    def nbytes(self) -> int:
        total = sum(int(sdf.mem_bytes) for sdf in self.grids.values())
        if self._dense is not None:
            total += sum(array.nbytes for array in self._dense.values())
        if self._solid is not None:
            total += self._solid.nbytes
        for mesh in self.meshes.values():
            total += sum(getattr(part, "nbytes", 0) for item in mesh for part in item if hasattr(part, "nbytes"))
        return 64 * 1024 + total

    # -- files ----------------------------------------------------------------

    def save(self, path: Path) -> None:
        names = self.materials()
        with tempfile.TemporaryDirectory(prefix="ps-vdb-") as scratch:
            grid_path = Path(scratch) / "grids.vdb"
            vc.write_vdb([self.grids[name] for name in names], names, str(grid_path))
            meta = {"format": FORMAT, "lattice": self.lattice.to_json(), "order": names}
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("state.json", json.dumps(meta))
                archive.write(grid_path, "grids.vdb")

    @classmethod
    def load_file(cls, path: Path) -> "VdbState":
        with zipfile.ZipFile(path) as archive:
            meta = json.loads(archive.read("state.json"))
            if meta.get("format") != FORMAT:
                raise ValueError(f"not an OpenVDB kernel state ({meta.get('format')!r})")
            with tempfile.TemporaryDirectory(prefix="ps-vdb-") as scratch:
                grid_path = Path(scratch) / "grids.vdb"
                grid_path.write_bytes(archive.read("grids.vdb"))
                grids = dict(vc.read_vdb(str(grid_path)))
        return cls(Lattice.from_json(meta["lattice"]), grids, meta["order"])


def initial(lattice: Lattice, substrate: str) -> VdbState:
    """The bare wafer: the substrate up to z = 0, continuing under the floor."""
    if lattice.z_floor >= 0:
        raise ValueError("the project window needs room below z = 0 for the substrate")
    top = int(math.ceil(lattice.k_of(0.0))) + int(HALF_WIDTH) + 3
    k0 = -BELOW
    nz = top - k0 + 1
    z = lattice.z_of(np.arange(k0, k0 + nz))
    bg = HALF_WIDTH * lattice.g
    column = np.clip(z - 0.0, -bg, bg).astype(np.float32)
    field = np.ascontiguousarray(np.broadcast_to(column, (lattice.nx, lattice.ny, nz)), dtype=np.float32)
    grid = vc.from_dense(field, (0, 0, k0), lattice.g, lattice.origin, HALF_WIDTH)
    return VdbState(lattice, {substrate: grid}, [substrate])
