"""GDS layer discovery and rasterization."""

from __future__ import annotations

from pathlib import Path

import gdstk
import numpy as np
from matplotlib.path import Path as MplPath


def available_gds_layers(path: str | Path) -> list[tuple[int, int]]:
    library = gdstk.read_gds(path)
    layers: set[tuple[int, int]] = set()
    for cell in library.top_level():
        for polygon in cell.get_polygons():
            layers.add((int(polygon.layer), int(polygon.datatype)))
    return sorted(layers)


def rasterize_gds(
    path: str | Path,
    xx: np.ndarray,
    yy: np.ndarray,
    *,
    layer: int,
    datatype: int,
    keep: str = "inside",
    scale_to_um: float = 1.0,
) -> np.ndarray:
    if xx.shape != yy.shape:
        raise ValueError("xx and yy must have matching shapes")
    if keep not in {"inside", "outside"}:
        raise ValueError("keep must be inside or outside")
    library = gdstk.read_gds(path)
    query = np.column_stack((xx.ravel(), yy.ravel()))
    mask = np.zeros(xx.size, dtype=bool)
    for cell in library.top_level():
        for polygon in cell.get_polygons(layer=layer, datatype=datatype):
            points = np.asarray(polygon.points, dtype=float) * scale_to_um
            mask |= MplPath(points, closed=True).contains_points(
                query, radius=np.finfo(float).eps * 16
            )
    result = mask.reshape(xx.shape)
    return result if keep == "inside" else ~result
