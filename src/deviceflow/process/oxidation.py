"""Oxidation: the exposed skin of a material turns into another one.

The listed materials lose ``depth`` from every exposed surface, exactly as
a wet etch would take it (the same front, the same barriers, the same
square or round model), and what was taken becomes the product material
in place. Volume is conserved: the grown oxide fills precisely the region
the consumed material left. Real oxidation swells (SiO2 takes 2.2 times
the silicon it consumes); this model deliberately keeps the geometry and
only relabels the skin, which is what a flow needs to place a gate oxide
or a liner without tracking the swelling.
"""

from __future__ import annotations

import shapely
from shapely.geometry import MultiPolygon

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState
from ..exceptions import ProcessError
from ..material import Material
from .conformal import apply_film
from .isotropic_etch import etch_isotropic


def oxidize(
    state: ProcessState,
    depths: dict[Material, float],
    product: Material,
    resolution: float,
    opening: MultiPolygon | None = None,
    xy_resolution: float | None = None,
    *,
    square: bool = False,
) -> dict[Material, float]:
    """Convert ``depth`` of each listed material's exposed skin into ``product``.

    Returns the volume converted per material.
    """
    if product in depths:
        raise ProcessError(f"{product.name} cannot be both oxidised and the oxide it becomes")
    before = state.copy()
    converted = etch_isotropic(
        state, depths, resolution, opening, xy_resolution, square=square
    )
    # The skin the etch took is void now, and may have left the stack
    # altogether (a top layer consumed whole is dropped as a void slab), so
    # the comparison walks the planes of both states and the product goes
    # back in through the film merge, which also recreates slabs above.
    planes = sorted(set(before.z_planes) | set(state.z_planes))
    pieces: list[tuple[float, float, MultiPolygon]] = []
    for za, zb in zip(planes[:-1], planes[1:]):
        if zb - za <= 0:
            continue
        zm = (za + zb) / 2
        was = before.slab_at(zm)
        if was is None:
            continue
        had = [was.regions[m] for m in depths if m in was.regions]
        if not had:
            continue
        now = state.slab_at(zm)
        still = [] if now is None else [now.regions[m] for m in depths if m in now.regions]
        gone = shapely.unary_union(had)
        if still:
            gone = gone.difference(shapely.unary_union(still))
        gone = state.clean(gone)
        if not gone.is_empty:
            pieces.append((za, zb, gone))
    changed = apply_film(state, product, pieces)
    state.harmonize(changed)
    state.consolidate()
    state.validate()
    return converted


__all__ = ["oxidize", "P"]
