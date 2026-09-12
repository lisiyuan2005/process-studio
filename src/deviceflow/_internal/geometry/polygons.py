"""2D polygon hygiene shared by masks, slabs and the mesh builder.

Every XY region in the process state is a valid, grid-snapped, oriented
``MultiPolygon`` (exterior CCW, holes CW). ``clean`` is the single function
that establishes that invariant; every Boolean result goes through it.
"""

from __future__ import annotations

import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

EMPTY = MultiPolygon()


def as_multipolygon(geom) -> MultiPolygon:
    """Return the polygonal part of any geometry as a MultiPolygon."""
    if geom is None or geom.is_empty:
        return EMPTY
    parts = []
    for part in _iter_polygons(geom):
        if not part.is_empty:
            parts.append(part)
    return MultiPolygon(parts) if parts else EMPTY


def _iter_polygons(geom):
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        yield from geom.geoms
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            yield from _iter_polygons(g)


def snap(geom, grid: float) -> MultiPolygon:
    """Snap coordinates to ``grid``; collapses anything thinner than the grid."""
    snapped = shapely.set_precision(geom, grid, mode="valid_output")
    return as_multipolygon(snapped)


def clean(geom, grid: float, area_eps: float | None = None) -> MultiPolygon:
    """Valid + unioned + snapped + oriented MultiPolygon, tiny parts removed."""
    if area_eps is None:
        area_eps = grid * grid
    if geom is None or geom.is_empty:
        return EMPTY
    valid = shapely.make_valid(geom)
    merged = shapely.unary_union(as_multipolygon(valid))
    snapped = snap(merged, grid)
    parts = []
    for poly in snapped.geoms:
        if poly.area <= area_eps:
            continue
        holes = [r for r in poly.interiors if Polygon(r).area > area_eps]
        parts.append(orient(Polygon(poly.exterior, holes), sign=1.0))
    return MultiPolygon(parts) if parts else EMPTY


def equals(a, b, area_eps: float = 1e-18) -> bool:
    """Geometric equality independent of vertex representation."""
    if a is b:
        return True
    if a.is_empty and b.is_empty:
        return True
    if a.is_empty != b.is_empty:
        return False
    if a.equals(b):
        return True
    return a.symmetric_difference(b).area <= area_eps
