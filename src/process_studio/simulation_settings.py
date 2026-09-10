"""Project-wide spatial resolution helpers used by the desktop UI."""

from __future__ import annotations

from dataclasses import dataclass
from math import prod

from .kernel.grid import UniformGrid3D

# A desktop safety ceiling, not a physical limit: one double-precision field
# per material plus solver temporaries has to fit in memory.
MAXIMUM_NODES = 20_000_000


@dataclass(frozen=True)
class GridEstimate:
    spacing_nm: float
    shape: tuple[int, int, int]
    node_count: int
    state_bytes: int
    recommended_bytes: int


def grid_for_target_spacing(
    grid: UniformGrid3D,
    target_nm: float,
    *,
    search_radius: int = 2048,
) -> UniformGrid3D:
    """Return the closest equal-spacing grid that preserves all project bounds.

    Project extents are not necessarily equal, so rounding nx/ny/nz separately can
    create three subtly different spacings.  We instead search x interval counts
    near the requested value and keep only lattices that divide every extent.
    """
    if not 0.1 <= target_nm <= 1000.0:
        raise ValueError("grid spacing must be between 0.1 nm and 1000 nm")
    target = target_nm / 1000.0
    extents = (
        grid.x_max - grid.x_min,
        grid.y_max - grid.y_min,
        grid.z_max - grid.z_min,
    )
    center = max(2, round(extents[0] / target))
    best: tuple[float, tuple[int, int, int], float] | None = None
    lower = max(2, center - search_radius)
    upper = center + search_radius
    for x_intervals in range(lower, upper + 1):
        spacing = extents[0] / x_intervals
        intervals = tuple(round(extent / spacing) for extent in extents)
        if min(intervals) < 2:
            continue
        if any(
            abs(extent / count - spacing) > max(1e-12, spacing * 1e-9)
            for extent, count in zip(extents, intervals, strict=True)
        ):
            continue
        error = abs(spacing - target)
        if best is None or error < best[0]:
            best = (error, intervals, spacing)
    if best is None:
        raise ValueError(
            "could not preserve the project bounds with one uniform spacing; "
            "choose a nearby value"
        )
    _, intervals, _ = best
    return UniformGrid3D(
        grid.x_min,
        grid.x_max,
        grid.y_min,
        grid.y_max,
        grid.z_min,
        grid.z_max,
        intervals[0] + 1,
        intervals[1] + 1,
        intervals[2] + 1,
    )


def estimate_grid(grid: UniformGrid3D, material_fields: int) -> GridEstimate:
    """Estimate saved-state and practical working memory without allocating arrays."""
    node_count = prod((grid.nx, grid.ny, grid.nz))
    field_count = max(1, material_fields)
    state_bytes = node_count * 8 * field_count
    # Level-set evolution and distance rebuilds use several temporary float fields.
    recommended_bytes = node_count * 8 * (field_count + 12)
    return GridEstimate(
        spacing_nm=grid.dx * 1000.0,
        shape=(grid.nx, grid.ny, grid.nz),
        node_count=node_count,
        state_bytes=state_bytes,
        recommended_bytes=recommended_bytes,
    )
