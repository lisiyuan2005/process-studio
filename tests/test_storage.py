from pathlib import Path

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


def test_delete_project_snapshots_preserves_flow(tmp_path) -> None:
    repository = ProjectRepository(tmp_path / "project.sqlite3")
    grid_spec = dict(
        x_min=-0.2, x_max=0.2, y_min=-0.2, y_max=0.2,
        z_min=-0.2, z_max=0.2, nx=11, ny=11, nz=11,
    )
    project = ProjectDefinition("demo", grid_spec)
    recipe = Recipe("Inspect", ProcessType.NO_GEOMETRY)
    step = ProcessStep("Step 1", recipe.id)
    branch = FlowBranch("main", [step])
    project.active_branch_id = branch.id
    repository.save_project(project)
    repository.save_branch(project.id, branch)
    state = MaterialState(UniformGrid3D(**grid_spec))
    state.add_material("Si", state.grid.substrate())
    snapshot_id = repository.save_snapshot(project.id, branch.id, step.id, state)

    assert repository.delete_project_snapshots(project.id) == 1
    assert repository.load_branch(branch.id).steps[0].id == step.id
    assert repository.load_latest_snapshot(branch.id) is None
    assert not (repository.snapshot_directory / f"{snapshot_id}.npz").exists()


def test_a_workspace_that_moves_keeps_the_results_it_computed(tmp_path):
    """Snapshot paths are stored relative to the workspace.

    Every step's stored result was recorded by absolute path, so a
    workspace copied anywhere -- another machine, another home directory,
    a zip and back -- pointed every one of them at a directory that is not
    there. Nothing said so: the flow looked current and each step
    recomputed, or failed, when it was asked for.
    """
    import shutil

    from process_studio.storage import ProjectRepository

    class _State:
        def save(self, path):
            Path(path).write_bytes(b"a result")

    here = tmp_path / "here"
    repository = ProjectRepository(here / "process_studio.sqlite3")
    repository.save_project(ProjectDefinition(id="p", name="p", grid={}))
    repository.save_branch("p", FlowBranch(id="b", name="main", steps=[]))
    repository.save_snapshot("p", "b", "s1", _State(), suffix=".dfz")

    with repository.connect() as connection:
        stored = connection.execute("SELECT path FROM snapshots").fetchone()[0]
    assert stored.startswith("process_studio_snapshots/"), stored
    assert repository.snapshot_path("b", "s1").read_bytes() == b"a result"

    there = tmp_path / "there"
    shutil.copytree(here, there)
    moved = ProjectRepository(there / "process_studio.sqlite3")
    found = moved.snapshot_path("b", "s1")
    assert found.parent == moved.snapshot_directory
    assert found.read_bytes() == b"a result"


def test_a_snapshot_recorded_by_absolute_path_is_adopted(tmp_path):
    """What the workspaces written before this look like."""
    from process_studio.storage import ProjectRepository

    class _State:
        def save(self, path):
            Path(path).write_bytes(b"a result")

    repository = ProjectRepository(tmp_path / "process_studio.sqlite3")
    repository.save_project(ProjectDefinition(id="p", name="p", grid={}))
    repository.save_branch("p", FlowBranch(id="b", name="main", steps=[]))
    repository.save_snapshot("p", "b", "s1", _State(), suffix=".dfz")
    with repository.connect() as connection:
        name = connection.execute("SELECT path FROM snapshots").fetchone()[0].rsplit("/", 1)[-1]
        connection.execute(
            "UPDATE snapshots SET path = ?", (f"/somewhere/else/snapshots/{name}",)
        )

    assert repository.snapshot_path("b", "s1").read_bytes() == b"a result"
