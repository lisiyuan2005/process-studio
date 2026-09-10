"""Top view: the topmost visible material at every XY point, from ProcessState.

Slabs are visited from the top down; each material region claims the part
of the footprint not yet claimed by anything above it.
"""

from __future__ import annotations

import shapely
from shapely.geometry import MultiPolygon, Point, box
from shapely.geometry.polygon import orient

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState
from ..material import Material
from ._png import write_png
from ._svg import write_svg

Ring = list[tuple[float, float]]


class TopView:
    def __init__(self, state: ProcessState, materials: list[Material]):
        self._state = state
        self._materials = {m.name: m for m in materials}
        self._pieces: list[tuple[str, float, MultiPolygon]] = []  # (material, z_top, region)
        claimed = P.EMPTY
        for slab in reversed(state.slabs):
            for m, region in slab.regions.items():
                visible = state.clean(region.difference(claimed)) if not claimed.is_empty else region
                if not visible.is_empty:
                    self._pieces.append((m.name, slab.z1, visible))
            claimed = P.as_multipolygon(shapely.unary_union([claimed, slab.occupied()]))
        self._void = state.clean(box(*state.bounds).difference(claimed)) if not claimed.is_empty else P.clean(box(*state.bounds), state.grid)

    # -- queries ----------------------------------------------------------

    def _piece_at(self, x, y):
        pt = Point(float(x), float(y))
        for name, z_top, region in self._pieces:
            if region.covers(pt):
                return name, z_top
        return None, None

    def material_at(self, x, y) -> str | None:
        return self._piece_at(x, y)[0]

    def height_at(self, x, y) -> float | None:
        return self._piece_at(x, y)[1]

    def area(self, material) -> float:
        if material is None:
            return self._void.area
        name = material.name if isinstance(material, Material) else str(material)
        return sum(r.area for n, _, r in self._pieces if n == name)

    def polygons(self, material) -> list[Ring]:
        if material is None:
            mp = self._void
        else:
            name = material.name if isinstance(material, Material) else str(material)
            parts = [r for n, _, r in self._pieces if n == name]
            mp = P.as_multipolygon(shapely.unary_union(parts)) if parts else P.EMPTY
        return [[(float(x), float(y)) for x, y in orient(p).exterior.coords[:-1]] for p in mp.geoms]

    @property
    def materials(self) -> list[str]:
        seen = []
        for n, _, _ in self._pieces:
            if n not in seen:
                seen.append(n)
        return seen

    def to_dict(self) -> dict:
        """Serialize the exact visible polygons for lightweight GUI clients.

        Coordinates remain in the device's native micrometre unit.  Each item
        contains an exterior ring followed by zero or more hole rings, so a
        canvas can preserve mask holes with the even-odd fill rule.
        """
        polygons = []
        for rings, color, name in self._shapes():
            channels = [max(0, min(255, round(float(value) * 255))) for value in color[:3]]
            polygons.append({
                "material": name,
                "color": "#" + "".join(f"{channel:02x}" for channel in channels),
                "rings": [
                    [[float(x), float(y)] for x, y in ring]
                    for ring in rings
                ],
            })
        return {
            "schemaVersion": 1,
            "units": "um",
            "bounds": [float(value) for value in self._state.bounds],
            "polygons": polygons,
        }

    # -- export -----------------------------------------------------------

    def _shapes(self):
        shapes = []
        for name in self.materials:
            color = self._materials[name].color if name in self._materials else (0.7, 0.7, 0.7, 1.0)
            parts = [r for n, _, r in self._pieces if n == name]
            for p in P.as_multipolygon(shapely.unary_union(parts)).geoms:
                p = orient(p)
                rings = [list(p.exterior.coords[:-1])] + [list(r.coords[:-1]) for r in p.interiors]
                shapes.append((rings, color, name))
        return shapes

    def export_svg(self, path, px_per_um: float = 300.0):
        return write_svg(
            path, self._shapes(), self._state.bounds, title="top view (topmost material)",
            x_label="x", y_label="y", px_per_unit=px_per_um,
        )

    def export_png(self, path, dpi: int = 200):
        return write_png(path, self._shapes(), self._state.bounds, title="top view (topmost material)", x_label="x (um)", y_label="y (um)", dpi=dpi)

    def __repr__(self) -> str:
        return f"TopView(materials={self.materials}, void_area={self._void.area:.6g}um2)"
