"""The project window, and the sampling grids that describe it.

``UniformGrid3D`` was the level-set kernel's lattice. The slab kernel has
no lattice -- it works on exact polygons -- so for it this is the project
**window**: the bounds are what matter and the node counts are only a
sampling hint. The equal-spacing rule that the field solver needed is
therefore gone: it constrained which windows a project could have (the
spacing had to divide all three spans) for no reason the slab kernel has.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class UniformGrid2D:
    """A node-centered square grid with equal x/y spacing."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    nx: int
    ny: int

    def __post_init__(self) -> None:
        if self.nx < 3 or self.ny < 3:
            raise ValueError("nx and ny must be at least 3")
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("grid bounds must have positive extent")
        if not np.isclose(self.dx, self.dy):
            raise ValueError("the prototype requires equal x/y spacing")

    @property
    def dx(self) -> float:
        return (self.x_max - self.x_min) / (self.nx - 1)

    @property
    def dy(self) -> float:
        return (self.y_max - self.y_min) / (self.ny - 1)

    @property
    def x(self) -> np.ndarray:
        return np.linspace(self.x_min, self.x_max, self.nx)

    @property
    def y(self) -> np.ndarray:
        return np.linspace(self.y_min, self.y_max, self.ny)

    @property
    def mesh(self) -> tuple[np.ndarray, np.ndarray]:
        return np.meshgrid(self.x, self.y, indexing="xy")

    def circle(self, center: tuple[float, float], radius: float) -> np.ndarray:
        """Return a signed-distance circle, negative on the inside."""
        if radius <= 0:
            raise ValueError("radius must be positive")
        xx, yy = self.mesh
        cx, cy = center
        return np.hypot(xx - cx, yy - cy) - radius

    def vertical_plane(self, x_position: float) -> np.ndarray:
        """Return a signed-distance field for the half-plane x < x_position."""
        xx, _ = self.mesh
        return xx - x_position


@dataclass(frozen=True)
class UniformGrid3D:
    """A node-centered cubic grid stored in ``(z, y, x)`` array order."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float
    nx: int
    ny: int
    nz: int

    def __post_init__(self) -> None:
        if self.nx < 3 or self.ny < 3 or self.nz < 3:
            raise ValueError("nx, ny and nz must be at least 3")
        if self.x_max <= self.x_min or self.y_max <= self.y_min or self.z_max <= self.z_min:
            raise ValueError("grid bounds must have positive extent")

    @property
    def dx(self) -> float:
        return (self.x_max - self.x_min) / (self.nx - 1)

    @property
    def dy(self) -> float:
        return (self.y_max - self.y_min) / (self.ny - 1)

    @property
    def dz(self) -> float:
        return (self.z_max - self.z_min) / (self.nz - 1)

    @property
    def x(self) -> np.ndarray:
        return np.linspace(self.x_min, self.x_max, self.nx)

    @property
    def y(self) -> np.ndarray:
        return np.linspace(self.y_min, self.y_max, self.ny)

    @property
    def z(self) -> np.ndarray:
        return np.linspace(self.z_min, self.z_max, self.nz)

    @property
    def mesh_xy(self) -> tuple[np.ndarray, np.ndarray]:
        return np.meshgrid(self.x, self.y, indexing="xy")

    def substrate(self, surface_z: float = 0.0) -> np.ndarray:
        """Return a planar substrate SDF, negative below ``surface_z``.

        The surface is placed a hair inside the node it would otherwise land on.
        A node whose value is exactly zero is neither inside nor outside, and
        subtracting a void from it with ``max`` can never lift it out of the
        material: it survives as a zero-thickness membrane that later steps then
        treat as a real surface to deposit on. The offset is nine orders of
        magnitude below the node spacing, so it moves no interface anyone can
        measure; it only removes the tie.
        """
        zz = self.z[:, None, None]
        inside_nudge = self.dz * 1e-9
        return np.broadcast_to(
            zz - surface_z - inside_nudge, (self.nz, self.ny, self.nx)
        ).copy()


#: Nodes per micrometre when a window is described as a grid. Nothing samples
#: at this rate -- the slab kernel reads the bounds -- but the stored shape
#: should be of a plausible size rather than arbitrary.
WINDOW_NODES_PER_UM = 40.0


def window_grid(
    x_min: float, x_max: float, y_min: float, y_max: float, z_min: float, z_max: float
) -> UniformGrid3D:
    """A window, as the grid a project stores.

    The node counts follow the bounds at a nominal density; no spacing has
    to divide anything, so any window the user asks for is a window they get.
    """
    counts = [
        max(3, int(round((high - low) * WINDOW_NODES_PER_UM)) + 1)
        for low, high in ((x_min, x_max), (y_min, y_max), (z_min, z_max))
    ]
    return UniformGrid3D(x_min, x_max, y_min, y_max, z_min, z_max, *counts)
