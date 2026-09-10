"""Uniform two-dimensional Cartesian grid."""

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
        if not (np.isclose(self.dx, self.dy) and np.isclose(self.dx, self.dz)):
            raise ValueError("the prototype requires equal x/y/z spacing")

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
        """Return a planar substrate SDF, negative below ``surface_z``."""
        zz = self.z[:, None, None]
        return np.broadcast_to(zz - surface_z, (self.nz, self.ny, self.nx)).copy()
