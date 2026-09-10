from process_studio.kernel.grid import UniformGrid3D
from process_studio.kernel.material_state import MaterialState
from process_studio.models import (
    FlowBranch,
    MaterialDefinition,
    ProcessStep,
    ProcessType,
    ProjectDefinition,
    Recipe,
)
from process_studio.storage import ProjectRepository


def test_sqlite_round_trip_and_shared_snapshot_lifetime(tmp_path) -> None:
    repository = ProjectRepository(tmp_path / "project.sqlite3")
    grid_spec = {
        "x_min": -0.2,
        "x_max": 0.2,
        "y_min": -0.2,
        "y_max": 0.2,
        "z_min": -0.2,
        "z_max": 0.2,
        "nx": 11,
        "ny": 11,
        "nz": 11,
    }
    project = ProjectDefinition("demo", grid_spec)
    recipe = Recipe("Inspect", ProcessType.NO_GEOMETRY)
    step_one = ProcessStep("Step 1", recipe.id)
    step_two = ProcessStep("Step 2", recipe.id)
    branch = FlowBranch("main", [step_one, step_two])
    project.active_branch_id = branch.id

    repository.save_project(project)
    repository.save_material(MaterialDefinition("Si", color="#777777"))
    repository.save_recipe(recipe)
    repository.save_branch(project.id, branch)

    grid = UniformGrid3D(**grid_spec)
    state = MaterialState(grid)
    state.add_material("Si", grid.substrate())
    first_snapshot = repository.save_snapshot(project.id, branch.id, step_one.id, state)
    second_snapshot = repository.save_snapshot(project.id, branch.id, step_two.id, state)
    fork = repository.create_branch(project.id, branch.id, step_one.id, "variant")

    removed = repository.delete_step_and_dependents(branch.id, step_one.id)

    assert removed == [step_one.id, step_two.id]
    assert repository.load_project(project.id).name == "demo"
    assert repository.load_materials()[0].name == "Si"
    assert repository.load_recipes()[0].name == "Inspect"
    assert repository.load_snapshot(fork.id, step_one.id).priority == ["Si"]
    assert (repository.snapshot_directory / f"{first_snapshot}.npz").exists()
    assert not (repository.snapshot_directory / f"{second_snapshot}.npz").exists()
