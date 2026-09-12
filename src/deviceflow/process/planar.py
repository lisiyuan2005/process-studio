"""Planar (directional) deposition: the film arrives from straight above.

Every XY column receives ``thickness`` of material on the highest solid it
shows to the top, the way a sputtered or evaporated film with no sidewall
coverage lands: a step in the surface stays a step of the same height, a
hole keeps its depth while its floor rises, and a cavity under an overhang
(a recess etched into a sidewall) receives nothing because nothing arrives
there from above. Sidewalls get no film. A column with no solid at all (a
hole through the whole stack) gets nothing either: there is no surface for
the film to land on. On an empty device the film is a blanket slab from
z = 0; that is how a substrate is made.
"""

from __future__ import annotations

from shapely.geometry import box

from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material
from .conformal import apply_film


def deposit_planar(state: ProcessState, material: Material, thickness: float) -> tuple[float, float]:
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
    # Walk the stack from the top down. The first slab occupying a column is
    # the surface that column's film lands on; the columns it takes are
    # closed to everything below it, which is what keeps undercuts empty.
    open_columns = state.clean(box(*state.bounds))
    landings: list[tuple[float, object]] = []
    for slab in reversed(state.slabs):
        if open_columns.is_empty:
            break
        occupied = slab.occupied()
        if occupied.is_empty:
            continue
        landing = state.clean(occupied.intersection(open_columns))
        if landing.is_empty:
            continue
        landings.append((slab.z1, landing))
        open_columns = state.clean(open_columns.difference(occupied))
    landings.sort(key=lambda item: item[0])
    changed = apply_film(
        state, material, [(z, znorm(z + t), footprint) for z, footprint in landings]
    )
    state.harmonize(changed)
    state.consolidate()
    state.validate()
    z_high = state.top
    return landings[0][0], z_high if z_high is not None else znorm(landings[-1][0] + t)
