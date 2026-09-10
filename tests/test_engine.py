import numpy as np

from process_studio.engine import ProcessEngine
from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.layout.quick_sketch import QuickSketch, SketchShape
from process_studio.models import (
    MaterialResponse,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
)


def test_engine_runs_deposit_and_masked_selective_etch() -> None:
    grid = UniformGrid3D(-0.4, 0.4, -0.4, 0.4, -0.4, 0.2, 41, 41, 31)
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())

    deposit = Recipe(
        "Deposit oxide",
        ProcessType.DEPOSIT,
        output_material="SiO2",
        parameters={"target": 0.04, "rate": 0.02},
    )
    etch = Recipe(
        "BOE oxide open",
        ProcessType.ETCH,
        parameters={"target": 0.04, "directional_fraction": 1.0},
        material_responses={
            "SiO2": MaterialResponse("SiO2", 0.08),
            "Si": MaterialResponse("Si", 0.0, stop_layer=True),
        },
    )
    sketch = QuickSketch(
        "center opening",
        [
            SketchShape(
                "rectangle",
                parameters={"center": (0.0, 0.0), "size": (0.2, 0.2)},
            )
        ],
    )
    logs: list[str] = []
    engine = ProcessEngine(
        {deposit.id: deposit, etch.id: etch},
        sketches={"open": sketch},
        logger=logs.append,
    )
    project = ProjectDefinition("engine demo", grid.__dict__)

    deposited = engine.run_step(
        state, ProcessStep("Deposit", deposit.id), project=project
    )
    etched = engine.run_step(
        deposited,
        ProcessStep(
            "Open oxide",
            etch.id,
            overrides={"sketch_id": "open"},
            mask_source="quick_sketch",
        ),
        project=project,
    )

    center = (grid.ny // 2, grid.nx // 2)
    film_z = int(np.argmin(np.abs(grid.z - 0.02)))
    outside_x = int(np.argmin(np.abs(grid.x - 0.35)))
    substrate_z = int(np.argmin(np.abs(grid.z + 0.1)))
    assert deposited.fields["SiO2"][film_z, *center] <= 0.0
    assert etched.fields["SiO2"][film_z, *center] > 0.0
    assert etched.fields["SiO2"][film_z, grid.ny // 2, outside_x] <= 0.0
    assert etched.fields["Si"][substrate_z, *center] <= 0.0
    assert any(message.startswith("RUN") for message in logs)
    assert any(message.startswith("DONE") for message in logs)


def test_step_can_override_material_rate_map_and_stop_material() -> None:
    grid = UniformGrid3D(-0.3, 0.3, -0.3, 0.3, -0.3, 0.2, 31, 31, 26)
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())
    oxide = Recipe(
        "Oxide",
        ProcessType.DEPOSIT,
        output_material="SiO2",
        parameters={"target": 0.04},
    )
    etch = Recipe(
        "Editable etch",
        ProcessType.ETCH,
        parameters={"target": 0.06},
        material_responses={"Si": MaterialResponse("Si", 0.1)},
    )
    engine = ProcessEngine({oxide.id: oxide, etch.id: etch})
    project = ProjectDefinition("override", grid.__dict__)
    deposited = engine.run_step(state, ProcessStep("oxide", oxide.id), project=project)
    result = engine.run_step(
        deposited,
        ProcessStep(
            "override",
            etch.id,
            overrides={
                "material_rates": {"SiO2": 0.08},
                "stop_materials": "Si",
            },
        ),
        project=project,
    )
    center = (grid.ny // 2, grid.nx // 2)
    oxide_z = int(np.argmin(np.abs(grid.z - 0.02)))
    silicon_z = int(np.argmin(np.abs(grid.z + 0.1)))

    assert result.fields["SiO2"][oxide_z, *center] > 0.0
    assert result.fields["Si"][silicon_z, *center] <= 0.0
