"""Multi-material level-set state with deterministic overlap priority."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.ndimage import distance_transform_edt

from .grid import UniformGrid3D


def signed_distance_from_inside(inside: np.ndarray, spacing: float) -> np.ndarray:
    """Build an approximate signed-distance field from an occupancy array."""
    if inside.ndim not in (2, 3):
        raise ValueError("inside must be a 2D or 3D array")
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    inside = inside.astype(bool, copy=False)
    scale = max(inside.shape) * spacing
    if inside.all():
        return np.full(inside.shape, -scale, dtype=float)
    if not inside.any():
        return np.full(inside.shape, scale, dtype=float)
    distance_inside = distance_transform_edt(inside, sampling=spacing)
    distance_outside = distance_transform_edt(~inside, sampling=spacing)
    return distance_outside - distance_inside


@dataclass
class MaterialState:
    """One level set per material; later priority entries win overlaps."""

    grid: UniformGrid3D
    fields: dict[str, np.ndarray] = field(default_factory=dict)
    priority: list[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int, int]:
        return (self.grid.nz, self.grid.ny, self.grid.nx)

    def clone(self) -> "MaterialState":
        return MaterialState(
            self.grid,
            {name: phi.copy() for name, phi in self.fields.items()},
            list(self.priority),
        )

    def add_material(
        self,
        name: str,
        phi: np.ndarray,
        *,
        merge_existing: bool = True,
        resolve_overlap: bool = True,
    ) -> None:
        phi = np.asarray(phi, dtype=float)
        if phi.shape != self.shape:
            raise ValueError(f"material field must have shape {self.shape}")
        incoming = phi.copy()
        if name in self.fields and merge_existing:
            incoming = np.minimum(self.fields[name], incoming)
        if resolve_overlap:
            for other_name in self.priority:
                if other_name != name:
                    self.fields[other_name] = np.maximum(
                        self.fields[other_name], -incoming
                    )
        self.fields[name] = incoming
        if name in self.priority:
            self.priority.remove(name)
        self.priority.append(name)

    def remove_material(self, name: str) -> None:
        self.fields.pop(name, None)
        if name in self.priority:
            self.priority.remove(name)

    def combined_phi(self) -> np.ndarray:
        if not self.fields:
            return np.full(self.shape, self.grid.dx * max(self.shape), dtype=float)
        return np.minimum.reduce([self.fields[name] for name in self.priority])

    def labels(self) -> tuple[np.ndarray, list[str]]:
        labels = np.full(self.shape, -1, dtype=np.int16)
        names = list(self.priority)
        for index, name in enumerate(names):
            labels[self.fields[name] <= 0.0] = index
        return labels, names

    def occupied(self, name: str | None = None) -> np.ndarray:
        if name is None:
            return self.combined_phi() <= 0.0
        return self.fields[name] <= 0.0

    def rebuild_from_occupancy(self, name: str, inside: np.ndarray) -> None:
        if inside.shape != self.shape:
            raise ValueError(f"occupancy must have shape {self.shape}")
        self.fields[name] = signed_distance_from_inside(inside, self.grid.dx)
        if name not in self.priority:
            self.priority.append(name)

    def save(self, path: str | Path) -> None:
        payload: dict[str, np.ndarray] = {
            "priority": np.asarray(self.priority, dtype=str),
            "grid_bounds": np.asarray(
                [
                    self.grid.x_min,
                    self.grid.x_max,
                    self.grid.y_min,
                    self.grid.y_max,
                    self.grid.z_min,
                    self.grid.z_max,
                ],
                dtype=float,
            ),
            "grid_shape": np.asarray(
                [self.grid.nx, self.grid.ny, self.grid.nz], dtype=int
            ),
        }
        for index, name in enumerate(self.priority):
            payload[f"field_{index}"] = self.fields[name]
        np.savez_compressed(path, **payload)

    @classmethod
    def load(cls, path: str | Path) -> "MaterialState":
        with np.load(path, allow_pickle=False) as data:
            bounds = data["grid_bounds"]
            shape = data["grid_shape"]
            grid = UniformGrid3D(*bounds.tolist(), *shape.tolist())
            priority = data["priority"].tolist()
            fields = {
                name: data[f"field_{index}"].copy()
                for index, name in enumerate(priority)
            }
        return cls(grid=grid, fields=fields, priority=priority)
