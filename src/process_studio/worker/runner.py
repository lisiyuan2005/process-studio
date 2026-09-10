"""Workspace document assembly and cached flow execution."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..engine import ProcessEngine
from ..kernel.grid import UniformGrid3D
from ..kernel.material_state import MaterialState
from ..layout.quick_sketch import QuickSketch
from ..models import FlowBranch, ProjectDefinition, Recipe
from ..storage import ProjectRepository
from .errors import InvalidRequest, WorkspaceError
from .serialize import (
    branch_to_json,
    grid_dict,
    material_to_json,
    project_to_json,
    recipe_to_json,
)
from .workspace import (
    DigestCache,
    branch_digests,
    load_project,
    load_sketches,
    open_repository,
    sketch_to_json,
)

ProgressCallback = Callable[[dict[str, Any]], None]
SUBSTRATE_MATERIAL = "Si"


def initial_state(grid: UniformGrid3D) -> MaterialState:
    """The starting wafer every run begins from.

    Runs always replay from here rather than from a stored final state, so a
    refined grid never inherits an interpolated coarse result.
    """
    state = MaterialState(grid)
    state.add_material(SUBSTRATE_MATERIAL, grid.substrate())
    return state


def _branch_or_fail(repository: ProjectRepository, branch_id: str) -> FlowBranch:
    try:
        return repository.load_branch(branch_id)
    except KeyError as error:
        raise InvalidRequest(f"branch {branch_id!r} was not found") from error


def build_document(
    root: Path,
    repository: ProjectRepository,
    project: ProjectDefinition,
) -> dict[str, Any]:
    """Everything the client needs to render a workspace in one payload."""
    branches = repository.list_branches(project.id)
    if not branches:
        raise WorkspaceError("This project has no process branch.")
    active_id = project.active_branch_id or branches[0].id
    if all(branch.id != active_id for branch in branches):
        active_id = branches[0].id
    loaded = [repository.load_branch(branch.id) for branch in branches]
    recipes = repository.load_recipes()
    recipes_by_id = {recipe.id: recipe for recipe in recipes}
    for branch in loaded:
        migrated = False
        for step in branch.steps:
            migrated = step.detach_from_library(recipes_by_id) or migrated
        if migrated:
            repository.save_branch(project.id, branch)
    sketches = load_sketches(root)
    statuses = {
        branch.id: step_statuses(repository, branch, recipes, sketches, project)
        for branch in loaded
    }
    return {
        "root": str(root),
        "project": {**project_to_json(project), "activeBranchId": active_id},
        "branches": [branch_to_json(branch, recipes_by_id) for branch in loaded],
        "recipes": [recipe_to_json(recipe) for recipe in recipes],
        "materials": [material_to_json(material) for material in repository.load_materials()],
        "sketches": [
            sketch_to_json(sketch_id, sketch) for sketch_id, sketch in sorted(sketches.items())
        ],
        "stepStatuses": statuses,
    }


def step_statuses(
    repository: ProjectRepository,
    branch: FlowBranch,
    recipes: list[Recipe],
    sketches: Mapping[str, QuickSketch],
    project: ProjectDefinition,
) -> dict[str, str]:
    """Mark each step clean only when a snapshot matches its current digest."""
    by_id = {recipe.id: recipe for recipe in recipes}
    digests = branch_digests(branch, by_id, sketches, project.grid)
    stored = DigestCache(repository).load(branch.id)
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT step_id FROM branch_snapshots WHERE branch_id=?",
            (branch.id,),
        ).fetchall()
    with_snapshot = {row["step_id"] for row in rows}
    statuses: dict[str, str] = {}
    stale = False
    for step, digest in zip(branch.steps, digests):
        clean = (
            not stale and step.id in with_snapshot and stored.get(step.id) == digest
        )
        statuses[step.id] = "clean" if clean else "dirty"
        if not clean:
            # A changed step invalidates everything built on top of it.
            stale = True
    return statuses


def run_flow(
    root: Path,
    *,
    branch_id: str | None = None,
    project_id: str | None = None,
    through_step_id: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Execute the branch, reusing snapshots whose digest still matches."""
    emit = progress or (lambda _event: None)
    repository = open_repository(root)
    project = load_project(repository, project_id)
    branch = _branch_or_fail(repository, branch_id or project.active_branch_id or "")
    recipes = repository.load_recipes()
    by_id = {recipe.id: recipe for recipe in recipes}
    sketches = load_sketches(root)
    digests = branch_digests(branch, by_id, sketches, project.grid)
    cache = DigestCache(repository)
    stored = cache.load(branch.id)
    grid = UniformGrid3D(**project.grid)
    engine = ProcessEngine(
        by_id,
        sketches=sketches,
        repository=repository,
        logger=lambda message: emit({"kind": "log", "message": message}),
    )

    if through_step_id and all(step.id != through_step_id for step in branch.steps):
        raise InvalidRequest(f"step {through_step_id!r} is not part of this branch")

    total = len(branch.steps)
    if through_step_id:
        total = next(
            index + 1 for index, step in enumerate(branch.steps) if step.id == through_step_id
        )
    state = initial_state(grid)
    executed: list[str] = []
    cached: list[str] = []
    reusable = not force
    started = time.perf_counter()
    for index, (step, digest) in enumerate(zip(branch.steps, digests)):
        if index >= total:
            break
        if reusable and stored.get(step.id) == digest:
            try:
                state = repository.load_snapshot(branch.id, step.id)
                cached.append(step.id)
                emit(
                    {
                        "kind": "progress",
                        "stepId": step.id,
                        "message": f"Cached {step.name}",
                        "completed": index + 1,
                        "total": total,
                    }
                )
                continue
            except KeyError:
                # The digest survived but the snapshot file did not; recompute.
                reusable = False
        reusable = False
        emit(
            {
                "kind": "progress",
                "stepId": step.id,
                "message": f"Running {step.name}",
                "completed": index,
                "total": total,
            }
        )
        state = engine.run_step(state, step, project=project)
        repository.save_snapshot(project.id, branch.id, step.id, state)
        cache.store(branch.id, step.id, digest)
        executed.append(step.id)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    emit(
        {
            "kind": "log",
            "message": f"Finished {len(executed)} step(s), {len(cached)} cache hit(s) in {elapsed_ms:.0f} ms",
        }
    )
    return {
        "branchId": branch.id,
        "executedStepIds": executed,
        "cachedStepIds": cached,
        "elapsedMs": elapsed_ms,
        "materials": list(state.priority),
        "stepStatuses": step_statuses(repository, branch, recipes, sketches, project),
    }


def state_for_step(
    root: Path,
    *,
    branch_id: str,
    step_id: str | None,
    project_id: str | None = None,
) -> tuple[MaterialState, ProjectRepository, ProjectDefinition]:
    """Load the geometry a view should show, or the bare wafer before step one."""
    repository = open_repository(root)
    project = load_project(repository, project_id)
    branch = _branch_or_fail(repository, branch_id or project.active_branch_id or "")
    if not step_id:
        return initial_state(UniformGrid3D(**project.grid)), repository, project
    if all(step.id != step_id for step in branch.steps):
        raise InvalidRequest(f"step {step_id!r} is not part of this branch")
    try:
        return repository.load_snapshot(branch.id, step_id), repository, project
    except KeyError as error:
        raise InvalidRequest(
            "This step has no stored result yet. Run the flow first."
        ) from error


def apply_grid(
    repository: ProjectRepository,
    project: ProjectDefinition,
    grid: UniformGrid3D,
) -> ProjectDefinition:
    """Change the project grid and drop every result computed on the old one."""
    project.grid = grid_dict(grid)
    repository.save_project(project)
    cache = DigestCache(repository)
    for branch in repository.list_branches(project.id):
        loaded = repository.load_branch(branch.id)
        cache.forget(branch.id, [step.id for step in loaded.steps])
    return project
