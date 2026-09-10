"""Level-set geometry kernel."""

from .adaptive import (
    AdaptiveMaterialState,
    AdaptivePatch,
    RefinementBox,
    tiled_refinement_boxes,
)
from .grid import UniformGrid2D, UniformGrid3D
from .material_state import MaterialState, signed_distance_from_inside
from .multimaterial import (
    cmp_planarize,
    deposit_material,
    patterned_deposit,
    selective_etch,
)
from .level_set import (
    evolve_advection,
    evolve_constant_normal_speed,
    evolve_normal_speed,
    reinitialize_signed_distance,
    reinitialize_signed_distance_subcell,
    upwind_advection_term,
)
from .processes import conformal_deposition, directional_trench_etch, mixed_trench_etch

__all__ = [
    "AdaptiveMaterialState",
    "AdaptivePatch",
    "RefinementBox",
    "tiled_refinement_boxes",
    "UniformGrid2D",
    "UniformGrid3D",
    "MaterialState",
    "signed_distance_from_inside",
    "deposit_material",
    "patterned_deposit",
    "selective_etch",
    "cmp_planarize",
    "evolve_constant_normal_speed",
    "evolve_advection",
    "evolve_normal_speed",
    "directional_trench_etch",
    "mixed_trench_etch",
    "conformal_deposition",
    "reinitialize_signed_distance",
    "reinitialize_signed_distance_subcell",
    "upwind_advection_term",
]
