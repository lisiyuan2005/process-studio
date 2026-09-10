"""Masks: XY regions that select where a process operation acts.

A mask is opaque to users; the polygon library behind it is an
implementation detail. Masks come from :class:`MaskFactory` (procedural) or
from :class:`deviceflow.layout.Layout` (GDS).
"""

from __future__ import annotations

import math

import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box

from ._internal.geometry import polygons as P
from .exceptions import MaskError, UnitError
from .units import parse_length

DEFAULT_GRID = 1e-6  # um


class Mask:
    __slots__ = ("_geom", "_grid")

    def __init__(self, geom, grid: float = DEFAULT_GRID):
        # Internal constructor; user code goes through MaskFactory / Layout.
        # ``geom`` may be a shapely geometry or a list of them (unioned).
        self._grid = float(grid)
        if isinstance(geom, (list, tuple)):
            geom = shapely.unary_union([shapely.make_valid(g) for g in geom]) if geom else P.EMPTY
        self._geom: MultiPolygon = P.clean(geom, self._grid)

    # -- inspection -------------------------------------------------------

    @property
    def area(self) -> float:
        return self._geom.area

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        if self._geom.is_empty:
            return (math.nan,) * 4
        return tuple(self._geom.bounds)

    @property
    def is_empty(self) -> bool:
        return self._geom.is_empty

    @property
    def num_parts(self) -> int:
        return len(self._geom.geoms)

    def equals(self, other: "Mask") -> bool:
        return P.equals(self._geom, _geom_of(other))

    def representative_point(self) -> tuple[float, float]:
        """A point guaranteed to lie inside the mask (largest part)."""
        if self.is_empty:
            raise MaskError("empty mask has no interior point")
        largest = max(self._geom.geoms, key=lambda g: g.area)
        p = largest.representative_point()
        return (p.x, p.y)

    def contains(self, x, y) -> bool:
        return bool(self._geom.covers(Point(parse_length(x), parse_length(y))))

    def __repr__(self) -> str:
        if self.is_empty:
            return "Mask(empty)"
        return f"Mask(parts={self.num_parts}, area={self.area:.6g}um2, bounds={self.bounds})"

    # -- algebra ----------------------------------------------------------

    def __or__(self, other: "Mask") -> "Mask":
        return Mask(self._geom.union(_geom_of(other)), self._grid)

    def __and__(self, other: "Mask") -> "Mask":
        return Mask(self._geom.intersection(_geom_of(other)), self._grid)

    def __sub__(self, other: "Mask") -> "Mask":
        return Mask(self._geom.difference(_geom_of(other)), self._grid)

    def bias(self, amount) -> "Mask":
        """Grow (positive) or shrink (negative) the mask with square corners."""
        d = parse_length(amount)
        if d == 0:
            return self
        grown = self._geom.buffer(d, join_style="mitre", mitre_limit=2.0)
        return Mask(grown, self._grid)

    def clip(self, bounds) -> "Mask":
        x0, y0, x1, y1 = (float(v) for v in bounds)
        return Mask(self._geom.intersection(box(x0, y0, x1, y1)), self._grid)

    def transformed(self, *, x=0, y=0, rotation=0) -> "Mask":
        """Return a translated/rotated copy of this mask.

        Rotation is counter-clockwise in degrees around the layout origin,
        followed by translation in micrometres.  Keeping this operation on the
        public Mask surface lets desktop layout placement remain independent of
        the polygon implementation.
        """
        try:
            dx = parse_length(x)
            dy = parse_length(y)
            angle = float(rotation)
        except (UnitError, TypeError, ValueError) as exc:
            raise MaskError(f"invalid mask placement: {exc}") from exc
        if not all(math.isfinite(value) for value in (dx, dy, angle)):
            raise MaskError("mask placement values must be finite")
        geom = self._geom
        if angle:
            geom = affinity.rotate(geom, angle, origin=(0, 0), use_radians=False)
        if dx or dy:
            geom = affinity.translate(geom, xoff=dx, yoff=dy)
        return Mask(geom, self._grid)


def _geom_of(mask) -> MultiPolygon:
    if not isinstance(mask, Mask):
        raise MaskError(f"expected a Mask, got {mask!r}")
    return mask._geom


def _xy(point) -> tuple[float, float]:
    try:
        x, y = point
    except (TypeError, ValueError):
        raise MaskError(f"expected an (x, y) pair, got {point!r}") from None
    return parse_length(x), parse_length(y)


class MaskFactory:
    """Procedural masks. Attached to a device as ``device.masks``."""

    def __init__(self, grid: float = DEFAULT_GRID):
        self._grid = float(grid)

    def rectangle(self, size, center=None, corner=None) -> Mask:
        w, h = _xy(size)
        if w <= 0 or h <= 0:
            raise MaskError(f"rectangle size must be positive, got {size!r}")
        if (center is None) == (corner is None):
            raise MaskError("rectangle needs exactly one of center= or corner=")
        if center is not None:
            cx, cy = _xy(center)
            x0, y0 = cx - w / 2, cy - h / 2
        else:
            x0, y0 = _xy(corner)
        return self._make(box(x0, y0, x0 + w, y0 + h))

    def circle(self, center, diameter, segments: int = 64) -> Mask:
        r = parse_length(diameter) / 2
        if r <= 0:
            raise MaskError(f"circle diameter must be positive, got {diameter!r}")
        if segments < 8:
            raise MaskError("circle needs at least 8 segments")
        cx, cy = _xy(center)
        geom = Point(cx, cy).buffer(r, quad_segs=max(2, segments // 4))
        return self._make(geom)

    def polygon(self, points, holes=None) -> Mask:
        shell = [_xy(p) for p in points]
        if len(shell) < 3:
            raise MaskError("polygon needs at least 3 points")
        rings = [[_xy(p) for p in h] for h in (holes or [])]
        return self._make(Polygon(shell, rings))

    def _make(self, geom) -> Mask:
        mask = Mask(geom, self._grid)
        if mask.is_empty:
            raise MaskError("mask geometry is empty or degenerate")
        return mask
