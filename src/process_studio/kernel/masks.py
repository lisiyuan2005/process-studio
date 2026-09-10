"""Two-dimensional process-region masks.

True values are exposed to the process. False values are protected.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt


def full_exposure(shape: tuple[int, int]) -> np.ndarray:
    """Return a mask exposing the full simulation region."""
    return np.ones(shape, dtype=bool)


def rectangle(
    xx: np.ndarray,
    yy: np.ndarray,
    *,
    center: tuple[float, float],
    size: tuple[float, float],
) -> np.ndarray:
    """Return an axis-aligned rectangular exposure mask."""
    if xx.shape != yy.shape:
        raise ValueError("xx and yy must have the same shape")
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("rectangle size must be positive")
    cx, cy = center
    tolerance = max(width, height, 1.0) * np.finfo(float).eps * 8
    return (np.abs(xx - cx) <= width / 2 + tolerance) & (
        np.abs(yy - cy) <= height / 2 + tolerance
    )


def circle(
    xx: np.ndarray,
    yy: np.ndarray,
    *,
    center: tuple[float, float],
    radius: float,
) -> np.ndarray:
    """Return a circular exposure mask."""
    if xx.shape != yy.shape:
        raise ValueError("xx and yy must have the same shape")
    if radius <= 0:
        raise ValueError("radius must be positive")
    cx, cy = center
    tolerance = max(radius, 1.0) * np.finfo(float).eps * 8
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= (radius + tolerance) ** 2


def merge(*masks: np.ndarray) -> np.ndarray:
    """Union one or more masks."""
    _validate_masks(masks)
    return np.logical_or.reduce(masks)


def intersect(*masks: np.ndarray) -> np.ndarray:
    """Intersect one or more masks."""
    _validate_masks(masks)
    return np.logical_and.reduce(masks)


def subtract(base: np.ndarray, *cuts: np.ndarray) -> np.ndarray:
    """Subtract all cut masks from a base mask."""
    _validate_masks((base, *cuts))
    if not cuts:
        return base.copy()
    return base & ~np.logical_or.reduce(cuts)


def invert(mask: np.ndarray) -> np.ndarray:
    """Swap exposed and protected regions."""
    if mask.ndim != 2:
        raise ValueError("mask must be two-dimensional")
    return ~mask


def signed_distance(mask: np.ndarray, spacing: float) -> np.ndarray:
    """Convert a binary exposure mask to an approximate signed-distance field."""
    if mask.ndim != 2:
        raise ValueError("mask must be two-dimensional")
    if spacing <= 0:
        raise ValueError("spacing must be positive")

    inside = mask.astype(bool, copy=False)
    domain_scale = max(mask.shape) * spacing
    if inside.all():
        return np.full(mask.shape, -domain_scale, dtype=float)
    if (~inside).all():
        return np.full(mask.shape, domain_scale, dtype=float)

    distance_inside = distance_transform_edt(inside, sampling=spacing)
    distance_outside = distance_transform_edt(~inside, sampling=spacing)
    return distance_outside - distance_inside


def _validate_masks(masks: tuple[np.ndarray, ...]) -> None:
    if not masks:
        raise ValueError("at least one mask is required")
    shape = masks[0].shape
    if len(shape) != 2 or any(mask.shape != shape for mask in masks):
        raise ValueError("all masks must be two-dimensional with matching shapes")
