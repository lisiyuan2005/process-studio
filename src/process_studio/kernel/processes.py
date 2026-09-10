"""Process-oriented wrappers around the numerical level-set operations."""

from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import binary_propagation

from .level_set import (
    evolve_advection,
    godunov_gradient_norm,
    reinitialize_signed_distance,
    reinitialize_signed_distance_subcell,
    upwind_advection_term,
)
from .masks import signed_distance as mask_signed_distance


def directional_trench_etch(
    phi: np.ndarray,
    exposure_mask: np.ndarray,
    spacing: float,
    target_depth: float,
    *,
    etch_rate: float = 1.0,
    cfl: float = 0.8,
    reinitialize: bool = False,
) -> tuple[np.ndarray, int]:
    """Etch a three-dimensional structure vertically down through a 2D mask.

    Arrays use ``(z, y, x)`` order. ``True`` mask pixels are exposed. The
    target-geometry mode maps depth to an equivalent time using ``etch_rate``.
    """
    if phi.ndim != 3:
        raise ValueError("directional trench etch requires a 3D level set")
    if exposure_mask.shape != phi.shape[1:]:
        raise ValueError("exposure mask shape must match the level set y/x plane")
    if exposure_mask.dtype != np.bool_:
        exposure_mask = exposure_mask.astype(bool)
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if target_depth < 0:
        raise ValueError("target_depth cannot be negative")
    if etch_rate <= 0:
        raise ValueError("etch_rate must be positive")

    if target_depth == 0 or not exposure_mask.any():
        return np.asarray(phi, dtype=float).copy(), 0

    vertical_velocity = np.broadcast_to(
        np.where(exposure_mask, -etch_rate, 0.0)[None, :, :], phi.shape
    )
    duration = target_depth / etch_rate
    result, steps = evolve_advection(
        phi,
        spacing,
        (vertical_velocity, 0.0, 0.0),
        duration,
        cfl=cfl,
    )
    if reinitialize:
        result = reinitialize_signed_distance(result, spacing)
    return result, steps


def mixed_trench_etch(
    phi: np.ndarray,
    exposure_mask: np.ndarray,
    z: np.ndarray,
    spacing: float,
    target_depth: float,
    *,
    directional_rate: float,
    isotropic_rate: float,
    surface_z: float = 0.0,
    cfl: float = 0.35,
    exposure_sdf: np.ndarray | None = None,
) -> tuple[np.ndarray, int]:
    """Etch with independently controlled directional and isotropic rates.

    The reachable void is represented by its own level set, initialized as the
    open mask prism above the wafer. Directional transport pushes that void down
    through the opening while positive normal speed expands it isotropically.
    Subtracting the evolved void from the input material naturally produces
    undercut without nucleating disconnected cavities inside the substrate.
    """
    if phi.ndim != 3:
        raise ValueError("mixed trench etch requires a 3D level set")
    if exposure_mask.shape != phi.shape[1:]:
        raise ValueError("exposure mask shape must match the level set y/x plane")
    if z.ndim != 1 or z.size != phi.shape[0]:
        raise ValueError("z must be one-dimensional and match the level set z axis")
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if target_depth < 0:
        raise ValueError("target_depth cannot be negative")
    if directional_rate < 0 or isotropic_rate < 0:
        raise ValueError("etch rates cannot be negative")
    if directional_rate + isotropic_rate <= 0:
        raise ValueError("at least one etch rate must be positive")
    if not 0 < cfl <= 0.5:
        raise ValueError("cfl must be in (0, 0.5]")

    mask = exposure_mask.astype(bool, copy=False)
    if target_depth == 0 or not mask.any():
        return np.asarray(phi, dtype=float).copy(), 0

    total_time = target_depth / (directional_rate + isotropic_rate)
    maximum_characteristic_speed = directional_rate + isotropic_rate * math.sqrt(3.0)
    stable_dt = cfl * spacing / maximum_characteristic_speed
    steps = max(1, math.ceil(total_time / stable_dt))
    dt = total_time / steps

    material = np.asarray(phi, dtype=float).copy()
    z_3d = np.broadcast_to(z[:, None, None], material.shape)
    if exposure_sdf is None:
        mask_phi = mask_signed_distance(mask, spacing)
    else:
        mask_phi = np.asarray(exposure_sdf, dtype=float)
        if mask_phi.shape != mask.shape:
            raise ValueError("exposure_sdf shape must match the mask y/x plane")
    if isotropic_rate == 0:
        # Exact translation of the mask prism for spatially constant vertical
        # etching. Applies to arbitrary masks, without a raster edge velocity.
        void_phi = np.maximum(mask_phi[None, :, :], surface_z - target_depth - z_3d)
        return np.maximum(material, -void_phi), 1
    void_phi = np.maximum(
        np.broadcast_to(mask_phi[None, :, :], material.shape),
        surface_z - z_3d,
    ).copy()
    vertical_velocity = -directional_rate

    for _ in range(steps):
        directional_term = upwind_advection_term(
            void_phi,
            spacing,
            (vertical_velocity, 0.0, 0.0),
        )
        isotropic_term = isotropic_rate * godunov_gradient_norm(
            void_phi,
            spacing,
            isotropic_rate,
        )

        void_phi -= dt * (directional_term + isotropic_term)
    # The ideal mask protects the top surface, but not the subsurface undercut.
    # Constraining every intermediate advection inlet would suppress undercut
    # by continuously importing the original opening into the growing void.
    inlet = z >= surface_z - spacing * 1e-9
    void_phi[inlet] = np.maximum(void_phi[inlet], mask_phi[None, :, :])
    etched = np.maximum(material, -void_phi)
    return etched, steps


def conformal_deposition(
    phi: np.ndarray,
    spacing: float,
    target_thickness: float,
    *,
    deposition_rate: float = 1.0,
    rebuild_distance: bool = True,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Deposit a conformal film by expanding every exposed material surface.

    The returned fields are ``(combined_phi, film_phi, steps)``. Both level sets
    use the kernel convention ``phi < 0`` inside their respective material.
    ``film_phi`` is constructed as the newly occupied region outside the input
    material, so a later multi-material renderer can color the film separately.
    """
    if phi.ndim not in (2, 3):
        raise ValueError("conformal deposition requires a 2D or 3D level set")
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if target_thickness < 0:
        raise ValueError("target_thickness cannot be negative")
    if deposition_rate <= 0:
        raise ValueError("deposition_rate must be positive")

    original = np.asarray(phi, dtype=float).copy()
    if target_thickness == 0:
        return original, np.abs(original), 0

    reference = (
        reinitialize_signed_distance_subcell(original, spacing)
        if rebuild_distance
        else original
    )
    # For a constant conformal thickness, offsetting a signed-distance field is
    # the exact level-set operation. It also handles front merging without the
    # small positive plateaus that explicit time integration can leave behind.
    combined = reference - target_thickness
    film = np.maximum(combined, -original)
    # Ambient can enter through any open simulation boundary. A pre-existing
    # sealed pore has no material supply and must not receive a new film.
    # Connectivity is tested at this step's start; sub-step pinch-off transport
    # is intentionally not claimed by this constant-thickness geometry model.
    air = original > 0
    seeds = np.zeros(original.shape, dtype=bool)
    for axis in range(original.ndim):
        for index in (0, -1):
            face = [slice(None)] * original.ndim
            face[axis] = index
            seeds[tuple(face)] = air[tuple(face)]
    reachable = binary_propagation(seeds, mask=air)
    sealed = air & ~reachable
    film[sealed] = np.maximum(film[sealed], original[sealed])
    return np.minimum(original, film), film, 1
