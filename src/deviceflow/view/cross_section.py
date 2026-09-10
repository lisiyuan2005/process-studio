"""Cross-section along a line, computed from ProcessState.

For every slab and material: ``region ∩ line`` gives intervals along the
line; each interval times the slab's Z range is a rectangle in the (s, z)
section plane. Rectangles of one material are unioned, so a material that
spans several slabs is one polygon without internal seams.

``s`` is the distance along the line from ``start`` (um); ``z`` is height.
"""

from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.geometry.polygon import orient

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material
from ._png import write_png
from ._svg import write_svg

Ring = list[tuple[float, float]]


class CrossSection:
    def __init__(self, state: ProcessState, start, end, materials: list[Material]):
        x0, y0 = (float(v) for v in start)
        x1, y1 = (float(v) for v in end)
        self.start, self.end = (x0, y0), (x1, y1)
        self.length = math.hypot(x1 - x0, y1 - y0)
        if self.length <= 0:
            raise ProcessError("cross-section needs two distinct points")
        self._u = ((x1 - x0) / self.length, (y1 - y0) / self.length)
        self._state = state
        self._materials = {m.name: m for m in materials}
        self._line = LineString([(x0, y0), (x1, y1)])
        self._polys: dict[str, MultiPolygon] = self._build()

    # -- construction -----------------------------------------------------

    def _s(self, x: float, y: float) -> float:
        return (x - self.start[0]) * self._u[0] + (y - self.start[1]) * self._u[1]

    def _segments(self, region) -> list[tuple[float, float]]:
        """Intervals of the line covered by ``region``, as (s0, s1)."""
        inter = region.intersection(self._line)
        out = []
        for part in shapely.get_parts(inter):
            if isinstance(part, LineString) and not part.is_empty:
                c = np.asarray(part.coords)
                s = [self._s(x, y) for x, y in c]
                a, b = min(s), max(s)
                if b - a > 0:
                    out.append((a, b))
        return sorted(out)

    def _build(self) -> dict[str, MultiPolygon]:
        rects: dict[str, list[Polygon]] = {}
        for slab in self._state.slabs:
            for m, region in slab.regions.items():
                for s0, s1 in self._segments(region):
                    rects.setdefault(m.name, []).append(box(s0, slab.z0, s1, slab.z1))
        polys = {}
        for name, rs in rects.items():
            merged = shapely.unary_union(rs).simplify(0)
            polys[name] = P.as_multipolygon(merged)
        return polys

    def _point(self, s: float) -> tuple[float, float]:
        return (self.start[0] + self._u[0] * s, self.start[1] + self._u[1] * s)

    # -- queries ----------------------------------------------------------

    def material_at(self, s: float, z: float) -> str | None:
        x, y = self._point(s)
        m = self._state.material_at(x, y, z)
        return None if m is None else m.name

    def column(self, s: float) -> list[tuple[float, float, str]]:
        """Solid intervals (z0, z1, material) at position s, bottom to top, merged."""
        x, y = self._point(s)
        out: list[list] = []
        for slab in self._state.slabs:
            m = slab.material_at(x, y)
            if m is None:
                continue
            if out and out[-1][2] == m.name and out[-1][1] == slab.z0:
                out[-1][1] = slab.z1
            else:
                out.append([slab.z0, slab.z1, m.name])
        return [tuple(v) for v in out]

    def intervals(self, z: float) -> list[tuple[float, float, str]]:
        """Solid intervals (s0, s1, material) along the line at height z, merged."""
        slab = self._state.slab_at(z)
        if slab is None:
            return []
        found = []
        for m, region in slab.regions.items():
            for s0, s1 in self._segments(region):
                found.append([s0, s1, m.name])
        found.sort()
        out: list[list] = []
        for iv in found:
            if out and out[-1][2] == iv[2] and math.isclose(out[-1][1], iv[0], abs_tol=self._state.grid):
                out[-1][1] = iv[1]
            else:
                out.append(iv)
        return [(znorm(a), znorm(b), n) for a, b, n in out]

    def opening_width(self, z: float) -> float:
        """Total void length along the line at height z."""
        solid = sum(b - a for a, b, _ in self.intervals(z))
        return max(0.0, znorm(self.length - solid))

    def surface_z(self, s: float) -> float | None:
        col = self.column(s)
        return col[-1][1] if col else None

    def polygons(self, material) -> list[Ring]:
        """Exterior rings (s, z) of the material's section polygons."""
        name = material.name if isinstance(material, Material) else str(material)
        mp = self._polys.get(name)
        if mp is None:
            return []
        return [[(float(x), float(y)) for x, y in orient(p).exterior.coords[:-1]] for p in mp.geoms]

    @property
    def materials(self) -> list[str]:
        return list(self._polys)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        zs = self._state.z_planes
        return (0.0, zs[0] if zs else 0.0, self.length, zs[-1] if zs else 0.0)

    # -- export -----------------------------------------------------------

    def _shapes(self, z_scale: float):
        if z_scale <= 0:
            raise ProcessError("z_scale must be positive")
        shapes = []
        for name, mp in self._polys.items():
            color = self._materials[name].color if name in self._materials else (0.7, 0.7, 0.7, 1.0)
            for p in mp.geoms:
                p = orient(p)
                rings = [[(x, z * z_scale) for x, z in p.exterior.coords[:-1]]]
                rings += [[(x, z * z_scale) for x, z in r.coords[:-1]] for r in p.interiors]
                shapes.append((rings, color, name))
        s0, z0, s1, z1 = self.bounds
        note = f" (z x{z_scale:g}, display only)" if z_scale != 1 else ""
        title = f"section {self.start} -> {self.end}{note}"
        return shapes, (s0, z0 * z_scale, s1, z1 * z_scale), title, note

    def export_svg(self, path, z_scale: float = 1.0, px_per_um: float = 300.0):
        """Write the section as SVG. ``z_scale`` exaggerates Z for display only."""
        shapes, bounds, title, note = self._shapes(z_scale)
        return write_svg(path, shapes, bounds, title=title, note=note, x_label="s", y_label="z", px_per_unit=px_per_um)

    def export_png(self, path, z_scale: float = 1.0, dpi: int = 200):
        """Write the section as PNG (needs matplotlib). ``z_scale`` is display only."""
        shapes, bounds, title, _ = self._shapes(z_scale)
        return write_png(path, shapes, bounds, title=title, x_label="s (um)", y_label="z (um)", dpi=dpi)

    def __repr__(self) -> str:
        return f"CrossSection({self.start} -> {self.end}, length={self.length:.6g}um, materials={self.materials})"
