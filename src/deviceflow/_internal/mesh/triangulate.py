"""Horizontal face triangulation.

Uses GEOS constrained Delaunay triangulation through shapely: every input
vertex (exterior and holes, including collinear ones) is a triangle vertex,
no Steiner points are added, and every boundary edge is a triangle edge.
Both properties are verified, because the mesh builder relies on them to
share vertices exactly with the side walls.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Polygon

from ...exceptions import MeshError

XY = tuple[float, float]
Triangle = tuple[XY, XY, XY]


def triangulate(face: Polygon) -> list[Triangle]:
    """CCW triangles covering ``face``; vertices are exactly the face's vertices."""
    if face.is_empty or face.area <= 0:
        return []
    tri = shapely.constrained_delaunay_triangles(face)
    parts = shapely.get_parts(tri)
    if len(parts) == 0:
        raise MeshError("triangulation produced no triangles")

    allowed = {tuple(c) for c in shapely.get_coordinates(face)}
    out: list[Triangle] = []
    total = 0.0
    for t in parts:
        c = shapely.get_coordinates(t)[:3]
        a, b, d = (tuple(p) for p in c)
        if not ({a, b, d} <= allowed):
            raise MeshError("triangulation introduced a vertex that is not on the face")
        area2 = (b[0] - a[0]) * (d[1] - a[1]) - (d[0] - a[0]) * (b[1] - a[1])
        if area2 == 0:
            continue
        if area2 < 0:
            b, d = d, b
        total += abs(area2) / 2
        out.append((a, b, d))
    if not np.isclose(total, face.area, rtol=1e-9, atol=1e-18):
        raise MeshError(f"triangulation area {total} != face area {face.area}")
    return out
