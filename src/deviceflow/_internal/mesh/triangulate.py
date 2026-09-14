"""Horizontal face triangulation.

The mesh builder relies on two properties: every input vertex (exterior
and holes, collinear ones included) is a triangle vertex, and no Steiner
points are added. The side walls are built from the same vertices, so a
vertex the caps do not use is a T-junction and a visible crack. Both
properties are verified here whichever way the triangles were made.

Ear clipping through ``mapbox_earcut`` does it about thirty times faster
than GEOS's constrained Delaunay -- a cap with a hole array is a thousand
vertices and GEOS takes 40 ms over it, which is half the time to build
the whole display mesh of a stack. The triangles are thinner, which no
flat-shaded coplanar cap can tell apart, and there are exactly as many:
any triangulation of a polygon that adds no vertex and drops none has
``V + 2H - 2`` of them. GEOS remains the fallback when the wheel is not
installed, and also when ear clipping returns something that fails the
checks, so nothing depends on the accelerator being there.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Polygon

from ...exceptions import MeshError

try:  # optional accelerator
    import mapbox_earcut
except ImportError:  # pragma: no cover - exercised by the fallback test
    mapbox_earcut = None

XY = tuple[float, float]
Triangle = tuple[XY, XY, XY]


def triangulate(face: Polygon) -> list[Triangle]:
    """CCW triangles covering ``face``; vertices are exactly the face's vertices."""
    if face.is_empty or face.area <= 0:
        return []
    corners = None
    if mapbox_earcut is not None:
        corners = _by_ears(face)
    if corners is None:
        corners = _by_delaunay(face)

    a, b, d = corners[:, 0], corners[:, 1], corners[:, 2]
    area2 = (b[:, 0] - a[:, 0]) * (d[:, 1] - a[:, 1]) - (d[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    if not area2.all():  # degenerate triangles cover nothing and are dropped
        keep = area2 != 0
        corners, area2 = corners[keep], area2[keep]
    total = float(np.abs(area2).sum()) / 2
    if not np.isclose(total, face.area, rtol=1e-9, atol=1e-18):
        raise MeshError(f"triangulation area {total} != face area {face.area}")
    clockwise = area2 < 0
    if clockwise.any():  # wind them all the same way round
        corners = corners.copy()
        corners[clockwise] = corners[clockwise][:, [0, 2, 1], :]
    return [(tuple(t[0]), tuple(t[1]), tuple(t[2])) for t in corners.tolist()]


def _by_ears(face: Polygon) -> np.ndarray | None:
    """Ear-clipped triangles as an (n, 3, 2) array, or None to fall back."""
    rings = [np.asarray(face.exterior.coords[:-1], dtype=np.float64)]
    rings += [np.asarray(ring.coords[:-1], dtype=np.float64) for ring in face.interiors]
    points = np.concatenate(rings)
    ends = np.cumsum([len(ring) for ring in rings])
    index = mapbox_earcut.triangulate_float64(points, ends)
    if len(index) == 0 or len(index) % 3:
        return None
    index = index.reshape(-1, 3)
    # Ears are clipped off a ring, never inserted into it, so every vertex
    # should still be there. A ring that repeats a point is the one way it
    # can fail, and that is what the fallback is for.
    if len(np.unique(index)) != len(points):
        return None
    return points[index]


def _by_delaunay(face: Polygon) -> np.ndarray:
    """Constrained Delaunay triangles as an (n, 3, 2) array.

    The whole triangulation is read in one call. Taking a triangle at a
    time meant a shapely call and a handful of Python objects for each of
    tens of thousands, which cost more than the triangulation.
    """
    tri = shapely.constrained_delaunay_triangles(face)
    count = int(shapely.get_num_geometries(tri))
    if count == 0:
        raise MeshError("triangulation produced no triangles")
    coords = shapely.get_coordinates(tri)  # a triangle is a closed ring of four
    if len(coords) != 4 * count:
        raise MeshError("triangulation did not return triangles")
    corners = coords.reshape(count, 4, 2)[:, :3, :]
    # No Steiner points: every corner is a vertex of the face. The pair is
    # packed into one complex so this is a single sorted lookup; it holds
    # both doubles exactly, and -0.0 matches 0.0 as it did when these were
    # tuples in a set.
    on_face = shapely.get_coordinates(face)
    allowed = np.unique(on_face[:, 0] + 1j * on_face[:, 1])
    used = corners.reshape(-1, 2)
    if not np.isin(used[:, 0] + 1j * used[:, 1], allowed).all():
        raise MeshError("triangulation introduced a vertex that is not on the face")
    return corners
