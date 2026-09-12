"""Planar deposition: the film arrives from straight above.

The film forms wherever material can arrive from the sky, the way a
sputtered or evaporated film lands: tops, floors and the walls that face
upward all receive ``thickness`` (walls included, so a hole narrows as it
fills and a step is coated on its riser), while a recess under an overhang
(a cavity etched sideways into a wall) receives nothing because nothing
arrives there from above. A step therefore stays a step of the same
height and a hole keeps its depth while its floor rises. On an empty
device the film is a blanket slab from z = 0; that is how a substrate is
made. The film is the conformal film cut to what the sky can see, so it
is sampled in z the same way.
"""

from __future__ import annotations

from shapely.geometry import box

from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material
from .conformal import deposit_conformal


def deposit_planar(
    state: ProcessState,
    material: Material,
    thickness: float,
    resolution: float,
    xy_resolution: float | None = None,
) -> tuple[float, float]:
    """Land ``thickness`` of ``material`` on every surface seen from above; returns (z_low, z_high)."""
    t = float(thickness)
    if not t > 0:
        raise ProcessError(f"deposition thickness must be positive, got {thickness}")
    if state.top is None:
        z0, z1 = 0.0, znorm(t)
        state.add_slab(z0, z1, {material: box(*state.bounds)})
        state.consolidate()
        state.validate()
        return z0, z1
    z_low, z_high, _volume = deposit_conformal(
        state, material, t, resolution, xy_resolution, from_above=True
    )
    return z_low, z_high
