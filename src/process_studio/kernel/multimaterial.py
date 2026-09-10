"""Multi-material process operations used by the process engine."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np

from .material_state import MaterialState
from .masks import signed_distance as mask_signed_distance
from .processes import conformal_deposition, mixed_trench_etch


def deposit_material(
    state: MaterialState,
    material: str,
    thickness: float,
    *,
    rate: float = 1.0,
) -> MaterialState:
    result = state.clone()
    if thickness == 0:
        return result
    combined, film, _ = conformal_deposition(
        result.combined_phi(),
        result.grid.dx,
        thickness,
        deposition_rate=rate,
    )
    del combined
    # The film already excludes the original union. Re-clipping old phases
    # would move buried interfaces merely because another film was deposited.
    result.add_material(material, film, merge_existing=True, resolve_overlap=False)
    return result


def patterned_deposit(
    state: MaterialState,
    material: str,
    exposure_mask: np.ndarray,
    thickness: float,
    *,
    base_z: float | None = None,
    exposure_sdf: np.ndarray | None = None,
) -> MaterialState:
    """Deposit a vertical prism through a mask, approximating evaporation/fill."""
    if exposure_mask.shape != (state.grid.ny, state.grid.nx):
        raise ValueError("exposure mask must match the grid y/x plane")
    if thickness < 0:
        raise ValueError("thickness cannot be negative")
    if thickness == 0 or not exposure_mask.any():
        return state.clone()
    if base_z is None:
        occupied = np.argwhere(state.occupied())
        base_z = (
            float(state.grid.z[occupied[:, 0].max()])
            if occupied.size
            else 0.0
        )
    top_z = base_z + thickness
    if exposure_sdf is None:
        mask_phi = mask_signed_distance(exposure_mask, state.grid.dx)
    else:
        mask_phi = np.asarray(exposure_sdf, dtype=float)
        if mask_phi.shape != exposure_mask.shape:
            raise ValueError("exposure_sdf must match the grid y/x plane")
    z = state.grid.z[:, None, None]
    slab_phi = np.maximum(base_z - z, z - top_z)
    prism = np.maximum(mask_phi[None, :, :], slab_phi)
    result = state.clone()
    result.add_material(material, prism, merge_existing=True, resolve_overlap=True)
    return result


def selective_etch(
    state: MaterialState,
    exposure_mask: np.ndarray,
    target_depth: float,
    material_rates: Mapping[str, float],
    *,
    directional_fraction: float = 1.0,
    surface_z: float = 0.0,
    exposure_sdf: np.ndarray | None = None,
) -> MaterialState:
    """Apply a rate-scaled etch volume only to materials with nonzero response."""
    if not 0.0 <= directional_fraction <= 1.0:
        raise ValueError("directional_fraction must be between 0 and 1")
    if target_depth < 0:
        raise ValueError("target_depth cannot be negative")
    positive_rates = {name: rate for name, rate in material_rates.items() if rate > 0}
    if not positive_rates or target_depth == 0:
        return state.clone()

    primary_rate = max(positive_rates.values())
    result = state.clone()
    original_union = state.combined_phi()
    for name, rate in positive_rates.items():
        if name not in state.fields:
            continue
        scaled_depth = target_depth * rate / primary_rate
        etched_union, _ = mixed_trench_etch(
            original_union,
            exposure_mask,
            state.grid.z,
            state.grid.dx,
            scaled_depth,
            directional_rate=max(rate * directional_fraction, 0.0),
            isotropic_rate=max(rate * (1.0 - directional_fraction), 0.0),
            surface_z=surface_z,
            exposure_sdf=exposure_sdf,
        )
        result.fields[name] = np.maximum(state.fields[name], etched_union)
    return result


def cmp_planarize(
    state: MaterialState,
    *,
    target_z: float | None = None,
    removal_amount: float | None = None,
    materials: Iterable[str] | None = None,
    stop_material: str | None = None,
) -> tuple[MaterialState, float]:
    """Ideal planar CMP with optional material selection and stop layer."""
    if (target_z is None) == (removal_amount is None):
        raise ValueError("provide exactly one of target_z or removal_amount")
    if removal_amount is not None and removal_amount < 0:
        raise ValueError("removal_amount cannot be negative")

    labels, _ = state.labels()
    occupied_indices = np.argwhere(labels >= 0)
    current_top = (
        float(state.grid.z[occupied_indices[:, 0].max()])
        if occupied_indices.size
        else state.grid.z_min
    )
    plane = float(target_z) if target_z is not None else current_top - float(removal_amount)

    if stop_material and stop_material in state.fields:
        stop_cells = np.argwhere(state.fields[stop_material] <= 0.0)
        if stop_cells.size:
            stop_top = float(state.grid.z[stop_cells[:, 0].max()])
            plane = max(plane, stop_top)

    selected = set(materials) if materials is not None else set(state.priority)
    result = state.clone()
    plane_phi = state.grid.z[:, None, None] - plane
    for name in selected:
        if name in result.fields and name != stop_material:
            result.fields[name] = np.maximum(result.fields[name], plane_phi)
    return result, plane
