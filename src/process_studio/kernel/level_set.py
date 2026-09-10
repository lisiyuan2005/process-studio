"""First-order Hamilton-Jacobi level-set evolution."""

from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import distance_transform_edt
import skfmm


def _one_sided_differences(
    phi: np.ndarray, spacing: float
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return backward/forward differences for every array axis."""
    differences: list[tuple[np.ndarray, np.ndarray]] = []
    for axis in range(phi.ndim):
        previous = np.roll(phi, 1, axis=axis)
        following = np.roll(phi, -1, axis=axis)

        first = [slice(None)] * phi.ndim
        first[axis] = 0
        previous[tuple(first)] = phi[tuple(first)]

        last = [slice(None)] * phi.ndim
        last[axis] = -1
        following[tuple(last)] = phi[tuple(last)]

        differences.append(((phi - previous) / spacing, (following - phi) / spacing))
    return differences


def godunov_gradient_norm(
    phi: np.ndarray, spacing: float, speed: float | np.ndarray
) -> np.ndarray:
    """Compute the monotone Godunov approximation of ``|grad(phi)|``."""
    speed_array = np.broadcast_to(np.asarray(speed, dtype=float), phi.shape)
    positive_sq = np.zeros_like(phi, dtype=float)
    negative_sq = np.zeros_like(phi, dtype=float)

    for backward, forward in _one_sided_differences(phi, spacing):
        positive_sq += np.maximum(
            np.maximum(backward, 0.0) ** 2,
            np.minimum(forward, 0.0) ** 2,
        )
        negative_sq += np.maximum(
            np.minimum(backward, 0.0) ** 2,
            np.maximum(forward, 0.0) ** 2,
        )

    return np.sqrt(np.where(speed_array >= 0, positive_sq, negative_sq))


def evolve_normal_speed(
    phi: np.ndarray,
    spacing: float,
    speed: float | np.ndarray,
    total_time: float,
    *,
    cfl: float = 0.45,
) -> tuple[np.ndarray, int]:
    """Evolve an interface under scalar or spatially varying normal speed."""
    if phi.ndim not in (2, 3):
        raise ValueError("phi must be a two- or three-dimensional array")
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if total_time < 0:
        raise ValueError("total_time cannot be negative")
    if not 0 < cfl <= 0.5:
        raise ValueError("cfl must be in (0, 0.5]")

    speed_array = np.broadcast_to(np.asarray(speed, dtype=float), phi.shape)
    maximum_speed = float(np.max(np.abs(speed_array)))
    if maximum_speed == 0 or total_time == 0:
        return np.asarray(phi, dtype=float).copy(), 0

    stable_dt = cfl * spacing / (maximum_speed * math.sqrt(phi.ndim))
    steps = max(1, math.ceil(total_time / stable_dt))
    dt = total_time / steps
    result = np.asarray(phi, dtype=float).copy()

    for _ in range(steps):
        gradient_norm = godunov_gradient_norm(result, spacing, speed_array)
        result -= dt * speed_array * gradient_norm

    return result, steps


def evolve_constant_normal_speed(
    phi: np.ndarray,
    spacing: float,
    speed: float,
    total_time: float,
    *,
    cfl: float = 0.45,
) -> tuple[np.ndarray, int]:
    """Evolve ``phi_t + speed * |grad(phi)| = 0``.

    Positive speed expands the negative region (deposition). Negative speed
    contracts it (etching). The returned integer is the number of time steps.
    """
    return evolve_normal_speed(phi, spacing, speed, total_time, cfl=cfl)


def evolve_advection(
    phi: np.ndarray,
    spacing: float,
    velocity: tuple[float | np.ndarray, ...],
    total_time: float,
    *,
    cfl: float = 0.8,
) -> tuple[np.ndarray, int]:
    """Advect a level set with a velocity component for each array axis."""
    if phi.ndim not in (2, 3):
        raise ValueError("phi must be a two- or three-dimensional array")
    if len(velocity) != phi.ndim:
        raise ValueError("velocity must contain one component per array axis")
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if total_time < 0:
        raise ValueError("total_time cannot be negative")
    if not 0 < cfl <= 1:
        raise ValueError("cfl must be in (0, 1]")

    components = [
        np.broadcast_to(np.asarray(component, dtype=float), phi.shape) for component in velocity
    ]
    local_l1_speed = np.zeros_like(phi, dtype=float)
    for component in components:
        local_l1_speed += np.abs(component)
    maximum_l1_speed = float(np.max(local_l1_speed))
    if maximum_l1_speed == 0 or total_time == 0:
        return np.asarray(phi, dtype=float).copy(), 0

    stable_dt = cfl * spacing / maximum_l1_speed
    steps = max(1, math.ceil(total_time / stable_dt))
    dt = total_time / steps
    result = np.asarray(phi, dtype=float).copy()

    for _ in range(steps):
        transport = upwind_advection_term(result, spacing, tuple(components))
        result -= dt * transport

    return result, steps


def upwind_advection_term(
    phi: np.ndarray,
    spacing: float,
    velocity: tuple[float | np.ndarray, ...],
) -> np.ndarray:
    """Return the monotone upwind approximation of ``velocity dot grad(phi)``."""
    if len(velocity) != phi.ndim:
        raise ValueError("velocity must contain one component per array axis")
    if spacing <= 0:
        raise ValueError("spacing must be positive")

    transport = np.zeros_like(phi, dtype=float)
    for axis, component in enumerate(velocity):
        if np.ndim(component) == 0 and component == 0:
            continue
        previous = np.roll(phi, 1, axis=axis)
        following = np.roll(phi, -1, axis=axis)
        first, last = [slice(None)] * phi.ndim, [slice(None)] * phi.ndim
        first[axis], last[axis] = 0, -1
        previous[tuple(first)] = phi[tuple(first)]
        following[tuple(last)] = phi[tuple(last)]
        backward = (phi - previous) / spacing
        forward = (following - phi) / spacing
        component_array = np.broadcast_to(np.asarray(component, dtype=float), phi.shape)
        upwind = np.where(component_array >= 0, backward, forward)
        transport += component_array * upwind
    return transport


def reinitialize_signed_distance(phi: np.ndarray, spacing: float) -> np.ndarray:
    """Rebuild an approximate signed-distance field in two or three dimensions."""
    if phi.ndim not in (2, 3):
        raise ValueError("phi must be a two- or three-dimensional array")
    if spacing <= 0:
        raise ValueError("spacing must be positive")

    inside = phi <= 0
    if inside.all() or (~inside).all():
        raise ValueError("reinitialization requires both inside and outside cells")

    distance_inside = distance_transform_edt(inside, sampling=spacing)
    distance_outside = distance_transform_edt(~inside, sampling=spacing)
    return distance_outside - distance_inside


def reinitialize_signed_distance_subcell(phi: np.ndarray, spacing: float) -> np.ndarray:
    """Second-order fast marching distance to the sampled zero interface.

    This solves the Eikonal equation, not nearest distance to disconnected
    crossing points. It remains a discretized approximation. Callers must not
    overwrite existing material boundaries with the reconstructed field.
    """
    phi = np.asarray(phi, dtype=float)
    if phi.ndim not in (2, 3):
        raise ValueError("phi must be a two- or three-dimensional array")
    if not np.isfinite(spacing) or spacing <= 0 or not np.isfinite(phi).all():
        raise ValueError("finite phi and positive finite spacing are required")
    if np.all(phi <= 0) or np.all(phi > 0):
        raise ValueError("reinitialization requires both inside and outside cells")
    return np.asarray(skfmm.distance(phi, dx=float(spacing), order=2))
