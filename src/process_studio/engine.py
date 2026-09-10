"""Process-flow execution, parameter overrides, mask resolution, and snapshots."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import numpy as np

from .kernel.material_state import MaterialState
from .kernel.masks import full_exposure, invert
from .kernel.multimaterial import (
    cmp_planarize,
    deposit_material,
    patterned_deposit,
    selective_etch,
)
from .layout.gds import rasterize_gds
from .layout.quick_sketch import QuickSketch
from .models import FlowBranch, ProcessStep, ProcessType, ProjectDefinition, Recipe
from .storage import ProjectRepository


LogCallback = Callable[[str], None]


class ProcessEngine:
    def __init__(
        self,
        recipes: Mapping[str, Recipe],
        *,
        sketches: Mapping[str, QuickSketch] | None = None,
        repository: ProjectRepository | None = None,
        logger: LogCallback | None = None,
    ) -> None:
        self.recipes = dict(recipes)
        self.sketches = dict(sketches or {})
        self.repository = repository
        self.logger = logger or (lambda _: None)

    def resolve_mask(
        self,
        state: MaterialState,
        step: ProcessStep,
        project: ProjectDefinition | None = None,
    ) -> np.ndarray:
        grid = state.grid
        if step.mask_source == "none":
            mask = full_exposure((grid.ny, grid.nx))
        elif step.mask_source == "quick_sketch":
            sketch_id = str(step.overrides.get("sketch_id", "default"))
            if sketch_id not in self.sketches:
                raise KeyError(f"quick sketch {sketch_id!r} was not found")
            mask = self.sketches[sketch_id].render(*grid.mesh_xy)
        else:
            if project is None or not project.gds_path:
                raise ValueError("the project has no GDS file")
            if step.layer is None or step.datatype is None:
                raise ValueError("GDS steps require layer and datatype")
            mask = rasterize_gds(
                project.gds_path,
                *grid.mesh_xy,
                layer=step.layer,
                datatype=step.datatype,
                keep="inside",
            )
        return invert(mask) if step.keep == "outside" else mask

    def resolve_mask_level_set(
        self,
        state: MaterialState,
        step: ProcessStep,
    ) -> np.ndarray | None:
        """Return an analytic mask SDF when the source can provide one."""
        if step.mask_source != "quick_sketch":
            return None
        sketch_id = str(step.overrides.get("sketch_id", "default"))
        if sketch_id not in self.sketches:
            raise KeyError(f"quick sketch {sketch_id!r} was not found")
        phi = self.sketches[sketch_id].signed_distance(*state.grid.mesh_xy)
        return -phi if step.keep == "outside" else phi

    def run_step(
        self,
        state: MaterialState,
        step: ProcessStep,
        *,
        project: ProjectDefinition | None = None,
    ) -> MaterialState:
        if not step.enabled:
            self.logger(f"SKIP {step.name}: disabled")
            return state.clone()
        if step.recipe_id not in self.recipes:
            raise KeyError(f"recipe {step.recipe_id!r} was not found")
        recipe = self.recipes[step.recipe_id]
        parameters = recipe.resolved_parameters(step.overrides)
        mask = self.resolve_mask(state, step, project)
        mask_level_set = self.resolve_mask_level_set(state, step)
        started = time.perf_counter()
        self.logger(f"RUN {step.name} [{recipe.process_type.value}]")

        if recipe.process_type is ProcessType.DEPOSIT:
            material = str(parameters.get("material") or recipe.output_material or "")
            if not material:
                raise ValueError("deposit recipe requires an output material")
            thickness = float(parameters.get("target", parameters.get("thickness", 0.0)))
            rate = float(parameters.get("rate", 1.0))
            if parameters.get("mode") in {"directional", "evaporation", "fill"}:
                result = patterned_deposit(
                    state,
                    material,
                    mask,
                    thickness,
                    base_z=(
                        None
                        if parameters.get("base_z") is None
                        else float(parameters["base_z"])
                    ),
                    exposure_sdf=mask_level_set,
                )
            else:
                result = deposit_material(state, material, thickness, rate=rate)
        elif recipe.process_type is ProcessType.ETCH:
            rates = {
                name: response.rate_um_per_min
                for name, response in recipe.material_responses.items()
            }
            material_rate_overrides = parameters.get("material_rates", {})
            if isinstance(material_rate_overrides, Mapping):
                rates.update(
                    {str(name): float(rate) for name, rate in material_rate_overrides.items()}
                )
            selected_material = str(parameters.get("material", ""))
            if selected_material and parameters.get("rate") is not None:
                rates[selected_material] = float(parameters["rate"])
            stop_materials = parameters.get("stop_materials", [])
            if isinstance(stop_materials, str):
                stop_materials = [
                    name.strip() for name in stop_materials.split(",") if name.strip()
                ]
            for name in stop_materials:
                rates[str(name)] = 0.0
            if not rates:
                if not selected_material:
                    raise ValueError("etch recipe requires material responses")
                rates[selected_material] = float(parameters.get("rate", 1.0))
            if parameters.get("target") is not None:
                depth = float(parameters["target"])
            elif parameters.get("time_min") is not None:
                depth = float(parameters["time_min"]) * max(rates.values())
            else:
                raise ValueError("etch recipe requires target depth or time")
            if parameters.get("surface_z") is not None:
                surface_z = float(parameters["surface_z"])
            else:
                occupied = np.argwhere(state.occupied())
                surface_z = (
                    float(state.grid.z[occupied[:, 0].max()])
                    if occupied.size
                    else 0.0
                )
            result = selective_etch(
                state,
                mask,
                depth,
                rates,
                directional_fraction=float(parameters.get("directional_fraction", 1.0)),
                surface_z=surface_z,
                exposure_sdf=mask_level_set,
            )
        elif recipe.process_type is ProcessType.CMP:
            selected = parameters.get("materials")
            if isinstance(selected, str):
                selected = [name.strip() for name in selected.split(",") if name.strip()]
            stop = next(
                (
                    name
                    for name, response in recipe.material_responses.items()
                    if response.stop_layer
                ),
                parameters.get("stop_material"),
            )
            if parameters.get("target_z") is not None:
                result, plane = cmp_planarize(
                    state,
                    target_z=float(parameters["target_z"]),
                    materials=selected,
                    stop_material=stop,
                )
            else:
                result, plane = cmp_planarize(
                    state,
                    removal_amount=float(parameters.get("removal_amount", 0.0)),
                    materials=selected,
                    stop_material=stop,
                )
            self.logger(f"CMP plane z={plane:.4f} µm")
        else:
            result = state.clone()

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.logger(f"DONE {step.name}: {elapsed_ms:.1f} ms")
        if self.repository and project:
            self.repository.log(
                project.id,
                f"{step.name} completed",
                step_id=step.id,
                elapsed_ms=elapsed_ms,
            )
        return result

    def run_branch(
        self,
        initial_state: MaterialState,
        project: ProjectDefinition,
        branch: FlowBranch,
        *,
        through_step_id: str | None = None,
    ) -> MaterialState:
        state = initial_state.clone()
        for step in branch.steps:
            state = self.run_step(state, step, project=project)
            if self.repository:
                self.repository.save_snapshot(project.id, branch.id, step.id, state)
            if step.id == through_step_id:
                break
        return state
