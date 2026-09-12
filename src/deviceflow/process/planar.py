"""Planar deposition: the film arrives from straight above and walls are coated.

Only what the sky can see receives film: tops, floors and the walls that
face upward, never a recess under an overhang. Two models are offered.
The detailed one is the conformal film cut to the columns with no solid
above them, so corners round with the film's radius and both lips of a
recess's mouth grow a lip: the overhang's lower edge curls down and the
wall under it caps up, the way a sputtered film pinches a mouth off. The
simplified one (``square``) is the square-cornered film of
:mod:`.square`: wall films end flat at the wall's edges, faces rise by
``t`` with square corners, and both lips stay flat. On an empty device the
film is a blanket slab from z = 0; that is how a substrate is made.
"""

from __future__ import annotations

from shapely.geometry import box

from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material
from .conformal import deposit_conformal
from .square import deposit_square


def deposit_planar(
    state: ProcessState,
    material: Material,
    thickness: float,
    resolution: float,
    xy_resolution: float | None = None,
    *,
    square: bool = False,
) -> tuple[float, float]:
    """Land ``thickness`` of ``material`` from above; returns (z_low, z_high)."""
    t = float(thickness)
    if not t > 0:
        raise ProcessError(f"deposition thickness must be positive, got {thickness}")
    if state.top is None:
        z0, z1 = 0.0, znorm(t)
        state.add_slab(z0, z1, {material: box(*state.bounds)})
        state.consolidate()
        state.validate()
        return z0, z1
    xy = resolution if xy_resolution is None else float(xy_resolution)
    if square:
        z_low, z_high, _ = deposit_square(state, material, t, xy, from_above=True)
    else:
        z_low, z_high, _ = deposit_conformal(
            state, material, t, resolution, xy, from_above=True
        )
    return z_low, z_high
