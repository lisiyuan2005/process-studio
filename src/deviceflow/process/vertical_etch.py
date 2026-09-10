"""Vertical etch: 90 degree sidewalls, constant XY opening.

Semantics (rate model)
----------------------
* Every material has a vertical etch rate; materials not listed have rate
  0 and act as etch stops. The etch runs for a time budget ``time``.
* The front starts at the local surface inside the mask opening. In a
  material with rate ``r`` a thickness ``h`` costs ``h / r`` of the budget.
  Void costs nothing (the front passes to the next surface). A rate-0
  material stops the front for good in that column.
* ``depth``/``target`` calls are the special case of equal unit rates with
  ``time = depth``.

Implementation: the mask opening is tracked top-down as *pieces*
``(region, remaining_time)``; ``remaining_time is None`` until the front
has met a solid with rate > 0. Each slab is split at the plane where the
shallowest (piece, material) runs out of budget, so a slab is always etched
through its full thickness. Everything is a 2D polygon Boolean.
"""

from __future__ import annotations

import shapely
from shapely.geometry import MultiPolygon

from .._internal.geometry import polygons as P
from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material

Piece = tuple[MultiPolygon, float | None]
TIME_EPS = 1e-12


def etch_vertical(
    state: ProcessState, opening: MultiPolygon, rates: dict[Material, float], time: float
) -> dict[Material, float]:
    """Etch inside ``opening``; returns removed volume per material."""
    rates = {m: float(r) for m, r in rates.items() if r > 0}
    if not rates:
        raise ProcessError("etch needs at least one material with a positive rate")
    if not time > 0:
        raise ProcessError(f"etch time must be positive, got {time}")
    opening = state.clean(opening)
    before = {m: state.volume(m) for m in rates}
    if opening.is_empty:
        return {m: 0.0 for m in rates}

    pieces: list[Piece] = [(opening, None)]
    changed: list = []
    i = len(state.slabs) - 1
    while i >= 0 and pieces:
        slab = state.slabs[i]
        present = [m for m in rates if m in slab.regions]

        # Split so that every (piece, material) etching here etches the whole slab.
        z_split = None
        for region, remaining in pieces:
            budget = time if remaining is None else remaining
            for m in present:
                if budget * rates[m] < slab.thickness and shapely.intersects(region, slab.regions[m]):
                    z = znorm(slab.z1 - budget * rates[m])
                    z_split = z if z_split is None else max(z_split, z)
        if z_split is not None and slab.z0 < z_split < slab.z1:
            state.split_at(z_split)
            i += 1  # the upper part of the split
            slab = state.slabs[i]

        occupied = slab.occupied()
        thickness = slab.thickness
        etched: dict[Material, MultiPolygon] = {}
        next_pieces: list[Piece] = []
        for region, remaining in pieces:
            void = state.clean(region.difference(occupied))
            if not void.is_empty:
                next_pieces.append((void, remaining))  # passes through, no time consumed
            budget = time if remaining is None else remaining
            for m in present:
                hit = state.clean(region.intersection(slab.regions[m]))
                if hit.is_empty:
                    continue
                etched[m] = P.as_multipolygon(etched[m].union(hit)) if m in etched else hit
                left = budget - thickness / rates[m]
                if left > TIME_EPS:
                    next_pieces.append((hit, left))
            # parts over rate-0 material are blocked and simply dropped

        if etched:
            changed.append(slab)
            for m, region in etched.items():
                g = state.clean(slab.regions[m].difference(region))
                if g.is_empty:
                    del slab.regions[m]
                else:
                    slab.regions[m] = g
        pieces = next_pieces
        i -= 1

    state.harmonize(changed)
    state.consolidate()
    state.validate()
    return {m: before[m] - state.volume(m) for m in rates}
