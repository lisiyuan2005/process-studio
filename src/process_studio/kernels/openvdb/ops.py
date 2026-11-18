"""Process steps on the OpenVDB kernel's state.

Each function takes a state and returns a new one; the old state is never
changed. The geometry follows the slab kernel's definitions (DeviceFlow's),
so a flow means the same thing on both:

* deposition -- conformal: every surface grows by the thickness; planar:
  the conformal film cut to the columns the sky sees (no solid anywhere
  above). Inside a mask, the lift-off result: only the space the gas
  reaches through the opening, grown from the surfaces it reaches;
* vertical etch -- per column from the top, a length of material costing
  length / rate of the time, open space free, a material with no rate a
  stop;
* isotropic etch -- every target within rate x time of the live open space
  along a path through open space and targets is removed; other materials
  are walls the front goes round; oxidation is the same front, turning what
  it consumes into the product in place;
* flip -- the wafer turned over, mirrored sideways about the turning axis;
* CMP -- everything above a plane removed.
"""

from __future__ import annotations

import math
from typing import Callable, Mapping

import numpy as np
from scipy import ndimage

from .field import (
    HALF_WIDTH,
    column_front,
    dilate,
    reached,
    shadow,
    skin_distance,
    solid_field,
    square_front,
    travel_time,
    union,
)
from .state import Block, VdbState


def _margin(length: float, g: float) -> int:
    return int(math.ceil(max(length, 0.0) / g)) + int(HALF_WIDTH) + 3


def _opening_block(opening_fn, block: Block):
    return None if opening_fn is None else opening_fn(block)


def deposit(
    state: VdbState,
    material: str,
    thickness: float,
    *,
    planar: bool,
    square: bool,
    opening_fn=None,
) -> VdbState:
    g = state.lattice.g
    block = state.block(headroom=thickness, margin=_margin(thickness, g))
    phis = state.load(block)
    solid = solid_field(phis.values(), g, like=np.zeros(block.shape, np.float32))
    grown = dilate(solid, thickness, block, square)
    film = np.maximum(grown, -solid)
    if planar:
        film = np.maximum(film, -shadow(solid))
    opening = _opening_block(opening_fn, block)
    if opening is not None:
        reach, resist = reached(solid, opening, block)
        # not in the resist, which fills the sky outside the opening
        resist_out = np.minimum(shadow(solid), opening[:, :, None])
        film = np.maximum(film, resist_out)
        # grown only from the faces the gas got to, measured from them, so a
        # wall the resist covers puts no film into a nearby opening
        reach_from = skin_distance(solid, reach, g, square) - thickness - 0.5 * g
        film = np.maximum(film, reach_from)
        stranded = (solid > 0) & ~reach & ~resist
        film[stranded] = np.maximum(film[stranded], HALF_WIDTH * g)
    if material in phis:
        film = np.minimum(phis[material], film)
    return state.with_fields(block, {material: film.astype(np.float32)})


def vertical_etch(
    state: VdbState,
    rates: Mapping[str, float],
    budget: float,
    *,
    opening_fn=None,
) -> VdbState:
    """``rates`` are per material (µm per unit of ``budget``); unlisted or
    zero-rate materials stop the front."""
    block = state.block(margin=1)
    phis = state.load(block)
    if not phis:
        return state
    opening = _opening_block(opening_fn, block)
    columns = np.ones(block.shape[:2], dtype=bool) if opening is None else opening < 0
    costs = {name: (1.0 / rate if rate > 0 else math.inf) for name, rate in rates.items()}
    front = column_front(phis, costs, budget, block, columns)
    _, _, z = block.axes()
    with np.errstate(invalid="ignore"):
        # columns outside the opening are walled off by the opening's own
        # distance alone, so the wall sits where the mask edge is
        removed = np.where(np.isfinite(front)[:, :, None], front[:, :, None] - z, -HALF_WIDTH * block.g)
    if opening is not None:
        removed = np.maximum(removed, opening[:, :, None])
    removed = removed.astype(np.float32)
    changed = {
        name: np.maximum(phis[name], -removed)
        for name, rate in rates.items()
        if rate > 0 and name in phis
    }
    return state.with_fields(block, changed)


def isotropic(
    state: VdbState,
    rates: Mapping[str, float],
    duration: float,
    *,
    square: bool,
    opening_fn=None,
    product: str | None = None,
    check: Callable[[], None] | None = None,
) -> VdbState:
    """Remove every target within its rate x ``duration`` of the live open
    space; with ``product``, what is removed becomes that material."""
    g = state.lattice.g
    active = {name: float(rate) for name, rate in rates.items() if rate > 0}
    reach_len = max(active.values()) * duration
    # A closed window's margin continues its edge columns outward, and no
    # front gets anywhere sooner through such a copy than through the column
    # itself; only a periodic window needs the whole reach of its neighbours.
    block = state.block(margin=_margin(reach_len if state.lattice.periodic else 0.0, g))
    phis = state.load(block)
    targets = [name for name in active if name in phis]
    if not targets:
        return state
    solid = solid_field(phis.values(), g)
    opening = _opening_block(opening_fn, block)
    live, resist = reached(solid, opening, block)
    bg = np.float32(HALF_WIDTH * g)
    if check is not None:
        check()
    if square:
        lost = square_front(solid, phis, active, live, duration, g, check)
    else:
        time = travel_time(solid, phis, active, live, resist, g)
        lost = {}
        for name in targets:
            with np.errstate(invalid="ignore"):
                reach = np.where(np.isfinite(time), (time - duration) * active[name], bg)
            lost[name] = np.maximum(phis[name], reach.astype(np.float32))
    changed = {}
    for name in targets:
        changed[name] = np.maximum(phis[name], -np.clip(lost[name], -bg, bg))
    if product is not None:
        made = union((np.clip(lost[name], -bg, bg) for name in targets))
        if product in phis:
            made = np.minimum(phis[product], made)
        changed[product] = made
    return state.with_fields(block, changed)


def cmp(state: VdbState, height: float) -> VdbState:
    block = state.block(margin=0)
    phis = state.load(block)
    _, _, z = block.axes()
    plane = (z - height).astype(np.float32)
    return state.with_fields(block, {name: np.maximum(phi, plane) for name, phi in phis.items()})


def _resample(array: np.ndarray, positions: np.ndarray, axis: int) -> np.ndarray:
    """``array`` read at fractional indices ``positions`` along ``axis``,
    linear between samples and continued at the ends."""
    n = array.shape[axis]
    p = np.clip(positions, 0.0, n - 1.0)
    low = np.floor(p).astype(int)
    high = np.minimum(low + 1, n - 1)
    w = (p - low).astype(np.float32)
    shape = [1, 1, 1]
    shape[axis] = -1
    w = w.reshape(shape)
    return np.take(array, low, axis=axis) * (1 - w) + np.take(array, high, axis=axis) * w


def flip(state: VdbState, axis: str, top: float) -> VdbState:
    """Turn the wafer over about the x or the y axis.

    Heights go to ``top + floor - z``: the old surface lands on the floor
    and the old floor becomes the new top. Void above the stack is not
    carried over, and turning over mirrors sideways: about y mirrors x.
    """
    lat = state.lattice
    block = state.block(margin=0)
    phis = state.load(block)
    _, _, z = block.axes()
    zs = z.ravel()
    source_k = lat.k_of(top + lat.z_floor - zs) - block.k0
    if axis == "y":
        x = lat.x_of(np.arange(lat.nx))
        source = (lat.x0 + lat.x1 - x - lat.origin[0]) / lat.g
        lateral = 0
    else:
        y = lat.y_of(np.arange(lat.ny))
        source = (lat.y0 + lat.y1 - y - lat.origin[1]) / lat.g
        lateral = 1
    lid = (z - top).astype(np.float32)
    changed = {}
    for name, phi in phis.items():
        turned = _resample(_resample(phi, source_k, 2), source, lateral)
        changed[name] = np.maximum(turned, lid)
    return state.with_fields(block, changed)
