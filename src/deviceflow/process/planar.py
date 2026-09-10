"""Planar (blanket) deposition.

Creates an ideal flat film of the given thickness over the whole device
bounds, sitting on the current top plane. It does not fill voids below the
top plane; it exists to initialise structures.
"""

from __future__ import annotations

from shapely.geometry import box

from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material


def deposit_planar(state: ProcessState, material: Material, thickness: float) -> tuple[float, float]:
    """Add a blanket slab [top, top + thickness) of ``material``; returns (z0, z1)."""
    if not thickness > 0:
        raise ProcessError(f"deposition thickness must be positive, got {thickness}")
    z0 = state.top if state.top is not None else 0.0
    z1 = znorm(z0 + thickness)
    state.add_slab(z0, z1, {material: box(*state.bounds)})
    state.consolidate()
    state.validate()
    return z0, z1
