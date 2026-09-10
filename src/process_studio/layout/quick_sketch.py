"""Dependency-light parametric top-view sketcher."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from .distance import polygon_distance, path_distance


@dataclass
class SketchShape:
    kind: str
    operation: str = "merge"
    parameters: dict[str, Any] = field(default_factory=dict)
    array: tuple[int, int, float, float] = (1, 1, 0.0, 0.0)

    def __post_init__(self) -> None:
        if self.kind not in {"rectangle", "circle", "polygon", "path"}:
            raise ValueError("unsupported sketch shape")
        if self.operation not in {"merge", "subtract", "intersect"}:
            raise ValueError("unsupported sketch operation")
        nx, ny, _, _ = self.array
        if nx < 1 or ny < 1:
            raise ValueError("array counts must be positive")


@dataclass
class QuickSketch:
    name: str = "Quick Sketch"
    shapes: list[SketchShape] = field(default_factory=list)

    def add(self, shape: SketchShape) -> None:
        self.shapes.append(shape)

    def render(self, xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
        return self.signed_distance(xx, yy) <= 0

    def signed_distance(self, xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
        """Return a continuous CSG level set for every supported primitive."""
        if xx.shape != yy.shape:
            raise ValueError("xx and yy must have matching shapes")
        result: np.ndarray | None = None
        for shape in self.shapes:
            shape_phi = self._sdf_array(shape, xx, yy)
            if result is None:
                result = shape_phi.copy() if shape.operation != "subtract" else -shape_phi
            elif shape.operation == "merge":
                result = np.minimum(result, shape_phi)
            elif shape.operation == "subtract":
                result = np.maximum(result, -shape_phi)
            else:
                result = np.maximum(result, shape_phi)
        return np.full(xx.shape, np.inf) if result is None else result

    def _sdf_array(
        self, shape: SketchShape, xx: np.ndarray, yy: np.ndarray
    ) -> np.ndarray:
        nx, ny, pitch_x, pitch_y = shape.array
        instances = []
        for iy in range(ny):
            for ix in range(nx):
                offset_x = (ix - (nx - 1) / 2.0) * pitch_x
                offset_y = (iy - (ny - 1) / 2.0) * pitch_y
                instances.append(self._sdf_one(shape, xx, yy, offset_x, offset_y))
        return np.minimum.reduce(instances)

    def _sdf_one(
        self,
        shape: SketchShape,
        xx: np.ndarray,
        yy: np.ndarray,
        offset_x: float,
        offset_y: float,
    ) -> np.ndarray:
        params = shape.parameters
        if shape.kind == "circle":
            cx, cy = params.get("center", (0.0, 0.0))
            if not np.isfinite(params["radius"]) or params["radius"] <= 0:
                raise ValueError("circle radius must be positive and finite")
            return np.hypot(xx - cx - offset_x, yy - cy - offset_y) - float(
                params["radius"]
            )
        if shape.kind == "rectangle":
            cx, cy = params.get("center", (0.0, 0.0))
            width, height = tuple(params["size"])
            if not np.isfinite([width, height]).all() or min(width, height) <= 0:
                raise ValueError("rectangle size must be positive and finite")
            qx = np.abs(xx - cx - offset_x) - width / 2.0
            qy = np.abs(yy - cy - offset_y) - height / 2.0
            return np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(
                np.maximum(qx, qy), 0.0
            )

        points = np.asarray(params["points"], dtype=float) + [offset_x, offset_y]
        if shape.kind == "polygon":
            return polygon_distance(xx, yy, points)
        return path_distance(xx, yy, points, float(params["width"]))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {"name": self.name, "shapes": [asdict(shape) for shape in self.shapes]},
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "QuickSketch":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            shapes=[SketchShape(**shape) for shape in data["shapes"]],
        )
