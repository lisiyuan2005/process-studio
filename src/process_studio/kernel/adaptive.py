"""Two-level block-adaptive material states for local feature refinement."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .grid import UniformGrid3D
from .material_state import MaterialState


def _node_count(lower: float, upper: float, spacing: float) -> int:
    intervals = round((upper - lower) / spacing)
    if intervals < 2 or not np.isclose(intervals * spacing, upper - lower):
        raise ValueError("refinement bounds must align to the requested spacing")
    return intervals + 1


@dataclass(frozen=True)
class RefinementBox:
    """Fine solve block with an interior core used for AMR composition."""

    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float
    factor: int = 2
    core_x_min: float | None = None
    core_x_max: float | None = None
    core_y_min: float | None = None
    core_y_max: float | None = None

    def __post_init__(self) -> None:
        if self.factor < 2 or self.factor & (self.factor - 1):
            raise ValueError("refinement factor must be a power of two >= 2")
        if not (
            self.x_min < self.x_max
            and self.y_min < self.y_max
            and self.z_min < self.z_max
        ):
            raise ValueError("refinement box bounds must have positive extent")
        core = self.core_bounds
        for name, value in zip(
            ("core_x_min", "core_x_max", "core_y_min", "core_y_max"), core, strict=True
        ):
            object.__setattr__(self, name, value)
        if not (
            self.x_min <= core[0] < core[1] <= self.x_max
            and self.y_min <= core[2] < core[3] <= self.y_max
        ):
            raise ValueError("refinement core must be contained by the solve box")

    @property
    def core_bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x_min if self.core_x_min is None else self.core_x_min,
            self.x_max if self.core_x_max is None else self.core_x_max,
            self.y_min if self.core_y_min is None else self.core_y_min,
            self.y_max if self.core_y_max is None else self.core_y_max,
        )

    def make_grid(self, coarse: UniformGrid3D) -> UniformGrid3D:
        spacing = coarse.dx / self.factor
        return UniformGrid3D(
            self.x_min,
            self.x_max,
            self.y_min,
            self.y_max,
            self.z_min,
            self.z_max,
            _node_count(self.x_min, self.x_max, spacing),
            _node_count(self.y_min, self.y_max, spacing),
            _node_count(self.z_min, self.z_max, spacing),
        )

    def contains_xy(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        tolerance = np.finfo(float).eps * 32
        return (
            (x >= self.x_min - tolerance)
            & (x <= self.x_max + tolerance)
            & (y >= self.y_min - tolerance)
            & (y <= self.y_max + tolerance)
        )

    def contains_core_xy(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        tolerance = np.finfo(float).eps * 32
        x_min, x_max, y_min, y_max = self.core_bounds
        return (
            (x >= x_min - tolerance)
            & (x <= x_max + tolerance)
            & (y >= y_min - tolerance)
            & (y <= y_max + tolerance)
        )


@dataclass
class AdaptivePatch:
    box: RefinementBox
    state: MaterialState

    def __post_init__(self) -> None:
        grid = self.state.grid
        actual = (
            grid.x_min,
            grid.x_max,
            grid.y_min,
            grid.y_max,
            grid.z_min,
            grid.z_max,
        )
        expected = (
            self.box.x_min,
            self.box.x_max,
            self.box.y_min,
            self.box.y_max,
            self.box.z_min,
            self.box.z_max,
        )
        if not np.allclose(actual, expected):
            raise ValueError("patch state grid does not match its refinement box")


def tiled_refinement_boxes(
    coarse: UniformGrid3D,
    *,
    centers_x: list[float],
    centers_y: list[float],
    tile_size: float,
    z_min: float,
    z_max: float,
    factor: int = 4,
) -> list[RefinementBox]:
    """Legacy manual tiling helper; NOT a safe simulation planner.

    Use ProcessEngine.run_refined_branch for flow-driven solve regions.

    The blocks refine only the device region.  The remainder of the project
    continues to use the coarse grid, so memory grows with feature area rather
    than with the complete wafer bounding box.
    """
    half = tile_size / 2.0
    boxes: list[RefinementBox] = []
    for row, center_y in enumerate(centers_y):
        for column, center_x in enumerate(centers_x):
            box = RefinementBox(
                f"cell-r{row}-c{column}",
                center_x - half,
                center_x + half,
                center_y - half,
                center_y + half,
                z_min,
                z_max,
                factor,
            )
            box.make_grid(coarse)  # validate grid alignment immediately
            boxes.append(box)
    return boxes


def _nearest_indices(values: np.ndarray, lower: float, spacing: float, size: int) -> np.ndarray:
    return np.clip(np.rint((values - lower) / spacing).astype(int), 0, size - 1)


@dataclass
class AdaptiveMaterialState:
    """A coarse state overlaid by independently re-simulated fine patches."""

    coarse: MaterialState
    patches: list[AdaptivePatch] = field(default_factory=list)

    @property
    def material_names(self) -> list[str]:
        names = list(self.coarse.priority)
        for patch in self.patches:
            for name in patch.state.priority:
                if name not in names:
                    names.append(name)
        return names

    def section_labels(
        self,
        section_y: float,
        x: np.ndarray,
        z: np.ndarray,
    ) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Composite a categorical x/z section; fine blocks replace coarse cells."""
        x = np.asarray(x, dtype=float)
        z = np.asarray(z, dtype=float)
        coarse_labels, coarse_names = self.coarse.labels()
        ix = _nearest_indices(x, self.coarse.grid.x_min, self.coarse.grid.dx, self.coarse.grid.nx)
        iy = int(_nearest_indices(np.asarray([section_y]), self.coarse.grid.y_min, self.coarse.grid.dy, self.coarse.grid.ny)[0])
        iz = _nearest_indices(z, self.coarse.grid.z_min, self.coarse.grid.dz, self.coarse.grid.nz)
        sampled = coarse_labels[np.ix_(iz, np.asarray([iy]), ix)][:, 0, :]
        names = self.material_names
        remap = np.asarray([names.index(name) for name in coarse_names], dtype=np.int16)
        result = np.where(sampled >= 0, remap[np.maximum(sampled, 0)], -1).astype(np.int16)
        level = np.zeros(result.shape, dtype=np.uint8)

        for patch in sorted(self.patches, key=lambda item: item.box.factor):
            box = patch.box
            tolerance = patch.state.grid.dx * 1e-9
            core_x_min, core_x_max, core_y_min, core_y_max = box.core_bounds
            if not core_y_min - tolerance <= section_y <= core_y_max + tolerance:
                continue
            x_mask = (x >= core_x_min - tolerance) & (x <= core_x_max + tolerance)
            z_mask = (z >= box.z_min - tolerance) & (z <= box.z_max + tolerance)
            if not x_mask.any() or not z_mask.any():
                continue
            patch_labels, patch_names = patch.state.labels()
            patch_ix = _nearest_indices(x[x_mask], box.x_min, patch.state.grid.dx, patch.state.grid.nx)
            patch_iy = int(_nearest_indices(np.asarray([section_y]), box.y_min, patch.state.grid.dy, patch.state.grid.ny)[0])
            patch_iz = _nearest_indices(z[z_mask], box.z_min, patch.state.grid.dz, patch.state.grid.nz)
            values = patch_labels[np.ix_(patch_iz, np.asarray([patch_iy]), patch_ix)][:, 0, :]
            patch_remap = np.asarray([names.index(name) for name in patch_names], dtype=np.int16)
            values = np.where(values >= 0, patch_remap[np.maximum(values, 0)], -1)
            result[np.ix_(z_mask, x_mask)] = values
            level[np.ix_(z_mask, x_mask)] = int(np.log2(box.factor))
        return result, names, level

    def top_labels(
        self,
        x: np.ndarray,
        y: np.ndarray,
    ) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Composite a categorical top view using the finest available block."""
        from process_studio.visualization import top_view_labels

        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        coarse_top = top_view_labels(self.coarse)
        coarse_names = list(self.coarse.priority)
        ix = _nearest_indices(x, self.coarse.grid.x_min, self.coarse.grid.dx, self.coarse.grid.nx)
        iy = _nearest_indices(y, self.coarse.grid.y_min, self.coarse.grid.dy, self.coarse.grid.ny)
        sampled = coarse_top[np.ix_(iy, ix)]
        names = self.material_names
        remap = np.asarray([names.index(name) for name in coarse_names], dtype=np.int16)
        result = np.where(sampled >= 0, remap[np.maximum(sampled, 0)], -1).astype(np.int16)
        level = np.zeros(result.shape, dtype=np.uint8)

        for patch in sorted(self.patches, key=lambda item: item.box.factor):
            box = patch.box
            tolerance = patch.state.grid.dx * 1e-9
            core_x_min, core_x_max, core_y_min, core_y_max = box.core_bounds
            x_mask = (x >= core_x_min - tolerance) & (x <= core_x_max + tolerance)
            y_mask = (y >= core_y_min - tolerance) & (y <= core_y_max + tolerance)
            if not x_mask.any() or not y_mask.any():
                continue
            patch_top = top_view_labels(patch.state)
            patch_ix = _nearest_indices(x[x_mask], box.x_min, patch.state.grid.dx, patch.state.grid.nx)
            patch_iy = _nearest_indices(y[y_mask], box.y_min, patch.state.grid.dy, patch.state.grid.ny)
            values = patch_top[np.ix_(patch_iy, patch_ix)]
            patch_remap = np.asarray(
                [names.index(name) for name in patch.state.priority], dtype=np.int16
            )
            values = np.where(values >= 0, patch_remap[np.maximum(values, 0)], -1)
            result[np.ix_(y_mask, x_mask)] = values
            level[np.ix_(y_mask, x_mask)] = int(np.log2(box.factor))
        return result, names, level

    def save(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.coarse.save(directory / "coarse-state.npz")
        manifest = {"version": 2, "patches": []}
        for index, patch in enumerate(self.patches):
            filename = f"patch-{index:02d}.npz"
            patch.state.save(directory / filename)
            manifest["patches"].append(
                {
                    "file": filename,
                    "name": patch.box.name,
                    "bounds": [
                        patch.box.x_min,
                        patch.box.x_max,
                        patch.box.y_min,
                        patch.box.y_max,
                        patch.box.z_min,
                        patch.box.z_max,
                    ],
                    "factor": patch.box.factor,
                    "core_bounds": list(patch.box.core_bounds),
                }
            )
        (directory / "adaptive-state.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, directory: str | Path) -> "AdaptiveMaterialState":
        directory = Path(directory)
        manifest = json.loads((directory / "adaptive-state.json").read_text(encoding="utf-8"))
        coarse = MaterialState.load(directory / "coarse-state.npz")
        patches = []
        for entry in manifest["patches"]:
            core = entry.get("core_bounds", entry["bounds"][:4])
            box = RefinementBox(
                entry["name"],
                *entry["bounds"],
                entry["factor"],
                *core,
            )
            patches.append(AdaptivePatch(box, MaterialState.load(directory / entry["file"])))
        return cls(coarse, patches)
