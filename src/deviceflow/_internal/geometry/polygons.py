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
    # A valid MultiPolygon is already what the repair and the union would
    # make of it: validity *is* "the parts' interiors are disjoint and they
    # meet in at most finitely many points", so nothing can be merged and
    # nothing is self-intersecting. Most callers hand over the result of a
    # GEOS overlay, which is exactly that; an isotropic etch step came
    # through here 12,000 times, and the union alone was a third of it.
    if isinstance(geom, MultiPolygon) and shapely.is_valid(geom):
        merged = geom
    else:
        merged = shapely.unary_union(as_multipolygon(shapely.make_valid(geom)))
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
    # The same rings in the same order: no predicate needed. Asked
    # structurally rather than by comparing serialisations, which for the
    # thousands of these an isotropic etch asks meant serialising megabytes
    # of coordinates to throw away.
    if shapely.equals_exact(a, b, 0.0):
        return True
    # Equal regions have equal areas, so a difference in area settles it
    # without building a geometry. It is the common answer here -- callers
    # ask "did this change?" of something that usually did -- and the
    # symmetric difference below is by far the most expensive thing in an
    # isotropic etch when it is not short-circuited. The threshold is
    # relative: the same region computed two ways has the same area only
    # to within rounding, and ``area_eps`` is far below that, so testing
    # against it would call equal regions different.
    if abs(a.area - b.area) > max(area_eps, 1e-9 * (a.area + b.area)):
        return False
    if a.equals(b):
        return True
    return a.symmetric_difference(b).area <= area_eps


def unique_segments(geometries):
    """Every distinct edge of the given polygons' rings, once, as an array of segments (None if there are none).

    The rings of a device's slabs coincide heavily: a boundary between two
    materials is a ring of both, a film's ring is repeated slab after slab,
    a plane's outline comes back in every slab above it. Noding the rings
    as they come makes GEOS work through every coincident copy, which for a
    filled and polished stack means gigabytes; noding each undirected edge
    once gives the same arrangement from a fraction of the input.
    """
    import numpy as np

    chunks = []
    for geometry in geometries:
        for polygon in _iter_polygons(geometry):
            for ring in (polygon.exterior, *polygon.interiors):
                coords = shapely.get_coordinates(ring)
                if len(coords) > 1:
                    chunks.append(np.hstack([coords[:-1], coords[1:]]))
    if not chunks:
        return None
    segments = np.vstack(chunks)
    flip = (segments[:, 0] > segments[:, 2]) | (
        (segments[:, 0] == segments[:, 2]) & (segments[:, 1] > segments[:, 3])
    )
    segments[flip] = segments[flip][:, [2, 3, 0, 1]]
    segments = np.unique(segments, axis=0)
    segments = segments[(segments[:, 0] != segments[:, 2]) | (segments[:, 1] != segments[:, 3])]
    if not len(segments):
        return None
    return shapely.linestrings(segments.reshape(-1, 2, 2))
