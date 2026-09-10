"""GDS layer discovery and rasterization."""

from __future__ import annotations

from pathlib import Path

import gdstk
import numpy as np
from .distance import polygon_distance


def available_gds_layers(path: str | Path) -> list[tuple[int, int]]:
    library = gdstk.read_gds(path)
    layers: set[tuple[int, int]] = set()
    for cell in library.top_level():
        for polygon in cell.get_polygons():
            layers.add((int(polygon.layer), int(polygon.datatype)))
    return sorted(layers)


def gds_level_set(
    path: str | Path,
    xx: np.ndarray,
    yy: np.ndarray,
    *,
    layer: int,
    datatype: int,
    keep: str = "inside",
    scale_to_um: float | None = None,
) -> np.ndarray:
    if xx.shape != yy.shape:
        raise ValueError("xx and yy must have matching shapes")
    if keep not in {"inside", "outside"}:
        raise ValueError("keep must be inside or outside")
    library = gdstk.read_gds(path)
    scale = library.unit * 1e6 if scale_to_um is None else scale_to_um
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("GDS scale must be positive and finite")
    phi = np.full(xx.shape, np.inf)
    for cell in library.top_level():
        for polygon in cell.get_polygons(layer=layer, datatype=datatype):
            points = np.asarray(polygon.points, dtype=float) * scale
            phi = np.minimum(phi, polygon_distance(xx, yy, points))
    return phi if keep == "inside" else -phi


def rasterize_gds(path, xx, yy, *, layer, datatype, keep="inside", scale_to_um=None):
    return gds_level_set(
        path, xx, yy, layer=layer, datatype=datatype,
        keep=keep, scale_to_um=scale_to_um,
    ) <= 0
