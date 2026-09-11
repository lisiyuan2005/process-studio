"""Starter grid, material and recipe library shared by every front end.

The desktop shells create the same starting point, so a workspace opened in
one of them is the same project when opened in the other.
"""

from __future__ import annotations

from .kernel.grid import UniformGrid3D
from .models import (
    FlowBranch,
    MaterialDefinition,
    MaterialResponse,
    ProcessStep,
    ProcessType,
    Recipe,
    ToolDefinition,
)


def default_grid() -> UniformGrid3D:
    return UniformGrid3D(-0.8, 0.8, -0.8, 0.8, -0.8, 0.4, 41, 41, 31)


def default_materials() -> list[MaterialDefinition]:
    return [
        MaterialDefinition("Si", "Semiconductor", "#7b68b8", 1.0, id="material-si"),
        MaterialDefinition("SiO2", "Dielectric", "#e8c46a", 0.9, id="material-sio2"),
        MaterialDefinition("Al2O3", "Dielectric", "#ef9b35", 0.9, id="material-al2o3"),
        MaterialDefinition("SiN", "Dielectric", "#42a5a5", 0.9, id="material-sin"),
        MaterialDefinition("TiN", "Metal", "#b9a43b", 1.0, id="material-tin"),
        MaterialDefinition("W", "Metal", "#7f8790", 1.0, id="material-w"),
        MaterialDefinition("Photoresist", "Mask", "#d95c76", 0.65, id="material-pr"),
    ]


def default_tools() -> list[ToolDefinition]:
    """The benches and chambers the starter recipes name, grouped by what they do."""
    return [
        ToolDefinition("ICP-RIE", "Etch/Dry", id="tool-icp-rie"),
        ToolDefinition("Wet Bench", "Etch/Wet", id="tool-wet-bench"),
        ToolDefinition("ALD", "Deposition", id="tool-ald"),
        ToolDefinition("CMP-01", "CMP", id="tool-cmp-01"),
        ToolDefinition("Stepper", "Lithography", id="tool-stepper"),
        ToolDefinition("Ash", "Lithography", id="tool-ash"),
    ]


def default_recipes(kernel: str = "levelset") -> list[Recipe]:
    """The starter library, written for the kernel the project runs on.

    The slab kernel etches either straight down or isotropically, so its
    starter etch asks for a fully directional profile rather than the mixed
    one the level-set kernel can resolve, and its CMP does not name materials
    because that kernel polishes everything above the plane.
    """
    slab = kernel == "slab"
    return [
        Recipe(
            "Si Directional Trench Etch",
            ProcessType.ETCH,
            tool="ICP-RIE",
            parameters={"target": 0.32, "directional_fraction": 1.0 if slab else 0.9},
            material_responses={"Si": MaterialResponse("Si", 0.12)},
            group="Dry",
            id="recipe-si-trench",
        ),
        Recipe(
            "BOE Oxide Etch",
            ProcessType.ETCH,
            tool="Wet Bench",
            parameters={"time_min": 1.0, "temperature_c": 25.0, "directional_fraction": 0.0},
            material_responses={
                "SiO2": MaterialResponse("SiO2", 0.08),
                "Si": MaterialResponse("Si", 0.0, stop_layer=True),
            },
            group="Wet",
            id="recipe-boe",
        ),
        Recipe(
            "Conformal Al2O3",
            ProcessType.DEPOSIT,
            tool="ALD",
            output_material="Al2O3",
            parameters={"target": 0.04, "temperature_c": 250.0, "rate": 0.002},
            group="ALD",
            id="recipe-ald-al2o3",
        ),
        Recipe(
            "Conformal TiN",
            ProcessType.DEPOSIT,
            tool="ALD",
            output_material="TiN",
            parameters={"target": 0.04, "temperature_c": 300.0, "rate": 0.003},
            group="ALD",
            id="recipe-ald-tin",
        ),
        Recipe(
            "Ideal CMP",
            ProcessType.CMP,
            tool="CMP-01",
            parameters=(
                {"target_z": 0.0}
                if slab
                else {"target_z": 0.0, "materials": "Al2O3,TiN,W"}
            ),
            material_responses=(
                {} if slab else {"Si": MaterialResponse("Si", 0.0, stop_layer=True)}
            ),
            id="recipe-cmp",
        ),
        Recipe("Lithography", ProcessType.NO_GEOMETRY, tool="Stepper", id="recipe-litho"),
        Recipe("Resist Strip", ProcessType.NO_GEOMETRY, tool="Ash", id="recipe-strip"),
    ]


def default_branch(kernel: str = "levelset") -> FlowBranch:
    recipes = {recipe.id: recipe for recipe in default_recipes(kernel)}
    return FlowBranch(
        "main",
        [
            ProcessStep.from_recipe(
                "Lithography",
                recipes["recipe-litho"],
                mask_source="quick_sketch",
                parameters={"sketch_id": "default"},
            ),
            ProcessStep.from_recipe(
                "Trench Etch",
                recipes["recipe-si-trench"],
                mask_source="quick_sketch",
                parameters={
                    **recipes["recipe-si-trench"].parameters,
                    "sketch_id": "default",
                },
            ),
            ProcessStep.from_recipe("Resist Strip", recipes["recipe-strip"]),
            ProcessStep.from_recipe("Conformal Al2O3", recipes["recipe-ald-al2o3"]),
        ],
        id="default-main",
    )
