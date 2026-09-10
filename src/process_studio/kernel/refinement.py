"""Conservative, flow-driven refinement on one project-anchored lattice.

This is NOT a dynamically coupled AMR solver. Local replays are allowed only
when this planner can bound the numerical stencil's domain of dependence.
Nonlocal operations replay the full domain at the requested true spacing.
"""

from dataclasses import dataclass
import math

import numpy as np
from scipy.ndimage import find_objects, label

from .adaptive import RefinementBox
from .material_state import MaterialState
from process_studio.models import ProcessType


@dataclass(frozen=True)
class RefinementPlan:
    mode: str
    boxes: tuple[RefinementBox, ...]
    node_count: int
    reasons: tuple[str, ...]


def merge_regions(regions):
    """Merge overlapping closed integer rectangles until a fixed point."""
    result = []
    for region in regions:
        current = list(region)
        again = True
        while again:
            again = False
            remaining = []
            for other in result:
                if (current[0] <= other[1] and other[0] <= current[1]
                        and current[2] <= other[3] and other[2] <= current[3]):
                    current = [
                        min(current[0], other[0]), max(current[1], other[1]),
                        min(current[2], other[2]), max(current[3], other[3]),
                    ]
                    again = True
                else:
                    remaining.append(other)
            result = remaining
        result.append(tuple(current))
    return sorted(result)


def plan_refinement(engine, grid, project, branch, *, factor=4, max_nodes=20_000_000):
    if not isinstance(factor, int) or factor < 2 or factor & (factor - 1):
        raise ValueError("factor must be a power of two >= 2")
    if not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("max_nodes must be a positive integer")
    full_box = RefinementBox(
        "full-domain", grid.x_min, grid.x_max, grid.y_min, grid.y_max,
        grid.z_min, grid.z_max, factor,
    )
    fine = full_box.make_grid(grid)
    h = fine.dx
    reasons = []
    steps_to_sample = []
    stencil_steps = 0
    for step in branch.steps:
        if not step.enabled:
            continue
        recipe = engine.recipes[step.recipe_id]
        p = recipe.resolved_parameters(step.overrides)
        kind = recipe.process_type
        if kind is ProcessType.NO_GEOMETRY:
            continue
        if kind is ProcessType.CMP:
            # Its top/stop reductions and blanket material truncation are global.
            reasons.append(f"{step.name}: global CMP reduction/blanket operation")
            continue
        if kind is ProcessType.DEPOSIT:
            if float(p.get("target", p.get("thickness", 0))) == 0:
                continue
            if p.get("mode") not in {"directional", "evaporation", "fill"}:
                reasons.append(f"{step.name}: nonlocal conformal distance reconstruction")
            elif p.get("base_z") is None:
                reasons.append(f"{step.name}: automatic global deposition base height")
        elif kind is ProcessType.ETCH:
            if p.get("target") == 0:
                continue
            if p.get("surface_z") is None:
                reasons.append(f"{step.name}: automatic global etch surface height")
            if p.get("target") is None:
                reasons.append(f"{step.name}: time/rate-derived etch requires full-domain replay")
            else:
                fraction = float(p.get("directional_fraction", 1))
                depth = float(p["target"])
                if not 0 <= fraction <= 1 or not np.isfinite(depth) or depth < 0:
                    raise ValueError("invalid etch depth or directional fraction")
                # Exact pure directional path has no neighbor stencil.
                # Mixed HJ is explicit Euler, one grid neighbor per iteration.
                if fraction < 1 and depth > 0:
                    stencil_steps += math.ceil(
                        depth * (fraction + (1-fraction)*math.sqrt(3)) / (0.35*h)
                    )
        else:
            reasons.append(f"{step.name}: unknown numerical support")
        steps_to_sample.append(step)

    def finish(boxes, mode, why):
        count = sum(math.prod((g.nx, g.ny, g.nz))
                    for g in (box.make_grid(grid) for box in boxes))
        if count > max_nodes:
            raise MemoryError(
                f"Requested h={h*1000:g} nm needs {count:,} fine nodes; "
                f"budget is {max_nodes:,}. No coarser solve or display upsampling was substituted."
            )
        return RefinementPlan(mode, tuple(boxes), count, tuple(why))

    if reasons:
        return finish([full_box], "full-domain", reasons)
    if not steps_to_sample:
        return finish([], "no-refinement", ["no geometry-changing steps"])
    if fine.nx * fine.ny > max_nodes:
        raise MemoryError("mask planning exceeds node budget before allocation")
    mask_state = MaterialState(fine)  # grid only: no 3D arrays allocated
    active = np.zeros((fine.ny, fine.nx), dtype=bool)
    for step in steps_to_sample:
        # Include intersected edge cells; CSG fields are 1-Lipschitz.
        active |= engine.resolve_mask_level_set(mask_state, step, project) <= h*math.sqrt(2)
    components, _ = label(active)
    regions = []
    # Entire numerical influence, plus one interpolation cell. Add another
    # stencil radius for a core unaffected by independently solved boundaries.
    padding = 2 * stencil_steps + 2
    for slices in find_objects(components):
        if slices is None:
            continue
        sy, sx = slices
        regions.append((
            max(0, sx.start-padding), min(fine.nx-1, sx.stop-1+padding),
            max(0, sy.start-padding), min(fine.ny-1, sy.stop-1+padding),
        ))
    regions = merge_regions(regions)
    boxes = []
    guard = stencil_steps + 1
    for index, (x0, x1, y0, y1) in enumerate(regions):
        # Physical domain boundaries are shared with the monolithic solve.
        cx0, cx1 = (x0 if x0 == 0 else x0+guard), (x1 if x1 == fine.nx-1 else x1-guard)
        cy0, cy1 = (y0 if y0 == 0 else y0+guard), (y1 if y1 == fine.ny-1 else y1-guard)
        boxes.append(RefinementBox(
            f"region-{index}", fine.x[x0], fine.x[x1], fine.y[y0], fine.y[y1],
            fine.z_min, fine.z_max, factor,
            fine.x[cx0], fine.x[cx1], fine.y[cy0], fine.y[cy1],
        ))
    return finish(
        boxes, "bounded-local",
        [f"project-anchored lattice; {stencil_steps} accumulated stencil steps; "
         "overlapping solve regions merged; full z extent"],
    )
