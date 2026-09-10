"""Layout: GDS files as a source of masks.

A GDS layer is a mask, never a material. Coordinates are converted to
micrometres regardless of the file's user unit; references, arrays and paths
are flattened; polygons on one ``(layer, datatype)`` are unioned into one
mask (keyhole polygons become polygons with holes in the process).

A layout is read through a *window* (default: 20 um x 20 um around the
origin) so that one device can be modelled and rendered out of a whole chip;
pass ``center=``/``size=``, an explicit ``window=(x0, y0, x1, y1)``, or
``window=None`` for the entire file.
"""

from __future__ import annotations

from pathlib import Path

import gdstk
from shapely.geometry import Polygon

from .exceptions import MaskError
from .mask import DEFAULT_GRID, Mask
from .units import parse_length


DEFAULT_WINDOW_SIZE = 20.0  # um, centred on the origin


def _resolve_window(window, center, size):
    """window: "default" | None (whole file) | (x0, y0, x1, y1); or center=/size=."""
    if window is not None and not (isinstance(window, str) and window == "default"):
        if center is not None or size is not None:
            raise MaskError("give either window= or center=/size=, not both")
        x0, y0, x1, y1 = (parse_length(v) for v in window)
    elif window is None and center is None and size is None:
        return None
    else:
        cx, cy = (parse_length(v) for v in (center if center is not None else (0, 0)))
        if size is None:
            w = h = DEFAULT_WINDOW_SIZE
        elif isinstance(size, (list, tuple)):
            w, h = (parse_length(v) for v in size)
        else:
            w = h = parse_length(size)
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    if not (x1 > x0 and y1 > y0):
        raise MaskError(f"window must have positive size, got {(x0, y0, x1, y1)}")
    return (x0, y0, x1, y1)


class Layout:
    def __init__(self, library: gdstk.Library, source: str = "<memory>", grid=DEFAULT_GRID, window=None):
        self._lib = library
        self._source = source
        self._grid = parse_length(grid)
        self._cells = {c.name: c for c in library.cells}
        self._window = window

    @classmethod
    def from_gds(cls, path, window="default", center=None, size=None, grid=DEFAULT_GRID) -> "Layout":
        """Read a GDS file. ``window``: (x0, y0, x1, y1) in um (strings allowed),
        ``None`` for the whole file, or leave it and give ``center=``/``size=``.
        Default: a 20 um x 20 um window around the origin."""
        path = Path(path)
        if not path.is_file():
            raise MaskError(f"GDS file not found: {path}")
        try:
            lib = gdstk.read_gds(str(path), unit=1e-6)  # everything in um
        except Exception as e:  # gdstk raises plain RuntimeError
            raise MaskError(f"cannot read GDS {path}: {e}") from e
        return cls(lib, source=str(path), grid=grid, window=_resolve_window(window, center, size))

    # -- inspection -------------------------------------------------------

    @property
    def window(self) -> tuple[float, float, float, float] | None:
        """The XY window masks are clipped to (um), or None for the whole file."""
        return self._window

    @property
    def cells(self) -> list[str]:
        return list(self._cells)

    @property
    def top_cells(self) -> list[str]:
        return [c.name for c in self._lib.top_level()]

    @property
    def top_cell(self) -> str:
        tops = self.top_cells
        if len(tops) != 1:
            raise MaskError(f"{self._source} has {len(tops)} top cells {tops}; pass cell= explicitly")
        return tops[0]

    @property
    def layers(self) -> list[tuple[int, int]]:
        """(layer, datatype) pairs present anywhere in the library."""
        found = set()
        for cell in self._lib.cells:
            for poly in cell.polygons:
                found.add((poly.layer, poly.datatype))
            for path in cell.paths:
                found.update(zip(path.layers, path.datatypes))
        return sorted(found)

    # -- masks ------------------------------------------------------------

    def mask(self, layer: int, datatype: int = 0, cell: str | None = None) -> Mask:
        """Flattened geometry of one (layer, datatype) as a Mask, in um."""
        name = cell if cell is not None else self.top_cell
        try:
            gcell = self._cells[name]
        except KeyError:
            raise MaskError(f"no cell {name!r} in {self._source}; cells: {self.cells}") from None
        polys = gcell.get_polygons(
            apply_repetitions=True,
            include_paths=True,
            depth=None,
            layer=int(layer),
            datatype=int(datatype),
        )
        if not polys:
            raise MaskError(
                f"no geometry on layer ({layer}, {datatype}) in cell {name!r}; layers present: {self.layers}"
            )
        shapes = [Polygon(p.points) for p in polys if len(p.points) >= 3]
        mask = Mask(shapes, self._grid)
        if mask.is_empty:
            raise MaskError(f"layer ({layer}, {datatype}) in cell {name!r} has only degenerate geometry")
        if self._window is not None:
            mask = mask.clip(self._window)
            if mask.is_empty:
                raise MaskError(
                    f"layer ({layer}, {datatype}) has no geometry inside the window {self._window}; "
                    "move it with center=/size=, widen it with window=(x0, y0, x1, y1), or use window=None"
                )
        return mask

    def __repr__(self) -> str:
        return f"Layout({self._source!r}, window={self._window}, cells={len(self._cells)}, layers={self.layers})"
