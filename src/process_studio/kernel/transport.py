"""Synchronized tiled Hamilton--Jacobi transport on a uniform lattice.

Tiles are execution units, not independently evolved geometries. Every RK stage
reads one immutable global state and writes disjoint cores to a new state.
Two ghost nodes are required for the limited second-order stencil.
Full fields are still stored: this is not sparse storage or multilevel AMR.
"""

from itertools import product
import math

import numpy as np


def stencil_reach_per_step(order):
    if isinstance(order, bool) or not isinstance(order, (int, np.integer)) or order not in (1, 2):
        raise ValueError("solver_order must be 1 or 2")
    return 1 if order == 1 else 4  # two neighbors per stage, two RK stages


def _minmod(a, b):
    return np.where(a*b > 0, np.sign(a)*np.minimum(np.abs(a), np.abs(b)), 0.0)


def _rhs(phi, h, normal_speed, velocity, order):
    transport = np.zeros_like(phi)
    gradient2 = np.zeros_like(phi)
    for axis, v in enumerate(velocity):
        if normal_speed == 0 and v == 0:
            continue
        left, right = np.roll(phi, 1, axis), np.roll(phi, -1, axis)
        backward, forward = (phi-left)/h, (right-phi)/h
        if order == 2:
            curvature = (right-2*phi+left)/(h*h)
            backward += .5*h*_minmod(curvature, np.roll(curvature, 1, axis))
            forward -= .5*h*_minmod(curvature, np.roll(curvature, -1, axis))
        if v != 0:
            transport += v*(backward if v > 0 else forward)
        if normal_speed > 0:
            gradient2 += np.maximum(np.maximum(backward, 0)**2, np.minimum(forward, 0)**2)
        elif normal_speed < 0:
            gradient2 += np.maximum(np.minimum(backward, 0)**2, np.maximum(forward, 0)**2)
    return -transport-normal_speed*np.sqrt(gradient2)


def _tiles(shape, tile_shape):
    for start in product(*(range(0, n, t) for n, t in zip(shape, tile_shape, strict=True))):
        yield tuple(slice(s, min(s+t, n)) for s, t, n in zip(start, tile_shape, shape, strict=True))


def _stage(source, h, normal_speed, velocity, order, dt, tiles, *, original=None):
    destination = np.empty_like(source)
    halo = order
    for core in tiles:
        region = tuple(slice(max(0, s.start-halo), min(n, s.stop+halo))
                       for s, n in zip(core, source.shape, strict=True))
        padding = tuple((max(0, halo-s.start), max(0, s.stop+halo-n))
                        for s, n in zip(core, source.shape, strict=True))
        # Only physical domain faces receive constant extrapolation.
        # Interior ghost values come directly from the same-stage neighbor.
        local = np.pad(source[region], padding, mode="edge")
        inner = tuple(slice(halo, halo+s.stop-s.start) for s in core)
        advanced = source[core]+dt*_rhs(local, h, normal_speed, velocity, order)[inner]
        destination[core] = advanced if original is None else .5*original[core]+.5*advanced
    return destination


def evolve_hamilton_jacobi(
    phi, spacing, normal_speed, velocity, total_time, *,
    order=2, tile_shape=None, cfl=.35, diagnostics=None,
):
    """Solve phi_t + v.grad(phi) + F|grad(phi)| = 0 with constant F and v.

    order=1: Godunov/upwind + Euler; order=2: minmod-limited derivative
    reconstruction + SSP-RK2. Limiting can reduce local order near corners.
    The external boundary condition is constant ghost extrapolation.
    """
    stencil_reach_per_step(order)
    phi = np.asarray(phi, dtype=float)
    if phi.ndim not in (2, 3) or min(phi.shape) < 3 or not np.isfinite(phi).all():
        raise ValueError("phi must be a finite 2D/3D field with >= 3 nodes per axis")
    velocity = tuple(velocity)
    if len(velocity) != phi.ndim or any(np.ndim(v) != 0 for v in velocity):
        raise ValueError("velocity requires one constant scalar per axis")
    if np.ndim(normal_speed) != 0 or not np.isfinite([spacing, normal_speed, total_time, *velocity]).all():
        raise ValueError("finite scalar spacing, speeds and total_time are required")
    if spacing <= 0 or total_time < 0 or not 0 < cfl <= .35:
        raise ValueError("positive spacing, nonnegative time and 0 < cfl <= .35 required")
    if tile_shape is None:
        tile_shape = phi.shape
    elif isinstance(tile_shape, int) and not isinstance(tile_shape, bool):
        tile_shape = (tile_shape,)*phi.ndim
    elif np.ndim(tile_shape) == 0:
        raise ValueError("tile_shape requires positive integer sizes")
    else:
        tile_shape = tuple(tile_shape)
    if len(tile_shape) != phi.ndim or any(isinstance(t, bool) or not isinstance(t, int) or t < 1 for t in tile_shape):
        raise ValueError("tile_shape requires a positive integer per axis")
    tile_count = math.prod(math.ceil(n/t) for n, t in zip(phi.shape, tile_shape, strict=True))
    maximum = sum(abs(v) for v in velocity)+math.sqrt(phi.ndim)*abs(normal_speed)
    steps = math.ceil(total_time*maximum/(cfl*spacing)) if maximum*total_time else 0
    state = phi.copy()
    dt = total_time/steps if steps else 0
    for _ in range(steps):
        stage = _stage(
            state, spacing, normal_speed, velocity, order, dt,
            _tiles(phi.shape, tile_shape),
        )
        state = stage if order == 1 else _stage(
            stage, spacing, normal_speed, velocity, order, dt,
            _tiles(phi.shape, tile_shape), original=state,
        )
    if diagnostics is not None:
        diagnostics.update({
            "order": order, "steps": steps, "stages_per_step": order,
            "tiles": tile_count, "ghost_nodes": order,
            "max_tile_work_nodes": math.prod(min(n, t)+2*order for n, t in zip(phi.shape, tile_shape, strict=True)),
            "global_nodes": phi.size, "storage": "dense-uniform",
        })
    return state, steps
