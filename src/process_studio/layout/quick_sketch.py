"""Dependency-light parametric top-view sketcher."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from matplotlib.path import Path as MplPath

from process_studio.kernel.masks import circle, intersect, merge, rectangle, subtract


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
        if xx.shape != yy.shape:
            raise ValueError("xx and yy must have matching shapes")
        result: np.ndarray | None = None
        for shape in self.shapes:
            shape_mask = self._render_array(shape, xx, yy)
            if result is None:
                result = shape_mask.copy() if shape.operation != "subtract" else ~shape_mask
            elif shape.operation == "merge":
                result = merge(result, shape_mask)
            elif shape.operation == "subtract":
                result = subtract(result, shape_mask)
            else:
                result = intersect(result, shape_mask)
        return np.zeros(xx.shape, dtype=bool) if result is None else result

    def _render_array(
        self, shape: SketchShape, xx: np.ndarray, yy: np.ndarray
    ) -> np.ndarray:
        nx, ny, pitch_x, pitch_y = shape.array
        instances = []
        for iy in range(ny):
            for ix in range(nx):
                offset_x = (ix - (nx - 1) / 2.0) * pitch_x
                offset_y = (iy - (ny - 1) / 2.0) * pitch_y
                instances.append(self._render_one(shape, xx, yy, offset_x, offset_y))
        return np.logical_or.reduce(instances)

    @staticmethod
    def _render_one(
        shape: SketchShape,
        xx: np.ndarray,
        yy: np.ndarray,
        offset_x: float,
        offset_y: float,
    ) -> np.ndarray:
        params = shape.parameters
        if shape.kind == "rectangle":
            cx, cy = params.get("center", (0.0, 0.0))
            return rectangle(
                xx,
                yy,
                center=(cx + offset_x, cy + offset_y),
                size=tuple(params["size"]),
            )
        if shape.kind == "circle":
            cx, cy = params.get("center", (0.0, 0.0))
            return circle(
                xx,
                yy,
                center=(cx + offset_x, cy + offset_y),
                radius=float(params["radius"]),
            )

        points = np.asarray(params["points"], dtype=float).copy()
        points[:, 0] += offset_x
        points[:, 1] += offset_y
        if shape.kind == "polygon":
            query = np.column_stack((xx.ravel(), yy.ravel()))
            return MplPath(points, closed=True).contains_points(
                query, radius=np.finfo(float).eps * 16
            ).reshape(xx.shape)

        width = float(params["width"])
        if width <= 0 or len(points) < 2:
            raise ValueError("path requires positive width and at least two points")
        distance_sq = np.full(xx.shape, np.inf)
        for start, end in zip(points[:-1], points[1:], strict=True):
            vx, vy = end - start
            length_sq = vx * vx + vy * vy
            if length_sq == 0:
                continue
            projection = np.clip(
                ((xx - start[0]) * vx + (yy - start[1]) * vy) / length_sq,
                0.0,
                1.0,
            )
            nearest_x = start[0] + projection * vx
            nearest_y = start[1] + projection * vy
            distance_sq = np.minimum(
                distance_sq, (xx - nearest_x) ** 2 + (yy - nearest_y) ** 2
            )
        return distance_sq <= (width / 2.0) ** 2

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
