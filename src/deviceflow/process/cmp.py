"""CMP (chemical-mechanical polishing): everything above a height is removed,
whatever the material. The stack is split at the height and the slabs above
it are dropped; nothing below changes."""

from __future__ import annotations

from .._internal.geometry.state import ProcessState, znorm
from ..exceptions import ProcessError
from ..material import Material


def planarize(state: ProcessState, height: float) -> dict[Material, float]:
    """Cut the stack at ``height``; returns the removed volume per material."""
    h = znorm(float(height))
    if state.floor is None:
        raise ProcessError("cmp on an empty device: nothing to polish")
    if h <= state.floor:
        raise ProcessError(f"cmp height {height} must lie above the device floor {state.floor}")
    materials = {m for s in state.slabs for m in s.regions}
    before = {m: state.volume(m) for m in materials}
    if h < state.top:
        state.split_at(h)
        state._slabs = [s for s in state.slabs if s.z1 <= h]
        state.consolidate()
        state.validate()
    return {m: before[m] - state.volume(m) for m in materials if before[m] - state.volume(m) > 0}
